"""
Per-day capacity: a facility is never booked past its capacity on any day,
even by two mothers at once, and one day filling up never affects another.

Facility.capacity is bookings PER DAY (see capacity.py). A booking takes a
slot on its own date, from the moment it is submitted, and gives it back
only when declined or expired.

Real simultaneity is hard to stage inside a test, and a test that only
sometimes reproduces a race proves nothing on the runs where it doesn't.
So these stage the dangerous ordering exactly instead: every mother is
handed a ranking that was read BEFORE any of them booked -- the stale view
each one has in the real race -- and then they book. That is the same
sequence of reads and writes, made deterministic. (In production the
facility row is locked before the day is counted -- see claim_slot.)
"""

from datetime import date, timedelta
from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APITestCase

from accounts.models import User
from core.models import AuditLogEntry
from notifications.models import NotificationItem

from .allocation import (
    MINIMUM_STOCK_THRESHOLD_ML,
    claim_slot,
    eligibility_filters,
    get_ranked_facilities,
)
from .capacity import day_summary, slots_taken
from .models import Facility, MilkBankRequest
from .tests import _first_weekday_after, fill_day, make_facility, make_request

Status = MilkBankRequest.Status

BOOKING_DATE = _first_weekday_after(7)
BOOKING_DAY = date.fromisoformat(BOOKING_DATE)
# A different weekday from BOOKING_DATE, guaranteed.
NEXT_DAY = BOOKING_DAY + timedelta(days=1 if BOOKING_DAY.weekday() < 4 else 3)


def make_mother(n):
    return User.objects.create_user(
        email=f"mother{n}@example.com", password="x", is_active=True,
        latitude=14.60, longitude=121.00,
    )


def taken(facility, day=BOOKING_DAY):
    return slots_taken(facility.pk, day)


class ClaimSlotTests(TestCase):
    """The check everything else rests on: is there room on THIS day."""

    def test_a_day_with_room_can_be_claimed(self):
        facility = make_facility(capacity=20)
        fill_day(facility, BOOKING_DAY, 5)

        self.assertTrue(claim_slot(facility, "DONOR", BOOKING_DAY))

    def test_a_full_day_cannot(self):
        facility = make_facility(capacity=3)
        fill_day(facility, BOOKING_DAY, 3)

        self.assertFalse(claim_slot(facility, "DONOR", BOOKING_DAY))

    def test_a_full_day_leaves_every_other_day_open(self):
        facility = make_facility(capacity=3)
        fill_day(facility, BOOKING_DAY, 3)

        self.assertTrue(claim_slot(facility, "DONOR", NEXT_DAY))

    def test_pending_requests_hold_their_day(self):
        # Submitted, not yet approved -- still reserves the day.
        facility = make_facility(capacity=2)
        fill_day(facility, BOOKING_DAY, 2, status=Status.PENDING)

        self.assertFalse(claim_slot(facility, "DONOR", BOOKING_DAY))

    def test_declined_and_expired_give_their_slot_back(self):
        facility = make_facility(capacity=2)
        fill_day(facility, BOOKING_DAY, 1, status=Status.DECLINED)
        fill_day(facility, BOOKING_DAY, 1, status=Status.EXPIRED)

        self.assertTrue(claim_slot(facility, "DONOR", BOOKING_DAY))
        self.assertEqual(taken(facility), 0)

    def test_a_completed_appointment_keeps_counting_on_its_day(self):
        facility = make_facility(capacity=1)
        fill_day(facility, BOOKING_DAY, 1, status=Status.COMPLETED)

        self.assertFalse(claim_slot(facility, "DONOR", BOOKING_DAY))

    def test_a_facility_switched_off_in_the_meantime_gives_nothing(self):
        facility = make_facility(capacity=20)
        Facility.objects.filter(pk=facility.pk).update(is_operational=False)

        self.assertFalse(claim_slot(facility, "DONOR", BOOKING_DAY))

    def test_a_recipient_cannot_claim_where_stock_fell_below_the_minimum(self):
        facility = make_facility(capacity=20, stock_level_ml=MINIMUM_STOCK_THRESHOLD_ML)
        # Dispensed to someone else between ranking and booking.
        Facility.objects.filter(pk=facility.pk).update(stock_level_ml=MINIMUM_STOCK_THRESHOLD_ML - 1)

        self.assertFalse(claim_slot(facility, "RECIPIENT", BOOKING_DAY))

    def test_a_donor_is_never_refused_over_stock(self):
        facility = make_facility(capacity=20, stock_level_ml=0)

        self.assertTrue(claim_slot(facility, "DONOR", BOOKING_DAY))

    def test_the_claim_applies_exactly_the_gate_that_ranking_applied(self):
        """
        One definition of "eligible", used for both the candidate list and
        the claim. If the two ever diverged, a facility could be ranked
        but unclaimable, or -- worse -- claimable but never ranked.
        (Room on the day is checked on top of this; here every day is empty.)
        """
        states = [
            dict(name="open", capacity=10, stock_level_ml=900),
            dict(name="closed", capacity=10, stock_level_ml=900, is_operational=False),
            dict(name="no capacity", capacity=0, stock_level_ml=900),
            dict(name="low stock", capacity=10, stock_level_ml=MINIMUM_STOCK_THRESHOLD_ML - 1),
            dict(name="exactly minimum stock", capacity=10, stock_level_ml=MINIMUM_STOCK_THRESHOLD_ML),
        ]
        facilities = [make_facility(**state) for state in states]

        for request_type in ("DONOR", "RECIPIENT"):
            ranked_ids = set(
                Facility.objects.filter(**eligibility_filters(request_type)).values_list("pk", flat=True)
            )
            for facility in facilities:
                with self.subTest(request_type=request_type, facility=facility.name):
                    self.assertEqual(claim_slot(facility, request_type, BOOKING_DAY), facility.pk in ranked_ids)


