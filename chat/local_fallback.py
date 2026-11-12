"""
The server-side version of the Kotlin app's getLocalClinicalResponse()
(data/Network.kt, CODEBASE-1.md section 6) -- the "safety net" answers
used whenever a real OpenAI call can't happen: no key configured, the
key is still the placeholder, the request fails, or anything throws.

Same caveat as guardrail.py: this repo only has a *description* of the
five original canned answers (latch/pain, supply, storage, donation,
greeting), not their exact wording. These are reasonable equivalents
covering the same five topics, not a verified copy. The Kotlin code
comments say to "preserve it exactly" -- if the panel demo needs the
literal original wording, paste it in here from data/Network.kt.
"""

import re

FALLBACK_RESPONSES = [
    (
        ("latch", "pain", "painful", "sore", "nipple",
         "masakit", "sumasakit", "utong", "sugat", "pagkapit", "kumapit"),
        "A good latch means baby's mouth covers most of the areola, not just the "
        "nipple, with lips flanged outward. Pain that continues throughout a feed "
        "usually means the latch needs adjusting -- breaking suction gently and "
        "relatching often helps. If pain persists, an IBCLC lactation consultant "
        "can check for tongue-tie or positioning issues in person.",
    ),
    (
        ("supply", "not enough", "low milk",
         "kulang", "kaunti", "konti", "dumami", "dumadami", "dagdagan", "madagdagan", "damihan"),
        "Milk supply works on demand: more frequent, effective removal (nursing or "
        "pumping) signals the body to make more. Common supply boosters include "
        "nursing on both sides, pumping after feeds, and staying hydrated and "
        "rested. If you're worried supply is genuinely low, a lactation consultant "
        "or your pediatric clinic can check baby's weight gain, which is the most "
        "reliable sign.",
    ),
    (
        ("storage", "store", "freeze", "frozen", "fridge", "freezer", "how long",
         "itabi", "maitatabi", "itatabi", "pagtatabi", "imbak", "ref", "gaano katagal"),
        "Freshly expressed milk is generally fine at room temperature for a few "
        "hours, in the fridge for several days, and in a freezer for months -- "
        "always label with the date and use the oldest milk first. Thawed milk "
        "shouldn't be refrozen. WHO/AAP guidance has the specific time windows if "
        "you need exact numbers for your storage method.",
    ),
    (
        ("donate", "donation", "donating", "donor", "milk bank",
         "mag donate", "magdonate", "donasyon"),
        "Milk donation and receiving pasteurized donor milk both go through an "
        "accredited milk bank, which screens donors and pasteurizes all stored "
        "milk for safety. You can start either the donor or recipient pathway "
        "from the Milk Bank section of this app.",
    ),
    (
        ("hello", "hi", "hey", "good morning", "good afternoon", "good evening",
         "kumusta", "kamusta", "magandang umaga", "magandang hapon", "magandang gabi"),
        "Hi, I'm Kali! I'm here to help with breastfeeding and lactation "
        "questions -- latching, milk supply, storage, or donor milk. What's on "
        "your mind?",
    ),
]

GENERIC_FALLBACK_RESPONSE = (
    "I'm currently running on offline mode and can't reach my full knowledge "
    "base, but I'm still here to help with breastfeeding basics -- latching, "
    "milk supply, storage, or donation. Could you tell me a bit more about what "
    "you're looking for?"
)


# Whole words, like guardrail.py: a plain substring test matched "hi" inside
# "hindi" (greeting instead of an answer) and "pain" inside "ipainom". The
# endings let "store" match stored/stores and "donate" donated.
_SEPARATORS = re.compile(r"[\s\-_]+")
_PATTERNS = [
    (
        re.compile(r"\b(?:" + "|".join(re.escape(k) for k in sorted(keywords, key=len, reverse=True)) + r")(?:s|es|d|ed|ing)?\b"),
        response,
    )
    for keywords, response in FALLBACK_RESPONSES
]


def get_local_clinical_response(prompt):
    lowered = _SEPARATORS.sub(" ", prompt.lower())
    for pattern, response in _PATTERNS:
        if pattern.search(lowered):
            return response
    return GENERIC_FALLBACK_RESPONSE
