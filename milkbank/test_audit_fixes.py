"""
The logic audit of 2026-11-17 (ROADMAP C16): what a mother, facility staff or
an admin could find wrong, each fixed and pinned here.

  1  closed days (weekends, holidays, a facility's closed days, real slot times)
  2  a mother cancels her own booking
  3  a counter-offer has a clock, staff can decline or replace it, and it holds
     the offered slot rather than her original one
  4  attendance must be confirmed before the appointment begins
  6  a donor's missing screening answers: shown, and no acceptance without them
  7  the steps notification says what can still be changed
  8  an admin deleting a mother tells her facility; completed bookings are
     kept, anonymised (owner decision)
  9  closing days with bookings on them: confirmed first, everyone told;
     staff close their own days (owner decision)
  10 "Missed the appointment" as a decline reason
  11 completing only from the Results step
  12 the dashboard counts months on the Manila calendar
"""

from datetime import date, datetime, timedelta
from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APITestCase

from accounts.models import User
from notifications.models import NotificationItem

from .allocation import claim_slot
from .business_hours import BUSINESS_TZ, is_business_day, philippine_holidays
from .capacity import slots_taken
from .collection import STEPS, STEPS_CONFIRMED, STEPS_TITLE
from .models import DonorQuestionnaire, Facility, MilkBankRequest
from .results import mother_result
from .scheduling import booking_slot_problem, counter_offer_errors, holiday_dates
from .tests import _first_weekday_after, make_facility, make_request, screen
from .transitions import apply_transition, attendance_deadline, counter_offer_deadline, sweep_expired_requests

Status = MilkBankRequest.Status
Method = MilkBankRequest.CollectionMethod
DONOR = MilkBankRequest.RequestType.DONOR
RECIPIENT = MilkBankRequest.RequestType.RECIPIENT


def manila(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=BUSINESS_TZ)


def today():
    return datetime.now(BUSINESS_TZ).date()


def next_weekday_holiday():
    """The next Philippine holiday that falls Mon-Fri, at least two days ahead."""
    start = today() + timedelta(days=2)
    days = sorted(d for year in (start.year, start.year + 1) for d in philippine_holidays(year))
    return next(d for d in days if d >= start and d.weekday() < 5)


def next_saturday():
    day = today() + timedelta(days=2)
    while day.weekday() != 5:
        day += timedelta(days=1)
    return day


def staff_for(facility, email="staff@example.com"):
    return User.objects.create_user(
        email=email, password="x", is_active=True, role=User.Role.FACILITY_STAFF, facility=facility,
    )


