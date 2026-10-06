"""
A citation Kali writes must name a source she was actually given.

The grounding rules ask for a "Source: ..." line and forbid inventing one;
nothing enforced it. A reply could cite an article that does not exist, or
a real one that was not among the passages retrieved for this question,
and look exactly like a verified answer. verify_citations() removes any
citation line that names a source outside the retrieved passages.

What it does NOT do is check that the answer follows from the passages.
That is not something a string comparison can establish, and nothing here
claims it.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APITestCase

from accounts.models import User
from core.models import AuditLogEntry

from .citations import verify_citations
from .gemini_client import get_ai_response
from .models import ChatMessage, ChatSession, KnowledgeChunk

from io import StringIO

NUTRITION = "Nutrition During Pregnancy and Breastfeeding"
LATCH = "How to Breastfeed Correctly: Positioning and Latch"
WHY = "Why Breastfeeding Matters for Your Newborn"
DONOR = "Donor Human Milk Screening Practices"
DONOR_REFERENCE = "Olonan-Jusi et al. (2021). Human milk banking in the Philippines."


def article_chunk(title, text="Some passage text about feeding.", category="Maternal Nutrition"):
    return KnowledgeChunk(
        source_kind=KnowledgeChunk.SourceKind.ARTICLE,
        source_id=1,
        source_title=title,
        source_reference=category,
        ordinal=0,
        text=text,
    )


def literature_chunk(title=DONOR, reference=DONOR_REFERENCE, text="Donor screening covers serology."):
    return KnowledgeChunk(
        source_kind=KnowledgeChunk.SourceKind.LITERATURE,
        source_id=2,
        source_title=title,
        source_reference=reference,
        ordinal=0,
        text=text,
    )


# Each seeded article ends with its own credit line; this one's quoted
# title is NOT the article's title in this system.
WHY_PASSAGE_TEXT = (
    "Mothers should never be shamed or made to feel guilty if they cannot breastfeed.\n\n"
    'Source: UNICEF, "Why breastfeeding is critical for babies." '
    "https://www.unicef.org/stories/why-breastfeeding-best-babies"
)

BODY = "Eat a varied diet with plenty of fluids. Would you like a sample day?"


def reply_citing(line):
    return f"{BODY}\n\n{line}"


class SupportedCitationsAreLeftAloneTests(SimpleTestCase):
    def setUp(self):
        self.passages = [
            article_chunk(NUTRITION),
            literature_chunk(),
            article_chunk(WHY, text=WHY_PASSAGE_TEXT, category="Newborn Health"),
        ]

    def assertKept(self, line):
        reply = reply_citing(line)
        cleaned, removed = verify_citations(reply, self.passages)
        self.assertEqual(removed, [], f"wrongly removed: {line}")
        self.assertEqual(cleaned, reply)

    def test_the_format_the_grounding_rules_ask_for(self):
        self.assertKept(f'Source: "{NUTRITION}" in the Knowledge Hub')

    def test_curly_quotes(self):
        self.assertKept(f"Source: “{NUTRITION}” in the Knowledge Hub")

    def test_case_and_punctuation_do_not_matter(self):
        self.assertKept('Source: "nutrition during pregnancy and breastfeeding." in the knowledge hub')

    def test_markdown_emphasis_around_the_line(self):
        self.assertKept(f'*Source: "{NUTRITION}" in the Knowledge Hub*')

    def test_a_shortened_title(self):
        self.assertKept('Source: "Nutrition During Pregnancy" in the Knowledge Hub')

    def test_a_plural_sources_label(self):
        self.assertKept(f'Sources: "{NUTRITION}" in the Knowledge Hub')

    def test_a_literature_citation_written_out_as_plain_text(self):
        self.assertKept(f"Source: {DONOR_REFERENCE}")

    def test_a_literature_document_cited_by_its_title(self):
        self.assertKept(f'Source: "{DONOR}"')

    def test_two_retrieved_sources_on_one_line(self):
        self.assertKept(f'Source: "{NUTRITION}" and "{DONOR}" in the Knowledge Hub')

    def test_echoing_the_credit_line_that_was_in_the_passage(self):
        """
        The passage Kali was shown ends with 'Source: UNICEF, "Why
        breastfeeding is critical for babies."' -- not the article's title
        here, but text she genuinely read. Citing it is not inventing it.
        """
        self.assertKept('Source: UNICEF, "Why breastfeeding is critical for babies."')

    def test_a_reply_with_no_citation_is_untouched(self):
        reply = "Eat a varied diet."
        self.assertEqual(verify_citations(reply, self.passages), (reply, []))

    def test_the_word_source_in_running_text_is_not_a_citation(self):
        reply = "The source of most soreness is a shallow latch.\nIt is usually fixable."
        self.assertEqual(verify_citations(reply, self.passages), (reply, []))

    def test_verifying_twice_changes_nothing_more(self):
        once, _ = verify_citations(reply_citing('Source: "Made Up Title" in the Knowledge Hub'), self.passages)
        twice, removed = verify_citations(once, self.passages)

        self.assertEqual(twice, once)
        self.assertEqual(removed, [])


class UnsupportedCitationsAreRemovedTests(SimpleTestCase):
    def setUp(self):
        self.passages = [
            article_chunk(NUTRITION),
            literature_chunk(),
            article_chunk(WHY, text=WHY_PASSAGE_TEXT, category="Newborn Health"),
        ]

    def assertRemoved(self, line, passages=None):
        cleaned, removed = verify_citations(
            reply_citing(line), self.passages if passages is None else passages
        )
        self.assertEqual(len(removed), 1, f"not removed: {line}")
        self.assertNotIn("Source", cleaned)
        self.assertEqual(cleaned, BODY, "the answer itself must survive, with no stray blank lines")

    def test_a_title_that_does_not_exist(self):
        self.assertRemoved('Source: "Breastfeeding and Medications" in the Knowledge Hub')

    def test_a_real_article_that_was_not_among_the_retrieved_passages(self):
        self.assertRemoved(f'Source: "{LATCH}" in the Knowledge Hub')

    def test_an_article_category_is_a_label_not_a_source(self):
        self.assertRemoved('Source: "Maternal Nutrition" in the Knowledge Hub')

    def test_a_real_source_with_an_invented_one_beside_it(self):
        """All or nothing: the line goes rather than being spliced back together."""
        self.assertRemoved(f'Source: "{NUTRITION}" and "Breastfeeding and Medications" in the Knowledge Hub')

    def test_a_single_word_does_not_vouch_for_itself(self):
        # "Breastfeeding" appears all over the passages; that is not a source.
        self.assertRemoved('Source: "Breastfeeding" in the Knowledge Hub')

    def test_two_words_that_only_appear_in_the_text_are_not_enough(self):
        self.assertRemoved('Source: "Breastfeeding Basics" in the Knowledge Hub')

    def test_a_plausible_literature_style_citation_that_was_never_provided(self):
        self.assertRemoved("Source: WHO (2023). Infant and young child feeding.")

    def test_when_nothing_was_retrieved_nothing_can_be_cited(self):
        self.assertRemoved(f'Source: "{NUTRITION}" in the Knowledge Hub', passages=[])

    def test_an_empty_citation_is_not_a_citation(self):
        cleaned, removed = verify_citations(reply_citing('Source: "" in the Knowledge Hub'), self.passages)

        self.assertEqual(len(removed), 1)

    def test_the_removed_line_is_reported_for_logging(self):
        line = 'Source: "Breastfeeding and Medications" in the Knowledge Hub'

        _, removed = verify_citations(reply_citing(line), self.passages)

        self.assertEqual(removed, [line])

    def test_a_reply_that_was_only_a_bad_citation_comes_back_empty(self):
        cleaned, removed = verify_citations('Source: "Made Up" in the Knowledge Hub', self.passages)

        self.assertEqual(cleaned, "")
        self.assertEqual(len(removed), 1)

    def test_only_the_bad_line_goes_when_there_are_two(self):
        reply = (
            f'{BODY}\n\nSource: "{NUTRITION}" in the Knowledge Hub\n'
            'Source: "Made Up Title" in the Knowledge Hub'
        )

        cleaned, removed = verify_citations(reply, self.passages)

        self.assertEqual(len(removed), 1)
        self.assertIn(NUTRITION, cleaned)
        self.assertNotIn("Made Up", cleaned)


def fake_gemini(reply):
    client = MagicMock()
    client.models.generate_content.return_value = SimpleNamespace(
        text=reply, usage_metadata=SimpleNamespace(total_token_count=10)
    )
    return client


@override_settings(
    GEMINI_API_KEY="test-key",
    GEMINI_MODEL="test-model",
    CHAT_RETRIEVAL_USE_EMBEDDINGS=False,
)
class CitationsInTheLiveReplyPathTests(APITestCase):
    """verify_citations wired into get_ai_response, against the real corpus."""

    QUESTION = "How should I hold my baby when breastfeeding?"

    def setUp(self):
        call_command("seed_articles", stdout=StringIO())

    def ask(self, reply, question=None):
        with patch("chat.gemini_client._get_client", return_value=fake_gemini(reply)):
            return get_ai_response(question or self.QUESTION)

    def test_a_correct_citation_reaches_the_mother(self):
        reply = reply_citing(f'Source: "{LATCH}" in the Knowledge Hub')

        text, _, used_fallback = self.ask(reply)

        self.assertFalse(used_fallback)
        self.assertEqual(text, reply)

    def test_a_made_up_citation_never_reaches_the_mother(self):
        reply = reply_citing('Source: "Breastfeeding and Medications" in the Knowledge Hub')

        text, _, used_fallback = self.ask(reply)

        self.assertFalse(used_fallback)
        self.assertEqual(text, BODY)

    def test_citing_an_article_that_was_not_retrieved_for_this_question_is_removed(self):
        # Asked about positioning; cites the pregnancy-nutrition article.
        reply = reply_citing(f'Source: "{NUTRITION}" in the Knowledge Hub')

        text, _, _ = self.ask(reply)

        self.assertEqual(text, BODY)

    def test_the_article_credit_line_is_accepted_when_its_passage_was_retrieved(self):
        reply = reply_citing('Source: UNICEF India, "How to Breastfeed Correctly."')

        text, _, _ = self.ask(reply)

        self.assertEqual(text, reply)

    def test_every_removal_is_recorded_so_the_rate_can_be_measured(self):
        line = 'Source: "Breastfeeding and Medications" in the Knowledge Hub'

        self.ask(reply_citing(line))

        entry = AuditLogEntry.objects.get(action="chat.citation_removed")
        self.assertIn("Breastfeeding and Medications", entry.target)

    def test_nothing_is_logged_when_the_citation_is_fine(self):
        self.ask(reply_citing(f'Source: "{LATCH}" in the Knowledge Hub'))

        self.assertFalse(AuditLogEntry.objects.filter(action="chat.citation_removed").exists())

    def test_a_reply_that_was_nothing_but_a_bad_citation_falls_back_not_blank(self):
        reply = 'Source: "Made Up Title" in the Knowledge Hub'

        text, tokens, used_fallback = self.ask(reply)

        self.assertTrue(used_fallback)
        self.assertEqual(tokens, 0)
        self.assertTrue(text.strip())

    def test_a_question_nothing_matches_cannot_be_given_a_citation_at_all(self):
        reply = reply_citing(f'Source: "{LATCH}" in the Knowledge Hub')

        text, _, _ = self.ask(reply, question="zzqx")

        self.assertEqual(text, BODY)

    def test_the_stored_reply_through_the_endpoint_has_no_made_up_citation(self):
        user = User.objects.create_user(email="mother@example.com", password="x", is_active=True)
        self.client.force_authenticate(user=user)
        reply = reply_citing('Source: "Breastfeeding and Medications" in the Knowledge Hub')

        with patch("chat.gemini_client._get_client", return_value=fake_gemini(reply)):
            response = self.client.post("/chat/message/", {"text": self.QUESTION})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["reply"]["text"], BODY)
        stored = ChatMessage.objects.filter(session__owner=user, is_user=False).get()
        self.assertEqual(stored.text, BODY)
        self.assertEqual(ChatSession.objects.get(owner=user).prompt_count, 1)
