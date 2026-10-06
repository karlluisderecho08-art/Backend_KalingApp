"""
Retrieval follows the conversation, for the messages that need it.

"yes", "does it hurt?", "what about at night?" have no topic of their own.
Searched alone they retrieve nothing (BM25 has no word to match) or the
wrong thing ("night" matches a passage about working night shifts), so
Kali answered a follow-up with the model's memory of the conversation and
none of the Knowledge Hub passages for what was being discussed.

Numbers quoted in these tests were measured against the real six-article
corpus (`seed_articles`), lexical retrieval only, not assumed.
"""

from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APITestCase

from accounts.models import User

from .gemini_client import get_ai_response
from .guardrail import OFF_TOPIC_RESPONSE
from .knowledge import (
    CONTEXT_QUESTION_CHARS,
    CONTEXT_REPLY_TAIL_CHARS,
    retrieval_query,
)
from .models import ChatMessage, ChatSession
from .retrieval import retrieve

LATCH_TITLE = "How to Breastfeed Correctly: Positioning and Latch"
WORK_TITLE = "Breastfeeding When You Return to Work"

LATCH_REPLY = (
    "A good latch means the baby's mouth is wide open, the lower lip is turned outward and "
    "the chin touches the breast. Would you like me to explain how to position your baby?\n\n"
    f'Source: "{LATCH_TITLE}" in the Knowledge Hub'
)
WORK_REPLY = (
    "Freshly expressed milk is best kept at the back of the refrigerator. Would you like to "
    "know how to store it for longer?\n\n"
    f'Source: "{WORK_TITLE}" in the Knowledge Hub'
)

LATCH_HISTORY = [(True, "How do I get a good latch?"), (False, LATCH_REPLY)]
WORK_HISTORY = [(True, "How do I keep my milk at work?"), (False, WORK_REPLY)]


def seed_articles():
    call_command("seed_articles", stdout=StringIO())


def text_of(passages):
    return " ".join(chunk.text for chunk in passages)


class RetrievalQueryTests(SimpleTestCase):
    """The decision itself, with no database and no retrieval."""

    def test_a_message_that_names_its_own_topic_is_searched_as_it_is(self):
        message = "How long can I keep breast milk in the refrigerator?"

        self.assertEqual(retrieval_query(message, LATCH_HISTORY), message)

    def test_a_message_is_searched_as_it_is_when_there_is_no_conversation(self):
        self.assertEqual(retrieval_query("yes", []), "yes")

    def test_a_follow_up_is_searched_together_with_what_was_being_discussed(self):
        query = retrieval_query("yes", LATCH_HISTORY)

        self.assertIn("How do I get a good latch?", query)
        self.assertIn("Would you like me to explain how to position your baby?", query)
        self.assertTrue(query.endswith("yes"))

    def test_the_question_comes_first_then_the_reply_then_the_message(self):
        query = retrieval_query("yes", LATCH_HISTORY)

        self.assertLess(query.index("How do I get a good latch?"), query.index("wide open"))
        self.assertLess(query.index("wide open"), query.rindex("yes"))

    def test_only_the_end_of_a_long_reply_is_used(self):
        reply = "OPENING-MARKER " + ("filler " * 200) + "CLOSING-MARKER"

        query = retrieval_query("yes", [(True, "a question"), (False, reply)])

        self.assertIn("CLOSING-MARKER", query)
        self.assertNotIn("OPENING-MARKER", query)

    def test_a_long_opening_question_is_cut_short_too(self):
        question = "QUESTION-START " + ("filler " * 200) + "QUESTION-END"

        query = retrieval_query("yes", [(True, question), (False, "ok")])

        self.assertIn("QUESTION-START", query)
        self.assertNotIn("QUESTION-END", query)

    def test_the_query_stays_bounded_however_long_the_conversation_was(self):
        history = [(True, "q " * 5000), (False, "r " * 5000)]

        query = retrieval_query("yes", history)

        self.assertLessEqual(
            len(query), CONTEXT_QUESTION_CHARS + CONTEXT_REPLY_TAIL_CHARS + len("yes") + 2
        )

    def test_a_reply_with_no_question_before_it_still_provides_context(self):
        query = retrieval_query("yes", [(False, LATCH_REPLY)])

        self.assertIn("position your baby", query)
        self.assertTrue(query.endswith("yes"))

    def test_the_canned_refusal_is_not_context(self):
        """
        The refusal names "latching, milk supply, storage, or donor
        milk". Searching on those words would steer every follow-up
        toward whichever article matches them best.
        """
        history = [(True, "how do I fix a car engine"), (False, OFF_TOPIC_RESPONSE)]

        self.assertEqual(retrieval_query("yes", history), "yes")

    def test_a_history_that_does_not_end_on_a_reply_is_not_context(self):
        history = [(False, LATCH_REPLY), (True, "something else I typed")]

        self.assertEqual(retrieval_query("yes", history), "yes")