class BookingDaysTests(APITestCase):
    """1 -- nothing can be booked on a day the milk banks are closed, or at a time that isn't a slot."""

    def setUp(self):
        self.mother = User.objects.create_user(
            email="mother@example.com", password="x", is_active=True, latitude=14.6, longitude=121.0,
        )
        self.near = make_facility(name="Near", latitude=14.6, longitude=121.0)
        self.far = make_facility(name="Far", latitude=15.6, longitude=121.0)
        self.client.force_authenticate(user=self.mother)

    def book(self, day, time="10:00 AM", kind="DONOR"):
        body = {"request_type": kind, "preferred_date": str(day), "preferred_time": time}
        if kind == "RECIPIENT":
            body["requested_ml"] = 100
        return self.client.post("/milkbank/requests/", body)

    def test_a_weekend_is_refused(self):
        response = self.book(next_saturday())
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "day_closed")
        self.assertIn("weekends", response.data["detail"])
        self.assertFalse(MilkBankRequest.objects.exists())

    def test_a_holiday_is_refused_by_name_of_date(self):
        holiday = next_weekday_holiday()
        response = self.book(holiday)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "day_closed")
        self.assertEqual(response.data["closed_date"], holiday.isoformat())
        self.assertIn("is a holiday", response.data["detail"])
        self.assertIn(f"{holiday:%B} {holiday.day}, {holiday.year}", response.data["detail"])

    def test_christmas_is_refused(self):
        christmas = date(today().year, 12, 25)
        if christmas <= today():
            christmas = date(today().year + 1, 12, 25)
        self.assertEqual(self.book(christmas).status_code, 400)

    def test_a_time_that_is_not_a_slot_is_refused(self):
        day = _first_weekday_after(10)
        for time in ("7:00 PM", "5:00 PM", "banana", ""):
            response = self.book(day, time)
            self.assertEqual(response.status_code, 400, time)
        self.assertFalse(MilkBankRequest.objects.exists())

    def test_the_nearest_facility_closed_that_day_sends_her_to_the_next(self):
        day = _first_weekday_after(10)
        self.near.unavailable_donor_dates = [day]
        self.near.save()
        response = self.book(day)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["allocated_facility"], self.far.id)

    def test_a_day_closed_for_donors_only_still_takes_a_recipient(self):
        day = _first_weekday_after(10)
        self.near.unavailable_donor_dates = [day]
        self.near.save()
        response = self.book(day, kind="RECIPIENT")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["allocated_facility"], self.near.id)

    def test_every_facility_closed_that_day_is_a_409_she_can_act_on(self):
        day = _first_weekday_after(10)
        for facility in (self.near, self.far):
            facility.unavailable_donor_dates = [day]
            facility.save()
        response = self.book(day)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "day_closed")
        self.assertIn("No milk bank is open", response.data["detail"])

    def test_the_last_second_fallback_never_lands_on_a_closed_day(self):
        # The day is closed between ranking and claiming the slot.
        day = date.fromisoformat(_first_weekday_after(10))
        self.far.unavailable_donor_dates = [day.isoformat()]
        self.far.save()
        from django.db import transaction

        with transaction.atomic():
            self.assertFalse(claim_slot(self.far, "DONOR", day, time="10:00 AM"))
            self.assertTrue(claim_slot(self.near, "DONOR", day, time="10:00 AM"))

    def test_choosing_a_new_time_is_checked_the_same_way_at_her_own_facility(self):
        req = make_request(self.mother, self.near, current_sub_status=Status.COUNTER_OFFERED,
                           counter_offer_date=_first_weekday_after(12), counter_offer_time="9:00 AM")
        closed = _first_weekday_after(14)
        self.near.unavailable_donor_dates = [closed]
        self.near.save()
        url = f"/milkbank/requests/{req.id}/reject-counter-offer/"
        for day, time in ((next_saturday(), "10:00 AM"), (next_weekday_holiday(), "10:00 AM"),
                          (closed, "10:00 AM"), (_first_weekday_after(16), "6:00 PM")):
            response = self.client.post(url, {"preferred_date": str(day), "preferred_time": time})
            self.assertEqual(response.status_code, 400, (day, time))
        req.refresh_from_db()
        self.assertEqual(req.current_sub_status, Status.COUNTER_OFFERED)

    def test_staff_cannot_offer_a_holiday(self):
        req = make_request(self.mother, self.near)
        errors = counter_offer_errors(req, next_weekday_holiday(), "10:00 AM")
        self.assertIn("holiday", errors["counter_offer_date"])

    def test_facilities_publish_the_holidays_for_the_calendars(self):
        data = self.client.get(f"/milkbank/facilities/{self.near.id}/").data
        self.assertIn(next_weekday_holiday().isoformat(), data["holiday_dates"])
        self.assertEqual(data["holiday_dates"], holiday_dates())
        self.assertTrue(all(d >= today().isoformat() for d in data["holiday_dates"]))

    def test_the_slot_check_itself(self):
        open_day = date.fromisoformat(_first_weekday_after(10))
        self.assertIsNone(booking_slot_problem(open_day, "4:00 PM"))
        self.assertEqual(booking_slot_problem(open_day, "4:30 PM")[2], "invalid_time")
        self.assertEqual(booking_slot_problem(today() - timedelta(days=3), "10:00 AM")[2], "slot_passed")
        self.assertEqual(booking_slot_problem(next_saturday(), "10:00 AM")[2], "day_closed")


