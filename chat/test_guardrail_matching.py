"""
The keyword guardrail matches whole words, not runs of letters.

It used to be `keyword in text.lower()`. A substring test cannot tell
"feed" from "feedback", "milk" from "milkshake", "nurse" from "nursery" or
"pump" from "pumpkin" -- every one of those skipped the guardrail and
reached the model. Matching whole words fixes that, but it is also a
change that could start refusing real questions, so half of this file is
there to prove it does not: a differential test holds the new matcher to
"never refuses anything the old one accepted".
"""

from django.test import SimpleTestCase

from .guardrail import TOPIC_KEYWORDS, is_breastfeeding_topic

# The list and logic exactly as they were before whole-word matching.
LEGACY_KEYWORDS = [
    "breastfeed", "breastfeeding", "breast milk", "breastmilk", "nurse", "nursing",
    "lactation", "lactating", "latch", "latching", "pump", "pumping", "nipple",
    "colostrum", "engorge", "engorgement", "mastitis", "milk supply", "let-down",
    "wean", "weaning", "formula", "milk bank", "donor milk", "milk storage",
    "newborn", "infant feeding", "baby feeding",
    "milk", "feed", "feeding", "cluster feeding", "growth spurt", "hunger cues",
]


def legacy_is_breastfeeding_topic(text):
    lowered = text.lower()
    return any(keyword in lowered for keyword in LEGACY_KEYWORDS)


# Realistic on-topic messages. Every one must be accepted.
ON_TOPIC = [
    "How do I get a good latch?",
    "Is my baby getting enough milk?",
    "How long can I store breast milk in the fridge?",
    "What's a good way to increase my milk supply?",
    "Is it normal for my baby to cluster feed in the evening?",
    "My nipples are cracked and bleeding, what do I do?",
    "I think I have mastitis, it's red and painful",
    "My breasts feel hard and hot, is this engorgement?",
    "Can I donate my breastmilk to a milk bank?",
    "When should I start weaning my baby?",
    "How often should a newborn feed at night?",
    "Is formula safe to mix with expressed milk?",
    "What is colostrum and why does it matter?",
    "How do I use a breast pump properly?",
    "I was pumping at work and my supply dropped",
    "Does pumping hurt?",
    "Can I freeze breast milk for six months?",
    "My baby was breastfed for a year, how do I wean gently?",
    "Why does my baby fall asleep while nursing?",
    "How do I know my let-down reflex is working?",
    "What are hunger cues in a newborn?",
    "Is a growth spurt making my baby feed all day?",
    "Can I eat spicy food while breastfeeding?",
    "Does coffee affect my breast milk?",
    "My baby latches and unlatches constantly",
    "Baby feeding schedule for the first month",
    "Infant feeding guidelines from WHO",
    "How do lactating mothers stay hydrated?",
    "Which medications are safe while lactating?",
    "Is it okay to nurse during a fever?",
    "How much milk should a 2 week old drink?",
    "Do I need to burp my baby after feeding?",
    "How long should each feed last?",
    "Does my milk change when my baby is sick?",
    "Can donor milk be given to preterm babies?",
    "Where can I get a milk bank referral?",
    "Why is my breast milk watery?",
    "How do I clean pump parts?",
    "How do I thaw frozen breastmilk?",
    "Should I wake my newborn to feed?",
    "Can I breastfeed with inverted nipples?",
    "Pumped milk keeps for how long in a cooler?",
    "I'm nursing my toddler and pregnant again",
    "Tips for night feeds",
    "FEEDING A NEWBORN",
    "milk?",
    "(milk)",
    "milk, please",
    "is it ok to give my baby formula?",
]

# Questions the old matcher refused and the new one accepts: the same
# keyword in a spelling that a substring test, which needs the exact run
# of letters, could not see.
NEWLY_ACCEPTED = [
    "What is the let down reflex?",
    "I have a strong letdown and my baby chokes",
    "Breast fed babies and diaper counts",
    "How often do breastfed babies poop?",
    "Do I need a hunger cue chart?",
]

# Compound spellings the old matcher accepted only BY ACCIDENT -- "feed"
# or "pump" happened to be a substring of them. Matching whole words
# would have started refusing every one of these, which is why they were
# added as keywords of their own. Kept as a list so that stays true.
COMPOUND_SPELLINGS = [
    "Can I bottlefeed my baby at night?",
    "Is bottle-feeding okay for the first week?",
    "Can I use a breastpump on one side only?",
    "my breastpump stopped working",
    "bottlefeeding versus breastfeeding",
]

# A keyword that is only a fragment of an unrelated word. Each of these
# reached the model before.
FRAGMENTS_OF_OTHER_WORDS = [
    "Please leave feedback on my essay",
    "can you give me feedback on this code",
    "I want a chocolate milkshake recipe",
    "What time does the nursery open?",
    "How do I carve a pumpkin?",
    "Is the milky way visible tonight?",
    "my printer is jammed with feedstock",
    "latchkey kids and screen time",
]

OFF_TOPIC = [
    "what's the weather like today",
    "can you recommend a good movie",
    "how do I fix a car engine",
    "what is the capital of France",
    "tell me about the stock market",
    "how do I bake a chocolate cake",
    "who won the basketball game last night",
    "how do I reset my router",
    "what is 17 times 23",
    "yes",
    "what about at night?",
    "",
]


