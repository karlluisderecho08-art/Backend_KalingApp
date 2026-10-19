"""POST /auth/me/delete/ -- a mother deleting her own account from the app."""

import shutil
import tempfile
from pathlib import Path

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from articles.models import ArticleComment, SavedArticle
from articles.tests import make_article
from chat.models import ChatMessage, ChatSession
from core.models import AuditLogEntry
from milkbank.models import DonorQuestionnaire, MilkBankRequest, TransactionRecord
from milkbank.tests import make_facility, make_request
from notifications.models import NotificationItem

from .models import User
from .tests import ThrottleIsolatedTestCase

Status = MilkBankRequest.Status
URL = "/auth/me/delete/"


class DeleteMyAccountTests(ThrottleIsolatedTestCase):
    def setUp(self):
        super().setUp()  # fresh rate-limit counters: this endpoint shares the sign-in limit
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        override = override_settings(MEDIA_ROOT=self.media)
        override.enable()
        self.addCleanup(override.disable)

        self.mother = User.objects.create_user(email="leaving@example.com", password="correct-horse", is_active=True)
        self.facility = make_facility(name="Fabella")
        self.staff = User.objects.create_user(
            email="fabella-staff@example.com", password="x", is_active=True,
            role=User.Role.FACILITY_STAFF, facility=self.facility,
        )
        self.client.force_authenticate(user=self.mother)

    def delete(self, password="correct-horse"):
        return self.client.post(URL, {"password": password}, format="json")

    def test_everything_of_hers_is_deleted(self):
        req = make_request(self.mother, self.facility, current_sub_status=Status.COMPLETED)
        DonorQuestionnaire.objects.create(
            request=req, good_general_health=True, lactating_with_excess_supply=True,
            free_of_infectious_disease=True, recent_transfusion_or_transplant=False,
            uses_tobacco_alcohol_or_drugs=False, on_medication_or_supplements=False,
            has_recent_serology_test=True,
            serology_photo=SimpleUploadedFile("test.jpg", b"\xff\xd8\xff\xe0data", content_type="image/jpeg"),
        )
        photo_path = Path(self.media) / DonorQuestionnaire.objects.get(request=req).serology_photo.name
        self.assertTrue(photo_path.exists())
        TransactionRecord.objects.create(
            owner=self.mother, type=TransactionRecord.TransactionType.DONATION,
            facility_name="Fabella", date="2026-10-01", status=TransactionRecord.TransactionStatus.COMPLETED,
        )
        article = make_article()
        ArticleComment.objects.create(article=article, author=self.mother, text="Thank you")
        SavedArticle.objects.create(user=self.mother, article=article)
        NotificationItem.objects.create(owner=self.mother, title="t", description="d", category="Reminders")
        session = ChatSession.objects.create(owner=self.mother)
        ChatMessage.objects.create(session=session, text="hello", is_user=True)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.delete()

        self.assertEqual(response.status_code, 204)
        self.assertFalse(User.objects.filter(email="leaving@example.com").exists())
        for model in (MilkBankRequest, DonorQuestionnaire, TransactionRecord, ArticleComment, SavedArticle,
                      ChatSession, ChatMessage):
            self.assertFalse(model.objects.exists(), model.__name__)
        self.assertFalse(NotificationItem.objects.filter(owner_id=self.mother.id).exists())
        self.assertFalse(photo_path.exists(), "the serology photo file must leave storage too")

    def test_an_open_booking_is_cancelled_and_the_facility_told(self):
        make_request(self.mother, self.facility, preferred_date="2026-12-15", preferred_time="10:00 AM")
        make_request(
            self.mother, self.facility, current_sub_status=Status.COMPLETED,
            preferred_date="2026-11-01",
        )

        self.assertEqual(self.delete().status_code, 204)

        notes = NotificationItem.objects.filter(owner=self.staff, title="Booking Cancelled")
        self.assertEqual(notes.count(), 1)  # only the open one
        self.assertIn("December 15, 2026 at 10:00 AM", notes.get().description)
        self.assertNotIn("leaving@example.com", notes.get().description)
        self.assertTrue(AuditLogEntry.objects.filter(action="booking.cancelled_account_deleted").exists())

    def test_the_deletion_is_logged_without_her_email(self):
        user_id = self.mother.id
        self.assertEqual(self.delete().status_code, 204)

        entry = AuditLogEntry.objects.get(action="user.self_deleted")
        self.assertEqual(entry.target, f"User:{user_id} (mother)")
        self.assertIsNone(entry.actor)

    def test_a_wrong_password_deletes_nothing_and_counts_toward_the_lock(self):
        response = self.delete("wrong")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "wrong_password")
        self.mother.refresh_from_db()
        self.assertEqual(self.mother.failed_login_attempts, 1)

    def test_five_wrong_passwords_lock_it_like_signing_in(self):
        for _ in range(4):
            self.delete("wrong")
        self.assertEqual(self.delete("wrong").status_code, 423)
        # Even the right password is refused while locked.
        self.assertEqual(self.delete().status_code, 423)
        self.assertTrue(User.objects.filter(pk=self.mother.pk).exists())

    def test_staff_and_admins_cannot_delete_themselves(self):
        self.client.force_authenticate(user=self.staff)
        self.assertEqual(self.client.post(URL, {"password": "x"}, format="json").status_code, 403)
        admin = User.objects.create_superuser(email="root@example.com", password="password123")
        self.client.force_authenticate(user=admin)
        self.assertEqual(self.client.post(URL, {"password": "password123"}, format="json").status_code, 403)
        self.assertTrue(User.objects.filter(pk=admin.pk).exists())

    def test_signed_out_is_refused(self):
        self.client.force_authenticate(user=None)
        self.assertEqual(self.delete().status_code, 401)

    def test_her_tokens_stop_working(self):
        from rest_framework_simplejwt.tokens import RefreshToken

        access = str(RefreshToken.for_user(self.mother).access_token)
        self.assertEqual(self.delete().status_code, 204)
        self.client.force_authenticate(user=None)
        response = self.client.get("/auth/me/", HTTP_AUTHORIZATION=f"Bearer {access}")
        self.assertEqual(response.status_code, 401)
