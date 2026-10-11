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

from .citations import cited_articles, verify_citations
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



# --- Linking a citation to the article it names ---------------------------


def article(article_id, title, content="Some article body."):
    return SimpleNamespace(id=article_id, title=title, content=content)


class CitedArticlesTests(SimpleTestCase):
    """
    cited_articles() -- which Knowledge Hub article(s) a reply's "Source:"
    line names, so the app can make the citation a link that opens it.
    """

    def setUp(self):
        self.nutrition = article(1, NUTRITION)
        self.latch = article(2, LATCH)
        self.why = article(3, WHY, content=WHY_PASSAGE_TEXT)
        self.articles = [self.nutrition, self.latch, self.why]

    def cited(self, line):
        return cited_articles(reply_citing(line), self.articles)

    def test_the_format_the_grounding_rules_ask_for(self):
        self.assertEqual(self.cited(f'Source: "{NUTRITION}" in the Knowledge Hub'), [self.nutrition])

    def test_curly_quotes_and_markdown_emphasis(self):
        self.assertEqual(self.cited(f"*Source: “{LATCH}” in the Knowledge Hub*"), [self.latch])

    def test_a_shortened_title(self):
        self.assertEqual(self.cited('Source: "Positioning and Latch"'), [self.latch])

    def test_two_articles_on_one_line_in_the_order_named(self):
        self.assertEqual(
            self.cited(f'Sources: "{LATCH}" and "{NUTRITION}" in the Knowledge Hub'),
            [self.latch, self.nutrition],
        )

    def test_echoing_the_articles_own_credit_line(self):
        self.assertEqual(
            self.cited('Source: UNICEF, "Why breastfeeding is critical for babies."'),
            [self.why],
        )

    def test_a_literature_source_links_nowhere(self):
        self.assertEqual(self.cited(f"Source: {DONOR_REFERENCE}"), [])

    def test_an_article_that_no_longer_exists_links_nowhere(self):
        self.assertEqual(cited_articles(reply_citing(f'Source: "{LATCH}"'), [self.nutrition]), [])

    def test_no_citation_line_means_no_links(self):
        self.assertEqual(cited_articles(BODY, self.articles), [])
        # "source" in running text is not a citation
        self.assertEqual(cited_articles("Milk is the best source of nutrition.", self.articles), [])

    def test_the_same_article_twice_is_listed_once(self):
        reply = f'{BODY}\n\nSource: "{NUTRITION}"\nSource: "{NUTRITION}" in the Knowledge Hub'
        self.assertEqual(cited_articles(reply, self.articles), [self.nutrition])

    def test_an_ambiguous_shortened_title_is_not_guessed(self):
        twins = article(4, "Breastfeeding Twins: Positioning and Latch")
        self.assertEqual(
            cited_articles(reply_citing('Source: "Positioning and Latch"'), self.articles + [twins]), []
        )

    def test_the_longer_whole_title_wins_over_a_shorter_one_inside_it(self):
        basics = article(5, "Breastfeeding Basics")
        twins = article(6, "Breastfeeding Basics for Twins")
        self.assertEqual(
            cited_articles(reply_citing('Source: "Breastfeeding Basics for Twins"'), [basics, twins]), [twins]
        )


class ChatHistoryCarriesSourceLinksTests(APITestCase):
    """GET /chat/history/ hands the app each Kali reply's linked articles."""

    def setUp(self):
        from articles.models import Article

        self.user = User.objects.create_user(email="links@example.com", password="x", is_active=True)
        self.client.force_authenticate(user=self.user)
        self.article = Article.objects.create(
            title=NUTRITION, category=Article.Category.NUTRITION, read_time="3 min read",
            teaser="t", content="Eat well.", author="A", date="2026",
        )
        session = ChatSession.objects.create(owner=self.user)
        ChatMessage.objects.create(session=session, text="What should I eat?", is_user=True)
        ChatMessage.objects.create(
            session=session, is_user=False, text=reply_citing(f'Source: "{NUTRITION}" in the Knowledge Hub'),
        )
        ChatMessage.objects.create(session=session, is_user=False, text="Hello! How can I help?")

    def test_a_cited_article_comes_back_as_a_link_and_other_messages_have_none(self):
        data = self.client.get("/chat/history/").data
        results = data["results"] if isinstance(data, dict) else data

        self.assertEqual(results[0]["sources"], [])  # her own message
        self.assertEqual(results[1]["sources"], [{"article_id": self.article.id, "title": NUTRITION}])
        self.assertEqual(results[2]["sources"], [])  # no citation

    def test_the_articles_are_read_once_for_the_whole_history(self):
        # 3 messages, but the article list is loaded once for the response.
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as queries:
            self.client.get("/chat/history/")
        article_queries = [q for q in queries.captured_queries if "articles_article" in q["sql"]]
        self.assertEqual(len(article_queries), 1)