class MotherCancelTests(APITestCase):
    """2 -- she can cancel her own booking, as the app's FAQ says."""

    def setUp(self):
        self.mother = User.objects.create_user(email="mother@example.com", password="x", is_active=True,
                                               mom_name="Rachel")
        self.facility = make_facility()
        self.staff = staff_for(self.facility)
        self.client.force_authenticate(user=self.mother)

    def cancel(self, req):
        return self.client.post(f"/milkbank/requests/{req.id}/cancel/")

    def test_from_every_open_status(self):
        for status in (Status.PENDING, Status.AWAITING_ATTENDANCE, Status.SCHEDULED, Status.COUNTER_OFFERED):
            with self.subTest(status=status):
                MilkBankRequest.objects.all().delete()
                extra = {"counter_offer_date": _first_weekday_after(70), "counter_offer_time": "9:00 AM"} \
                    if status == Status.COUNTER_OFFERED else {}
                stage = 2 if status == Status.SCHEDULED else 1
                req = make_request(self.mother, self.facility, current_sub_status=status,
                                   current_stage_index=stage, **extra)
                response = self.cancel(req)
                self.assertEqual(response.status_code, 200, response.data)
                self.assertEqual(response.data["current_sub_status"], Status.CANCELLED)
                self.assertEqual(response.data["result"]["title"], "Booking cancelled")
                self.assertFalse(response.data["can_cancel"])

    def test_frees_the_slot_tells_the_staff_and_lets_her_book_again(self):
        req = make_request(self.mother, self.facility)
        self.assertEqual(slots_taken(self.facility.pk, req.preferred_date), 1)
        self.cancel(req)
        self.assertEqual(slots_taken(self.facility.pk, req.preferred_date), 0)
        notice = NotificationItem.objects.get(owner=self.staff)
        self.assertEqual(notice.title, "Booking Cancelled")
        self.assertIn("Rachel cancelled her donation booking", notice.description)
        mine = NotificationItem.objects.get(owner=self.mother)
        self.assertEqual(mine.title, "Milk Bank Request: Cancelled")
        req.refresh_from_db()
        self.assertEqual(req.closed_from, Status.PENDING)
        self.assertIsNotNone(req.closed_at)

    def test_a_donor_whose_milk_was_collected_cannot(self):
        req = make_request(self.mother, self.facility, current_sub_status=Status.SCHEDULED, current_stage_index=4)
        self.assertEqual(req.stages[4], "Results")
        response = self.cancel(req)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "milk_collected")

    def test_a_recipient_at_her_pickup_still_can(self):
        req = make_request(self.mother, self.facility, request_type=RECIPIENT,
                           current_sub_status=Status.SCHEDULED, current_stage_index=3)
        self.assertEqual(self.cancel(req).status_code, 200)

    def test_not_someone_elses_and_not_once_ended(self):
        other = User.objects.create_user(email="other@example.com", password="x", is_active=True)
        theirs = make_request(other, self.facility)
        self.assertIn(self.cancel(theirs).status_code, (403, 404))
        ended = make_request(self.mother, self.facility, current_sub_status=Status.DECLINED)
        self.assertEqual(self.cancel(ended).status_code, 400)


class CounterOfferClockTests(APITestCase):
    """3 -- an unanswered counter-offer ends, staff can act on it, and it holds the offered slot."""

    def setUp(self):
        self.mother = User.objects.create_user(email="mother@example.com", password="x", is_active=True)
        self.facility = make_facility(capacity=1)
        self.staff = staff_for(self.facility)
        self.client.force_authenticate(user=self.staff)
        self.req = make_request(self.mother, self.facility)
        screen(self.req)
        self.offer_day = _first_weekday_after(20)

    def propose(self, day=None, time="9:00 AM"):
        return self.client.post(f"/milkbank/requests/{self.req.id}/propose-counter-offer/", {
            "counter_offer_date": day or self.offer_day, "counter_offer_time": time, "staff_message": "No doctor.",
        })

    def test_proposing_starts_a_clock_and_tells_her_until_when(self):
        self.assertEqual(self.propose().status_code, 200)
        self.req.refresh_from_db()
        self.assertIsNotNone(self.req.response_deadline)
        text = NotificationItem.objects.filter(owner=self.mother).latest("id").description
        self.assertIn("Please accept it or choose a new time in Booking Status before", text)

    def test_the_clock_never_runs_past_the_offered_time(self):
        # Proposed Wednesday Dec 2, 2026 at 4 PM: 8 business hours run to Thursday 3 PM.
        req = MilkBankRequest(counter_offer_date=date(2026, 12, 3), counter_offer_time="9:00 AM")
        self.assertEqual(counter_offer_deadline(req, now=manila(2026, 12, 2, 16)), manila(2026, 12, 3, 9))
        later = MilkBankRequest(counter_offer_date=date(2026, 12, 10), counter_offer_time="9:00 AM")
        self.assertEqual(counter_offer_deadline(later, now=manila(2026, 12, 2, 16)), manila(2026, 12, 3, 15))

    def test_unanswered_it_expires_and_says_why(self):
        self.propose()
        self.req.refresh_from_db()
        expired = sweep_expired_requests(now=self.req.response_deadline + timedelta(seconds=1))
        self.assertEqual([r.id for r in expired], [self.req.id])
        self.req.refresh_from_db()
        self.assertEqual(self.req.current_sub_status, Status.EXPIRED)
        self.assertEqual(self.req.closed_from, Status.COUNTER_OFFERED)
        self.assertIn("proposed date wasn't answered", mother_result(self.req)["message"])
        self.assertEqual(slots_taken(self.facility.pk, date.fromisoformat(self.offer_day)), 0)

    def test_staff_can_decline_it(self):
        self.propose()
        response = self.client.post(f"/milkbank/requests/{self.req.id}/decline/",
                                    {"reason": "Others", "staff_message": "Others — she asked us to."})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["current_sub_status"], Status.DECLINED)

    def test_staff_can_replace_it_with_another_date(self):
        self.propose()
        second = _first_weekday_after(24)
        response = self.propose(day=second, time="1:00 PM")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["counter_offer_date"], second)
        # ...but not with the very same slot again.
        self.assertEqual(self.propose(day=second, time="1:00 PM").status_code, 400)

    def test_it_holds_the_offered_day_not_her_original_one(self):
        original = self.req.preferred_date
        self.propose()
        self.assertEqual(slots_taken(self.facility.pk, original), 0)
        self.assertEqual(slots_taken(self.facility.pk, date.fromisoformat(self.offer_day)), 1)
        # Capacity 1: the offered day is now full for everyone else.
        data = self.client.get(f"/milkbank/facilities/{self.facility.id}/").data
        self.assertIn(self.offer_day, data["fully_booked_dates"])
        self.assertNotIn(str(original), data["fully_booked_dates"])