class TwoMothersOneSlotTests(APITestCase):
    """The same thing through POST /milkbank/requests/, the way the app books."""

    def setUp(self):
        # Nearest first: `near` is where every mother here would be sent.
        self.near = make_facility(name="Near", capacity=20, latitude=14.60, longitude=121.00)
        self.far = make_facility(name="Far", capacity=20, latitude=14.90, longitude=121.30)
        fill_day(self.near, BOOKING_DAY, 19)  # one slot left on the booking day

    def book(self, mother, stale_ranking=None, request_type="DONOR", day=BOOKING_DATE):
        self.client.force_authenticate(user=mother)
        payload = {"request_type": request_type, "preferred_date": day, "preferred_time": "10:00 AM"}
        if request_type == "RECIPIENT":
            payload["requested_ml"] = 100
        if stale_ranking is None:
            return self.client.post("/milkbank/requests/", payload, format="json")
        with patch("milkbank.views.get_ranked_facilities", return_value=stale_ranking):
            return self.client.post("/milkbank/requests/", payload, format="json")

    def ranking_read_now(self, mother, request_type="DONOR"):
        """What a mother's request reads at the moment it ranks -- before anyone books."""
        return get_ranked_facilities(mother, request_type, day=BOOKING_DAY)

    def test_one_mother_alone_is_booked_at_the_nearest_facility_as_before(self):
        response = self.book(make_mother(1))

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["allocated_facility_name"], "Near")
        self.assertEqual(taken(self.near), 20)
        self.assertEqual(taken(self.far), 0)

    def test_the_slot_is_taken_on_her_day_not_on_the_day_she_submitted(self):
        response = self.book(make_mother(1), day=NEXT_DAY.isoformat())

        self.assertEqual(response.status_code, 201)
        self.assertEqual(taken(self.near, NEXT_DAY), 1)
        self.assertEqual(taken(self.near, BOOKING_DAY), 19, "the other day is untouched")

    def test_two_mothers_who_both_saw_the_last_slot_cannot_both_have_it(self):
        first, second = make_mother(1), make_mother(2)
        # Both rank while Near still shows 19 of 20 for the day.
        seen_by_first = self.ranking_read_now(first)
        seen_by_second = self.ranking_read_now(second)
        self.assertEqual(seen_by_first[0].name, "Near")
        self.assertEqual(seen_by_second[0].name, "Near")

        first_response = self.book(first, stale_ranking=seen_by_first)
        second_response = self.book(second, stale_ranking=seen_by_second)

        self.assertEqual(first_response.status_code, 201)
        self.assertEqual(first_response.data["allocated_facility_name"], "Near")
        self.assertEqual(taken(self.near), 20, "Near must stop at its capacity for the day, not go to 21")

        # The second mother is not turned away -- she goes to the next
        # facility in her own ranking, on the same day.
        self.assertEqual(second_response.status_code, 201)
        self.assertEqual(second_response.data["allocated_facility_name"], "Far")
        self.assertEqual(taken(self.far), 1)

    def test_the_mother_who_lost_the_slot_is_told_the_facility_she_actually_got(self):
        first, second = make_mother(1), make_mother(2)
        seen_by_first = self.ranking_read_now(first)
        seen_by_second = self.ranking_read_now(second)
        self.book(first, stale_ranking=seen_by_first)
        self.book(second, stale_ranking=seen_by_second)

        notice = NotificationItem.objects.get(owner=second, title="Milk Bank Request Submitted")
        self.assertIn("Far", notice.description)
        self.assertNotIn("Near", notice.description)
        self.assertEqual(MilkBankRequest.objects.get(owner=second).allocated_facility, self.far)

    def test_when_no_other_facility_has_room_that_day_the_second_booking_is_refused(self):
        fill_day(self.far, BOOKING_DAY, 20)
        first, second = make_mother(1), make_mother(2)
        seen_by_first = self.ranking_read_now(first)
        seen_by_second = self.ranking_read_now(second)
        self.assertEqual([f.name for f in seen_by_second], ["Near"])

        self.assertEqual(self.book(first, stale_ranking=seen_by_first).status_code, 201)
        refused = self.book(second, stale_ranking=seen_by_second)

        self.assertEqual(refused.status_code, 409)
        self.assertIn("fully booked", refused.data["detail"])
        self.assertEqual(refused.data["fully_booked_date"], BOOKING_DATE)
        self.assertEqual(taken(self.near), 20)

    def test_a_refused_booking_leaves_nothing_behind(self):
        fill_day(self.far, BOOKING_DAY, 20)
        first, second = make_mother(1), make_mother(2)
        seen_by_first = self.ranking_read_now(first)
        seen_by_second = self.ranking_read_now(second)
        self.book(first, stale_ranking=seen_by_first)
        self.book(second, stale_ranking=seen_by_second)

        self.assertFalse(MilkBankRequest.objects.filter(owner=second).exists())
        self.assertFalse(NotificationItem.objects.filter(owner=second).exists())
        self.assertFalse(AuditLogEntry.objects.filter(actor=second, action="booking.created").exists())
        # ...and she is free to book another day, not stuck behind a phantom request.
        self.assertEqual(self.book(second, day=NEXT_DAY.isoformat()).status_code, 201)

    def test_ten_mothers_racing_for_three_slots_fill_exactly_three(self):
        MilkBankRequest.objects.filter(allocated_facility=self.near).delete()
        fill_day(self.near, BOOKING_DAY, 17)  # 3 left of 20 that day
        mothers = [make_mother(n) for n in range(10)]
        rankings = [self.ranking_read_now(mother) for mother in mothers]

        responses = [self.book(mother, stale_ranking=ranking) for mother, ranking in zip(mothers, rankings)]

        self.assertEqual([r.status_code for r in responses], [201] * 10)
        placed = [r.data["allocated_facility_name"] for r in responses]
        self.assertEqual(placed.count("Near"), 3)
        self.assertEqual(placed.count("Far"), 7)
        self.assertEqual(taken(self.near), 20)
        self.assertEqual(taken(self.far), 7)

    def test_no_facility_ever_ends_up_over_capacity_on_a_scarce_day(self):
        MilkBankRequest.objects.all().delete()
        Facility.objects.filter(pk=self.near.pk).update(capacity=2)
        Facility.objects.filter(pk=self.far.pk).update(capacity=3)
        mothers = [make_mother(n) for n in range(12)]
        rankings = [self.ranking_read_now(mother) for mother in mothers]

        codes = [self.book(m, stale_ranking=r).status_code for m, r in zip(mothers, rankings)]

        self.assertEqual(codes.count(201), 5, "2 + 3 slots existed that day, so exactly 5 bookings")
        self.assertEqual(codes.count(409), 7)
        for facility in Facility.objects.all():
            self.assertLessEqual(taken(facility), facility.capacity, facility.name)
        self.assertEqual(MilkBankRequest.objects.count(), 5)

    def test_a_recipient_is_not_sent_where_stock_ran_out_while_she_was_booking(self):
        Facility.objects.filter(pk=self.near.pk).update(stock_level_ml=MINIMUM_STOCK_THRESHOLD_ML)
        mother = make_mother(1)
        seen = self.ranking_read_now(mother, "RECIPIENT")
        self.assertEqual(seen[0].name, "Near")
        # Someone else's dispensing completes in between.
        Facility.objects.filter(pk=self.near.pk).update(stock_level_ml=10)

        response = self.book(mother, stale_ranking=seen, request_type="RECIPIENT")

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["allocated_facility_name"], "Far")
        self.assertEqual(taken(self.near), 19)

    def test_nothing_is_held_if_creating_the_booking_fails_after_the_claim(self):
        """
        The claim and the booking are one transaction, and the day's count
        IS its bookings -- so an error between the two cannot leave a slot
        taken by a booking that does not exist.
        """
        mother = make_mother(1)
        self.client.force_authenticate(user=mother)
        payload = {"request_type": "DONOR", "preferred_date": BOOKING_DATE, "preferred_time": "10:00 AM"}

        with patch("milkbank.views.MilkBankRequest.objects.create", side_effect=RuntimeError("database hiccup")):
            with self.assertRaises(RuntimeError):
                self.client.post("/milkbank/requests/", payload, format="json")

        self.assertEqual(taken(self.near), 19)
        self.assertFalse(MilkBankRequest.objects.filter(owner=mother).exists())


