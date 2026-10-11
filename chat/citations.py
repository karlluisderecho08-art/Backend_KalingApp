"""
Checks the citation Kali ends a reply with against what she was given.

The grounding rules (chat/knowledge.py) tell the model to cite its source
once, in a "Source: ..." line, and never to invent a title. That is an
instruction to a language model, and nothing enforced it: a reply could
name an article that does not exist in the Knowledge Hub, or one that was
not among the passages retrieved for this question, and reach the mother
looking exactly like a verified answer. For a health topic where the whole
point of the Knowledge Hub is that its content is organisation-verified,
a made-up citation is worse than none, because it lends borrowed
authority to a sentence the knowledge base never said.

This does NOT check that the answer is faithful to the passages -- no
string comparison can. It checks the narrower, mechanical thing: that
every source the reply names is one Kali was actually shown. A citation
that fails is removed rather than corrected, because the honest options
are "cite what she used" and "cite nothing", and which passage she used
is exactly what cannot be recovered after the fact.
"""

import re

# A line that starts with "Source:" / "Sources:", tolerating the bullet
# or markdown emphasis a model sometimes wraps it in (`*Source: ...*`).
# Anchored to a line start on purpose: "the source of the problem" in the
# middle of a sentence must not be mistaken for a citation.
_CITATION_LINE = re.compile(
    r"^[ \t>*_\-]*sources?[ \t]*:[ \t]*(?P<body>\S.*?)[ \t*_]*$",
    re.IGNORECASE | re.MULTILINE,
)

# Double quotes only, straight or curly. A single quote is an apostrophe
# in titles like "Baby's First Feed", so it cannot delimit anything.
_QUOTED = re.compile(r"[\"“]([^\"“”]+)[\"”]")

# The tail of the form the grounding rules ask for -- `Source: "Title" in
# the Knowledge Hub` -- which names the place, not the source.
_PLACE_SUFFIX = re.compile(r"\bin the knowledge hub\b", re.IGNORECASE)

# A quoted fragment only counts as "text she was shown" if it is a phrase,
# not a word or two. "Breastfeeding" occurs in nearly every passage, so
# accepting it would let a one-word citation vouch for itself.
_MIN_PASSAGE_QUOTE_WORDS = 3
_MIN_PASSAGE_QUOTE_CHARS = 12

_WORDS = re.compile(r"[a-z0-9]+")


def _tokens(text):
    return _WORDS.findall(text.lower())


def _cited_segments(body):
    """The individual sources a citation line names."""
    quoted = [match.group(1) for match in _QUOTED.finditer(body)]
    if quoted:
        return quoted
    # No quotation marks: a literature-style citation written out as
    # plain text. Judged whole, minus the "in the Knowledge Hub" tail.
    unquoted = _PLACE_SUFFIX.sub("", body).strip(" .,;:")
    return [unquoted] if unquoted else []


def _title_candidates(passages):
    """
    Token sets for every name a retrieved source can legitimately be
    cited by: an article by its title, a literature document by its title
    or its full reference. An article's *category* is deliberately not a
    candidate -- it is a label in this system, not a source.
    """
    candidates = []
    for chunk in passages:
        names = [chunk.source_title]
        if chunk.source_kind == chunk.SourceKind.LITERATURE and chunk.source_reference:
            names.append(chunk.source_reference)
        for name in names:
            tokens = set(_tokens(name))
            if tokens:
                candidates.append(tokens)
    return candidates


