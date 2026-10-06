"""
Retrieval over the domain-limited knowledge base -- the "restricted,
read-only retrieval" the AI assistant is specified to answer from.

Replaces the previous approach of sending every article in full with
every message. That worked while the library was six articles, and its
own docstring named this as the seam to cut when the library outgrew a
single prompt; it does not survive a knowledge base that also holds
reviewed literature, and it spent ~20k characters of prompt on eight
articles to answer a question that two paragraphs covered.

Ranking is hybrid, and the two rankers fail in opposite directions,
which is the reason for using both:

  - Lexical (BM25) matches the words the mother actually typed. It is
    exact on clinical terms -- "colostrum", "mastitis", a storage time --
    and blind to paraphrase: "my milk hasn't come in" shares no term
    with a passage about colostrum.
  - Vector (Gemini embeddings) matches meaning, so it catches the
    paraphrase, and will happily rank a passage that is topically
    adjacent but does not answer the question.

Results are combined with Reciprocal Rank Fusion rather than by adding
the scores. BM25 scores are unbounded and corpus-dependent while cosine
sits in [-1, 1], so any weighted sum needs a normalization constant that
has to be retuned as the corpus grows. RRF uses only each ranker's
ordering, so it needs no such constant, and it degrades cleanly to
lexical-only when no embeddings exist -- which is the state this runs in
with no GEMINI_API_KEY set, and after any embedding outage.
"""

import math
import re

from django.conf import settings
from django.db.models import Q

from .chunking import embedding_input, revision_of, split_into_passages
from .embeddings import cosine_similarity, embed_passages, embed_query, embeddings_available
from .models import KnowledgeChunk, KnowledgeDocument

# Standard BM25 parameters. k1 bounds how much a repeated term keeps
# helping; b is how hard to penalize a long passage for its length.
BM25_K1 = 1.5
BM25_B = 0.75

# RRF's damping constant, from the original paper. Large relative to the
# ranks that matter, so being 1st vs 2nd in one ranker does not dominate
# appearing in both.
RRF_K = 60

# How deep into each ranker's ordering to fuse. Past this a result is not
# going to survive fusion into the top handful anyway.
CANDIDATE_DEPTH = 25

# Embeddings are filled in lazily on the request path (see
# ensure_embeddings), so this bounds how many a single chat message can
# be made to pay for. A bulk import gets embedded across the next few
# messages instead of making one mother wait for all of it -- and
# `manage.py reindex_knowledge` does the whole corpus up front so that
# normally never happens.
MAX_EMBEDDINGS_PER_REQUEST = 32

# Words carrying no retrieval signal. Short list on purpose: an
# aggressive stoplist strips exactly the words that make a short question
# specific, and BM25's IDF term already discounts common words by itself.
STOPWORDS = frozenset(
    (
        "a an and are as at be been but by can do does for from had has have how i if in into "
        "is it its me of on or our should so than that the their them then there these they "
        "this to too was we were what when where which while who why will with would you"
    ).split()
)

_TOKEN = re.compile(r"[a-z0-9]+")


def tokenize(text):
    """Lowercase alphanumeric terms, minus stopwords and single characters."""
    return [
        token
        for token in _TOKEN.findall(text.lower())
        if len(token) > 1 and token not in STOPWORDS
    ]


# --------------------------------------------------------------------------
# Index maintenance
# --------------------------------------------------------------------------

def _source_documents():
    """
    Every document in the knowledge base, as
    (source_kind, source_id, title, reference, content) tuples.

    articles.Article is imported inside the function, not at module
    scope: chat is a sibling app to articles, and a module-level import
    would make merely importing this module depend on the articles app
    being loaded -- which bites in tests and management commands.
    """
    from articles.models import Article

    documents = []
    for article in Article.objects.all().order_by("id"):
        documents.append((
            KnowledgeChunk.SourceKind.ARTICLE,
            article.id,
            article.title,
            article.category,
            article.content,
        ))
    for document in KnowledgeDocument.objects.filter(is_active=True).order_by("id"):
        documents.append((
            KnowledgeChunk.SourceKind.LITERATURE,
            document.id,
            document.title,
            document.citation,
            document.content,
        ))
    return documents