class MovingABookingToAnotherDayTests(APITestCase):
    """
    The three ways a booking's date changes after it was made -- staff
    proposing a new date, her accepting it, her rejecting it and choosing
    her own -- each need a free slot on the day it moves to.
    """

    def setUp(self):
        self.facility = make_facility(name="St. Luke's", capacity=2)
        self.mother = make_mother(1)
        self.staff = User.objects.create_user(
            email="staff@example.com", password="x", is_active=True,
            role=User.Role.FACILITY_STAFF, facility=self.facility,
        )
        self.req = make_request(self.mother, self.facility, preferred_date=BOOKING_DAY)

    def propose(self, day):
        self.client.force_authenticate(user=self.staff)
        return self.client.post(
            f"/milkbank/requests/{self.req.id}/propose-counter-offer/",
            {"counter_offer_date": day.isoformat(), "counter_offer_time": "10:00 AM", "staff_message": "No doctor"},
            format="json",
        )

    def test_staff_cannot_offer_a_full_day(self):
        fill_day(self.facility, NEXT_DAY, 2)

        response = self.propose(NEXT_DAY)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["counter_offer_date"], "This date is fully booked.")

    def test_staff_can_offer_a_day_with_room(self):
        fill_day(self.facility, NEXT_DAY, 1)

        self.assertEqual(self.propose(NEXT_DAY).status_code, 200)

    def test_accepting_moves_her_slot_to_the_new_day(self):
        self.propose(NEXT_DAY)
        self.client.force_authenticate(user=self.mother)

        response = self.client.post(f"/milkbank/requests/{self.req.id}/accept-counter-offer/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(taken(self.facility, NEXT_DAY), 1)
        self.assertEqual(taken(self.facility, BOOKING_DAY), 0)

    def test_accepting_is_refused_if_the_offered_day_filled_up_meanwhile(self):
        self.propose(NEXT_DAY)
        fill_day(self.facility, NEXT_DAY, 2)  # other mothers took it after the offer
        self.client.force_authenticate(user=self.mother)

        response = self.client.post(f"/milkbank/requests/{self.req.id}/accept-counter-offer/")

        self.assertEqual(response.status_code, 409)
        self.req.refresh_from_db()
        self.assertEqual(self.req.current_sub_status, Status.COUNTER_OFFERED)
        self.assertEqual(self.req.preferred_date, BOOKING_DAY)
        self.assertEqual(taken(self.facility, NEXT_DAY), 2)

    def test_rejecting_and_choosing_a_full_day_is_refused(self):
        self.propose(NEXT_DAY)
        third_day = NEXT_DAY + timedelta(days=1 if NEXT_DAY.weekday() < 4 else 3)
        fill_day(self.facility, third_day, 2)
        self.client.force_authenticate(user=self.mother)

        response = self.client.post(
            f"/milkbank/requests/{self.req.id}/reject-counter-offer/",
            {"preferred_date": third_day.isoformat(), "preferred_time": "9:00 AM"},
            format="json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["fully_booked_date"], third_day.isoformat())
        self.req.refresh_from_db()
        self.assertEqual(self.req.current_sub_status, Status.COUNTER_OFFERED)

    def test_rejecting_and_choosing_a_day_with_room_moves_her_there(self):
        self.propose(NEXT_DAY)
        third_day = NEXT_DAY + timedelta(days=1 if NEXT_DAY.weekday() < 4 else 3)
        self.client.force_authenticate(user=self.mother)

        response = self.client.post(
            f"/milkbank/requests/{self.req.id}/reject-counter-offer/",
            {"preferred_date": third_day.isoformat(), "preferred_time": "9:00 AM"},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(taken(self.facility, third_day), 1)
        self.assertEqual(taken(self.facility, BOOKING_DAY), 0)

    def test_her_own_slot_does_not_count_against_her(self):
        # Capacity 1, and the only booking that day is hers: moving it back
        # onto the same day must not be refused as "full".
        Facility.objects.filter(pk=self.facility.pk).update(capacity=1)
        self.propose(NEXT_DAY)
        self.client.force_authenticate(user=self.mother)

        response = self.client.post(
            f"/milkbank/requests/{self.req.id}/reject-counter-offer/",
            {"preferred_date": BOOKING_DATE, "preferred_time": "2:00 PM"},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)


class FacilityCalendarTests(APITestCase):
    """What GET /milkbank/facilities/ publishes about each facility's days."""

    def test_the_payload_reports_full_days_and_upcoming_counts(self):
        facility = make_facility(name="St. Luke's", capacity=2)
        fill_day(facility, BOOKING_DAY, 2)
        fill_day(facility, NEXT_DAY, 1)

        data = self.client.get(f"/milkbank/facilities/{facility.id}/").data

        self.assertEqual(data["capacity"], 2)
        self.assertIn(BOOKING_DATE, data["fully_booked_dates"])
        self.assertNotIn(NEXT_DAY.isoformat(), data["fully_booked_dates"])
        upcoming = {d["date"]: d["count"] for d in data["upcoming_bookings"]}
        self.assertEqual(upcoming.get(BOOKING_DATE), 2)
        self.assertEqual(upcoming.get(NEXT_DAY.isoformat()), 1)
        self.assertEqual(len(data["upcoming_bookings"]), 10)
        self.assertTrue(all(date.fromisoformat(d["date"]).weekday() < 5 for d in data["upcoming_bookings"]))

    def test_bookings_today_counts_only_today(self):
        facility = make_facility(capacity=5)
        summary = day_summary(facility, start=BOOKING_DAY)
        self.assertEqual(summary["bookings_today"], 0)
        fill_day(facility, BOOKING_DAY, 3)
        fill_day(facility, NEXT_DAY, 4)

        summary = day_summary(facility, start=BOOKING_DAY)

        self.assertEqual(summary["bookings_today"], 3)

    def test_a_facility_taking_no_bookings_lists_no_full_days(self):
        facility = make_facility(capacity=0)
        self.assertEqual(day_summary(facility)["fully_booked_dates"], [])

    def test_admin_can_still_edit_a_facility_without_sending_any_count(self):
        admin = User.objects.create_user(email="admin@example.com", password="x", is_active=True, is_staff=True)
        facility = make_facility(capacity=5)
        self.client.force_authenticate(user=admin)

        response = self.client.patch(f"/milkbank/facilities/{facility.id}/", {"capacity": 8}, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        facility.refresh_from_db()
        self.assertEqual(facility.capacity, 8)
