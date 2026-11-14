"""
The security and privacy fixes from the 2026-11 review, one class per finding.
"""

import io
import os
import shutil
import tempfile
from unittest.mock import patch

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from rest_framework.test import APIClient, APITestCase

from accounts.models import PendingRegistration, User
from accounts.tests import _SynchronousThread
from articles.models import Article, ArticleComment
from core.audit import log_action
from milkbank.models import DonorQuestionnaire, MilkBankRequest
from milkbank.tests import make_facility, make_request

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
HTML = b"<html><script>alert(document.cookie)</script></html>"
PASSWORD = "correct-horse-battery-staple"
ANSWERS = {
    "good_general_health": "true",
    "lactating_with_excess_supply": "true",
    "free_of_infectious_disease": "true",
    "recent_transfusion_or_transplant": "false",
    "uses_tobacco_alcohol_or_drugs": "false",
    "on_medication_or_supplements": "false",
    "has_recent_serology_test": "true",
}


class ThrottleIsolationMixin:
    """Fresh rate-limit counters, and emails sent inline instead of on a thread."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        patcher = patch("accounts.views.threading.Thread", new=_SynchronousThread)
        patcher.start()
        self.addCleanup(patcher.stop)


# --- Article comments: no email, no names for anonymous readers ------------

class CommentPrivacyTests(APITestCase):
    def setUp(self):
        self.article = Article.objects.create(
            title="Latching", category=Article.Category.choices[0][0], read_time="5 min read",
            teaser="t", content="c", author="KalingApp", date="2026-11-01",
        )
        self.named = User.objects.create_user(email="maria@example.com", password="x", mom_name="Maria")
        self.unnamed = User.objects.create_user(email="secret.mother@example.com", password="x", mom_name="")
        ArticleComment.objects.create(article=self.article, author=self.named, text="Helpful!")
        ArticleComment.objects.create(article=self.article, author=self.unnamed, text="Thanks")

    def test_anonymous_readers_get_the_article_but_no_comments(self):
        response = self.client.get(f"/articles/{self.article.id}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["title"], "Latching")
        self.assertEqual(response.data["comments"], [])

    def test_a_comment_never_shows_its_authors_email(self):
        self.client.force_authenticate(user=self.named)
        comments = self.client.get(f"/articles/{self.article.id}/").data["comments"]
        self.assertEqual(len(comments), 2)
        self.assertNotIn("secret.mother@example.com", str(comments))
        names = {c["text"]: c["author_name"] for c in comments}
        self.assertEqual(names, {"Helpful!": "Maria", "Thanks": ""})

    def test_is_mine_marks_only_the_viewers_own_comment(self):
        self.client.force_authenticate(user=self.named)
        comments = self.client.get(f"/articles/{self.article.id}/").data["comments"]
        self.assertEqual({c["text"]: c["is_mine"] for c in comments}, {"Helpful!": True, "Thanks": False})

    def test_admins_moderating_still_see_who_wrote_it(self):
        comment = ArticleComment.objects.get(author=self.unnamed)
        ArticleComment.objects.filter(pk=comment.pk).update(is_reported=True)
        admin = User.objects.create_superuser(email="admin@example.com", password="x")
        self.client.force_authenticate(user=admin)
        rows = self.client.get("/articles/comments/reported/").data
        self.assertEqual(rows[0]["author_email"], "secret.mother@example.com")

    def test_a_comment_has_a_length_limit(self):
        self.client.force_authenticate(user=self.named)
        response = self.client.post(f"/articles/{self.article.id}/comments/", {"text": "x" * 2001})
        self.assertEqual(response.status_code, 400)


# --- Facility staff see only their own facility's mothers -------------------

class StaffSeesOnlyTheirMothersTests(APITestCase):
    def setUp(self):
        self.here = make_facility(name="Here")
        self.there = make_facility(name="There")
        self.staff = User.objects.create_user(
            email="staff@example.com", password="x", role=User.Role.FACILITY_STAFF, facility=self.here,
        )
        self.mine = User.objects.create_user(email="mine@example.com", password="x", mom_name="Mine")
        self.both = User.objects.create_user(email="both@example.com", password="x", mom_name="Both")
        self.elsewhere = User.objects.create_user(email="elsewhere@example.com", password="x")
        self.never = User.objects.create_user(email="never@example.com", password="x")
        make_request(self.mine, self.here)
        make_request(self.mine, self.here)  # two bookings: still listed once
        donated_here = make_request(self.both, self.here)
        donated_there = make_request(self.both, self.there)
        received_here = make_request(self.both, self.here, request_type=MilkBankRequest.RequestType.RECIPIENT)
        make_request(self.elsewhere, self.there)
        completed = MilkBankRequest.Status.COMPLETED
        MilkBankRequest.objects.filter(pk=donated_here.pk).update(current_sub_status=completed, amount_ml=150)
        MilkBankRequest.objects.filter(pk=donated_there.pk).update(current_sub_status=completed, amount_ml=900)
        MilkBankRequest.objects.filter(pk=received_here.pk).update(current_sub_status=completed, amount_ml=60)
        User.objects.filter(pk=self.both.pk).update(total_drawn_ml=1050, total_received_ml=60)
        self.client.force_authenticate(user=self.staff)

    def test_only_mothers_who_booked_here_are_listed_once_each(self):
        emails = [row["email"] for row in self.client.get("/auth/users/").data]
        self.assertEqual(sorted(emails), ["both@example.com", "mine@example.com"])

    def test_totals_are_this_facilitys_only(self):
        row = next(r for r in self.client.get("/auth/users/").data if r["email"] == "both@example.com")
        self.assertEqual((row["total_drawn_ml"], row["total_received_ml"]), (150, 60))

    def test_no_baby_or_clinic_details_are_shared(self):
        row = self.client.get("/auth/users/").data[0]
        for field in ("baby_name", "baby_birth_date", "pediatric_clinic", "baby_age_weeks", "location_consent_given"):
            self.assertNotIn(field, row)

    def test_a_mother_from_another_facility_cannot_be_deactivated(self):
        response = self.client.post(f"/auth/users/{self.elsewhere.id}/deactivate/")
        self.assertEqual(response.status_code, 404)
        self.elsewhere.refresh_from_db()
        self.assertTrue(self.elsewhere.is_active)

    def test_own_mother_can_still_be_deactivated(self):
        self.assertEqual(self.client.post(f"/auth/users/{self.mine.id}/deactivate/").status_code, 200)

    def test_staff_with_no_facility_see_nobody(self):
        loose = User.objects.create_user(email="loose@example.com", password="x", role=User.Role.FACILITY_STAFF)
        self.client.force_authenticate(user=loose)
        self.assertEqual(self.client.get("/auth/users/").data, [])


# --- Signing up again never replaces a real account -------------------------

class SignupNeverReplacesAnAccountTests(ThrottleIsolationMixin, APITestCase):
    def register(self, email):
        return self.client.post("/auth/register/", {"email": email, "password": PASSWORD})

    def test_a_deactivated_verified_mother_cannot_sign_up_again(self):
        mother = User.objects.create_user(email="banned@example.com", password="x", email_verified=True)
        User.objects.filter(pk=mother.pk).update(is_active=False)
        self.assertEqual(self.register("banned@example.com").status_code, 400)
        self.assertTrue(User.objects.filter(pk=mother.pk).exists())

    def test_a_deactivated_older_account_with_history_is_kept(self):
        # From before email verification existed: never verified, but used.
        mother = User.objects.create_user(email="old@example.com", password="x", is_active=False)
        make_request(mother, make_facility())
        self.assertEqual(self.register("old@example.com").status_code, 400)
        self.assertTrue(User.objects.filter(pk=mother.pk).exists())

    def test_a_deactivation_on_record_keeps_even_an_empty_account(self):
        mother = User.objects.create_user(email="empty@example.com", password="x", is_active=False)
        log_action(None, "user.deactivated", f"User:{mother.id}")
        self.assertEqual(self.register("empty@example.com").status_code, 400)

    def test_deactivated_staff_are_never_replaced(self):
        staff = User.objects.create_user(
            email="oldstaff@example.com", password="x", is_active=False, role=User.Role.FACILITY_STAFF,
        )
        self.assertEqual(self.register("oldstaff@example.com").status_code, 400)
        self.assertTrue(User.objects.filter(pk=staff.pk).exists())

    def test_verify_refuses_if_the_address_was_taken_meanwhile(self):
        self.assertEqual(self.register("race@example.com").status_code, 201)
        code = PendingRegistration.objects.get(email="race@example.com").code
        taken = User.objects.create_user(email="race@example.com", password="x", email_verified=True)
        User.objects.filter(pk=taken.pk).update(is_active=False)

        response = self.client.post("/auth/verify-email/", {"email": "race@example.com", "code": code})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(User.objects.filter(email="race@example.com").count(), 1)
        self.assertTrue(User.objects.filter(pk=taken.pk).exists())


# --- Rate limits can't be dodged with a forged header -----------------------

class ForgedForwardedForTests(ThrottleIsolationMixin, APITestCase):
    def test_rotating_a_fake_first_entry_does_not_reset_the_login_limit(self):
        User.objects.create_user(email="mother@example.com", password="the-real-password")
        statuses = [
            self.client.post(
                "/auth/login/",
                {"email": "mother@example.com", "password": f"guess-{i}"},
                # The caller writes the first entry; the proxies append theirs.
                HTTP_X_FORWARDED_FOR=f"198.51.100.{i}, 203.0.113.7, 104.23.160.{i}, 10.28.132.{i}",
            ).status_code
            for i in range(12)
        ]
        self.assertIn(429, statuses)

    def test_the_client_is_read_from_the_right(self):
        from rest_framework.test import APIRequestFactory

        from core.throttling import client_ip

        factory = APIRequestFactory()
        forged = factory.get("/", HTTP_X_FORWARDED_FOR="1.1.1.1, 2.2.2.2, 203.0.113.7, 104.23.1.1, 10.0.0.1")
        normal = factory.get("/", HTTP_X_FORWARDED_FOR="203.0.113.7, 104.23.1.1, 10.0.0.1")
        short = factory.get("/", HTTP_X_FORWARDED_FOR="203.0.113.7")
        self.assertEqual(client_ip(forged), "203.0.113.7")
        self.assertEqual(client_ip(normal), "203.0.113.7")
        self.assertEqual(client_ip(short), "203.0.113.7")


# --- Kali has its own rate limit --------------------------------------------

class ChatRateLimitTests(ThrottleIsolationMixin, APITestCase):
    def test_an_account_cannot_send_unlimited_messages(self):
        mother = User.objects.create_user(email="chatty@example.com", password="x")
        self.client.force_authenticate(user=mother)
        with patch("chat.views.get_ai_response", return_value=("A short answer about latching.", 10, False)):
            statuses = [
                self.client.post("/chat/message/", {"text": "How do I improve my baby's latch?"}).status_code
                for _ in range(12)
            ]
        self.assertEqual(statuses[:10], [200] * 10)
        self.assertEqual(statuses[10:], [429, 429])


# --- A password change signs every other session out ------------------------

class PasswordChangeEndsOtherSessionsTests(ThrottleIsolationMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(email="mother@example.com", password=PASSWORD, email_verified=True)
        login = self.client.post("/auth/login/", {"email": "mother@example.com", "password": PASSWORD})
        self.access, self.refresh = login.data["access"], login.data["refresh"]

    def me(self, access):
        return APIClient().get("/auth/me/", HTTP_AUTHORIZATION=f"Bearer {access}")

    def test_old_tokens_work_until_the_password_changes(self):
        self.assertEqual(self.me(self.access).status_code, 200)
        self.user.set_password("a-brand-new-password-9")
        self.user.save()
        self.assertEqual(self.me(self.access).status_code, 401)
        self.assertEqual(self.client.post("/auth/refresh/", {"refresh": self.refresh}).status_code, 401)

    def test_a_password_reset_ends_the_old_sessions_and_the_new_one_works(self):
        self.client.post("/auth/forgot-password/", {"email": "mother@example.com"})
        code = User.objects.get(pk=self.user.pk).password_reset_code
        reset = self.client.post("/auth/reset-password/", {
            "email": "mother@example.com", "code": code, "new_password": "a-brand-new-password-9",
        })
        self.assertEqual(reset.status_code, 200)
        self.assertEqual(self.me(self.access).status_code, 401)
        self.assertEqual(self.me(reset.data["access"]).status_code, 200)
        refreshed = self.client.post("/auth/refresh/", {"refresh": reset.data["refresh"]})
        self.assertEqual(refreshed.status_code, 200)
        self.assertEqual(self.me(refreshed.data["access"]).status_code, 200)

    def test_refresh_for_a_deleted_account_is_a_401_not_a_crash(self):
        self.user.delete()
        self.assertEqual(self.client.post("/auth/refresh/", {"refresh": self.refresh}).status_code, 401)

    def test_refresh_for_a_deactivated_account_is_refused(self):
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        self.assertEqual(self.client.post("/auth/refresh/", {"refresh": self.refresh}).status_code, 401)


# --- Serology photos: only real images, stored and served safely ------------

class SerologyPhotoSafetyTests(APITestCase):
    def setUp(self):
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        override = override_settings(MEDIA_ROOT=self.media)
        override.enable()
        self.addCleanup(override.disable)
        self.facility = make_facility()
        self.mother = User.objects.create_user(email="donor@example.com", password="x")
        self.staff = User.objects.create_user(
            email="staff@example.com", password="x", role=User.Role.FACILITY_STAFF, facility=self.facility,
        )
        self.req = make_request(self.mother, self.facility)

    def submit(self, name, data, content_type):
        self.client.force_authenticate(user=self.mother)
        upload = SimpleUploadedFile(name, data, content_type=content_type)
        return self.client.post(
            f"/milkbank/requests/{self.req.id}/donor-questionnaire/",
            {**ANSWERS, "serology_photo": upload}, format="multipart",
        )

    def test_a_web_page_claiming_to_be_a_jpeg_is_refused(self):
        response = self.submit("photo.jpg", HTML, "image/jpeg")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(DonorQuestionnaire.objects.exists())

    def test_a_real_image_is_stored_under_a_random_name_with_its_true_type(self):
        # Declared as a JPEG named .html; it is really a PNG.
        self.assertEqual(self.submit("evil.html", PNG, "image/jpeg").status_code, 201)
        stored = DonorQuestionnaire.objects.get().serology_photo.name
        self.assertRegex(os.path.basename(stored), r"^[0-9a-f]{32}\.png$")

    def test_an_oversized_photo_is_refused(self):
        with patch("milkbank.serializers.MAX_PHOTO_BYTES", 10):
            self.assertEqual(self.submit("big.jpg", JPEG, "image/jpeg").status_code, 400)

    def test_an_oversized_upload_is_refused_before_it_is_read(self):
        with patch("milkbank.views.MAX_PHOTO_BYTES", 10), patch("milkbank.views.UPLOAD_FORM_OVERHEAD_BYTES", 0):
            self.assertEqual(self.submit("big.jpg", JPEG, "image/jpeg").status_code, 413)

    def test_staff_get_it_back_as_an_image_that_is_never_cached(self):
        self.submit("photo.jpg", JPEG, "image/jpeg")
        self.client.force_authenticate(user=self.staff)
        response = self.client.get(f"/milkbank/requests/{self.req.id}/serology-photo/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/jpeg")
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertIn("sandbox", response["Content-Security-Policy"])
        self.assertTrue(response["Content-Disposition"].startswith("inline"))
        self.assertEqual(b"".join(response.streaming_content), JPEG)

    def test_a_stored_file_that_is_not_an_image_is_never_served(self):
        # Something stored before uploads were checked by content.
        self.submit("photo.jpg", JPEG, "image/jpeg")
        questionnaire = DonorQuestionnaire.objects.get()
        with open(questionnaire.serology_photo.path, "wb") as f:
            f.write(HTML)
        self.client.force_authenticate(user=self.staff)
        self.assertEqual(self.client.get(f"/milkbank/requests/{self.req.id}/serology-photo/").status_code, 404)


# --- Facility staff passwords: one per account, never overwritten needlessly -

@patch.dict(os.environ, {}, clear=False)
class SeedFacilityStaffTests(TestCase):
    STRONG_A = "Gatas-ng-Ina-PGH-2026!"
    STRONG_B = "Kalinga-StLukes-2026#"

    def setUp(self):
        for key in list(os.environ):
            if key.startswith("FACILITY_STAFF_"):
                del os.environ[key]
        self.a = make_facility(name="Facility A")
        self.b = make_facility(name="St. Luke's Medical Center")

    def run_command(self, **env):
        os.environ.update(env)
        out = io.StringIO()
        call_command("seed_facility_staff", stdout=out)
        return out.getvalue()

    def test_each_account_gets_its_own_password(self):
        self.run_command(**{
            f"FACILITY_STAFF_PASSWORD_{self.a.id}": self.STRONG_A,
            f"FACILITY_STAFF_PASSWORD_{self.b.id}": self.STRONG_B,
        })
        a = User.objects.get(email=f"staff{self.a.id}@kalingapp.demo")
        b = User.objects.get(email=f"staff{self.b.id}@kalingapp.demo")
        self.assertTrue(a.check_password(self.STRONG_A))
        self.assertTrue(b.check_password(self.STRONG_B))
        self.assertEqual((a.facility, b.facility), (self.a, self.b))

    def test_the_old_shared_password_is_ignored(self):
        output = self.run_command(FACILITY_STAFF_DEMO_PASSWORD=self.STRONG_A)
        self.assertFalse(User.objects.filter(email__endswith="@kalingapp.demo").exists())
        self.assertIn("no longer used", output)

    def test_weak_published_and_shared_passwords_are_refused(self):
        self.run_command(**{
            f"FACILITY_STAFF_PASSWORD_{self.a.id}": "demo-only-not-a-real-password",
            f"FACILITY_STAFF_PASSWORD_{self.b.id}": "short",
        })
        self.assertFalse(User.objects.filter(email__endswith="@kalingapp.demo").exists())
        self.run_command(**{
            f"FACILITY_STAFF_PASSWORD_{self.a.id}": self.STRONG_A,
            f"FACILITY_STAFF_PASSWORD_{self.b.id}": self.STRONG_A,
        })
        self.assertFalse(User.objects.filter(email__endswith="@kalingapp.demo").exists())

    def test_an_unchanged_password_is_not_reset_so_staff_stay_signed_in(self):
        env = {f"FACILITY_STAFF_PASSWORD_{self.a.id}": self.STRONG_A}
        self.run_command(**env)
        before = User.objects.get(email=f"staff{self.a.id}@kalingapp.demo").password
        self.run_command(**env)
        self.assertEqual(User.objects.get(email=f"staff{self.a.id}@kalingapp.demo").password, before)

    def test_an_existing_account_without_a_variable_is_left_alone(self):
        existing = User.objects.create_user(
            email=f"staff{self.a.id}@kalingapp.demo", password="whatever-it-was-before",
            role=User.Role.FACILITY_STAFF, facility=self.a,
        )
        self.run_command()
        existing.refresh_from_db()
        self.assertTrue(existing.check_password("whatever-it-was-before"))


# --- The API map is not published by the live server ------------------------

class ApiDocsTests(APITestCase):
    def test_docs_are_not_served_outside_development(self):
        # The test runner always runs with DEBUG off, like production.
        self.assertEqual(self.client.get("/api/docs/").status_code, 404)
        self.assertEqual(self.client.get("/api/schema/").status_code, 404)


# --- Staff login addresses are not handed out with the facility list ---------

class FacilityStaffEmailsTests(APITestCase):
    def setUp(self):
        self.facility = make_facility(name="St. Luke's")
        User.objects.create_user(
            email="login.staff@example.com", password="x", is_active=True,
            role=User.Role.FACILITY_STAFF, facility=self.facility,
        )
        # A location, which facility matching needs.
        self.mother = User.objects.create_user(
            email="mother@example.com", password="x", is_active=True, latitude=14.6, longitude=121.0,
        )
        self.staff = User.objects.get(email="login.staff@example.com")
        self.admin = User.objects.create_user(email="admin@example.com", password="x", is_active=True, is_staff=True)

    def facility_payloads(self):
        listing = self.client.get("/milkbank/facilities/")
        detail = self.client.get(f"/milkbank/facilities/{self.facility.id}/")
        self.assertEqual((listing.status_code, detail.status_code), (200, 200))
        rows = listing.data.get("results", listing.data) if isinstance(listing.data, dict) else listing.data
        return list(rows) + [detail.data]

    def test_anonymous_readers_get_no_staff_emails(self):
        for payload in self.facility_payloads():
            self.assertNotIn("staff_emails", payload)
        self.assertNotIn("login.staff@example.com", str(self.client.get("/milkbank/facilities/").content))

    def test_mothers_and_facility_staff_get_no_staff_emails(self):
        for user in (self.mother, self.staff):
            self.client.force_authenticate(user=user)
            for payload in self.facility_payloads():
                self.assertNotIn("staff_emails", payload)

    def test_a_mothers_facility_matching_carries_no_staff_emails(self):
        self.client.force_authenticate(user=self.mother)
        response = self.client.post("/milkbank/allocate/", {"request_type": "DONOR"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["ranked_facilities"], "the facility should have been ranked")
        for payload in response.data["ranked_facilities"]:
            self.assertNotIn("staff_emails", payload)
        self.assertNotIn("login.staff@example.com", str(response.content))

    def test_the_platform_admin_still_sees_who_can_sign_in(self):
        self.client.force_authenticate(user=self.admin)
        detail = self.client.get(f"/milkbank/facilities/{self.facility.id}/")
        self.assertEqual(detail.data["staff_emails"], ["login.staff@example.com"])