def sync_index():
    """
    Bring the chunk table in line with its sources. Returns a summary
    dict of what changed.

    Called on the retrieval path as well as from the management command,
    because admins add and edit articles through the admin panel on a
    running server: the previous implementation re-read every article on
    every message, which meant a new article was answerable immediately,
    and losing that to a "remember to reindex" step would be a
    regression. It costs one query over source content and a hash per
    document -- cheap, unlike embedding, which is why only the chunk
    *text* is reconciled here and vectors are filled separately.
    """
    documents = _source_documents()
    live_revisions = {
        (kind, source_id): revision_of(content or "")
        for kind, source_id, _title, _reference, content in documents
    }

    existing = {}
    for kind, source_id, revision in KnowledgeChunk.objects.values_list(
        "source_kind", "source_id", "source_revision"
    ):
        existing.setdefault((kind, source_id), revision)

    stale = {
        key for key, revision in existing.items()
        if key not in live_revisions or live_revisions[key] != revision
    }
    new = {key for key in live_revisions if key not in existing}

    removed_chunks = 0
    for kind, source_id in stale:
        removed_chunks += KnowledgeChunk.objects.filter(
            source_kind=kind, source_id=source_id
        ).delete()[0]

    created = []
    for kind, source_id, title, reference, content in documents:
        key = (kind, source_id)
        if key not in stale and key not in new:
            continue
        for ordinal, passage in enumerate(split_into_passages(content)):
            created.append(KnowledgeChunk(
                source_kind=kind,
                source_id=source_id,
                source_title=title,
                source_reference=reference or "",
                ordinal=ordinal,
                text=passage,
                source_revision=live_revisions[key],
            ))
    if created:
        # ignore_conflicts because this runs on the request path and
        # production serves several gunicorn workers: two messages
        # arriving together can both see a new article as unindexed and
        # both try to write its passages, and the unique constraint on
        # (source_kind, source_id, ordinal) would make one of them raise.
        # That exception would be swallowed by gemini_client's catch-all
        # and silently downgrade a real answer to a canned fallback
        # reply. Passages are deterministic from their source, so the
        # loser of the race has nothing to add and skipping it is exactly
        # right.
        KnowledgeChunk.objects.bulk_create(created, ignore_conflicts=True)

    return {
        "documents": len(documents),
        "reindexed_documents": len(stale | new),
        "removed_chunks": removed_chunks,
        "created_chunks": len(created),
    }


def unembedded_chunks():
    """
    Chunks with no vector usable by the model currently configured.

    Expressed as "empty OR embedded by another model" rather than with a
    length lookup on the JSON field: `embedding__len` is not a length at
    all to Django, it is a lookup for a JSON key literally named "len",
    which an array never has -- so that spelling silently matches nothing.
    """
    return KnowledgeChunk.objects.filter(
        Q(embedding=[]) | ~Q(embedding_model=settings.GEMINI_EMBEDDING_MODEL)
    )


def ensure_embeddings(limit=None, timeout_ms=None):
    """
    Embed chunks that have no usable vector. Returns how many were
    embedded.

    A chunk with no vector is not excluded from retrieval -- it still
    competes lexically -- so this is an optimization, not a
    prerequisite, and it is safe for it to do nothing at all.

    `timeout_ms` is for callers with no mother waiting (the reindex
    command). Left as None, embed_passages() uses its short request-path
    timeout; it is only forwarded when given, so the request path calls
    embed_passages() exactly as it always has.
    """
    # Checked before the query, not after: with no key there is nothing
    # this can do, and the pending-chunk lookup would otherwise run on
    # every single message to reach the same conclusion.
    if not settings.CHAT_RETRIEVAL_USE_EMBEDDINGS or not embeddings_available():
        return 0

    queryset = unembedded_chunks()
    pending = list(queryset[:limit]) if limit else list(queryset)
    if not pending:
        return 0

    passage_texts = [
        embedding_input(chunk.source_title, chunk.source_reference, chunk.text)
        for chunk in pending
    ]
    if timeout_ms is None:
        vectors = embed_passages(passage_texts)
    else:
        vectors = embed_passages(passage_texts, timeout_ms=timeout_ms)

    embedded = []
    for chunk, vector in zip(pending, vectors):
        if not vector:
            continue
        chunk.embedding = vector
        chunk.embedding_model = settings.GEMINI_EMBEDDING_MODEL
        embedded.append(chunk)
    if embedded:
        KnowledgeChunk.objects.bulk_update(embedded, ["embedding", "embedding_model"])
    return len(embedded)


# --------------------------------------------------------------------------
# Ranking
# --------------------------------------------------------------------------

def _bm25_scores(query_terms, chunks):
    """BM25 score per chunk index. Chunks sharing no term are absent."""
    if not query_terms:
        return {}

    documents = [
        tokenize(embedding_input(chunk.source_title, chunk.source_reference, chunk.text))
        for chunk in chunks
    ]
    lengths = [len(document) for document in documents]
    average_length = (sum(lengths) / len(lengths)) if lengths else 0.0
    if average_length <= 0:
        return {}

    frequencies = []
    document_frequency = {}
    for document in documents:
        counts = {}
        for token in document:
            counts[token] = counts.get(token, 0) + 1
        frequencies.append(counts)
        for token in counts:
            document_frequency[token] = document_frequency.get(token, 0) + 1

    total = len(documents)
    scores = {}
    for index, counts in enumerate(frequencies):
        score = 0.0
        for term in query_terms:
            term_frequency = counts.get(term, 0)
            if not term_frequency:
                continue
            # BM25's probabilistic IDF, +1 inside the log so a term
            # present in every passage scores a small positive value
            # rather than a negative one.
            idf = math.log(
                1 + (total - document_frequency[term] + 0.5) / (document_frequency[term] + 0.5)
            )
            denominator = term_frequency + BM25_K1 * (
                1 - BM25_B + BM25_B * (lengths[index] / average_length)
            )
            score += idf * (term_frequency * (BM25_K1 + 1)) / denominator
        if score > 0:
            scores[index] = score
    return scores