@override_settings(CHAT_RETRIEVAL_USE_EMBEDDINGS=False)
class FollowUpRetrievalTests(APITestCase):
    def setUp(self):
        seed_articles()
        retrieve("warm the index")

    def test_a_bare_follow_up_finds_nothing_to_answer_from(self):
        """The defect, stated as a fact about retrieval so the next tests mean something."""
        self.assertEqual(retrieve("yes"), [])
        self.assertEqual(retrieve("does it hurt?"), [])

    def test_yes_after_a_latching_reply_retrieves_the_latching_article(self):
        passages = retrieve(retrieval_query("yes", LATCH_HISTORY))

        self.assertEqual(passages[0].source_title, LATCH_TITLE)
        # Kali had just offered to explain positioning; that passage is in
        # what the model is given.
        self.assertIn("ear, shoulder, and hip form a straight line", text_of(passages))

    def test_a_follow_up_with_words_of_its_own_still_stays_on_the_topic(self):
        """
        "night" alone matches a passage about working night shifts, which
        is what searching for it bare returns first. In a conversation
        about latching she means night *feeds*.
        """
        passages = retrieve(retrieval_query("what about at night?", LATCH_HISTORY))

        self.assertEqual(passages[0].source_title, LATCH_TITLE)
        self.assertIn("Night feeds are especially important", text_of(passages))

    def test_yes_after_a_storage_reply_retrieves_the_storage_passages(self):
        passages = retrieve(retrieval_query("yes", WORK_HISTORY))

        self.assertEqual(passages[0].source_title, WORK_TITLE)
        self.assertIn("within 24 hours", text_of(passages))

    def test_the_same_follow_up_goes_to_whichever_topic_was_just_discussed(self):
        after_latching = retrieve(retrieval_query("yes", LATCH_HISTORY))[0].source_title
        after_storage = retrieve(retrieval_query("yes", WORK_HISTORY))[0].source_title

        self.assertNotEqual(after_latching, after_storage)

    def test_changing_the_subject_is_not_dragged_back_to_the_old_topic(self):
        question = "How long can I keep breast milk in the refrigerator?"

        passages = retrieve(retrieval_query(question, LATCH_HISTORY))

        self.assertEqual(passages[0].source_title, WORK_TITLE)
        self.assertEqual(
            [chunk.pk for chunk in passages],
            [chunk.pk for chunk in retrieve(question)],
            "a message that names its own topic must retrieve exactly what it would alone",
        )

    def test_a_follow_up_after_a_refusal_is_not_steered_by_the_refusal(self):
        history = [(True, "how do I fix a car engine"), (False, OFF_TOPIC_RESPONSE)]

        self.assertEqual(retrieve(retrieval_query("yes", history)), [])


def fake_gemini(reply="Here is a bit more.\n\n"):
    client = MagicMock()
    client.models.generate_content.return_value = SimpleNamespace(
        text=reply, usage_metadata=SimpleNamespace(total_token_count=42)
    )
    return client


def system_instruction_sent(client):
    return client.models.generate_content.call_args.kwargs["config"].system_instruction


def message_sent(client):
    """The text of the final turn the model is sent -- the mother's own message."""
    return client.models.generate_content.call_args.kwargs["contents"][-1].parts[0].text


@override_settings(
    GEMINI_API_KEY="test-key",
    GEMINI_MODEL="test-model",
    CHAT_RETRIEVAL_USE_EMBEDDINGS=False,
)
class FollowUpReachesTheModelTests(APITestCase):
    """The same behaviour, observed at what is actually sent to Gemini."""

    def setUp(self):
        seed_articles()

    def test_the_prompt_carries_the_topics_passages_for_a_bare_yes(self):
        client = fake_gemini()
        with patch("chat.gemini_client._get_client", return_value=client):
            get_ai_response("yes", history=LATCH_HISTORY)

        instruction = system_instruction_sent(client)
        self.assertIn("ear, shoulder, and hip form a straight line", instruction)
        self.assertIn(LATCH_TITLE, instruction)

    def test_the_model_is_still_sent_exactly_what_the_mother_typed(self):
        """Only the search is widened. The conversation turn is untouched."""
        client = fake_gemini()
        with patch("chat.gemini_client._get_client", return_value=client):
            get_ai_response("yes", history=LATCH_HISTORY)

        self.assertEqual(message_sent(client), "yes")

    def test_a_bare_yes_with_no_conversation_is_told_nothing_is_covered(self):
        from .knowledge import NO_MATCH_NOTICE

        client = fake_gemini()
        with patch("chat.gemini_client._get_client", return_value=client):
            get_ai_response("yes", history=[])

        self.assertIn(NO_MATCH_NOTICE, system_instruction_sent(client))

    def test_a_follow_up_reaches_the_model_through_the_real_endpoint(self):
        """POST /chat/message/ "yes", the way the app sends it."""
        user = User.objects.create_user(email="mother@example.com", password="x", is_active=True)
        session = ChatSession.objects.create(owner=user)
        ChatMessage.objects.create(session=session, text="How do I get a good latch?", is_user=True)
        ChatMessage.objects.create(session=session, text=LATCH_REPLY, is_user=False)
        self.client.force_authenticate(user=user)

        client = fake_gemini(reply="Position the baby tummy to tummy.")
        with patch("chat.gemini_client._get_client", return_value=client):
            response = self.client.post("/chat/message/", {"text": "yes"})

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["used_fallback"])
        self.assertIn("ear, shoulder, and hip form a straight line", system_instruction_sent(client))
        self.assertEqual(message_sent(client), "yes")
