"""
The server-side version of the Kotlin app's isBreastfeedingTopic()
keyword check (CODEBASE-1.md section 5, step 2). The roadmap is explicit
that this must run server-side, not just be trusted from the client --
otherwise anyone could bypass it by calling the API directly.

This backend repo doesn't contain the Kotlin source, only its
description, so this keyword list is a reasonable equivalent, not a
verified byte-for-byte port. Worth swapping in the real list from
data/Network.kt if exact parity with the Kotlin app matters (e.g. so
the same message is/isn't flagged on both).
"""

import re

TOPIC_KEYWORDS = [
    "breastfeed", "breastfeeding", "breast milk", "breastmilk", "nurse", "nursing",
    "lactation", "lactating", "latch", "latching", "pump", "pumping", "nipple",
    "colostrum", "engorge", "engorgement", "mastitis", "milk supply", "let-down",
    "wean", "weaning", "formula", "milk bank", "donor milk", "milk storage",
    "newborn", "infant feeding", "baby feeding",
    # Added after real questions were incorrectly rejected as off-topic:
    # only compound phrases like "milk supply"/"breast milk" were listed
    # before, so a perfectly on-topic question phrased around plain
    # "milk" or "feed"/"feeding" alone (e.g. "is my baby getting enough
    # milk", "is cluster feeding normal") matched nothing here.
    "milk", "feed", "feeding", "cluster feeding", "growth spurt", "hunger cues",
    # Spellings that matching by whole word (below) no longer picks up by
    # accident. These used to match only because "feed"/"pump"/"let-down"
    # happened to be a substring of them.
    "bottlefeed", "breastpump", "breastfed", "breast fed", "letdown", "hunger cue",
]


# Words are separated by whitespace, hyphens or underscores, all treated
# as the same thing, so "let-down", "let down" and "cluster-feeding" match
# the keywords written with a space.
_SEPARATORS = re.compile(r"[\s\-_]+")

# Endings a keyword may carry and still count as that word: "pump" ->
# pumps / pumped / pumping, "latch" -> latches, "nurse" -> nursed.
_INFLECTIONS = r"(?:s|es|d|ed|ing|er|ers)?"


def _normalize(text):
    return _SEPARATORS.sub(" ", text.lower()).strip()


# A keyword has to be a whole word, not just a run of letters inside one.
# This used to be `keyword in text.lower()`, and a substring test cannot
# tell "feed" from "feedback", "milk" from "milkshake", "nurse" from
# "nursery", or "pump" from "pumpkin" or "trump" -- every one of those
# skipped the guardrail and reached the model.
#
# Longest keywords first so a phrase is tried before the shorter word it
# starts with; for a match/no-match answer the order does not change the
# result, but it keeps the pattern easy to read when debugging.
_KEYWORD_PATTERN = re.compile(
    r"\b(?:"
    + "|".join(re.escape(_normalize(keyword)) for keyword in sorted(TOPIC_KEYWORDS, key=len, reverse=True))
    + r")"
    + _INFLECTIONS
    + r"\b"
)


def is_breastfeeding_topic(text):
    return _KEYWORD_PATTERN.search(_normalize(text)) is not None


OFF_TOPIC_RESPONSE = (
    "I'm Kali, and I'm here specifically to help with breastfeeding and lactation "
    "questions. That question is outside what I can help with -- feel free to ask "
    "me anything about latching, milk supply, storage, or donor milk instead."
)
