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

TOPIC_KEYWORDS = [
    "breastfeed", "breastfeeding", "breast milk", "breastmilk", "nurse", "nursing",
    "lactation", "lactating", "latch", "latching", "pump", "pumping", "nipple",
    "colostrum", "engorge", "engorgement", "mastitis", "milk supply", "let-down",
    "wean", "weaning", "formula", "milk bank", "donor milk", "milk storage",
    "newborn", "infant feeding", "baby feeding",
]


def is_breastfeeding_topic(text):
    lowered = text.lower()
    return any(keyword in lowered for keyword in TOPIC_KEYWORDS)


OFF_TOPIC_RESPONSE = (
    "I'm Kali, and I'm here specifically to help with breastfeeding and lactation "
    "questions. That question is outside what I can help with -- feel free to ask "
    "me anything about latching, milk supply, storage, or donor milk instead."
)