def _segment_is_supported(segment, candidates, passage_texts):
    segment_tokens = _tokens(segment)
    if not segment_tokens:
        return False
    segment_set = set(segment_tokens)

    for title in candidates:
        # The whole title is present (it may carry extra words around it,
        # e.g. "... article in the Knowledge Hub").
        if title <= segment_set:
            return True
        # A shortened title: every word of the citation comes from a
        # retrieved title. Needs two words, or a bare "Latch" would
        # vouch for any article that happens to contain it.
        if len(segment_tokens) >= 2 and segment_set <= title:
            return True

    # Each seeded article ends with its own credit line ("Source: UNICEF
    # India, 'How to Breastfeed Correctly.'"). When that passage was
    # retrieved, Kali echoing it is citing text she was shown, even
    # though it is not the article's title in this system.
    joined = " ".join(segment_tokens)
    if len(segment_tokens) >= _MIN_PASSAGE_QUOTE_WORDS and len(joined) >= _MIN_PASSAGE_QUOTE_CHARS:
        return any(joined in text for text in passage_texts)

    return False


def verify_citations(reply, passages):
    """
    Returns (reply, removed): `reply` with every citation line that names
    a source not among `passages` taken out, and the lines removed.

    A reply with no citation line comes back untouched -- a missing
    citation is a style lapse, not a misleading one. With no passages at
    all (retrieval found nothing) every citation is unsupported by
    definition, since there was nothing to cite.

    All-or-nothing per line: if any one source on a line is unsupported
    the whole line goes, rather than splicing a sentence back together
    around the survivors.
    """
    candidates = _title_candidates(passages)
    passage_texts = [" ".join(_tokens(chunk.text)) for chunk in passages]

    removed = []

    def judge(match):
        segments = _cited_segments(match.group("body"))
        supported = bool(segments) and all(
            _segment_is_supported(segment, candidates, passage_texts)
            for segment in segments
        )
        if supported:
            return match.group(0)
        removed.append(match.group(0).strip())
        return ""

    cleaned = _CITATION_LINE.sub(judge, reply)
    if not removed:
        return reply, []

    # Taking a line out leaves its blank neighbours behind.
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, removed


def cited_articles(reply, articles):
    """
    The Knowledge Hub articles a reply's "Source:" line(s) name, in the order
    they are named, each at most once -- so the app can make the citation a
    link that opens the article.

    `articles` is every article that could be linked (objects with id, title
    and content). Matching uses the same reading of a citation line as
    verify_citations() above, against articles instead of retrieved passages:

      - the article's whole title appears in the cited text, or
      - the citation is a shortened title (2+ words, all from one title), or
      - the citation echoes a phrase from inside the article -- the seeded
        articles end with their own credit line ("Source: UNICEF India, ..."),
        which Kali sometimes repeats instead of the title.

    A literature source (not an article) matches nothing and stays plain
    text; so does a citation that fits more than one article equally well,
    rather than guessing which to open. Read-only: it never changes the reply.
    """
    if not reply:
        return []
    indexed = [
        (article, set(_tokens(article.title)), " ".join(_tokens(article.content or "")))
        for article in articles
    ]

    found = []
    for match in _CITATION_LINE.finditer(reply):
        for segment in _cited_segments(match.group("body")):
            article = _article_for_segment(segment, indexed)
            if article is not None and article not in found:
                found.append(article)
    return found


def _article_for_segment(segment, indexed):
    segment_tokens = _tokens(segment)
    if not segment_tokens:
        return None
    segment_set = set(segment_tokens)

    # Whole title present -- the longest such title wins, so "Breastfeeding
    # Basics" cannot steal a citation of "Breastfeeding Basics for Twins".
    whole = [(len(title), article) for article, title, _ in indexed if title and title <= segment_set]
    if whole:
        best = max(size for size, _ in whole)
        winners = [article for size, article in whole if size == best]
        return winners[0] if len(winners) == 1 else None

    if len(segment_tokens) >= 2:
        shortened = [article for article, title, _ in indexed if segment_set <= title]
        if len(shortened) == 1:
            return shortened[0]
        if len(shortened) > 1:
            return None

    joined = " ".join(segment_tokens)
    if len(segment_tokens) >= _MIN_PASSAGE_QUOTE_WORDS and len(joined) >= _MIN_PASSAGE_QUOTE_CHARS:
        echoed = [article for article, _, content in indexed if joined in content]
        if len(echoed) == 1:
            return echoed[0]
    return None
