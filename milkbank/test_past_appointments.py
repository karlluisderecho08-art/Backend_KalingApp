"""
Nothing is booked, accepted or agreed to for a time that is already gone
(scheduling.slot_has_started). A request still waiting for the facility when
its appointment hour passes can't be accepted as it stands: staff propose a
new date or decline it, and left alone it still expires on its own clock.

Every test holds "now" at a fixed moment, so none of them depends on the date
it happens to run on.
"""

from datetime import datetime
from unittest.mock import patch

from accounts.models import User
from accounts.tests import ThrottleIsolatedTestCase
from core.i18n import translate

from .business_hours import BUSINESS_TZ
from .models import MilkBankRequest
from .scheduling import appointment_label, slot_has_started
from .tests import make_facility, make_request, screen
from .transitions import sweep_expired_requests

Status = MilkBankRequest.Status
APP = {"HTTP_X_KALINGAPP_VERSION_CODE": "7"}


def manila(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=BUSINESS_TZ)


# Tuesday, November 17, 2026, half past noon (Manila).
NOON_THIRTY = manila(2026, 11, 17, 12, 30)


class SlotHasStartedTests(ThrottleIsolatedTestCase):
    def test_the_boundary_is_the_start_of_the_slot(self):
        self.assertFalse(slot_has_started("2026-11-17", "12:00 PM", now=manila(2026, 11, 17, 11, 59)))
        self.assertTrue(slot_has_started("2026-11-17", "12:00 PM", now=manila(2026, 11, 17, 12, 0)))
        self.assertTrue(slot_has_started("2026-11-16", "4:00 PM", now=NOON_THIRTY))
        self.assertFalse(slot_has_started("2026-11-18", "8:00 AM", now=NOON_THIRTY))

    def test_a_date_object_works_too(self):
        from datetime import date
        self.assertTrue(slot_has_started(date(2026, 11, 17), "9:00 AM", now=NOON_THIRTY))

    def test_what_cannot_be_read_is_never_refused(self):
        for day, time in (("2026-11-17", ""), ("2026-11-17", "noon"), ("", "9:00 AM"), (None, "9:00 AM"),
                          ("not-a-date", "9:00 AM")):
            with self.subTest(day=day, time=time):
                self.assertFalse(slot_has_started(day, time, now=NOON_THIRTY))

    def test_utc_now_is_read_on_the_manila_clock(self):
        # 04:30 UTC is 12:30 in Manila.
        from datetime import timezone as dt_timezone
        utc = datetime(2026, 11, 17, 4, 30, tzinfo=dt_timezone.utc)
        self.assertTrue(slot_has_started("2026-11-17", "12:00 PM", now=utc))
        self.assertFalse(slot_has_started("2026-11-17", "1:00 PM", now=utc))

    def test_the_label_reads_naturally(self):
        self.assertEqual(appointment_label("2026-11-17", "12:00 PM"), "November 17, 2026 at 12:00 PM")


