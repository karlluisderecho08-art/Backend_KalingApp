"""
The Tagalog the mother app gets (core/i18n.py, core/tagalog_catalog.py).

The coverage tests build each message the way the server really builds it
(results.py, transitions.py, the notification f-strings), so a message added
or reworded later without a Tagalog line fails here instead of quietly
reaching a Tagalog-reading mother in English.
"""

import re

from django.core import mail
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from accounts.emails import send_password_reset_email, send_verification_email
from accounts.models import User
from accounts.serializers import RegisterSerializer
from chat.guardrail import OFF_TOPIC_RESPONSE, is_breastfeeding_topic
from chat.local_fallback import get_local_clinical_response
from milkbank import results
from milkbank.models import MilkBankRequest
from milkbank.transitions import SLA_EXPIRY_MESSAGES, STATUS_NOTIFICATIONS
from notifications.models import NotificationItem
from notifications.services import notify

from .i18n import is_tagalog, translate
from .tagalog_catalog import LABELS, MESSAGES

Status = MilkBankRequest.Status
SLOT = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


class CatalogTests(TestCase):
    def test_every_line_keeps_its_placeholders(self):
        for english, tagalog in MESSAGES.items():
            self.assertEqual(sorted(SLOT.findall(english)), sorted(SLOT.findall(tagalog)), english)

    def test_no_line_is_left_in_english(self):
        # A line identical to its English is a missed translation -- except
        # the pure joins whose fixed text is only punctuation.
        for english, tagalog in MESSAGES.items():
            if any(c.isalpha() for c in SLOT.sub("", english)):
                self.assertNotEqual(english, tagalog, english)

    def test_language_header(self):
        for header in ("tl", "fil", "tl-PH", "fil-PH,en;q=0.5"):
            self.assertTrue(is_tagalog(header), header)
        for header in ("", "en", "en-US,tl;q=0.5", None):
            self.assertFalse(is_tagalog(header), header)


class EveryMotherMessageIsTranslatedTests(TestCase):
    def assertTranslated(self, english):
        self.assertNotEqual(translate(english), english, f"No Tagalog for: {english!r}")

    def test_booking_results(self):
        for reason in results.DECLINE_RESULTS:
            req = MilkBankRequest(current_sub_status=Status.DECLINED, decline_reason=reason)
            result = results.mother_result(req)
            self.assertTranslated(result["title"])
            self.assertTranslated(result["message"])
        generic = results.mother_result(MilkBankRequest(current_sub_status=Status.DECLINED, decline_reason="Others"))
        self.assertTranslated(generic["title"])
        self.assertTranslated(generic["message"])
        for title, message in (results.EXPIRED_FACILITY, results.EXPIRED_MOTHER):
            self.assertTranslated(title)
            self.assertTranslated(message)
        for kind in MilkBankRequest.RequestType:
            req = MilkBankRequest(current_sub_status=Status.COMPLETED, request_type=kind)
            result = results.mother_result(req)
            self.assertTranslated(result["title"])
            self.assertTranslated(result["message"])

    def test_decline_notifications(self):
        for reason in list(results.DECLINE_RESULTS) + ["Others"]:
            text = results.decline_notification(reason, "")
            self.assertTranslated(text)
        # A staff note is her facility's own words: kept, the rest translated.
        non_health = next(r for r, (_, _, health) in results.DECLINE_RESULTS.items() if not health)
        text = results.decline_notification(non_health, "Please bring your ID.")
        self.assertIn("Paalala mula sa pasilidad: Please bring your ID.", translate(text))
        # An unknown reason: the staff note is the explanation, kept as written.
        text = translate(results.decline_notification("Others", "Please bring your ID."))
        self.assertEqual(text, "Hindi tinanggap ang kahilingan. Please bring your ID.")

    def test_status_notifications(self):
        for message in list(STATUS_NOTIFICATIONS.values()) + list(SLA_EXPIRY_MESSAGES.values()):
            self.assertTranslated(message)
        for status in Status:
            self.assertTranslated(f"Milk Bank Request: {status.label}")

    def test_stage_moved_notification(self):
        for stage in MilkBankRequest.DONOR_STAGES + MilkBankRequest.RECIPIENT_STAGES:
            text = translate(f'Your request has moved to the "{stage}" phase.')
            self.assertIn(f'"{LABELS[stage]}"', text)

    def test_collection_and_booking_chat_messages(self):
        from messaging.views import CHAT_CLOSED
        from milkbank.serializers import DELIVERY_LINK_INVALID, DELIVERY_LINK_REQUIRED

        for message in (
            DELIVERY_LINK_REQUIRED, DELIVERY_LINK_INVALID, CHAT_CLOSED,
            "Choose how your milk will reach the facility.",
            "Only a donation has a collection method.",
            "This booking has already ended.",
            "Your milk has already been collected.",
            "A message can't be empty.",
            "New message from PGH Human Milk Bank",
            "PGH Human Milk Bank sent you a message about your booking. Open Booking Status to reply.",
        ):
            self.assertTranslated(message)

    def test_kali_fixed_replies(self):
        self.assertTranslated(OFF_TOPIC_RESPONSE)
        for prompt in ("latch", "milk supply", "storage", "donate", "hello", "something else"):
            self.assertTranslated(get_local_clinical_response(prompt))

    def test_dates_inside_messages(self):
        self.assertEqual(
            translate("Every milk bank is fully booked on November 12, 2026. Please choose another date."),
            "Puno na ang lahat ng milk bank sa Nobyembre 12, 2026. Pumili ng ibang petsa.",
        )

    def test_unknown_text_is_left_alone(self):
        for text in ("Results", "Completed but different", "Some sentence. Another one.", ""):
            if text not in MESSAGES:
                self.assertEqual(translate(text), text)


class ResponseLanguageTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="mother@example.com", password="the-password", is_active=True)
        self.client = APIClient()

    def test_errors_follow_the_header(self):
        body = {"email": "mother@example.com", "password": "wrong"}
        english = self.client.post("/auth/login/", body, format="json")
        tagalog = self.client.post("/auth/login/", body, format="json", HTTP_ACCEPT_LANGUAGE="tl")
        self.assertEqual(english.json()["detail"], "No active account found with the given credentials")
        self.assertEqual(tagalog.json()["detail"], MESSAGES["No active account found with the given credentials"])

    def test_notifications_translated_with_english_kept_for_matching(self):
        notify(self.user, "Milk Bank Request: Declined", "The facility declined your request.", NotificationItem.Category.BOOKINGS)
        self.client.force_authenticate(self.user)
        item = self.client.get("/notifications/mine/", HTTP_ACCEPT_LANGUAGE="tl").json()[0]
        self.assertEqual(item["title"], "Kahilingan sa Milk Bank: Tinanggihan")
        self.assertEqual(item["title_en"], "Milk Bank Request: Declined")
        self.assertEqual(item["description_en"], "The facility declined your request.")
        self.assertEqual(item["category"], "Bookings")
        english = self.client.get("/notifications/mine/").json()[0]
        self.assertEqual(english["title"], "Milk Bank Request: Declined")

    def test_chat_keeps_her_words_and_translates_kalis_fixed_reply(self):
        self.client.force_authenticate(self.user)
        response = self.client.post("/chat/message/", {"text": "Results"}, format="json", HTTP_ACCEPT_LANGUAGE="tl")
        self.assertEqual(response.json()["reply"]["text"], MESSAGES[OFF_TOPIC_RESPONSE])
        history = self.client.get("/chat/history/", HTTP_ACCEPT_LANGUAGE="tl").json()
        messages = history if isinstance(history, list) else history.get("messages", history.get("results", []))
        self.assertIn("Results", [m["text"] for m in messages if m["is_user"]])
        # Stored in English, so the guardrail's own comparisons still work.
        self.assertTrue(self.user.chat_session.messages.filter(text=OFF_TOPIC_RESPONSE).exists())


class EmailLanguageTests(TestCase):
    def _pending(self):
        serializer = RegisterSerializer(data={
            "email": "mother@example.com", "password": "correct-horse-battery-staple",
            "mom_name": "Rachel", "baby_name": "James",
        })
        self.assertTrue(serializer.is_valid(), serializer.errors)
        return serializer.save()

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_verification_email_in_tagalog(self):
        pending = self._pending()
        send_verification_email(pending, tagalog=True)
        pending.refresh_from_db()
        message = mail.outbox[0]
        self.assertEqual(message.subject, MESSAGES["Your KalingApp verification code"])
        self.assertTrue(message.body.startswith("Kumusta, Rachel!"))
        self.assertIn(pending.code, message.body)
        self.assertIn("15 minuto", message.body)

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_reset_email_in_tagalog_and_english_by_default(self):
        user = User.objects.create_user(email="m@example.com", password="x", is_active=True)
        send_password_reset_email(user, tagalog=True)
        send_password_reset_email(user)
        self.assertEqual(mail.outbox[0].subject, MESSAGES["Your KalingApp password reset code"])
        self.assertTrue(mail.outbox[0].body.startswith("Kumusta!"))
        self.assertEqual(mail.outbox[1].subject, "Your KalingApp password reset code")


class TagalogTopicTests(TestCase):
    def test_tagalog_breastfeeding_questions_reach_kali(self):
        for question in (
            "Gaano kadalas ako dapat magpasuso?", "Sapat ba ang gatas na nakukuha ng baby ko?",
            "Bakit masakit ang pagpapasuso?", "Masakit ang utong ko", "Paano magpadede nang tama?",
        ):
            self.assertTrue(is_breastfeeding_topic(question), question)

    def test_tagalog_off_topic_questions_still_refused(self):
        for question in ("Ano ang lagay ng panahon bukas?", "Sino ang presidente?", "Magluto tayo ng adobo"):
            self.assertFalse(is_breastfeeding_topic(question), question)
