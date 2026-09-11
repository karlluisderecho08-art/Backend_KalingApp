from unittest.mock import patch

from rest_framework.test import APITestCase

from accounts.models import User

from .gemini_client import get_ai_response
from .guardrail import is_breastfeeding_topic
from .models import ChatSession


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