class PastAppointmentTests(ThrottleIsolatedTestCase):
    def setUp(self):
        super().setUp()
        clock = patch("django.utils.timezone.now", return_value=NOON_THIRTY)
        clock.start()
        self.addCleanup(clock.stop)
        self.facility = make_facility(name="PGH")
        self.mother = User.objects.create_user(
            email="mother@example.com", password="x", is_active=True, latitude=14.6, longitude=121.0,
        )
        self.staff = User.objects.create_user(
            email="staff@example.com", password="x", is_active=True,
            role=User.Role.FACILITY_STAFF, facility=self.facility,
        )

    def pending(self, time="12:00 PM", day="2026-11-17", **extra):
        return make_request(self.mother, self.facility, preferred_date=day, preferred_time=time, **extra)

    def as_staff(self):
        self.client.force_authenticate(user=self.staff)

    def as_mother(self):
        self.client.force_authenticate(user=self.mother)

    # --- the facility ----------------------------------------------------------

    def test_staff_cannot_accept_once_her_appointment_time_has_passed(self):
        req = self.pending()
        self.as_staff()

        response = self.client.post(f"/milkbank/requests/{req.id}/accept/")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "appointment_passed")
        self.assertIn("November 17, 2026 at 12:00 PM", response.data["detail"])
        self.assertIn("Propose a new date", response.data["detail"])
        self.assertEqual(MilkBankRequest.objects.get(pk=req.pk).current_sub_status, Status.PENDING)

    def test_a_recipient_request_is_held_to_the_same_rule(self):
        req = self.pending(request_type=MilkBankRequest.RequestType.RECIPIENT)
        self.as_staff()
        self.assertEqual(self.client.post(f"/milkbank/requests/{req.id}/accept/").data["code"], "appointment_passed")

    def test_staff_can_still_accept_before_the_time(self):
        req = self.pending(time="1:00 PM")
        screen(req)
        self.as_staff()

        response = self.client.post(f"/milkbank/requests/{req.id}/accept/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(MilkBankRequest.objects.get(pk=req.pk).current_sub_status, Status.AWAITING_ATTENDANCE)

    def test_staff_can_still_propose_a_new_date_or_decline(self):
        offered = self.pending()
        declined = make_request(
            User.objects.create_user(email="other@example.com", password="x", is_active=True),
            self.facility, preferred_date="2026-11-17", preferred_time="9:00 AM",
        )
        self.as_staff()

        proposed = self.client.post(f"/milkbank/requests/{offered.id}/propose-counter-offer/", {
            "counter_offer_date": "2026-11-18", "counter_offer_time": "10:00 AM", "staff_message": "No doctor at noon",
        }, format="json")
        self.assertEqual(proposed.status_code, 200, proposed.data)
        self.assertEqual(MilkBankRequest.objects.get(pk=offered.pk).current_sub_status, Status.COUNTER_OFFERED)

        response = self.client.post(f"/milkbank/requests/{declined.id}/decline/", {"reason": "Others",
                                                                                   "staff_message": "Too late"})
        self.assertEqual(response.status_code, 200, response.data)

    def test_left_alone_it_still_expires_on_its_own_clock(self):
        req = self.pending()
        MilkBankRequest.objects.filter(pk=req.pk).update(response_deadline=manila(2026, 11, 17, 14))

        self.assertEqual(sweep_expired_requests(now=manila(2026, 11, 17, 13, 59)), [])
        self.assertEqual([r.pk for r in sweep_expired_requests(now=manila(2026, 11, 17, 14, 1))], [req.pk])

    # --- the mother ------------------------------------------------------------------

    def counter_offered(self, offer_time):
        return self.pending(
            time="9:00 AM", current_sub_status=Status.COUNTER_OFFERED,
            counter_offer_date="2026-11-17", counter_offer_time=offer_time,
        )

    def test_she_cannot_accept_an_offer_whose_time_has_passed(self):
        req = self.counter_offered("11:00 AM")
        self.as_mother()

        response = self.client.post(f"/milkbank/requests/{req.id}/accept-counter-offer/", **APP)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "offer_passed")
        self.assertEqual(MilkBankRequest.objects.get(pk=req.pk).current_sub_status, Status.COUNTER_OFFERED)

    def test_she_can_accept_an_offer_still_ahead(self):
        req = self.counter_offered("3:00 PM")
        self.as_mother()
        response = self.client.post(f"/milkbank/requests/{req.id}/accept-counter-offer/", **APP)
        self.assertEqual(response.status_code, 200, response.data)

    def test_choosing_a_new_time_must_be_a_time_still_ahead(self):
        req = self.counter_offered("11:00 AM")
        self.as_mother()
        url = f"/milkbank/requests/{req.id}/reject-counter-offer/"

        past = self.client.post(url, {"preferred_date": "2026-11-17", "preferred_time": "12:00 PM"}, format="json", **APP)
        self.assertEqual(past.status_code, 400)
        self.assertEqual(past.data["code"], "slot_passed")
        self.assertEqual(MilkBankRequest.objects.get(pk=req.pk).current_sub_status, Status.COUNTER_OFFERED)

        later = self.client.post(url, {"preferred_date": "2026-11-18", "preferred_time": "10:00 AM"}, format="json", **APP)
        self.assertEqual(later.status_code, 200, later.data)
        self.assertEqual(MilkBankRequest.objects.get(pk=req.pk).current_sub_status, Status.PENDING)

    def test_a_new_booking_cannot_be_for_a_time_already_gone(self):
        self.as_mother()
        booking = {"request_type": "RECIPIENT", "preferred_date": "2026-11-17", "preferred_time": "12:00 PM"}

        response = self.client.post("/milkbank/requests/", booking, format="json", **APP)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "slot_passed")
        self.assertFalse(MilkBankRequest.objects.filter(owner=self.mother).exists())

        booking["preferred_time"] = "1:00 PM"
        later = self.client.post("/milkbank/requests/", booking, format="json", **APP)
        self.assertNotEqual(later.data.get("code"), "slot_passed")

    def test_her_messages_come_in_tagalog_too(self):
        for text in (
            "The time the facility offered has passed. Please choose a new time instead.",
            "This time has already passed. Please choose a later time.",
        ):
            with self.subTest(text=text):
                self.assertNotEqual(translate(text), text)

        req = self.counter_offered("11:00 AM")
        self.as_mother()
        response = self.client.post(
            f"/milkbank/requests/{req.id}/accept-counter-offer/", HTTP_ACCEPT_LANGUAGE="tl", **APP,
        )
        self.assertEqual(response.json()["detail"], translate(
            "The time the facility offered has passed. Please choose a new time instead.",
        ))
