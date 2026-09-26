"""
Splits a knowledge-base document into retrievable passages.

Paragraph-aware rather than a fixed character stride: these documents
are prose with short headings ("Positioning", "Latching"), and cutting
mid-sentence every N characters reliably produces passages that start
halfway through a thought. Retrieval then returns a fragment that reads
as a non-sequitur, and the model grounds an answer in it anyway.

So paragraphs are the unit, packed up to a target size, and only a
paragraph that is itself longer than the hard ceiling gets split by
sentence.
"""

import hashlib
import re

# Target passage size in characters. Big enough to carry a whole idea
# with its qualifiers -- a storage time is useless without the
# temperature it applies to -- and small enough that retrieving a handful
# of them costs a fraction of what sending the whole library did.
TARGET_CHARS = 900
# A single paragraph longer than this is split by sentence rather than
# sent as one oversized passage.
MAX_CHARS = 1400
# Trailing context carried into the next passage, so a fact that lands on
# a paragraph boundary is not orphaned from the sentence that set it up.
OVERLAP_CHARS = 150

_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def revision_of(text):
    """Stable fingerprint of a document's indexed text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _split_long_paragraph(paragraph):
    """Break one oversized paragraph on sentence boundaries."""
    sentences = _SENTENCE_END.split(paragraph)
    pieces = []
    current = ""
    for sentence in sentences:
        if current and len(current) + len(sentence) + 1 > TARGET_CHARS:
            pieces.append(current.strip())
            current = sentence
        else:
            current = f"{current} {sentence}".strip() if current else sentence
    if current.strip():
        pieces.append(current.strip())
    return pieces


def split_into_passages(content):
    """
    Returns the document's passages as a list of strings.

    Empty or whitespace-only content yields [] rather than one empty
    passage, so an unfinished document contributes nothing to the index
    instead of a chunk that matches everything weakly.
    """
    if not content or not content.strip():
        return []

    units = []
    for paragraph in _PARAGRAPH_BREAK.split(content.strip()):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) > MAX_CHARS:
            units.extend(_split_long_paragraph(paragraph))
        else:
            units.append(paragraph)

    passages = []
    current = ""
    for unit in units:
        if current and len(current) + len(unit) + 2 > TARGET_CHARS:
            passages.append(current)
            # Carry the tail of the finished passage into the next one.
            tail = current[-OVERLAP_CHARS:]
            # Start the overlap at a word boundary so it doesn't open
            # mid-word.
            space = tail.find(" ")
            current = f"{tail[space + 1:]}\n\n{unit}" if space != -1 else unit
        else:
            current = f"{current}\n\n{unit}" if current else unit
    if current:
        passages.append(current)

    return passages


def embedding_input(source_title, source_reference, text):
    """
    The text actually embedded (and lexically scored) for a passage.

    The passage alone loses its topic once it is detached from its
    document: a paragraph about "the first few days" means something
    different under a colostrum article than under a storage one.
    Prefixing the title puts that signal back, and keeps the lexical and
    vector rankers scoring the same string.
    """
    header = f"{source_title} ({source_reference})" if source_reference else source_title
    return f"{header}\n{text}"
