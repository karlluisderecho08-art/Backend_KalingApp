from datetime import timedelta
from unittest.mock import patch

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from articles.models import Article

from .gemini_client import get_ai_response
from .guardrail import OFF_TOPIC_RESPONSE, is_breastfeeding_topic
from .knowledge import build_system_prompt
from .models import ChatMessage, ChatSession
from .views import FOLLOW_UP_WINDOW


class KnowledgeGroundingTests(APITestCase):
    """
    Kali answers from the Knowledge Hub articles rather than from
    general training data. Deliberately not a vector store: the library
    is small enough to send whole, so these assert that it actually
    reaches the prompt and stays current as admins add articles.
    """

    def _article(self, **overrides):
        fields = {
            "title": "How to Breastfeed Correctly: Positioning and Latch",
            "category": "Latching Techniques",
            "read_time": "4 min read",
            "teaser": "Positioning basics.",
            "content": "Bring the baby to the breast, not the breast to the baby.",
            "author": "KalingApp",
        }
        fields.update(overrides)
        return Article.objects.create(**fields)

    def test_articles_are_embedded_in_the_system_prompt(self):
        self._article()

        prompt = build_system_prompt("BASE", strict=True)

        self.assertIn("BASE", prompt)
        self.assertIn("How to Breastfeed Correctly: Positioning and Latch", prompt)
        self.assertIn("Bring the baby to the breast", prompt)
        self.assertIn("Latching Techniques", prompt)

    def test_strict_mode_forbids_answering_beyond_the_articles(self):
        self._article()

        strict = build_system_prompt("BASE", strict=True)
        preferred = build_system_prompt("BASE", strict=False)

        self.assertIn("ONLY", strict)
        self.assertIn("do not guess", strict)
        # The looser mode is the one that still helps past the library.
        self.assertNotIn("Answer ONLY using", preferred)

    def test_a_newly_added_article_is_picked_up_without_a_restart(self):
        """
        Admins add articles through the admin panel on a running server.
        The context is rebuilt per request rather than cached at import
        precisely so a new article doesn't silently fail to exist for
        Kali until the next deploy.
        """
        self._article()
        self.assertNotIn("Mastitis", build_system_prompt("BASE"))

        self._article(title="Recognising Mastitis Early", content="Mastitis often starts as a sore, red patch.")

        prompt = build_system_prompt("BASE")
        self.assertIn("Recognising Mastitis Early", prompt)
        self.assertIn("sore, red patch", prompt)

    def test_an_empty_knowledge_hub_falls_back_to_the_base_persona(self):
        """
        With no articles, strict grounding would produce a Kali that
        refuses literally everything -- a worse failure than answering
        from general guidance, so the rules are left off entirely.
        """
        self.assertEqual(Article.objects.count(), 0)

        self.assertEqual(build_system_prompt("BASE", strict=True), "BASE")


class GuardrailTests(APITestCase):
    """
    Reproduces a real bug found by actually testing chat against the
    live backend: genuinely on-topic questions ("is my baby getting
    enough milk", "is cluster feeding normal") were being rejected as
    off-topic because the keyword list only had compound phrases like
    "milk supply"/"breast milk", nothing matching plain "milk" or
    "feed"/"feeding" alone.
    """

    def test_recognizes_questions_that_were_previously_missed(self):
        self.assertTrue(is_breastfeeding_topic("How do I know if my baby is getting enough milk?"))
        self.assertTrue(is_breastfeeding_topic("Is it normal for my baby to cluster feed in the evening?"))

    def test_still_recognizes_the_original_compound_phrases(self):
        self.assertTrue(is_breastfeeding_topic("What's a good way to increase my milk supply?"))
        self.assertTrue(is_breastfeeding_topic("How long can I store breast milk in the fridge?"))

    def test_still_rejects_clearly_unrelated_questions(self):
        self.assertFalse(is_breastfeeding_topic("what's the weather like today"))
        self.assertFalse(is_breastfeeding_topic("can you recommend a good movie"))


class GeminiClientFallbackTests(APITestCase):
    """
    get_ai_response() must never raise and must never actually attempt
    a real Gemini call in the test environment (no key is ever
    configured for tests, and none should be needed to run them) --
    every path here should land on the local fallback.
    """

    @patch("chat.gemini_client.settings")
    def test_falls_back_locally_with_no_key_configured(self, mock_settings):
        mock_settings.GEMINI_API_KEY = ""

        reply, tokens, used_fallback = get_ai_response("What is a good latch?")

        self.assertTrue(used_fallback)
        self.assertEqual(tokens, 0)
        self.assertTrue(reply)

    @patch("chat.gemini_client._get_client")
    @patch("chat.gemini_client.settings")
    def test_falls_back_locally_on_a_malformed_response(self, mock_settings, mock_get_client):
        mock_settings.GEMINI_API_KEY = "fake-key"
        mock_settings.GEMINI_MODEL = "gemini-2.5-flash"
        # A response with no text content at all should be treated as a
        # failure (raises internally, caught below), not returned to
        # the mother as an empty reply.
        mock_get_client.return_value.models.generate_content.return_value.text = ""

        reply, tokens, used_fallback = get_ai_response("What is a good latch?")

        self.assertTrue(used_fallback)
        self.assertEqual(tokens, 0)
        self.assertTrue(reply)


class SendMessageViewTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="mother@example.com", password="x", is_active=True)
        self.client.force_authenticate(user=self.user)

    def test_off_topic_message_never_reaches_the_model(self):
        with patch("chat.views.get_ai_response") as mock_get_ai_response:
            response = self.client.post("/chat/message/", {"text": "what's the weather like today"})

        self.assertEqual(response.status_code, 200)
        mock_get_ai_response.assert_not_called()

    @patch("chat.views.get_ai_response", return_value=("A good latch covers most of the areola.", 42, False))
    def test_on_topic_message_calls_the_model_and_tracks_counts(self, mock_get_ai_response):
        response = self.client.post("/chat/message/", {"text": "How do I get a good latch?"})

        self.assertEqual(response.status_code, 200)
        mock_get_ai_response.assert_called_once()
        session = ChatSession.objects.get(owner=self.user)
        self.assertEqual(session.prompt_count, 1)
        self.assertEqual(session.token_count, 42)

    def test_message_history_persists_across_requests(self):
        with patch("chat.views.get_ai_response", return_value=("Some reply.", 10, False)):
            self.client.post("/chat/message/", {"text": "How do I get a good latch?"})

        response = self.client.get("/chat/history/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 2)  # her message + the reply


class ConversationContinuityTests(APITestCase):
    """
    Reproduces a real complaint: Kali ends a reply with "would you like
    to know more about that?", the mother answers "yes", and she's told
    her own answer is off topic. Two separate defects caused it -- the
    keyword guardrail judging each message alone, and the model being
    sent one message with no memory of the exchange it belonged to --
    and both had to be fixed for the conversation to hold together.
    """

    def setUp(self):
        self.user = User.objects.create_user(email="mother@example.com", password="x", is_active=True)
        self.client.force_authenticate(user=self.user)

    def _kali_just_asked(self, question="Would you like to know more about that?"):
        session, _ = ChatSession.objects.get_or_create(owner=self.user)
        ChatMessage.objects.create(session=session, text="How do I get a good latch?", is_user=True)
        ChatMessage.objects.create(session=session, text=question, is_user=False)
        return session

    @patch("chat.views.get_ai_response", return_value=("Here's more detail.", 20, False))
    def test_yes_after_a_question_from_kali_is_not_rejected_as_off_topic(self, mock_get_ai_response):
        self._kali_just_asked()

        response = self.client.post("/chat/message/", {"text": "yes"})

        self.assertEqual(response.status_code, 200)
        mock_get_ai_response.assert_called_once()
        self.assertNotEqual(response.data["reply"]["text"], OFF_TOPIC_RESPONSE)

    @patch("chat.views.get_ai_response", return_value=("Here's more detail.", 20, False))
    def test_the_model_receives_the_conversation_so_far(self, mock_get_ai_response):
        self._kali_just_asked()

        self.client.post("/chat/message/", {"text": "yes"})

        history = mock_get_ai_response.call_args.kwargs["history"]
        # Oldest first, and the current message is NOT duplicated in it --
        # the client appends that itself.
        self.assertEqual(
            history,
            [(True, "How do I get a good latch?"), (False, "Would you like to know more about that?")],
        )

    @patch("chat.views.get_ai_response", return_value=("Here's more detail.", 20, False))
    def test_a_stale_conversation_is_judged_on_its_own_words_again(self, mock_get_ai_response):
        """
        The follow-up allowance is time-boxed: "yes" typed days later
        isn't answering anything, so the keyword guardrail applies as
        normal rather than the exchange staying open forever.
        """
        session = self._kali_just_asked()
        stale = timezone.now() - FOLLOW_UP_WINDOW - timedelta(minutes=1)
        session.messages.update(created_at=stale)

        response = self.client.post("/chat/message/", {"text": "yes"})

        self.assertEqual(response.status_code, 200)
        mock_get_ai_response.assert_not_called()
        self.assertEqual(response.data["reply"]["text"], OFF_TOPIC_RESPONSE)

    def test_a_first_message_with_no_conversation_still_needs_to_be_on_topic(self):
        with patch("chat.views.get_ai_response") as mock_get_ai_response:
            response = self.client.post("/chat/message/", {"text": "yes"})

        self.assertEqual(response.status_code, 200)
        mock_get_ai_response.assert_not_called()
        self.assertEqual(response.data["reply"]["text"], OFF_TOPIC_RESPONSE)
