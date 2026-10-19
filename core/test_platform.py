"""
Tests for the platform pieces in core/: the app-version check and gate,
the audit log and system status endpoints, and data retention.
"""

import io
from datetime import date, timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import PendingRegistration, User
from chat.models import ChatMessage, ChatSession
from milkbank.models import MilkBankRequest
from milkbank.tests import make_facility, make_request
from milkbank.transitions import apply_transition

from .models import AuditLogEntry
from .retention import maybe_purge, purge_expired_data
from .versioning import APP_VERSION_HEADER

Status = MilkBankRequest.Status


class AppVersionTests(APITestCase):
    @override_settings(
        APP_LATEST_VERSION_NAME="1.1.0", APP_LATEST_VERSION_CODE=2, APP_MIN_VERSION_CODE=2,
        APP_DOWNLOAD_URL="https://example.com/download",
    )
    def test_anyone_can_read_the_published_version(self):
        response = self.client.get("/app/version/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["latest_version_code"], 2)
        self.assertEqual(response.data["min_version_code"], 2)
        self.assertEqual(response.data["download_url"], "https://example.com/download")
        self.assertEqual(response.data["latest_version_name"], "1.1.0")


@override_settings(APP_MIN_VERSION_CODE=2, APP_DOWNLOAD_URL="https://example.com/download")
class AppVersionGateTests(APITestCase):
    """Below the minimum version, the mother-only writes answer 426 with where to update."""

    def setUp(self):
        self.mother = User.objects.create_user(
            email="gate@example.com", password="x", is_active=True, latitude=14.6, longitude=121.0,
        )
        self.client.force_authenticate(user=self.mother)
        self.facility = make_facility()
        self.booking = {"request_type": "DONOR", "preferred_date": "2026-12-15", "preferred_time": "10:00 AM"}

    def test_an_app_too_old_to_send_its_version_is_told_to_update(self):
        response = self.client.post("/milkbank/requests/", self.booking, format="json")

        self.assertEqual(response.status_code, 426)
        self.assertEqual(response.data["code"], "update_required")
        self.assertIn("https://example.com/download", response.data["detail"])
        self.assertFalse(MilkBankRequest.objects.exists())

    def test_an_older_version_is_told_to_update(self):
        response = self.client.post(
            "/milkbank/requests/", self.booking, format="json", headers={APP_VERSION_HEADER: "1"},
        )
        self.assertEqual(response.status_code, 426)

    def test_the_current_version_goes_through(self):
        response = self.client.post(
            "/milkbank/requests/", self.booking, format="json", headers={APP_VERSION_HEADER: "2"},
        )
        self.assertEqual(response.status_code, 201, response.data)

    def test_matching_kali_and_the_questionnaire_are_gated_too(self):
        req = make_request(self.mother, self.facility)
        self.assertEqual(self.client.post("/milkbank/allocate/", {"request_type": "DONOR"}, format="json").status_code, 426)
        self.assertEqual(self.client.post("/chat/message/", {"text": "hi"}, format="json").status_code, 426)
        self.assertEqual(
            self.client.post(f"/milkbank/requests/{req.id}/donor-questionnaire/", {}, format="multipart").status_code, 426,
        )

    def test_reads_are_never_gated(self):
        # The facility console reads a questionnaire without the header.
        staff = User.objects.create_user(
            email="gate-staff@example.com", password="x", is_active=True,
            role=User.Role.FACILITY_STAFF, facility=self.facility,
        )
        req = make_request(self.mother, self.facility)
        self.client.force_authenticate(user=staff)
        self.assertEqual(self.client.get(f"/milkbank/requests/{req.id}/donor-questionnaire/").status_code, 404)
        self.assertEqual(self.client.get("/milkbank/requests/all/").status_code, 200)

    def test_an_unreadable_header_counts_as_too_old(self):
        response = self.client.post(
            "/milkbank/requests/", self.booking, format="json", headers={APP_VERSION_HEADER: "latest"},
        )
        self.assertEqual(response.status_code, 426)


class AuditLogEndpointTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(email="audit-admin@example.com", password="password123")
        self.mother = User.objects.create_user(email="audit-mother@example.com", password="x", is_active=True)
        AuditLogEntry.objects.create(actor=self.admin, action="booking.accepted", target="MilkBankRequest:1")
        AuditLogEntry.objects.create(actor=None, action="booking.expired_sla_timeout", target="MilkBankRequest:2")
        AuditLogEntry.objects.create(actor=self.mother, action="comment.reported", target="ArticleComment:9 (Spam)")

    def test_only_platform_admins_can_read_it(self):
        self.client.force_authenticate(user=self.mother)
        self.assertEqual(self.client.get("/core/audit-log/").status_code, 403)
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get("/core/audit-log/").status_code, 401)

    def test_newest_first_with_who_did_it(self):
        self.client.force_authenticate(user=self.admin)
        data = self.client.get("/core/audit-log/").data

        self.assertEqual(data["count"], 3)
        self.assertEqual([r["action"] for r in data["results"]][0], "comment.reported")
        self.assertEqual(data["results"][0]["actor_email"], "audit-mother@example.com")
        self.assertIsNone(data["results"][1]["actor_email"])  # the system did it
        self.assertIn("booking.accepted", data["actions"])

    def test_filters_by_action_family_and_by_text(self):
        self.client.force_authenticate(user=self.admin)
        self.assertEqual(self.client.get("/core/audit-log/?action=booking.").data["count"], 2)
        self.assertEqual(self.client.get("/core/audit-log/?action=booking.accepted").data["count"], 1)
        self.assertEqual(self.client.get("/core/audit-log/?q=spam").data["count"], 1)
        self.assertEqual(self.client.get("/core/audit-log/?q=audit-admin").data["count"], 1)

    def test_pages_of_fifty(self):
        AuditLogEntry.objects.bulk_create(
            [AuditLogEntry(action="bulk.entry", target=str(i)) for i in range(60)]
        )
        self.client.force_authenticate(user=self.admin)
        first = self.client.get("/core/audit-log/").data
        second = self.client.get("/core/audit-log/?page=2").data
        self.assertEqual((len(first["results"]), len(second["results"])), (50, 13))

    def test_there_is_no_way_to_change_it(self):
        self.client.force_authenticate(user=self.admin)
        self.assertEqual(self.client.post("/core/audit-log/", {}).status_code, 405)
        self.assertEqual(self.client.delete("/core/audit-log/").status_code, 405)


class SystemStatusTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(email="status-admin@example.com", password="password123")

    def test_admin_only(self):
        mother = User.objects.create_user(email="status-mother@example.com", password="x", is_active=True)
        self.client.force_authenticate(user=mother)
        self.assertEqual(self.client.get("/core/system-status/").status_code, 403)

    @override_settings(MILKBANK_SWEEP_TOKEN="very-secret-token-value")
    def test_reports_configuration_without_revealing_secrets(self):
        self.client.force_authenticate(user=self.admin)
        response = self.client.get("/core/system-status/")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["expiry_pinger_token_set"])
        self.assertEqual(response.data["file_storage"]["kind"], "local")
        self.assertIn(response.data["email"]["kind"], ("console", "brevo_api", "smtp"))
        self.assertEqual(response.data["retention"]["representative_days"], 7)
        self.assertNotIn("very-secret-token-value", response.content.decode())


class RetentionTests(TestCase):
    def setUp(self):
        cache.clear()
        self.now = timezone.now()
        self.mother = User.objects.create_user(email="ret@example.com", password="x", is_active=True)
        self.facility = make_facility()

    def _ago(self, **kwargs):
        return self.now - timedelta(**kwargs)

    def test_unverified_sign_ups_go_after_24_hours_since_the_last_code(self):
        old = PendingRegistration.objects.create(email="old@example.com", password="h", mom_name="A", baby_name="B")
        still_resending = PendingRegistration.objects.create(
            email="resend@example.com", password="h", mom_name="A", baby_name="B",
        )
        fresh = PendingRegistration.objects.create(email="new@example.com", password="h", mom_name="A", baby_name="B")
        PendingRegistration.objects.filter(pk=old.pk).update(created_at=self._ago(hours=25), sent_at=self._ago(hours=25))
        PendingRegistration.objects.filter(pk=still_resending.pk).update(
            created_at=self._ago(hours=30), sent_at=self._ago(hours=1),
        )

        counts = purge_expired_data(now=self.now)

        self.assertEqual(counts["pending_signups"], 1)
        self.assertEqual(
            set(PendingRegistration.objects.values_list("email", flat=True)),
            {"resend@example.com", "new@example.com"},
        )
        self.assertTrue(PendingRegistration.objects.filter(pk=fresh.pk).exists())

    def _request_with_representative(self, closed_days_ago=None, **extra):
        req = make_request(
            self.mother, self.facility, request_type=MilkBankRequest.RequestType.RECIPIENT,
            needs_representative=True, representative_name="Ana Cruz",
            representative_birthday=date(1990, 1, 2), representative_contact_number="0917",
            **extra,
        )
        if closed_days_ago is not None:
            MilkBankRequest.objects.filter(pk=req.pk).update(
                current_sub_status=Status.COMPLETED, closed_at=self._ago(days=closed_days_ago),
            )
        req.refresh_from_db()
        return req

    def test_representative_details_go_7_days_after_the_request_closes(self):
        expired = self._request_with_representative(closed_days_ago=8)
        recent = self._request_with_representative(closed_days_ago=6)
        still_open = self._request_with_representative()

        counts = purge_expired_data(now=self.now)

        self.assertEqual(counts["representative_details"], 1)
        expired.refresh_from_db()
        self.assertEqual((expired.representative_name, expired.representative_contact_number), ("", ""))
        self.assertIsNone(expired.representative_birthday)
        self.assertIsNotNone(expired.representative_purged_at)
        self.assertTrue(expired.needs_representative)  # the fact she had one stays
        for req in (recent, still_open):
            req.refresh_from_db()
            self.assertEqual(req.representative_name, "Ana Cruz")
            self.assertIsNone(req.representative_purged_at)

    def test_a_request_without_a_representative_is_left_untouched(self):
        req = make_request(self.mother, self.facility)
        MilkBankRequest.objects.filter(pk=req.pk).update(current_sub_status=Status.DECLINED, closed_at=self._ago(days=30))

        self.assertEqual(purge_expired_data(now=self.now)["representative_details"], 0)
        req.refresh_from_db()
        self.assertIsNone(req.representative_purged_at)

    def test_purging_twice_changes_nothing_the_second_time(self):
        self._request_with_representative(closed_days_ago=8)
        purge_expired_data(now=self.now)
        self.assertEqual(purge_expired_data(now=self.now)["representative_details"], 0)

    def test_chat_messages_go_after_90_days(self):
        session = ChatSession.objects.create(owner=self.mother)
        old = ChatMessage.objects.create(session=session, text="old", is_user=True)
        ChatMessage.objects.create(session=session, text="new", is_user=True)
        ChatMessage.objects.filter(pk=old.pk).update(created_at=self._ago(days=91))

        self.assertEqual(purge_expired_data(now=self.now)["chat_messages"], 1)
        self.assertEqual(list(ChatMessage.objects.values_list("text", flat=True)), ["new"])

    def test_audit_entries_go_after_a_year_and_the_purge_is_recorded(self):
        old = AuditLogEntry.objects.create(action="booking.accepted", target="x")
        AuditLogEntry.objects.filter(pk=old.pk).update(created_at=self._ago(days=366))

        counts = purge_expired_data(now=self.now)

        self.assertEqual(counts["audit_entries"], 1)
        self.assertFalse(AuditLogEntry.objects.filter(pk=old.pk).exists())
        record = AuditLogEntry.objects.get(action="retention.purged")
        self.assertIn("audit_entries=1", record.target)

    def test_nothing_to_purge_writes_no_audit_entry(self):
        purge_expired_data(now=self.now)
        self.assertFalse(AuditLogEntry.objects.filter(action="retention.purged").exists())

    def test_ordinary_traffic_runs_it_at_most_once_an_hour(self):
        with patch("core.retention.purge_expired_data") as purge:
            maybe_purge()
            maybe_purge()
        self.assertEqual(purge.call_count, 1)

    def test_a_failing_purge_never_breaks_the_request_that_triggered_it(self):
        with patch("core.retention.purge_expired_data", side_effect=RuntimeError("db down")):
            maybe_purge()  # must not raise

    def test_the_deploy_command_runs_it(self):
        old = PendingRegistration.objects.create(email="cmd@example.com", password="h", mom_name="A", baby_name="B")
        PendingRegistration.objects.filter(pk=old.pk).update(created_at=self._ago(days=2), sent_at=None)

        out = io.StringIO()
        call_command("purge_expired_data", stdout=out)

        self.assertFalse(PendingRegistration.objects.filter(pk=old.pk).exists())
        self.assertIn("pending_signups=1", out.getvalue())