class AttendanceDeadlineTests(APITestCase):
    """4 -- attendance must be confirmed before the appointment begins."""

    def setUp(self):
        self.mother = User.objects.create_user(email="mother@example.com", password="x", is_active=True)
        self.facility = make_facility()
        self.client.force_authenticate(user=self.mother)

    def test_friday_afternoon_for_monday_morning_ends_at_the_appointment(self):
        # Friday Dec 4, 2026 at 3 PM; appointment Monday Dec 7 at 8 AM. Eight
        # business hours would run to Monday 2 PM -- six hours after it began.
        req = MilkBankRequest(request_type=DONOR, preferred_date=date(2026, 12, 7), preferred_time="8:00 AM")
        self.assertEqual(attendance_deadline(req, now=manila(2026, 12, 4, 15)), manila(2026, 12, 7, 8))

    def test_far_ahead_the_eight_business_hours_still_apply(self):
        req = MilkBankRequest(request_type=DONOR, preferred_date=date(2026, 12, 18), preferred_time="8:00 AM")
        self.assertEqual(attendance_deadline(req, now=manila(2026, 12, 4, 15)), manila(2026, 12, 7, 14))

    def test_she_cannot_confirm_once_it_has_begun(self):
        yesterday = today() - timedelta(days=1)
        req = make_request(self.mother, self.facility, current_sub_status=Status.AWAITING_ATTENDANCE,
                           current_stage_index=1, preferred_date=yesterday)
        response = self.client.post(f"/milkbank/requests/{req.id}/confirm-attendance/")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "appointment_passed")
        req.refresh_from_db()
        self.assertEqual(req.current_sub_status, Status.AWAITING_ATTENDANCE)

    def test_the_acceptance_notice_names_the_deadline(self):
        staff = staff_for(self.facility)
        req = make_request(self.mother, self.facility)
        screen(req)
        self.client.force_authenticate(user=staff)
        self.assertEqual(self.client.post(f"/milkbank/requests/{req.id}/accept/").status_code, 200)
        text = NotificationItem.objects.filter(owner=self.mother).latest("id").description
        self.assertIn("Please confirm your attendance in Booking Status before", text)
        self.assertIn("or the request expires", text)


class ScreeningAnswersTests(APITestCase):
    """6 -- a donor's screening answers that never arrived."""

    def setUp(self):
        self.mother = User.objects.create_user(email="mother@example.com", password="x", is_active=True)
        self.facility = make_facility()
        self.staff = staff_for(self.facility)

    def test_the_booking_says_whether_they_arrived(self):
        req = make_request(self.mother, self.facility)
        self.client.force_authenticate(user=self.mother)
        self.assertIs(self.client.get(f"/milkbank/requests/{req.id}/").data["has_questionnaire"], False)
        screen(req)
        self.assertIs(self.client.get(f"/milkbank/requests/{req.id}/").data["has_questionnaire"], True)
        recipient = make_request(self.mother, self.facility, request_type=RECIPIENT, current_sub_status=Status.DECLINED)
        self.assertIsNone(self.client.get(f"/milkbank/requests/{recipient.id}/").data["has_questionnaire"])

    def test_staff_cannot_accept_a_donor_without_them(self):
        req = make_request(self.mother, self.facility)
        self.client.force_authenticate(user=self.staff)
        response = self.client.post(f"/milkbank/requests/{req.id}/accept/")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "screening_missing")
        # Sent again from Booking Status -- now it can be.
        screen(req)
        self.assertEqual(self.client.post(f"/milkbank/requests/{req.id}/accept/").status_code, 200)

    def test_the_decline_reason_for_it_tells_her_what_to_do(self):
        req = MilkBankRequest(current_sub_status=Status.DECLINED, decline_reason="Screening answers missing")
        self.assertIn("answer the screening questions again", mother_result(req)["message"])


