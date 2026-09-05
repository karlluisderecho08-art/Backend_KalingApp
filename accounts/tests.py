from datetime import timedelta
from unittest.mock import patch

from django.core import mail
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from .emails import MAX_VERIFICATION_ATTEMPTS, RESEND_COOLDOWN_SECONDS, VERIFICATION_CODE_TTL_MINUTES
from .models import User


class _SynchronousThread:
    """
    Test double for threading.Thread: runs target(*args) immediately on
    .start() instead of actually spawning a thread.

    RegisterView/ResendVerificationView send the verification email on a
    real background thread (see accounts/views.py -- a slow/hung SMTP
    connection must never make the HTTP response wait on it). Without
    this, tests that check mail.outbox right after calling register()
    would be racing a real thread with no guarantee it's finished yet --
    flaky by construction, and a background thread still running once
    the test's DB transaction is torn down is its own separate problem.
    Patching threading.Thread to this for the duration of a test makes
    the "background" work happen deterministically, inline.
    """

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


class RegistrationAndVerificationTests(APITestCase):
    """
    Covers the account lifecycle a real mother goes through: register ->
    (inactive, code emailed) -> verify -> (active, logged in). This is
    the flow the Mother Android app's registration screen depends on --
    see the KalingApp Progress Report for the known client-side bug
    where the app still expects the OLD response shape from before
    email verification existed.
    """

    def setUp(self):
        patcher = patch("accounts.views.threading.Thread", new=_SynchronousThread)
        patcher.start()
        self.addCleanup(patcher.stop)

    def register(self, **overrides):
        payload = {
            "email": "mother@example.com",
            "password": "correct-horse-battery-staple",
            "mom_name": "Rachel",
            "baby_name": "James",
        }
        payload.update(overrides)
        return self.client.post("/auth/register/", payload)

    def test_register_creates_inactive_account_and_emails_a_code(self):
        response = self.register()

        self.assertEqual(response.status_code, 201)
        # New contract: no tokens back directly -- just a confirmation
        # and the email it was sent to. Access/refresh only ever come
        # from /auth/verify-email/ now.
        self.assertNotIn("access", response.data)
        self.assertNotIn("user", response.data)
        self.assertEqual(response.data["email"], "mother@example.com")

        user = User.objects.get(email="mother@example.com")
        self.assertFalse(user.is_active)
        self.assertFalse(user.email_verified)
        self.assertEqual(len(user.email_verification_code), 6)
        self.assertTrue(user.email_verification_code.isdigit())

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(user.email_verification_code, mail.outbox[0].body)
        self.assertEqual(mail.outbox[0].to, ["mother@example.com"])

    def test_register_defaults_blank_baby_name_to_james(self):
        self.register(baby_name="")
        user = User.objects.get(email="mother@example.com")
        self.assertEqual(user.baby_name, "James")

    def test_register_rejects_duplicate_email(self):
        self.register()
        response = self.register()
        self.assertEqual(response.status_code, 400)
        self.assertIn("email", response.data)

    def test_cannot_log_in_before_verifying(self):
        self.register()
        response = self.client.post("/auth/login/", {
            "email": "mother@example.com", "password": "correct-horse-battery-staple",
        })
        # simplejwt's TokenObtainPairView refuses an is_active=False
        # account the same way it refuses a wrong password -- both look
        # like "no active account found" to the client.
        self.assertEqual(response.status_code, 401)

    def test_verify_with_correct_code_activates_and_returns_tokens(self):
        self.register()
        user = User.objects.get(email="mother@example.com")
        code = user.email_verification_code

        response = self.client.post("/auth/verify-email/", {"email": user.email, "code": code})

        self.assertEqual(response.status_code, 200)
        self.assertIn("access", response.data)
        self.assertIn("refresh", response.data)
        self.assertEqual(response.data["user"]["email"], "mother@example.com")

        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertTrue(user.email_verified)
        self.assertEqual(user.email_verification_code, "")
        self.assertEqual(user.email_verification_attempts, 0)

        # And now login works.
        login = self.client.post("/auth/login/", {
            "email": "mother@example.com", "password": "correct-horse-battery-staple",
        })
        self.assertEqual(login.status_code, 200)

    def test_verify_with_wrong_code_increments_attempts_and_fails(self):
        self.register()
        user = User.objects.get(email="mother@example.com")

        response = self.client.post("/auth/verify-email/", {"email": user.email, "code": "000000"})

        self.assertEqual(response.status_code, 400)
        user.refresh_from_db()
        self.assertFalse(user.is_active)
        self.assertEqual(user.email_verification_attempts, 1)

    def test_verify_locks_out_after_max_attempts(self):
        self.register()
        user = User.objects.get(email="mother@example.com")

        for _ in range(MAX_VERIFICATION_ATTEMPTS):
            self.client.post("/auth/verify-email/", {"email": user.email, "code": "000000"})

        # The (MAX_VERIFICATION_ATTEMPTS + 1)th try is rejected on attempt
        # count alone, even with the real code -- can't be brute-forced
        # back in with a lucky guess after the cap is hit.
        response = self.client.post("/auth/verify-email/", {
            "email": user.email, "code": user.email_verification_code,
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("Too many", response.data["detail"])

    def test_verify_rejects_expired_code(self):
        self.register()
        user = User.objects.get(email="mother@example.com")
        user.email_verification_sent_at = timezone.now() - timedelta(minutes=VERIFICATION_CODE_TTL_MINUTES + 1)
        user.save(update_fields=["email_verification_sent_at"])

        response = self.client.post("/auth/verify-email/", {
            "email": user.email, "code": user.email_verification_code,
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("expired", response.data["detail"])

    def test_resend_respects_cooldown(self):
        self.register()
        user = User.objects.get(email="mother@example.com")
        first_code = user.email_verification_code

        # Immediately resending should be a no-op while inside the cooldown.
        self.client.post("/auth/resend-verification/", {"email": user.email})
        user.refresh_from_db()
        self.assertEqual(user.email_verification_code, first_code)
        self.assertEqual(len(mail.outbox), 1)  # only the original registration email

    def test_resend_after_cooldown_issues_a_new_code(self):
        self.register()
        user = User.objects.get(email="mother@example.com")
        first_code = user.email_verification_code
        user.email_verification_sent_at = timezone.now() - timedelta(seconds=RESEND_COOLDOWN_SECONDS + 1)
        user.save(update_fields=["email_verification_sent_at"])

        self.client.post("/auth/resend-verification/", {"email": user.email})

        user.refresh_from_db()
        self.assertNotEqual(user.email_verification_code, first_code)
        self.assertEqual(len(mail.outbox), 2)

    def test_resend_does_not_reveal_whether_an_email_exists(self):
        response = self.client.post("/auth/resend-verification/", {"email": "nobody@example.com"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 0)

    @patch("accounts.views.send_verification_email", side_effect=Exception("SMTP rejected: sender not verified"))
    def test_register_survives_an_email_sending_failure(self, mock_send):
        """
        Reproduces the real bug found while testing this against the
        live backend: a broken SendGrid config (bad credentials, an
        unverified sender identity, or just a slow/blocked connection)
        was crashing -- or in one case, simply hanging -- /auth/register/,
        even though the account had already been created. The email is
        now sent on a background thread specifically so a failure or a
        slow connection there can never affect this response at all;
        the account must still be created and the response must still
        be the normal success shape, immediately.
        """
        response = self.register()

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["email"], "mother@example.com")
        self.assertIn("Check your email", response.data["detail"])
        self.assertTrue(User.objects.filter(email="mother@example.com").exists())
        mock_send.assert_called_once()

    @patch("accounts.views.send_verification_email", side_effect=Exception("SMTP rejected: sender not verified"))
    def test_resend_survives_an_email_sending_failure(self, mock_send):
        user = User.objects.create_user(email="mother@example.com", password="x", is_active=False)

        response = self.client.post("/auth/resend-verification/", {"email": user.email})

        self.assertEqual(response.status_code, 200)
        mock_send.assert_called_once()


class ProfileAndConsentTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="rachel@example.com", password="password123", is_active=True)
        self.client.force_authenticate(user=self.user)

    def test_me_requires_authentication(self):
        self.client.force_authenticate(user=None)
        response = self.client.get("/auth/me/")
        self.assertEqual(response.status_code, 401)

    def test_me_returns_own_profile(self):
        response = self.client.get("/auth/me/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["email"], "rachel@example.com")

    def test_patch_me_updates_allowed_fields_only(self):
        response = self.client.patch("/auth/me/", {
            "mom_name": "Rachel G.",
            "is_staff": True,       # not in UpdateProfileSerializer -- must be silently ignored
            "email": "changed@example.com",  # same
        })
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.mom_name, "Rachel G.")
        self.assertFalse(self.user.is_staff)
        self.assertEqual(self.user.email, "rachel@example.com")

    def test_location_consent_requires_consent_true(self):
        response = self.client.post("/auth/location/", {
            "latitude": 14.6, "longitude": 121.0, "consent": False,
        })
        self.assertEqual(response.status_code, 400)
        self.user.refresh_from_db()
        self.assertFalse(self.user.location_consent_given)

    def test_location_consent_stores_coordinates_when_given(self):
        response = self.client.post("/auth/location/", {
            "latitude": 14.6, "longitude": 121.0, "consent": True,
        })
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.location_consent_given)
        self.assertAlmostEqual(self.user.latitude, 14.6)
        self.assertIsNotNone(self.user.location_consent_at)


class CheckInStreakTests(APITestCase):
    """
    CheckInView's three-way branch (no-op / +1 / reset to 1) is exactly
    the kind of off-by-one-prone logic worth pinning down with tests.
    """

    def setUp(self):
        self.user = User.objects.create_user(email="rachel@example.com", password="password123", is_active=True)
        self.client.force_authenticate(user=self.user)

    def test_first_ever_checkin_sets_streak_to_one(self):
        response = self.client.post("/auth/check-in/")
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.tracking_streaks, 1)
        self.assertEqual(self.user.last_active_date, timezone.localdate())

    def test_checking_in_twice_same_day_does_not_double_count(self):
        self.client.post("/auth/check-in/")
        self.client.post("/auth/check-in/")
        self.user.refresh_from_db()
        self.assertEqual(self.user.tracking_streaks, 1)

    def test_checking_in_next_day_increments_streak(self):
        self.user.tracking_streaks = 5
        self.user.last_active_date = timezone.localdate() - timedelta(days=1)
        self.user.save(update_fields=["tracking_streaks", "last_active_date"])

        self.client.post("/auth/check-in/")

        self.user.refresh_from_db()
        self.assertEqual(self.user.tracking_streaks, 6)

    def test_checking_in_after_a_gap_resets_to_one(self):
        self.user.tracking_streaks = 5
        self.user.last_active_date = timezone.localdate() - timedelta(days=3)
        self.user.save(update_fields=["tracking_streaks", "last_active_date"])

        self.client.post("/auth/check-in/")

        self.user.refresh_from_db()
        self.assertEqual(self.user.tracking_streaks, 1)


class FacilityStaffPermissionTests(APITestCase):
    """
    IsFacilityStaff gates the staff-only endpoints on the `role` field,
    not Django's own is_staff -- these two must never get conflated.
    """

    def setUp(self):
        self.mother = User.objects.create_user(email="mother@example.com", password="password123", is_active=True)
        self.staff = User.objects.create_user(
            email="staff@example.com", password="password123", is_active=True, role=User.Role.FACILITY_STAFF,
        )

    def test_mother_cannot_list_staff_user_management_endpoint(self):
        self.client.force_authenticate(user=self.mother)
        response = self.client.get("/auth/users/")
        self.assertEqual(response.status_code, 403)

    def test_facility_staff_can_list_users(self):
        self.client.force_authenticate(user=self.staff)
        response = self.client.get("/auth/users/")
        self.assertEqual(response.status_code, 200)

    def test_django_is_staff_alone_does_not_grant_facility_staff_access(self):
        # A platform admin (is_staff=True, is_superuser=True) but role
        # still "mother" -- these are two separate permission axes, and
        # is_staff must not accidentally satisfy the facility_staff gate.
        admin = User.objects.create_superuser(email="admin@example.com", password="password123")
        self.client.force_authenticate(user=admin)
        response = self.client.get("/auth/users/")
        self.assertEqual(response.status_code, 403)
