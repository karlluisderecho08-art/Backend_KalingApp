"""
What the mother is told about how her request ended (milkbank/results.py):
plain words, a nudge to see her doctor where the reason may be a health
concern, and never the staff's technical notes for those reasons.
"""

from django.test import SimpleTestCase
from rest_framework.test import APITestCase

from accounts.models import User
from notifications.models import NotificationItem

from .models import MilkBankRequest
from .results import DECLINE_RESULTS, mother_result
from .tests import make_facility, make_request

Status = MilkBankRequest.Status
LAB_NOTES = "E. coli 10^5 CFU/mL, total aerobic count above threshold"


def declined(reason, notes="", request_type=MilkBankRequest.RequestType.DONOR):
    staff_message = f"{reason} — {notes}" if notes else reason
    return MilkBankRequest(
        request_type=request_type, current_sub_status=Status.DECLINED,
        decline_reason=reason, staff_message=staff_message,
    )


class MotherResultTests(SimpleTestCase):
    def test_health_related_declines_suggest_a_doctor_and_hide_the_notes(self):
        for reason in ("Failed blood test", "Failed physical examination", "Failed breastmilk analysis"):
            with self.subTest(reason=reason):
                result = mother_result(declined(reason, LAB_NOTES))
                self.assertTrue(result["health_followup"])
                self.assertIn("doctor", result["message"])
                self.assertEqual(result["facility_note"], "")
                self.assertNotIn("CFU", result["title"] + result["message"])

    def test_practical_declines_pass_the_staff_note_through_without_its_label(self):
        result = mother_result(declined("Incomplete or invalid requirements", "Medical abstract was unsigned."))
        self.assertFalse(result["health_followup"])
        self.assertEqual(result["facility_note"], "Medical abstract was unsigned.")
        self.assertEqual(result["title"], "Requirements incomplete")

    def test_others_uses_the_staff_note_as_the_explanation(self):
        result = mother_result(declined("Others", "The facility is closed for renovation this month."))
        self.assertEqual(result["message"], "The facility is closed for renovation this month.")

    def test_others_without_a_note_gets_a_generic_message(self):
        result = mother_result(declined("Others"))
        self.assertIn("contact them", result["message"])

    def test_an_unknown_or_blank_reason_still_says_something(self):
        for reason in ("", "Something new"):
            with self.subTest(reason=reason):
                self.assertTrue(mother_result(declined(reason))["message"])

    def test_every_dashboard_reason_has_an_explanation(self):
        dashboard_reasons = [
            "Failed blood test", "Failed physical examination", "Did not pass counseling",
            "Failed breastmilk analysis", "Milk did not meet quality standards",
            "Incomplete or invalid requirements", "Not enough milk available", "Outdated Serological Test",
        ]
        for reason in dashboard_reasons:
            self.assertIn(reason.lower(), DECLINE_RESULTS, reason)

    def test_expired_says_whose_wait_ran_out(self):
        facility_side = MilkBankRequest(
            request_type="DONOR", current_sub_status=Status.EXPIRED, current_stage_index=0,
        )
        hers = MilkBankRequest(
            request_type="DONOR", current_sub_status=Status.EXPIRED,
            current_stage_index=MilkBankRequest.DONOR_STAGES.index("Booking Confirmation"),
        )
        self.assertIn("didn't respond", mother_result(facility_side)["message"])
        self.assertIn("attendance wasn't confirmed", mother_result(hers)["message"])

    def test_requests_still_in_progress_have_no_result(self):
        for status in (Status.PENDING, Status.SCHEDULED, Status.AWAITING_ATTENDANCE, Status.COUNTER_OFFERED):
            self.assertIsNone(mother_result(MilkBankRequest(request_type="DONOR", current_sub_status=status)))


class DeclineReachesTheMotherInPlainWordsTests(APITestCase):
    def setUp(self):
        self.facility = make_facility(name="St. Luke's")
        self.mother = User.objects.create_user(email="m@example.com", password=None, is_active=True)
        self.staff = User.objects.create_user(
            email="s@example.com", password=None, is_active=True,
            role=User.Role.FACILITY_STAFF, facility=self.facility,
        )
        self.req = make_request(self.mother, self.facility)

    def decline(self, reason, notes=""):
        self.client.force_authenticate(user=self.staff)
        message = f"{reason} — {notes}" if notes else reason
        return self.client.post(
            f"/milkbank/requests/{self.req.id}/decline/", {"reason": reason, "staff_message": message}, format="json",
        )

    def test_her_notification_explains_without_the_lab_notes(self):
        self.decline("Failed breastmilk analysis", LAB_NOTES)

        notice = NotificationItem.objects.filter(owner=self.mother).latest("id")
        self.assertIn("Milk sample not accepted", notice.description)
        self.assertIn("doctor", notice.description)
        self.assertNotIn("CFU", notice.description)

    def test_her_notification_carries_a_practical_note(self):
        self.decline("Outdated Serological Test", "Your test is from March.")

        notice = NotificationItem.objects.filter(owner=self.mother).latest("id")
        self.assertIn("A newer blood test is needed", notice.description)
        self.assertIn("Your test is from March.", notice.description)

    def test_her_request_history_carries_the_result(self):
        self.decline("Failed blood test", LAB_NOTES)
        self.client.force_authenticate(user=self.mother)

        data = self.client.get("/milkbank/requests/mine/").data
        rows = data["results"] if isinstance(data, dict) else data

        self.assertEqual(rows[0]["result"]["title"], "Not able to donate at this time")
        self.assertTrue(rows[0]["result"]["health_followup"])
        self.assertEqual(rows[0]["result"]["facility_note"], "")

    def test_staff_still_see_their_own_notes(self):
        self.decline("Failed blood test", LAB_NOTES)
        self.req.refresh_from_db()
        self.assertIn("CFU", self.req.staff_message)
