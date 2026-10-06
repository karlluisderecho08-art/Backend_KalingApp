"""
Does the sentence that answers a question actually reach Kali's prompt?

Everything else in chat/tests*.py checks mechanics -- that chunking splits
where it should, that a fallback falls back. None of it says whether
retrieval is any good, which is the thing the margin, top-k and chunk size
were tuned against three sample questions to achieve. This is the
measurement: 26 questions over the six real seeded articles, each with
the sentence that answers it, scored as "is that sentence in the
passages retrieved".

What this can and cannot tell you:

  * It runs keyword (BM25) retrieval only. Embeddings need a live API key
    and a test suite that makes paid calls depending on a developer's
    .env is not a test suite, so the semantic half is not measured here.
    The one miss below is exactly the kind it exists to rescue.
  * The questions were written knowing their answers, so their wording
    overlaps the articles more than a real mother's will. Treat the score
    as a regression floor -- a change that makes retrieval worse will drop
    it -- not as an estimate of accuracy on real questions.
"""

from io import StringIO

from django.core.management import call_command
from django.test import override_settings
from rest_framework.test import APITestCase

from .retrieval import retrieve

# (question, phrases of which at least one must appear in a retrieved passage)
EVAL_SET = [
    ("How should I hold my baby when breastfeeding?", ("ear, shoulder, and hip form a straight line",)),
    ("What are the signs of a good latch?", ("lower lip is turned outward",)),
    ("Why are night feeds important?", ("Night feeds are especially important",)),
    ("What if my baby is too weak to suckle?", ("expressing milk and feeding it to them is a safe alternative",)),
    ("What causes low milk supply?", ("delayed start to breastfeeding, little or no skin-to-skin",)),
    ("How do I tell engorgement from just full breasts?", ("Full breasts feel hot, heavy, and hard",)),
    ("What can I put on cracked nipples?", ("dabbing a little expressed breast milk onto the nipple",)),
    ("What is mastitis and what causes it?", ("a condition called mastitis",)),
    ("How do I relieve a blocked duct?", ("massage gently toward the nipple",)),
    ("How long does breast milk keep at room temperature?", ("about 8 hours at room temperature",)),
    ("Where in the fridge should I store breast milk?", ("never in the door",)),
    ("What should a lactation room have?", ("cold storage system",)),
    ("What containers can I store expressed milk in?", ("BPA-free plastic bottles",)),
    ("What should I pack for pumping at work?", ("nursing pads or cloths",)),
    ("How many extra calories do I need during pregnancy?", ("280 extra calories",)),
    ("Why is folate important?", ("too little of it can lead to serious birth defects",)),
    ("How much weight should an underweight woman gain in pregnancy?", ("12.5 to 18 kg",)),
    ("What should a breastfeeding mother eat?", ("varied, nutritious diet",)),
    ("What are the four steps of Unang Yakap?", ("Immediate and thorough drying of the baby",)),
    ("How soon after birth should I start breastfeeding?", ("first 60 to 90 minutes after birth",)),
    ("Why shouldn't pacifiers be given early?", ("pacifiers should be avoided to prevent nipple confusion",)),
    ("What is exclusive breastfeeding?", ("Exclusive breastfeeding means feeding a baby only breast milk",)),
    (
        "What is colostrum?",
        (
            "called colostrum, is rich in antibodies",
            "colostrum, the protein- and nutrient-rich first milk",
        ),
    ),
    ("What are the alternatives if I cannot breastfeed?", ("screened milk from a healthy donor or a human-milk bank",)),
    ("Do breastfed babies get fewer infections?", ("fewer ear infections, less diarrhea",)),
    ("Can I give my baby donor milk from a milk bank?", ("human-milk bank",)),
]

# Questions keyword retrieval is known to miss. "How soon after birth"
# shares no distinctive word with "within the first 60 to 90 minutes after
# birth": a paraphrase, which is what the embedding half is for. Listed
# so the test fails if this set changes in EITHER direction -- a miss that
# starts passing is news worth recording, not silently absorbed.
KNOWN_KEYWORD_MISSES = {
    "How soon after birth should I start breastfeeding?",
}

# Below this, retrieval has got meaningfully worse than when it was
# measured (25/26 = 96%).
RECALL_FLOOR = 0.90


@override_settings(CHAT_RETRIEVAL_USE_EMBEDDINGS=False)
class RetrievalEvaluationTests(APITestCase):
    def setUp(self):
        call_command("seed_articles", stdout=StringIO())
        retrieve("build the index")

    def found(self, question, answers):
        retrieved = " ".join(chunk.text for chunk in retrieve(question))
        return any(answer in retrieved for answer in answers)

    def test_every_answer_phrase_really_is_in_the_corpus(self):
        """
        Guards the evaluation itself: a typo in an expected phrase would
        read as a retrieval miss forever.
        """
        from .models import KnowledgeChunk

        everything = " ".join(chunk.text for chunk in KnowledgeChunk.objects.all())
        for question, answers in EVAL_SET:
            with self.subTest(question=question):
                self.assertTrue(
                    any(answer in everything for answer in answers),
                    "none of the expected phrases exist in any article",
                )

    def test_each_question_finds_the_passage_that_answers_it(self):
        for question, answers in EVAL_SET:
            if question in KNOWN_KEYWORD_MISSES:
                continue
            with self.subTest(question=question):
                self.assertTrue(self.found(question, answers), "the answering passage was not retrieved")

    def test_the_known_misses_are_still_exactly_the_known_misses(self):
        missed = {q for q, answers in EVAL_SET if not self.found(q, answers)}

        self.assertEqual(missed, KNOWN_KEYWORD_MISSES)

    def test_overall_recall_has_not_dropped_below_the_floor(self):
        hits = sum(self.found(question, answers) for question, answers in EVAL_SET)

        self.assertGreaterEqual(hits / len(EVAL_SET), RECALL_FLOOR)

    def test_the_prompt_stays_small_whatever_the_question(self):
        """Retrieval exists to send a few passages, not the library."""
        from django.conf import settings

        for question, _ in EVAL_SET:
            with self.subTest(question=question):
                self.assertLessEqual(len(retrieve(question)), settings.CHAT_RETRIEVAL_TOP_K)
