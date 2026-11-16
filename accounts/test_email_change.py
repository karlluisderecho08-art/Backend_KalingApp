"""
POST /auth/me/email/ and /auth/me/email/confirm/ -- an admin changing the
email they sign in with, keeping the same account.
"""

import io
import os
import re
from datetime import timedelta
from unittest.mock import patch

from django.core import mail
from django.core.management import call_command
from django.utils import timezone

from core.models import AuditLogEntry
from notifications.models import NotificationItem

from . import lockout
from .emails import MAX_VERIFICATION_ATTEMPTS, RESEND_COOLDOWN_SECONDS, VERIFICATION_CODE_TTL_MINUTES
from .models import User
from .tests import ThrottleIsolatedTestCase, _SynchronousThread

REQUEST_URL = "/auth/me/email/"
CONFIRM_URL = "/auth/me/email/confirm/"
PASSWORD = "the-admins-own-password-2026"


class EmailChangeTests(ThrottleIsolatedTestCase):
    def setUp(self):
        super().setUp()
        patcher = patch("accounts.views.threading.Thread", new=_SynchronousThread)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.admin = User.objects.create_superuser(
            email="old-admin@example.com", password=PASSWORD, role=User.Role.FACILITY_STAFF,
        )
        self.client.force_authenticate(user=self.admin)

    # --- helpers -----------------------------------------------------------

    def ask(self, new_email="new-admin@example.com", password=PASSWORD, **extra):
        return self.client.post(REQUEST_URL, {"new_email": new_email, "password": password, **extra}, format="json")

    def confirm(self, code):
        return self.client.post(CONFIRM_URL, {"code": code}, format="json")

    def emailed_code(self, to="new-admin@example.com"):
        sent = [m for m in mail.outbox if m.to == [to]]
        self.assertTrue(sent, f"nothing was emailed to {to}")
        found = re.search(r"\b(\d{6})\b", sent[-1].body)
        self.assertIsNotNone(found, "no code in the email")
        return found.group(1)

    def sign_in(self, email, password=PASSWORD):
        self.client.force_authenticate(user=None)
        response = self.client.post("/auth/login/", {"email": email, "password": password}, format="json")
        self.client.force_authenticate(user=self.admin)
        return response

    def allow_another_code(self):
        """Moves the last send out of the one-a-minute resend window."""
        User.objects.filter(pk=self.admin.pk).update(
            pending_email_sent_at=timezone.now() - timedelta(seconds=RESEND_COOLDOWN_SECONDS + 1),
        )

    # --- the change itself ---------------------------------------------------

    def test_the_whole_change_keeps_the_same_account(self):
        old_session = self.sign_in("old-admin@example.com").data
        NotificationItem.objects.create(
            owner=self.admin, title="Kept", description="Still attached afterwards.",
            category=NotificationItem.Category.SECURITY,
        )

        response = self.ask()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["pending_email"], "new-admin@example.com")
        self.assertEqual(response.data["code_minutes"], VERIFICATION_CODE_TTL_MINUTES)
        # Nothing changes before the code comes back.
        self.assertEqual(User.objects.get(pk=self.admin.pk).email, "old-admin@example.com")

        response = self.confirm(self.emailed_code())
        self.assertEqual(response.status_code, 200, response.data)

        admin = User.objects.get(pk=self.admin.pk)
        self.assertEqual(admin.email, "new-admin@example.com")
        self.assertTrue(admin.email_verified)
        self.assertEqual((admin.pending_email, admin.pending_email_code, admin.pending_email_sent_at),
                         ("", "", None))
        self.assertEqual(response.data["user"]["email"], "new-admin@example.com")
        self.assertEqual(User.objects.count(), 1)
        self.assertTrue(NotificationItem.objects.filter(owner=admin, title="Kept").exists())

        # The old address is told, and the audit log has both addresses.
        notice = [m for m in mail.outbox if m.to == ["old-admin@example.com"]]
        self.assertEqual(len(notice), 1)
        self.assertIn("new-admin@example.com", notice[0].body)
        self.assertTrue(AuditLogEntry.objects.filter(
            action="account.email_changed", actor=admin,
            target=f"User:{admin.id} from old-admin@example.com to new-admin@example.com",
        ).exists())

        # Every earlier session is over; the pair handed back carries this one on.
        self.assertEqual(self.client.post("/auth/refresh/", {"refresh": old_session["refresh"]}).status_code, 401)
        self.assertEqual(self.client.post("/auth/refresh/", {"refresh": response.data["refresh"]}).status_code, 200)

        # It signs in with the new address only, with the same password.
        self.assertEqual(self.sign_in("new-admin@example.com").status_code, 200)
        self.assertEqual(self.sign_in("old-admin@example.com").status_code, 401)

    def test_the_address_is_trimmed_and_lower_cased(self):
        self.assertEqual(self.ask("  New-Admin@Example.COM ").status_code, 200)
        self.assertEqual(User.objects.get(pk=self.admin.pk).pending_email, "new-admin@example.com")

    def test_codes_and_pending_addresses_never_leave_the_server(self):
        self.ask()
        me = self.client.get("/auth/me/").data
        listed = next(u for u in self.client.get("/auth/admin/users/").data if u["id"] == self.admin.id)
        for shape in (me, listed):
            self.assertFalse([key for key in shape if key.startswith("pending_email")])

    def test_a_password_reset_code_sent_to_the_old_address_stops_working(self):
        User.objects.filter(pk=self.admin.pk).update(
            password_reset_code="135790", password_reset_sent_at=timezone.now(), password_reset_attempts=0,
        )
        self.ask()
        self.confirm(self.emailed_code())

        response = self.client.post("/auth/reset-password/", {
            "email": "new-admin@example.com", "code": "135790", "new_password": "an-entirely-new-password-2026",
        })
        self.assertEqual(response.status_code, 400)
        self.assertTrue(User.objects.get(pk=self.admin.pk).check_password(PASSWORD))

    # --- step 1: what it refuses -------------------------------------------

    def test_only_admins_can_change_their_email(self):
        mother = User.objects.create_user(email="mother@example.com", password=PASSWORD, is_active=True)
        staff = User.objects.create_user(
            email="staff@example.com", password=PASSWORD, is_active=True, role=User.Role.FACILITY_STAFF,
        )
        for account in (mother, staff):
            with self.subTest(account=account.email):
                self.client.force_authenticate(user=account)
                self.assertEqual(self.ask().status_code, 403)
                self.assertEqual(self.confirm("123456").status_code, 403)
        self.client.force_authenticate(user=None)
        self.assertEqual(self.ask().status_code, 401)

    def test_a_bad_or_unchanged_address_is_refused_before_the_password_is_checked(self):
        for address in ("", "   ", "not-an-email", None, ["a@example.com"], "OLD-ADMIN@example.com"):
            with self.subTest(address=address):
                response = self.ask(address, password="a-wrong-password")
                self.assertEqual(response.status_code, 400)
                self.assertIn("new_email", response.data)
        self.assertEqual(User.objects.get(pk=self.admin.pk).failed_login_attempts, 0)
        self.assertEqual(mail.outbox, [])

    def test_a_wrong_password_sends_nothing_and_counts_toward_the_lock(self):
        response = self.ask(password="a-wrong-password")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "wrong_password")
        self.assertEqual(mail.outbox, [])
        self.assertEqual(User.objects.get(pk=self.admin.pk).pending_email, "")

        for _ in range(lockout.LOCK_THRESHOLD - 1):
            self.ask(password="a-wrong-password")
        # Locked now: even the right password is refused, as at sign-in.
        self.assertEqual(self.ask().status_code, 423)
        self.assertEqual(mail.outbox, [])

    def test_a_stranger_locking_the_account_does_not_block_a_trusted_browser(self):
        device = self.sign_in("old-admin@example.com").data["device_token"]
        for _ in range(lockout.LOCK_THRESHOLD):
            self.sign_in("old-admin@example.com", "a-strangers-guess")
        self.assertTrue(lockout.is_locked(User.objects.get(pk=self.admin.pk)))

        self.assertEqual(self.ask().status_code, 423)
        self.assertEqual(self.ask(device_token=device).status_code, 200)

    def test_one_code_a_minute(self):
        self.assertEqual(self.ask().status_code, 200)
        first = self.emailed_code()

        response = self.ask("another-address@example.com")
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.data["code"], "code_recently_sent")
        self.assertGreater(response.data["retry_after_seconds"], 0)
        self.assertEqual(User.objects.get(pk=self.admin.pk).pending_email, "new-admin@example.com")

        # Asking again later replaces the address and the code.
        self.allow_another_code()
        self.assertEqual(self.ask("another-address@example.com").status_code, 200)
        second = self.emailed_code("another-address@example.com")
        if first != second:
            self.assertEqual(self.confirm(first).data["code"], "wrong_code")
        self.assertEqual(self.confirm(second).status_code, 200)
        self.assertEqual(User.objects.get(pk=self.admin.pk).email, "another-address@example.com")

    # --- an address that's already in use ----------------------------------

    def test_an_address_in_use_gets_the_same_answer_and_can_never_be_confirmed(self):
        other = User.objects.create_user(email="someone@example.com", password="x", is_active=True)

        free = self.ask().data
        self.allow_another_code()
        taken = self.client.post(REQUEST_URL, {"new_email": "someone@example.com", "password": PASSWORD},
                                 format="json")
        self.assertEqual(taken.status_code, 200)
        self.assertEqual(set(taken.data), set(free))
        self.assertEqual(taken.data["detail"], free["detail"].replace("new-admin@", "someone@"))

        # Its inbox hears why no code came -- and gets no code.
        sent = [m for m in mail.outbox if m.to == ["someone@example.com"]]
        self.assertEqual(len(sent), 1)
        self.assertIn("already has a KalingApp account", sent[0].body)
        self.assertIsNone(re.search(r"\b\d{6}\b", sent[0].body))
        # No code exists for it at all, so none can ever be guessed.
        self.assertEqual(User.objects.get(pk=self.admin.pk).pending_email_code, "")

        self.assertEqual(self.confirm("000000").data["code"], "wrong_code")
        self.assertEqual(User.objects.get(pk=self.admin.pk).email, "old-admin@example.com")
        self.assertEqual(User.objects.get(pk=other.pk).email, "someone@example.com")

    def test_an_address_taken_after_the_code_was_sent_is_refused(self):
        self.ask()
        code = self.emailed_code()
        # In other capitals, which the database's own unique rule wouldn't catch.
        User.objects.create_user(email="New-Admin@Example.com", password="x", is_active=True)

        response = self.confirm(code)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "email_in_use")
        admin = User.objects.get(pk=self.admin.pk)
        self.assertEqual(admin.email, "old-admin@example.com")
        self.assertEqual(admin.pending_email, "")

    def test_an_abandoned_old_style_signup_gives_the_address_up(self):
        leftover = User.objects.create_user(email="new-admin@example.com", password="x", is_active=False)
        self.ask()
        self.assertEqual(self.confirm(self.emailed_code()).status_code, 200)
        self.assertFalse(User.objects.filter(pk=leftover.pk).exists())
        self.assertEqual(User.objects.get(pk=self.admin.pk).email, "new-admin@example.com")

    # --- step 2: the code ----------------------------------------------------

    def test_nothing_to_confirm(self):
        self.assertEqual(self.confirm("123456").data["code"], "no_pending_change")

    def test_an_expired_code(self):
        self.ask()
        code = self.emailed_code()
        User.objects.filter(pk=self.admin.pk).update(
            pending_email_sent_at=timezone.now() - timedelta(minutes=VERIFICATION_CODE_TTL_MINUTES, seconds=1),
        )
        self.assertEqual(self.confirm(code).data["code"], "code_expired")
        self.assertEqual(User.objects.get(pk=self.admin.pk).email, "old-admin@example.com")

    def test_five_wrong_codes_use_it_up(self):
        self.ask()
        code = self.emailed_code()
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(MAX_VERIFICATION_ATTEMPTS):
            self.assertEqual(self.confirm(wrong).data["code"], "wrong_code")

        self.assertEqual(self.confirm(code).data["code"], "too_many_attempts")
        self.assertEqual(User.objects.get(pk=self.admin.pk).email, "old-admin@example.com")

    def test_odd_codes_are_just_wrong(self):
        self.ask()
        for code in ("", "12345", "1234567", "١٢٣٤٥٦", None):
            with self.subTest(code=code):
                User.objects.filter(pk=self.admin.pk).update(pending_email_attempts=0)
                self.assertEqual(self.confirm(code).data["code"], "wrong_code")

    # --- Render's ADMIN_EMAIL ------------------------------------------------

    def test_the_answer_says_when_admin_email_needs_updating(self):
        for setting, outdated in (("old-admin@example.com", True), ("someone-else@example.com", False)):
            with self.subTest(setting=setting), patch.dict(os.environ, {"ADMIN_EMAIL": setting}):
                User.objects.filter(pk=self.admin.pk).update(email="old-admin@example.com")
                self.admin.refresh_from_db()
                self.allow_another_code()
                self.ask()
                response = self.confirm(self.emailed_code())
                self.assertEqual(response.status_code, 200, response.data)
                self.assertIs(response.data["admin_email_setting_outdated"], outdated)

    def test_a_deploy_does_not_recreate_an_admin_under_the_old_address(self):
        self.ask()
        self.confirm(self.emailed_code())

        out = io.StringIO()
        with patch.dict(os.environ, {"ADMIN_EMAIL": "Old-Admin@example.com", "ADMIN_PASSWORD": "whatever-2026"}):
            call_command("ensure_admin", stdout=out)

        self.assertFalse(User.objects.filter(email__iexact="old-admin@example.com").exists())
        self.assertEqual(User.objects.count(), 1)
        self.assertIn("no admin was created", out.getvalue())

    def test_a_deploy_still_creates_an_admin_at_a_never_used_address(self):
        self.ask()
        self.confirm(self.emailed_code())

        with patch.dict(os.environ, {"ADMIN_EMAIL": "brand-new-admin@example.com",
                                     "ADMIN_PASSWORD": "a-strong-admin-password-2026"}):
            call_command("ensure_admin", stdout=io.StringIO())

        created = User.objects.get(email="brand-new-admin@example.com")
        self.assertTrue(created.is_superuser)
        # An address that only shares the start of the old one isn't caught.
        with patch.dict(os.environ, {"ADMIN_EMAIL": "old-admin@example.co", "ADMIN_PASSWORD": "a-strong-2026-pass"}):
            call_command("ensure_admin", stdout=io.StringIO())
        self.assertTrue(User.objects.filter(email="old-admin@example.co").exists())