class WholeWordMatchingTests(SimpleTestCase):
    def test_realistic_on_topic_messages_are_accepted(self):
        for message in ON_TOPIC:
            with self.subTest(message=message):
                self.assertTrue(is_breastfeeding_topic(message))

    def test_spellings_the_old_matcher_refused_are_now_accepted(self):
        for message in NEWLY_ACCEPTED:
            with self.subTest(message=message):
                self.assertFalse(legacy_is_breastfeeding_topic(message), "premise: it used to be refused")
                self.assertTrue(is_breastfeeding_topic(message))

    def test_compound_spellings_that_only_matched_by_accident_still_match(self):
        for message in COMPOUND_SPELLINGS:
            with self.subTest(message=message):
                self.assertTrue(legacy_is_breastfeeding_topic(message), "premise: the old matcher took these")
                self.assertTrue(is_breastfeeding_topic(message))

    def test_a_keyword_inside_another_word_does_not_count(self):
        for message in FRAGMENTS_OF_OTHER_WORDS:
            with self.subTest(message=message):
                self.assertTrue(legacy_is_breastfeeding_topic(message), "premise: it used to slip through")
                self.assertFalse(is_breastfeeding_topic(message))

    def test_clearly_unrelated_messages_are_still_rejected(self):
        for message in OFF_TOPIC:
            with self.subTest(message=message):
                self.assertFalse(is_breastfeeding_topic(message))

    def test_matching_ignores_case_and_surrounding_punctuation(self):
        for message in ["MILK", "Milk.", "\"milk\"", "milk!!!", "...milk...", "Breast-Feeding", "LET-DOWN?"]:
            with self.subTest(message=message):
                self.assertTrue(is_breastfeeding_topic(message))

    def test_hyphens_spaces_and_underscores_are_interchangeable(self):
        for message in ["let-down", "let down", "let_down", "cluster-feeding", "cluster feeding", "breast-milk"]:
            with self.subTest(message=message):
                self.assertTrue(is_breastfeeding_topic(message))

    def test_a_keyword_may_carry_a_common_ending(self):
        for message in ["pumps", "pumped", "pumping", "latches", "latched", "nursed", "weaned", "feeds", "milks", "newborns"]:
            with self.subTest(message=message):
                self.assertTrue(is_breastfeeding_topic(message))

    def test_an_arbitrary_suffix_is_not_an_ending(self):
        for message in ["feedback", "milkshake", "pumpkin", "nursery", "formulaic", "latchkey"]:
            with self.subTest(message=message):
                self.assertFalse(is_breastfeeding_topic(message))


class EveryKeywordStillWorksTests(SimpleTestCase):
    """Properties that hold for each entry in the list, so a new keyword is covered without a new test."""

    def test_each_keyword_matches_on_its_own(self):
        for keyword in TOPIC_KEYWORDS:
            with self.subTest(keyword=keyword):
                self.assertTrue(is_breastfeeding_topic(keyword))

    def test_each_keyword_matches_inside_a_sentence(self):
        for keyword in TOPIC_KEYWORDS:
            with self.subTest(keyword=keyword):
                self.assertTrue(is_breastfeeding_topic(f"my question is about {keyword} today"))

    def test_each_single_word_keyword_stops_matching_when_glued_to_other_letters(self):
        # Single words only: gluing letters onto "breast milk" still
        # leaves the whole word "milk", which is a keyword in its own
        # right, so the property cannot hold for a phrase.
        for keyword in (k for k in TOPIC_KEYWORDS if " " not in k and "-" not in k):
            with self.subTest(keyword=keyword):
                self.assertFalse(is_breastfeeding_topic(f"zq{keyword}"), "letters glued on the front")
                self.assertFalse(is_breastfeeding_topic(f"{keyword}zq"), "letters glued on the back")


class NoNewRefusalsTests(SimpleTestCase):
    """
    The risk in tightening a filter is refusing real questions. Anything
    the old substring matcher accepted for a legitimate reason must still
    be accepted. Where the two disagree, the only allowed direction is the
    old one being wrong -- a fragment of an unrelated word.
    """

    def test_the_new_matcher_never_refuses_what_the_old_one_accepted_legitimately(self):
        for message in ON_TOPIC + NEWLY_ACCEPTED + COMPOUND_SPELLINGS:
            if legacy_is_breastfeeding_topic(message):
                with self.subTest(message=message):
                    self.assertTrue(is_breastfeeding_topic(message))

    def test_every_disagreement_in_the_corpus_is_one_we_can_account_for(self):
        corpus = ON_TOPIC + NEWLY_ACCEPTED + COMPOUND_SPELLINGS + FRAGMENTS_OF_OTHER_WORDS + OFF_TOPIC
        disagreements = {
            message for message in corpus
            if legacy_is_breastfeeding_topic(message) != is_breastfeeding_topic(message)
        }

        self.assertEqual(disagreements, set(NEWLY_ACCEPTED) | set(FRAGMENTS_OF_OTHER_WORDS))

    def test_a_genuinely_ambiguous_whole_word_still_passes(self):
        """
        Documents a limit rather than hiding it: "feed" and "nurse" are
        real words in other contexts, so no matching rule separates "feed
        my dog" from "feed my baby". Those are left to the grounding
        prompt, which tells the model to say a question is not covered.
        """
        self.assertTrue(is_breastfeeding_topic("I need to feed my dog"))
        self.assertTrue(is_breastfeeding_topic("my friend works as a nurse practitioner"))
