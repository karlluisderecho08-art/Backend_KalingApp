"""
Removing a facility with booking history from KalingApp, and restoring it
(milkbank/removal.py, FacilityRemovalView, FacilityRestoreView).
"""

import io
import os
from unittest.mock import patch

from django.core.management import call_command

from accounts.models import User
from accounts.tests import ThrottleIsolatedTestCase
from core.i18n import translate
from core.models import AuditLogEntry
from messaging.models import Conversation
from notifications.models import NotificationItem

from .allocation import eligibility_filters
from .models import Facility, MilkBankRequest
from .results import FACILITY_REMOVED_REASON, mother_result
from .tests import make_facility, make_request

Status = MilkBankRequest.Status
STAFF_PASSWORD = "A-long-staff-password-2026"


class FacilityRemovalTests(ThrottleIsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_superuser(email="admin@example.com", password="x")
        self.mother = User.objects.create_user(email="mother@example.com", password="x", is_active=True)
        self.facility = make_facility(name="St. Luke's Medical Center")
        self.other = make_facility(name="Philippine General Hospital")
        self.staff = User.objects.create_user(
            email="staff@example.com", password=STAFF_PASSWORD, is_active=True,
            role=User.Role.FACILITY_STAFF, facility=self.facility,
        )
        self.other_staff = User.objects.create_user(
            email="pgh-staff@example.com", password=STAFF_PASSWORD, is_active=True,
            role=User.Role.FACILITY_STAFF, facility=self.other,
        )
        self.open = {
            status: make_request(self.mother, self.facility, current_sub_status=status)
            for status in (Status.PENDING, Status.AWAITING_ATTENDANCE, Status.COUNTER_OFFERED, Status.SCHEDULED)
        }
        self.done = make_request(self.mother, self.facility, current_sub_status=Status.COMPLETED)
        self.elsewhere = make_request(self.mother, self.other, current_sub_status=Status.PENDING)

    def as_admin(self):
        self.client.force_authenticate(user=self.admin)

    def remove(self, facility=None):
        return self.client.post(f"/milkbank/facilities/{(facility or self.facility).id}/remove/")

    def restore(self, facility=None):
        return self.client.post(f"/milkbank/facilities/{(facility or self.facility).id}/restore/")

    def listed(self, query=""):
        return [f["name"] for f in self.client.get(f"/milkbank/facilities/{query}").data]

    # --- what the admin is told before confirming -------------------------

    def test_the_preview_counts_what_removing_would_do(self):
        self.as_admin()
        response = self.client.get(f"/milkbank/facilities/{self.facility.id}/remove/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {
            "bookings_on_record": 5, "open_bookings": 4, "staff_logins": 1, "removed": False,
        })

    # --- removing -------------------------------------------------------------

    def test_removing_takes_it_out_of_kalingapp_and_keeps_its_history(self):
        self.as_admin()
        response = self.remove()

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["cancelled_bookings"], 4)
        self.assertEqual(response.data["staff_logins_switched_off"], 1)
        self.assertIn("booking history is kept", response.data["detail"])

        self.facility.refresh_from_db()
        self.assertIsNotNone(self.facility.removed_at)
        self.assertFalse(self.facility.is_operational)
        # Every booking is still there -- nothing of hers was erased.
        self.assertEqual(MilkBankRequest.objects.filter(allocated_facility=self.facility).count(), 5)
        self.assertEqual(MilkBankRequest.objects.get(pk=self.done.pk).current_sub_status, Status.COMPLETED)
        self.assertTrue(AuditLogEntry.objects.filter(action="facility.removed", actor=self.admin).exists())

    def test_every_booking_in_progress_is_cancelled_and_its_mother_told(self):
        self.as_admin()
        self.remove()

        for status, req in self.open.items():
            with self.subTest(status=status):
                req.refresh_from_db()
                self.assertEqual(req.current_sub_status, Status.DECLINED)
                self.assertEqual(req.decline_reason, FACILITY_REMOVED_REASON)
                result = mother_result(req)
                self.assertEqual(result["title"], "Booking cancelled")
                self.assertFalse(result["health_followup"])
        notices = NotificationItem.objects.filter(owner=self.mother, description__startswith="Booking cancelled.")
        self.assertEqual(notices.count(), 4)
        self.assertNotEqual(translate(notices.first().description), notices.first().description)
        # Another facility's booking is untouched.
        self.assertEqual(MilkBankRequest.objects.get(pk=self.elsewhere.pk).current_sub_status, Status.PENDING)

    def test_its_staff_can_no_longer_sign_in_or_use_a_session(self):
        tokens = self.client.post("/auth/login/", {"email": "staff@example.com", "password": STAFF_PASSWORD}).data
        self.as_admin()
        self.remove()
        self.client.force_authenticate(user=None)

        self.assertEqual(
            self.client.post("/auth/login/", {"email": "staff@example.com", "password": STAFF_PASSWORD}).status_code,
            401,
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
        self.assertEqual(self.client.get("/auth/me/").status_code, 401)
        self.client.credentials()
        self.assertEqual(self.client.post("/auth/refresh/", {"refresh": tokens["refresh"]}).status_code, 401)
        # Another facility's staff are untouched.
        self.assertTrue(User.objects.get(pk=self.other_staff.pk).is_active)

    def test_it_leaves_every_list_and_booking(self):
        self.as_admin()
        self.remove()
        self.client.force_authenticate(user=None)

        self.assertEqual(self.listed(), ["Philippine General Hospital"])
        # Matching skips it even if it were switched back to operational.
        Facility.objects.filter(pk=self.facility.pk).update(is_operational=True)
        eligible = Facility.objects.filter(**eligibility_filters(MilkBankRequest.RequestType.DONOR))
        self.assertNotIn(self.facility, eligible)
        self.assertIn(self.other, eligible)

    def test_only_admins_see_the_removed_list(self):
        self.as_admin()
        self.remove()
        self.assertEqual(self.listed("?removed=1"), ["St. Luke's Medical Center"])
        removed_at = self.client.get("/milkbank/facilities/?removed=1").data[0]["removed_at"]
        self.assertTrue(removed_at)

        self.client.force_authenticate(user=self.mother)
        self.assertEqual(self.listed("?removed=1"), [])
        self.client.force_authenticate(user=None)
        self.assertEqual(self.listed("?removed=1"), [])

    def test_removing_twice_changes_nothing_more(self):
        self.as_admin()
        self.remove()
        again = self.remove()
        self.assertEqual(again.status_code, 200)
        self.assertEqual((again.data["cancelled_bookings"], again.data["staff_logins_switched_off"]), (0, 0))

    def test_only_admins_can_remove_or_restore(self):
        for account in (self.mother, self.staff, None):
            with self.subTest(account=account and account.email):
                self.client.force_authenticate(user=account)
                self.assertIn(self.remove().status_code, (401, 403))
                self.assertIn(self.restore().status_code, (401, 403))
                self.assertIn(self.client.get(f"/milkbank/facilities/{self.facility.id}/remove/").status_code,
                              (401, 403))
        self.facility.refresh_from_db()
        self.assertIsNone(self.facility.removed_at)

    def test_an_edit_cannot_set_or_clear_removal(self):
        self.as_admin()
        self.client.patch(f"/milkbank/facilities/{self.facility.id}/", {"removed_at": "2026-01-01T00:00:00Z"},
                          format="json")
        self.facility.refresh_from_db()
        self.assertIsNone(self.facility.removed_at)

    # --- restoring ------------------------------------------------------------

    def test_restoring_brings_it_back_not_operational_with_its_staff(self):
        self.as_admin()
        self.remove()
        response = self.restore()

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["staff_logins_switched_on"], 1)
        self.facility.refresh_from_db()
        self.assertIsNone(self.facility.removed_at)
        self.assertFalse(self.facility.is_operational)
        self.assertTrue(User.objects.get(pk=self.staff.pk).is_active)
        self.assertIn("St. Luke's Medical Center", self.listed())
        self.assertTrue(AuditLogEntry.objects.filter(action="facility.restored").exists())
        # Cancelled bookings stay cancelled.
        self.assertEqual(MilkBankRequest.objects.get(pk=self.open[Status.PENDING].pk).current_sub_status,
                         Status.DECLINED)

    # --- deleting outright ------------------------------------------------------

    def test_a_facility_with_bookings_still_cannot_be_erased(self):
        self.as_admin()
        response = self.client.delete(f"/milkbank/facilities/{self.facility.id}/")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "has_bookings")
        self.assertTrue(Facility.objects.filter(pk=self.facility.pk).exists())

    def test_a_facility_without_bookings_is_still_deleted_outright(self):
        empty = make_facility(name="Never Used")
        self.as_admin()
        self.assertEqual(self.client.get(f"/milkbank/facilities/{empty.id}/remove/").data["bookings_on_record"], 0)
        self.assertEqual(self.client.delete(f"/milkbank/facilities/{empty.id}/").status_code, 204)
        self.assertFalse(Facility.objects.filter(pk=empty.pk).exists())

    # --- elsewhere ----------------------------------------------------------------

    def test_a_deploy_does_not_create_a_staff_login_at_a_removed_facility(self):
        self.as_admin()
        self.remove()
        with patch.dict(os.environ, {f"FACILITY_STAFF_PASSWORD_{self.facility.id}": "A-fresh-staff-password-2026"}):
            out = io.StringIO()
            call_command("seed_facility_staff", stdout=out)
        self.assertFalse(User.objects.filter(email=f"staff{self.facility.id}@kalingapp.demo").exists())
        self.assertIn("was removed from KalingApp", out.getvalue())

    def test_admin_messages_keep_a_removed_facility_only_if_there_were_messages(self):
        self.as_admin()
        self.remove()
        names = [row["facility_name"] for row in self.client.get("/messaging/admin/conversations/").data]
        self.assertNotIn("St. Luke's Medical Center", names)
        self.assertNotIn("St. Luke's Medical Center (removed)", names)

        conversation = Conversation.objects.create(facility=self.facility)
        conversation.messages.create(side="facility", body="Hello", sender=self.staff)
        Conversation.objects.filter(pk=conversation.pk).update(last_message_at=conversation.messages.first().created_at)
        names = [row["facility_name"] for row in self.client.get("/messaging/admin/conversations/").data]
        self.assertIn("St. Luke's Medical Center (removed)", names)