def _vector_scores(question, chunks):
    """
    Cosine similarity per chunk index, for chunks close enough to the
    best match for this question.

    The cutoff is *relative* to the best similarity, not an absolute
    threshold, because an absolute one does not work with this embedding
    model. Measured over the seeded corpus (36 passages,
    gemini-embedding-001):

        "How long can I keep breastmilk in the fridge?"  0.579 - 0.734
        "how do I fix a car engine"                      0.506 - 0.580
        "what is the capital of France"                  0.476 - 0.520

    Every passage scores above 0.47 against a question about car
    engines: these vectors share a large component that is simply
    "English prose", so absolute cosine mostly measures that. Worse, the
    ranges overlap -- the relevant question's weakest passage (0.579)
    scores below the irrelevant question's strongest (0.580) -- so no
    absolute floor can separate them at all. Distance from the best match
    for the same question does separate them, because the shared
    component cancels out.

    What this cutoff does NOT do is decide whether a question belongs to
    the knowledge base: an off-topic question just produces a flat
    distribution where everything is equally mediocre, and the top of
    that still clears any relative cutoff. Two other things handle that,
    and they handle it better -- guardrail.is_breastfeeding_topic()
    rejects an off-topic message before the model is called at all, and
    the strict grounding rules make the model say a question is not
    covered when the passages do not answer it.
    """
    if not settings.CHAT_RETRIEVAL_USE_EMBEDDINGS or not embeddings_available():
        return {}

    usable = [
        index for index, chunk in enumerate(chunks)
        if chunk.embedding and chunk.embedding_model == settings.GEMINI_EMBEDDING_MODEL
    ]
    if not usable:
        return {}

    query_vector = embed_query(question)
    if not query_vector:
        return {}

    similarities = {}
    for index in usable:
        similarity = cosine_similarity(query_vector, chunks[index].embedding)
        # Negative similarity means actively opposed, never merely
        # weaker, so it is dropped regardless of what the best match was.
        if similarity is not None and similarity > 0:
            similarities[index] = similarity
    if not similarities:
        return {}

    cutoff = max(similarities.values()) - settings.CHAT_RETRIEVAL_SIMILARITY_MARGIN
    return {
        index: similarity
        for index, similarity in similarities.items()
        if similarity >= cutoff
    }


def _fuse(rankings):
    """
    Reciprocal Rank Fusion of several {index: score} rankings.

    Returns {index: fused_score}. An index present in both rankings beats
    one that only tops a single ranker, which is the property being
    bought here: agreement between a keyword match and a semantic match
    is the strongest signal available without labelled relevance data.
    """
    fused = {}
    for ranking in rankings:
        ordered = sorted(ranking, key=lambda index: ranking[index], reverse=True)
        for rank, index in enumerate(ordered[:CANDIDATE_DEPTH], start=1):
            fused[index] = fused.get(index, 0.0) + 1.0 / (RRF_K + rank)
    return fused


def retrieve(question, top_k=None):
    """
    The passages Kali may ground an answer in, most relevant first.

    Returns [] when nothing in the knowledge base is relevant -- which is
    a real answer, not a failure: the grounding rules turn it into Kali
    saying the topic is not covered yet, rather than her answering from
    general training data.
    """
    if not question or not question.strip():
        return []

    top_k = top_k or settings.CHAT_RETRIEVAL_TOP_K

    sync_index()
    ensure_embeddings(limit=MAX_EMBEDDINGS_PER_REQUEST)

    chunks = list(KnowledgeChunk.objects.all())
    if not chunks:
        return []

    lexical = _bm25_scores(tokenize(question), chunks)
    vector = _vector_scores(question, chunks)

    fused = _fuse([lexical, vector])
    if not fused:
        return []

    ordered = sorted(
        fused,
        # Break RRF ties (common, since it is a sum of a few discrete
        # reciprocals) by lexical score, then by a stable document
        # position, so the same question never returns a different
        # passage order run to run.
        key=lambda index: (fused[index], lexical.get(index, 0.0), -index),
        reverse=True,
    )
    return [chunks[index] for index in ordered[:top_k]]