class StepsNotificationTests(TestCase):
    """7 -- the steps notification never promises a change the server refuses."""

    def setUp(self):
        self.mother = User.objects.create_user(email="mother@example.com", password="x", is_active=True)
        self.facility = make_facility()

    def test_at_confirmation_it_says_to_message_the_facility(self):
        for method in (Method.PICKUP, Method.WALK_IN):
            with self.subTest(method=method):
                NotificationItem.objects.all().delete()
                req = make_request(self.mother, self.facility, collection_method=method,
                                   current_sub_status=Status.AWAITING_ATTENDANCE, current_stage_index=1)
                apply_transition(req, Status.SCHEDULED, self.mother, "attendance_confirmed")
                body = NotificationItem.objects.get(owner=self.mother, title=STEPS_TITLE).description
                self.assertIn("message the facility in Chat", body)
                self.assertNotIn("You can change this", body)
                req.delete()

    def test_before_confirmation_it_says_until_when_it_can_change(self):
        for method in (Method.PICKUP, Method.WALK_IN):
            self.assertIn("until your booking is confirmed", STEPS[method])
            self.assertNotIn("until your milk is collected", STEPS[method])
            self.assertNotIn("You can change this", STEPS_CONFIRMED[method])


class AccountDeletionTests(APITestCase):
    """8 -- an admin deleting a mother tells her facility; completed bookings stay, anonymised."""

    def setUp(self):
        self.admin = User.objects.create_user(email="admin@example.com", password="x", is_active=True, is_staff=True)
        self.mother = User.objects.create_user(email="mother@example.com", password="the-password",
                                               is_active=True, mom_name="Rachel")
        self.facility = make_facility()
        self.staff = staff_for(self.facility)

    def test_admin_delete_tells_the_staff_about_an_open_booking(self):
        make_request(self.mother, self.facility)
        self.client.force_authenticate(user=self.admin)
        self.assertEqual(self.client.delete(f"/auth/admin/users/{self.mother.id}/").status_code, 204)
        notice = NotificationItem.objects.get(owner=self.staff)
        self.assertEqual(notice.title, "Booking Cancelled")
        self.assertIn("a KalingApp admin deleted the mother's account", notice.description)
        self.assertNotIn("Rachel", notice.description)
        self.assertFalse(MilkBankRequest.objects.exists())

    def test_completed_bookings_stay_without_her(self):
        done = [
            make_request(self.mother, self.facility, current_sub_status=Status.COMPLETED, amount_ml=amount,
                         collection_method=Method.PICKUP, pickup_address="12 Rizal St.", staff_message="Rachel came")
            for amount in (150, 200)
        ]
        screen(done[0])
        declined = make_request(self.mother, self.facility, current_sub_status=Status.DECLINED)
        self.client.force_authenticate(user=self.admin)
        self.client.delete(f"/auth/admin/users/{self.mother.id}/")

        self.assertFalse(User.objects.filter(email="mother@example.com").exists())
        self.assertFalse(MilkBankRequest.objects.filter(pk=declined.pk).exists())
        kept = list(MilkBankRequest.objects.order_by("id"))
        self.assertEqual([r.id for r in kept], [r.id for r in done])
        refs = {r.former_owner_ref for r in kept}
        self.assertEqual(len(refs), 1)
        self.assertRegex(refs.pop(), r"^M-[A-Z2-9]{6}$")
        for req in kept:
            self.assertIsNone(req.owner_id)
            self.assertEqual((req.pickup_address, req.staff_message), ("", ""))
            self.assertEqual(req.current_sub_status, Status.COMPLETED)
        self.assertEqual(sorted(r.amount_ml for r in kept), [150, 200])
        self.assertFalse(DonorQuestionnaire.objects.exists())

        # The facility still sees them, as a deleted account.
        self.client.force_authenticate(user=self.staff)
        rows = self.client.get("/milkbank/requests/all/").data
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["owner_deleted"] and r["owner_email"] == "" and r["owner_name"] == "" for r in rows))

    def test_self_delete_keeps_them_too(self):
        make_request(self.mother, self.facility, current_sub_status=Status.COMPLETED, amount_ml=100)
        make_request(self.mother, self.facility)  # open: cancelled, staff told
        self.client.force_authenticate(user=self.mother)
        response = self.client.post("/auth/me/delete/", {"password": "the-password"})
        self.assertEqual(response.status_code, 204)
        self.assertEqual(MilkBankRequest.objects.count(), 1)
        self.assertIn("the mother deleted her KalingApp account",
                      NotificationItem.objects.get(owner=self.staff).description)

    def test_the_dashboard_still_counts_her_once(self):
        make_request(self.mother, self.facility, current_sub_status=Status.COMPLETED, amount_ml=100)
        make_request(self.mother, self.facility, current_sub_status=Status.COMPLETED, amount_ml=100)
        other = User.objects.create_user(email="other@example.com", password="x", is_active=True)
        make_request(other, self.facility, current_sub_status=Status.COMPLETED, amount_ml=100)
        self.client.force_authenticate(user=self.admin)
        self.client.delete(f"/auth/admin/users/{self.mother.id}/")
        self.assertEqual(self.client.get("/dashboard/stats/").data["total_donors"], 2)