class ClosedAtTests(TestCase):
    def setUp(self):
        self.mother = User.objects.create_user(email="closed@example.com", password="x", is_active=True)
        self.facility = make_facility()

    def test_set_when_a_request_reaches_a_final_status(self):
        for final in (Status.DECLINED, Status.EXPIRED):
            req = make_request(self.mother, self.facility)
            apply_transition(req, final, None, "test")
            req.refresh_from_db()
            self.assertIsNotNone(req.closed_at, final)

    def test_not_set_while_it_is_still_open(self):
        req = make_request(self.mother, self.facility)
        apply_transition(req, Status.AWAITING_ATTENDANCE, None, "accepted")
        req.refresh_from_db()
        self.assertIsNone(req.closed_at)

    def test_completing_sets_it(self):
        req = make_request(self.mother, self.facility, current_sub_status=Status.SCHEDULED)
        apply_transition(req, Status.COMPLETED, None, "completed", amount_ml=100)
        req.refresh_from_db()
        self.assertIsNotNone(req.closed_at)
        self.assertIsNotNone(req.completed_at)


@override_settings(MILKBANK_SWEEP_TOKEN="pinger-token")
class PingerRunsRetentionTests(APITestCase):
    def test_the_expiry_pinger_also_purges(self):
        old = PendingRegistration.objects.create(email="ping@example.com", password="h", mom_name="A", baby_name="B")
        PendingRegistration.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - timedelta(days=2), sent_at=None,
        )
        response = self.client.post("/milkbank/sweep-expired/", headers={"X-Sweep-Token": "pinger-token"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["purged"]["pending_signups"], 1)
