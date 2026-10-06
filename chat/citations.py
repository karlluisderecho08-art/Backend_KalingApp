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
