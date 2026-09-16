from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.hashers import check_password
from django.core import mail
from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from .emails import (
    MAX_VERIFICATION_ATTEMPTS,
    RESEND_COOLDOWN_SECONDS,
    VERIFICATION_CODE_TTL_MINUTES,
    send_verification_email,
)
from .models import PendingRegistration, User
from .serializers import RegisterSerializer


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


class ThrottleIsolatedTestCase(APITestCase):
    """
    Base class for anything that hits a rate-limited endpoint.

    Throttle counters live in the cache, not the database, so unlike
    database rows they are NOT rolled back between tests -- they
    accumulate across every test in the process. Without clearing them,
    a test's result depends on how many requests the tests before it
    happened to make, which fails in whatever order the suite happens
    to run in.
    """

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)


class RegistrationAndVerificationTests(ThrottleIsolatedTestCase):
    """
    Covers the account lifecycle a real mother goes through: register ->
    (inactive, code emailed) -> verify -> (active, logged in). This is
    the flow the Mother Android app's registration screen depends on --
    see the KalingApp Progress Report for the known client-side bug
    where the app still expects the OLD response shape from before
    email verification existed.
    """

    def setUp(self):
        super().setUp()
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

    def test_register_creates_no_account_only_a_pending_signup(self):
        """
        The central guarantee of this flow: signing up must not bring an
        account into existence. Until the right code comes back, there
        is nothing in the users table at all.
        """
        response = self.register()

        self.assertEqual(response.status_code, 201)
        # No tokens back directly -- just a confirmation and the email it
        # was sent to. Access/refresh only ever come from
        # /auth/verify-email/ now.
        self.assertNotIn("access", response.data)
        self.assertNotIn("user", response.data)
        self.assertEqual(response.data["email"], "mother@example.com")

        self.assertFalse(User.objects.filter(email="mother@example.com").exists())

        pending = PendingRegistration.objects.get(email="mother@example.com")
        self.assertEqual(len(pending.code), 6)
        self.assertTrue(pending.code.isdigit())

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(pending.code, mail.outbox[0].body)
        self.assertEqual(mail.outbox[0].to, ["mother@example.com"])

    def test_pending_registration_never_stores_a_plaintext_password(self):
        self.register()
        pending = PendingRegistration.objects.get(email="mother@example.com")

        self.assertNotEqual(pending.password, "correct-horse-battery-staple")
        self.assertNotIn("correct-horse-battery-staple", pending.password)
        # And it's the real hash, so the chosen password survives intact
        # through to the account created at verification time.
        self.assertTrue(check_password("correct-horse-battery-staple", pending.password))

    def test_only_the_email_send_generates_the_code(self):
        """
        Regression test. The code used to be generated twice: once when
        the pending row was created, then again by the send itself --
        so the code sitting in the database could be replaced moments
        after /auth/register/ returned, and which one she actually
        received was a race against a background thread.

        Caught only by exercising the real flow; the rest of this suite
        misses it because _SynchronousThread makes the send finish
        before any assertion runs, hiding the window entirely. Asserting
        the invariant directly (creating a signup issues no code at all)
        is what keeps it closed.
        """
        serializer = RegisterSerializer(data={
            "email": "mother@example.com",
            "password": "correct-horse-battery-staple",
            "mom_name": "Rachel",
            "baby_name": "James",
        })
        self.assertTrue(serializer.is_valid(), serializer.errors)
        pending = serializer.save()

        self.assertEqual(pending.code, "")
        self.assertIsNone(pending.sent_at)

        send_verification_email(pending)

        pending.refresh_from_db()
        self.assertEqual(len(pending.code), 6)
        self.assertIsNotNone(pending.sent_at)
        self.assertIn(pending.code, mail.outbox[0].body)

    def test_register_defaults_blank_baby_name_to_james(self):
        self.register(baby_name="")
        pending = PendingRegistration.objects.get(email="mother@example.com")
        self.assertEqual(pending.baby_name, "James")

    def test_register_rejects_an_email_that_already_has_an_account(self):
        User.objects.create_user(email="mother@example.com", password="x", is_active=True)

        response = self.register()

        self.assertEqual(response.status_code, 400)
        self.assertIn("email", response.data)

    def test_a_legacy_unverified_account_does_not_lock_the_address_out(self):
        """
        Signups made before this flow existed left inactive User rows
        that can never be verified now -- nothing reads them any more.
        Those addresses must still be registerable, or their owners are
        permanently locked out of their own email. The stale row is
        cleared when the replacement account is created.
        """
        User.objects.create_user(email="mother@example.com", password="x", is_active=False)

        response = self.register()
        self.assertEqual(response.status_code, 201)

        code = PendingRegistration.objects.get(email="mother@example.com").code
        verify = self.client.post("/auth/verify-email/", {"email": "mother@example.com", "code": code})

        self.assertEqual(verify.status_code, 200)
        # Exactly one account, and it's the new, usable one.
        self.assertEqual(User.objects.filter(email="mother@example.com").count(), 1)
        user = User.objects.get(email="mother@example.com")
        self.assertTrue(user.is_active)
        self.assertTrue(user.check_password("correct-horse-battery-staple"))

    def test_register_again_before_verifying_replaces_the_pending_signup(self):
        """
        An abandoned attempt (she lost the email, the app closed, she
        typo'd) must never lock an address out permanently. The second
        attempt replaces the first rather than being rejected as a
        duplicate -- and carries a fresh code, so the abandoned one
        can't still be used.
        """
        first_response = self.register()
        first_code = PendingRegistration.objects.get(email="mother@example.com").code

        second_response = self.register(mom_name="Rachel Retry")

        self.assertEqual(first_response.status_code, 201)
        self.assertEqual(second_response.status_code, 201)
        self.assertEqual(PendingRegistration.objects.filter(email="mother@example.com").count(), 1)

        pending = PendingRegistration.objects.get(email="mother@example.com")
        self.assertEqual(pending.mom_name, "Rachel Retry")
        self.assertNotEqual(pending.code, first_code)
        # Still no account -- retrying a signup doesn't create one either.
        self.assertFalse(User.objects.filter(email="mother@example.com").exists())

    def test_cannot_log_in_before_verifying(self):
        self.register()
        response = self.client.post("/auth/login/", {
            "email": "mother@example.com", "password": "correct-horse-battery-staple",
        })
        # There is no account to authenticate against at all yet, which
        # looks the same to the client as a wrong password.
        self.assertEqual(response.status_code, 401)

    def test_verify_with_correct_code_creates_the_account_and_returns_tokens(self):
        self.register()
        code = PendingRegistration.objects.get(email="mother@example.com").code

        response = self.client.post("/auth/verify-email/", {"email": "mother@example.com", "code": code})

        self.assertEqual(response.status_code, 200)
        self.assertIn("access", response.data)
        self.assertIn("refresh", response.data)
        self.assertEqual(response.data["user"]["email"], "mother@example.com")

        # The account exists only now, and the pending row is consumed.
        user = User.objects.get(email="mother@example.com")
        self.assertTrue(user.is_active)
        self.assertTrue(user.email_verified)
        self.assertEqual(user.mom_name, "Rachel")
        self.assertEqual(user.baby_name, "James")
        self.assertFalse(PendingRegistration.objects.filter(email="mother@example.com").exists())

        # And the password she chose at signup still works.
        login = self.client.post("/auth/login/", {
            "email": "mother@example.com", "password": "correct-horse-battery-staple",
        })
        self.assertEqual(login.status_code, 200)

    def test_verify_with_wrong_code_increments_attempts_and_creates_nothing(self):
        self.register()

        response = self.client.post("/auth/verify-email/", {"email": "mother@example.com", "code": "000000"})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(PendingRegistration.objects.get(email="mother@example.com").attempts, 1)
        self.assertFalse(User.objects.filter(email="mother@example.com").exists())

    def test_verify_locks_out_after_max_attempts(self):
        self.register()

        for _ in range(MAX_VERIFICATION_ATTEMPTS):
            self.client.post("/auth/verify-email/", {"email": "mother@example.com", "code": "000000"})

        # The (MAX_VERIFICATION_ATTEMPTS + 1)th try is rejected on attempt
        # count alone, even with the real code -- can't be brute-forced
        # back in with a lucky guess after the cap is hit.
        response = self.client.post("/auth/verify-email/", {
            "email": "mother@example.com",
            "code": PendingRegistration.objects.get(email="mother@example.com").code,
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("Too many", response.data["detail"])
        self.assertFalse(User.objects.filter(email="mother@example.com").exists())

    def test_verify_rejects_expired_code(self):
        self.register()
        pending = PendingRegistration.objects.get(email="mother@example.com")
        pending.sent_at = timezone.now() - timedelta(minutes=VERIFICATION_CODE_TTL_MINUTES + 1)
        pending.save(update_fields=["sent_at"])

        response = self.client.post("/auth/verify-email/", {
            "email": pending.email, "code": pending.code,
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("expired", response.data["detail"])
        self.assertFalse(User.objects.filter(email="mother@example.com").exists())

    def test_resend_respects_cooldown(self):
        self.register()
        pending = PendingRegistration.objects.get(email="mother@example.com")
        first_code = pending.code

        # Immediately resending should be a no-op while inside the cooldown.
        self.client.post("/auth/resend-verification/", {"email": pending.email})

        pending.refresh_from_db()
        self.assertEqual(pending.code, first_code)
        self.assertEqual(len(mail.outbox), 1)  # only the original registration email

    def test_resend_after_cooldown_issues_a_new_code(self):
        self.register()
        pending = PendingRegistration.objects.get(email="mother@example.com")
        first_code = pending.code
        pending.sent_at = timezone.now() - timedelta(seconds=RESEND_COOLDOWN_SECONDS + 1)
        pending.save(update_fields=["sent_at"])

        self.client.post("/auth/resend-verification/", {"email": pending.email})

        pending.refresh_from_db()
        self.assertNotEqual(pending.code, first_code)
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
        even though the signup had already been recorded. The email is
        now sent on a background thread specifically so a failure or a
        slow connection there can never affect this response at all;
        the pending signup must still be saved and the response must
        still be the normal success shape, immediately.
        """
        response = self.register()

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["email"], "mother@example.com")
        self.assertIn("Check your email", response.data["detail"])
        self.assertTrue(PendingRegistration.objects.filter(email="mother@example.com").exists())
        mock_send.assert_called_once()

    @patch("accounts.views.send_verification_email", side_effect=Exception("SMTP rejected: sender not verified"))
    def test_resend_survives_an_email_sending_failure(self, mock_send):
        self.register()
        mock_send.reset_mock()  # ignore the failed send from registration itself

        response = self.client.post("/auth/resend-verification/", {"email": "mother@example.com"})

        self.assertEqual(response.status_code, 200)
        # Attempted, and the failure was absorbed rather than surfacing.
        # Note this isn't blocked by the resend cooldown: the send during
        # registration failed, so sent_at was never set, and a first code
        # that never actually went out should be immediately retryable.
        mock_send.assert_called_once()


class AuthThrottlingTests(ThrottleIsolatedTestCase):
    """
    Nothing was rate limited before this: /auth/login/ accepted
    unlimited password guesses, and the 5-attempt cap on a verification
    code could be reset just by registering the same address again.

    Throttle state lives in the cache and persists between tests in the
    same process, so each test clears it first -- otherwise these pass
    or fail depending on what ran before them.
    """

    def test_throttling_survives_changing_proxy_hops(self):
        """
        Regression test for a limit that existed but never fired.

        DRF keys anonymous clients on the whole X-Forwarded-For chain,
        and behind this host's edge the proxy hops in that chain change
        between requests. Every request therefore landed in its own
        bucket and counted 1, so no number of attempts ever hit the
        limit -- confirmed in production, where 14 rapid logins produced
        14 separate counters.

        Nothing in the suite caught it because the test client sends no
        X-Forwarded-For at all, falling through to a stable REMOTE_ADDR.
        So this sends one: same client, different proxy path each time,
        exactly as the real edge does.
        """
        User.objects.create_user(email="mother@example.com", password="the-real-password", is_active=True)

        statuses = []
        for i in range(12):
            statuses.append(
                self.client.post(
                    "/auth/login/",
                    {"email": "mother@example.com", "password": f"guess-{i}"},
                    # Same originating client; the hops behind it churn.
                    HTTP_X_FORWARDED_FOR=f"203.0.113.7, 104.23.160.{i}, 10.28.132.{i}",
                ).status_code
            )

        self.assertIn(429, statuses)

    def test_login_stops_accepting_unlimited_password_guesses(self):
        User.objects.create_user(email="mother@example.com", password="the-real-password", is_active=True)

        statuses = [
            self.client.post(
                "/auth/login/", {"email": "mother@example.com", "password": f"guess-{i}"}
            ).status_code
            for i in range(12)
        ]

        self.assertIn(429, statuses)
        # And it's the throttle stopping it, not the wrong password --
        # the correct one is refused too once the limit is hit.
        blocked = self.client.post("/auth/login/", {"email": "mother@example.com", "password": "the-real-password"})
        self.assertEqual(blocked.status_code, 429)

    def test_registration_is_rate_limited(self):
        statuses = [
            self.client.post("/auth/register/", {
                "email": f"mother{i}@example.com",
                "password": "correct-horse-battery-staple",
                "mom_name": "Rachel",
                "baby_name": "James",
            }).status_code
            for i in range(8)
        ]

        # Each one of these would otherwise send a real email, so an
        # unthrottled endpoint was also a way to burn the provider's
        # daily quota and take signup down for genuine users.
        self.assertIn(429, statuses)


class DemoLoginTests(APITestCase):
    """
    /auth/demo-login/ hands out real tokens with no credentials at all.
    It must not be reachable unless deliberately switched on.
    """

    @override_settings(DEMO_LOGIN_ENABLED=False)
    def test_disabled_by_default_in_production_configuration(self):
        response = self.client.post("/auth/demo-login/")
        self.assertEqual(response.status_code, 404)

    @override_settings(DEMO_LOGIN_ENABLED=True)
    def test_enabled_explicitly_still_works_for_local_demos(self):
        User.objects.create_user(email="rachel@kalingapp.demo", password="x", is_active=True)

        response = self.client.post("/auth/demo-login/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("access", response.data)


class ForgotPasswordTests(ThrottleIsolatedTestCase):
    """
    Covers the "Forgot Password?" flow: request a code -> confirm code +
    new password -> logged in with the new password. Was a pure UI stub
    before this (ForgotPasswordScreen just flipped a local
    "checkSent = true" flag -- no request ever left the app), so there
    was no backend behavior at all to have previously tested.
    """

    def setUp(self):
        super().setUp()
        patcher = patch("accounts.views.threading.Thread", new=_SynchronousThread)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.user = User.objects.create_user(email="mother@example.com", password="old-password", is_active=True)

    def test_forgot_password_emails_a_code_for_an_active_account(self):
        response = self.client.post("/auth/forgot-password/", {"email": "mother@example.com"})

        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(len(self.user.password_reset_code), 6)
        self.assertTrue(self.user.password_reset_code.isdigit())
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(self.user.password_reset_code, mail.outbox[0].body)
        self.assertEqual(mail.outbox[0].to, ["mother@example.com"])

    def test_forgot_password_does_not_reveal_whether_an_email_exists(self):
        response = self.client.post("/auth/forgot-password/", {"email": "nobody@example.com"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 0)

    def test_forgot_password_ignores_a_never_verified_account(self):
        """
        An account that never finished email verification isn't a
        "forgot my password" case -- she needs verify-email/
        resend-verification instead, not a reset code for a password
        she can't even log in with yet.
        """
        User.objects.create_user(email="unverified@example.com", password="x", is_active=False)

        response = self.client.post("/auth/forgot-password/", {"email": "unverified@example.com"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 0)

    def test_reset_with_correct_code_changes_password_and_returns_tokens(self):
        self.client.post("/auth/forgot-password/", {"email": "mother@example.com"})
        code = User.objects.get(email="mother@example.com").password_reset_code

        response = self.client.post("/auth/reset-password/", {
            "email": "mother@example.com", "code": code, "new_password": "brand-new-password",
        })

        self.assertEqual(response.status_code, 200)
        self.assertIn("access", response.data)
        self.assertIn("refresh", response.data)

        self.user.refresh_from_db()
        self.assertEqual(self.user.password_reset_code, "")
        self.assertTrue(self.user.check_password("brand-new-password"))
        self.assertFalse(self.user.check_password("old-password"))

        # And now login works with the new password, not the old one.
        login = self.client.post("/auth/login/", {"email": "mother@example.com", "password": "brand-new-password"})
        self.assertEqual(login.status_code, 200)

    def test_reset_with_wrong_code_increments_attempts_and_fails(self):
        self.client.post("/auth/forgot-password/", {"email": "mother@example.com"})

        response = self.client.post("/auth/reset-password/", {
            "email": "mother@example.com", "code": "000000", "new_password": "brand-new-password",
        })

        self.assertEqual(response.status_code, 400)
        self.user.refresh_from_db()
        self.assertEqual(self.user.password_reset_attempts, 1)
        self.assertTrue(self.user.check_password("old-password"))  # unchanged

    def test_reset_locks_out_after_max_attempts(self):
        self.client.post("/auth/forgot-password/", {"email": "mother@example.com"})

        for _ in range(MAX_VERIFICATION_ATTEMPTS):
            self.client.post("/auth/reset-password/", {
                "email": "mother@example.com", "code": "000000", "new_password": "brand-new-password",
            })

        response = self.client.post("/auth/reset-password/", {
            "email": "mother@example.com",
            "code": User.objects.get(email="mother@example.com").password_reset_code,
            "new_password": "brand-new-password",
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("Too many", response.data["detail"])

    def test_reset_rejects_expired_code(self):
        self.client.post("/auth/forgot-password/", {"email": "mother@example.com"})
        self.user.refresh_from_db()
        self.user.password_reset_sent_at = timezone.now() - timedelta(minutes=VERIFICATION_CODE_TTL_MINUTES + 1)
        self.user.save(update_fields=["password_reset_sent_at"])

        response = self.client.post("/auth/reset-password/", {
            "email": "mother@example.com", "code": self.user.password_reset_code, "new_password": "brand-new-password",
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("expired", response.data["detail"])

    def test_reset_rejects_a_too_short_new_password(self):
        self.client.post("/auth/forgot-password/", {"email": "mother@example.com"})
        code = User.objects.get(email="mother@example.com").password_reset_code

        response = self.client.post("/auth/reset-password/", {
            "email": "mother@example.com", "code": code, "new_password": "short",
        })

        self.assertEqual(response.status_code, 400)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("old-password"))  # unchanged

    def test_resend_respects_cooldown(self):
        self.client.post("/auth/forgot-password/", {"email": "mother@example.com"})
        first_code = User.objects.get(email="mother@example.com").password_reset_code

        self.client.post("/auth/forgot-password/", {"email": "mother@example.com"})

        self.user.refresh_from_db()
        self.assertEqual(self.user.password_reset_code, first_code)
        self.assertEqual(len(mail.outbox), 1)

    @patch("accounts.views.send_password_reset_email", side_effect=Exception("SMTP rejected: sender not verified"))
    def test_forgot_password_survives_an_email_sending_failure(self, mock_send):
        response = self.client.post("/auth/forgot-password/", {"email": "mother@example.com"})

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