class ClosingDaysTests(APITestCase):
    """9 -- closing a day that has bookings: shown first, then everyone is told."""

    def setUp(self):
        self.facility = make_facility(name="PGH")
        self.staff = staff_for(self.facility)
        self.other_staff = staff_for(self.facility, email="staff2@example.com")
        self.mother = User.objects.create_user(email="mother@example.com", password="x", is_active=True,
                                               mom_name="Rachel")
        self.day = _first_weekday_after(15)
        self.req = make_request(self.mother, self.facility, preferred_date=self.day)
        screen(self.req)
        self.url = "/milkbank/facilities/mine/closed-days/"
        self.client.force_authenticate(user=self.staff)

    def close(self, **extra):
        return self.client.post(self.url, {"start": self.day, "close_for": "both", **extra}, format="json")

    def test_staff_are_shown_the_bookings_first(self):
        response = self.close()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "confirm_closing")
        self.assertEqual([b["id"] for b in response.data["affected_bookings"]], [self.req.id])
        self.assertEqual(response.data["affected_bookings"][0]["mother"], "Rachel")
        self.facility.refresh_from_db()
        self.assertEqual(self.facility.unavailable_donor_dates, [])
        self.assertFalse(NotificationItem.objects.exists())

    def test_confirmed_the_day_closes_and_everyone_is_told(self):
        response = self.close(confirm_closing=True)
        self.assertEqual(response.status_code, 200, response.data)
        self.facility.refresh_from_db()
        self.assertEqual(self.facility.unavailable_donor_dates, [self.day])
        self.assertEqual(self.facility.unavailable_recipient_dates, [self.day])
        self.assertEqual([b["id"] for b in response.data["bookings_on_closed_days"]], [self.req.id])
        mine = NotificationItem.objects.get(owner=self.mother)
        self.assertEqual(mine.title, "Facility closed on your appointment day")
        self.assertIn("PGH will be closed on", mine.description)
        self.assertTrue(NotificationItem.objects.filter(owner=self.other_staff).exists())
        self.assertFalse(NotificationItem.objects.filter(owner=self.staff).exists())  # she did it herself

    def test_on_a_closed_day_nobody_can_go_ahead_until_a_new_date_is_offered(self):
        self.close(confirm_closing=True)
        detail = self.client.get(f"/milkbank/requests/{self.req.id}/").data
        self.assertTrue(detail["appointment_day_closed"])
        response = self.client.post(f"/milkbank/requests/{self.req.id}/accept/")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "day_closed")
        # A new date can be proposed, and once she accepts it the flag is gone.
        new_day = _first_weekday_after(17)
        self.assertEqual(self.client.post(f"/milkbank/requests/{self.req.id}/propose-counter-offer/", {
            "counter_offer_date": new_day, "counter_offer_time": "9:00 AM",
        }).status_code, 200)
        self.client.force_authenticate(user=self.mother)
        response = self.client.post(f"/milkbank/requests/{self.req.id}/accept-counter-offer/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(response.data["appointment_day_closed"])

    def test_she_cannot_confirm_on_a_closed_day(self):
        self.req.current_sub_status = Status.AWAITING_ATTENDANCE
        self.req.current_stage_index = 1
        self.req.save()
        self.close(confirm_closing=True)
        self.client.force_authenticate(user=self.mother)
        response = self.client.post(f"/milkbank/requests/{self.req.id}/confirm-attendance/")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "day_closed")

    def test_she_cannot_accept_an_offer_whose_day_has_closed_since(self):
        offered = _first_weekday_after(18)
        self.req.current_sub_status = Status.COUNTER_OFFERED
        self.req.counter_offer_date = offered
        self.req.counter_offer_time = "9:00 AM"
        self.req.save()
        self.client.post(self.url, {"start": offered, "close_for": "donors", "confirm_closing": True}, format="json")
        self.client.force_authenticate(user=self.mother)
        response = self.client.post(f"/milkbank/requests/{self.req.id}/accept-counter-offer/")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "offer_unavailable")

    def test_a_confirmed_appointment_can_be_moved_and_reconfirmed(self):
        self.req.current_sub_status = Status.SCHEDULED
        self.req.current_stage_index = 2  # Counseling and Testing
        self.req.attendance_confirmed = True
        self.req.save()
        self.close(confirm_closing=True)
        new_day = _first_weekday_after(17)
        response = self.client.post(f"/milkbank/requests/{self.req.id}/propose-counter-offer/", {
            "counter_offer_date": new_day, "counter_offer_time": "9:00 AM",
        })
        self.assertEqual(response.status_code, 200, response.data)
        self.client.force_authenticate(user=self.mother)
        data = self.client.post(f"/milkbank/requests/{self.req.id}/accept-counter-offer/").data
        self.assertEqual(data["current_sub_status"], Status.AWAITING_ATTENDANCE)
        self.assertEqual(data["stages"][data["current_stage_index"]], "Booking Confirmation")
        self.assertFalse(data["attendance_confirmed"])
        self.assertEqual(data["preferred_date"], new_day)

    def test_once_seen_a_confirmed_appointment_cannot_be_moved(self):
        self.req.current_sub_status = Status.SCHEDULED
        self.req.current_stage_index = 3  # Breastmilk Collection: Counseling and Testing is done
        self.req.save()
        response = self.client.post(f"/milkbank/requests/{self.req.id}/propose-counter-offer/", {
            "counter_offer_date": _first_weekday_after(17), "counter_offer_time": "9:00 AM",
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "appointment_done")

    def test_reopening_and_ranges(self):
        end = _first_weekday_after(25)
        response = self.client.post(self.url, {"start": _first_weekday_after(20), "end": end,
                                               "close_for": "recipients"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        closed = response.data["unavailable_recipient_dates"]
        self.assertTrue(closed and all(is_business_day(date.fromisoformat(d)) for d in closed))
        self.assertEqual(response.data["unavailable_donor_dates"], [])
        response = self.client.post(self.url, {"start": _first_weekday_after(20), "end": end, "close_for": "none"},
                                    format="json")
        self.assertEqual(response.data["unavailable_recipient_dates"], [])

    def test_bad_input_and_other_facilities(self):
        for body in ({"start": self.day, "close_for": "everyone"}, {"start": "soon", "close_for": "both"},
                     {"start": str(today() - timedelta(days=1)), "close_for": "both"},
                     {"start": self.day, "end": str(today()), "close_for": "both"}):
            self.assertEqual(self.client.post(self.url, body, format="json").status_code, 400, body)
        self.client.force_authenticate(user=self.mother)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_an_admin_edit_asks_too_and_tells_everyone(self):
        admin = User.objects.create_user(email="admin@example.com", password="x", is_active=True, is_staff=True)
        self.client.force_authenticate(user=admin)
        url = f"/milkbank/facilities/{self.facility.id}/"
        response = self.client.patch(url, {"unavailable_donor_dates": [self.day]}, format="json")
        self.assertEqual(response.status_code, 409)
        response = self.client.patch(url, {"unavailable_donor_dates": [self.day], "confirm_closing": True},
                                     format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(NotificationItem.objects.filter(owner=self.mother).exists())
        self.assertIn("KalingApp admin closed PGH",
                      NotificationItem.objects.filter(owner=self.staff).latest("id").description)

    def test_an_admin_switching_it_off_is_asked_and_the_staff_are_told(self):
        admin = User.objects.create_user(email="admin@example.com", password="x", is_active=True, is_staff=True)
        self.client.force_authenticate(user=admin)
        url = f"/milkbank/facilities/{self.facility.id}/"
        response = self.client.patch(url, {"is_operational": False}, format="json")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["open_bookings_when_switched_off"], 1)
        self.assertEqual(self.client.patch(url, {"is_operational": False, "confirm_closing": True},
                                           format="json").status_code, 200)
        self.assertIn("not operational", NotificationItem.objects.filter(owner=self.staff).latest("id").description)
        self.assertFalse(NotificationItem.objects.filter(owner=self.mother).exists())  # her booking carries on

    def test_closed_days_must_be_dates(self):
        admin = User.objects.create_user(email="admin@example.com", password="x", is_active=True, is_staff=True)
        self.client.force_authenticate(user=admin)
        response = self.client.patch(f"/milkbank/facilities/{self.facility.id}/",
                                     {"unavailable_donor_dates": ["next tuesday"]}, format="json")
        self.assertEqual(response.status_code, 400)


class MissedAppointmentTests(TestCase):
    """10 -- a no-show has its own reason, and a plain explanation."""

    def test_reason_reads_plainly_and_passes_the_note_on(self):
        req = MilkBankRequest(current_sub_status=Status.DECLINED, decline_reason="Missed the appointment",
                              staff_message="Missed the appointment — we waited until 11 AM.")
        result = mother_result(req)
        self.assertEqual(result["title"], "Missed appointment")
        self.assertFalse(result["health_followup"])
        self.assertEqual(result["facility_note"], "we waited until 11 AM.")


class CompletionStageTests(APITestCase):
    """11 -- a booking can only be completed from its Results step."""

    def setUp(self):
        self.mother = User.objects.create_user(email="mother@example.com", password="x", is_active=True)
        self.facility = make_facility()
        self.client.force_authenticate(user=staff_for(self.facility))

    def test_not_from_an_earlier_step(self):
        req = make_request(self.mother, self.facility, current_sub_status=Status.SCHEDULED, current_stage_index=2)
        response = self.client.post(f"/milkbank/requests/{req.id}/confirm-completion/", {"amount_ml": 100})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "not_at_results")
        self.facility.refresh_from_db()
        self.assertEqual(self.facility.stock_level_ml, 1000)

    def test_from_results(self):
        req = make_request(self.mother, self.facility, current_sub_status=Status.SCHEDULED, current_stage_index=4)
        self.assertEqual(self.client.post(f"/milkbank/requests/{req.id}/confirm-completion/",
                                          {"amount_ml": 100}).status_code, 200)


class RecipientConfirmationTests(APITestCase):
    """4 -- a recipient isn't asked to confirm an appointment already begun."""

    def test_advancing_past_her_time_is_refused(self):
        mother = User.objects.create_user(email="mother@example.com", password="x", is_active=True)
        facility = make_facility()
        self.client.force_authenticate(user=staff_for(facility))
        req = make_request(mother, facility, request_type=RECIPIENT, current_sub_status=Status.SCHEDULED,
                           current_stage_index=1, preferred_date=today() - timedelta(days=1))
        response = self.client.post(f"/milkbank/requests/{req.id}/advance-stage/")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "appointment_passed")


class DashboardMonthTests(APITestCase):
    """12 -- the booking trend counts by the Manila month."""

    def test_early_on_the_first_counts_in_the_new_month(self):
        admin = User.objects.create_user(email="admin@example.com", password="x", is_active=True, is_staff=True)
        mother = User.objects.create_user(email="mother@example.com", password="x", is_active=True)
        facility = make_facility()
        req = make_request(mother, facility)
        # 7 AM on the 1st in Manila is still the previous day in UTC.
        start = datetime.now(BUSINESS_TZ).replace(day=1, hour=7, minute=0, second=0, microsecond=0)
        MilkBankRequest.objects.filter(pk=req.pk).update(submitted_at=start)
        self.client.force_authenticate(user=admin)
        trend = self.client.get("/dashboard/stats/").data["booking_trend"]
        self.assertEqual(trend[-1], {"month": start.strftime("%b"), "count": 1})
        self.assertEqual(trend[-2]["count"], 0)

    def test_facility_removals_are_not_counted_as_declines(self):
        from .results import FACILITY_REMOVED_REASON

        admin = User.objects.create_user(email="admin@example.com", password="x", is_active=True, is_staff=True)
        mother = User.objects.create_user(email="mother@example.com", password="x", is_active=True)
        facility = make_facility()
        make_request(mother, facility, current_sub_status=Status.DECLINED, decline_reason=FACILITY_REMOVED_REASON)
        make_request(mother, facility, current_sub_status=Status.DECLINED, decline_reason="Failed blood test")
        self.client.force_authenticate(user=admin)
        reasons = self.client.get("/dashboard/stats/").data["decline_reasons"]
        self.assertEqual(reasons, [{"reason": "Failed blood test", "count": 1}])
