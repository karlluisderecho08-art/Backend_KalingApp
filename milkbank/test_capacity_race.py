"""
A facility is never booked past its capacity, even by two mothers at once.

Booking used to be "read which facilities have room, then add one to the
one chosen" -- two steps, in Python, with time between them. Two mothers
booking the last slot at the same instant could both read 19 of 20, both
be told there was room, and both add one: 21 of 20, and nothing stopped
it. The slot is now claimed in a single conditional UPDATE, which the
database applies one request after the other.

Real simultaneity is hard to stage inside a test, and a test that only
sometimes reproduces a race proves nothing on the runs where it doesn't.
So these stage the dangerous ordering exactly instead: every mother is
handed a ranking that was read BEFORE any of them booked -- the stale view
each one has in the real race -- and then they book. That is the same
sequence of reads and writes, made deterministic.
"""

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
from .models import Facility, MilkBankRequest
from .tests import _first_weekday_after, make_facility

BOOKING_DATE = _first_weekday_after(7)


def make_mother(n):
    return User.objects.create_user(
        email=f"mother{n}@example.com", password="x", is_active=True,
        latitude=14.60, longitude=121.00,
    )


def booked(facility):
    return Facility.objects.get(pk=facility.pk).booked_count


class ClaimSlotTests(TestCase):
    """The single statement everything else rests on."""

    def test_a_free_slot_is_taken_and_counted(self):
        facility = make_facility(capacity=20, booked_count=5)

        self.assertTrue(claim_slot(facility, "DONOR"))
        self.assertEqual(booked(facility), 6)

    def test_a_full_facility_gives_nothing_and_its_count_does_not_move(self):
        facility = make_facility(capacity=20, booked_count=20)

        self.assertFalse(claim_slot(facility, "DONOR"))
        self.assertEqual(booked(facility), 20)

    def test_the_last_slot_can_only_be_taken_once(self):
        """
        Both callers hold the same in-memory facility, read at 19 of 20 --
        which is precisely what two simultaneous requests hold. What each
        one believes does not matter; only the row in the database does.
        """
        facility = make_facility(capacity=20, booked_count=19)
        seen_by_first = Facility.objects.get(pk=facility.pk)
        seen_by_second = Facility.objects.get(pk=facility.pk)
        self.assertEqual((seen_by_first.booked_count, seen_by_second.booked_count), (19, 19))

        self.assertTrue(claim_slot(seen_by_first, "DONOR"))
        self.assertFalse(claim_slot(seen_by_second, "DONOR"))
        self.assertEqual(booked(facility), 20)

    def test_claiming_past_capacity_is_impossible_however_many_try(self):
        facility = make_facility(capacity=3, booked_count=0)
        stale_views = [Facility.objects.get(pk=facility.pk) for _ in range(25)]

        results = [claim_slot(view, "DONOR") for view in stale_views]

        self.assertEqual(results.count(True), 3)
        self.assertEqual(booked(facility), 3)

    def test_a_facility_switched_off_in_the_meantime_gives_nothing(self):
        facility = make_facility(capacity=20, booked_count=5)
        Facility.objects.filter(pk=facility.pk).update(is_operational=False)

        self.assertFalse(claim_slot(facility, "DONOR"))
        self.assertEqual(booked(facility), 5)

    def test_a_recipient_cannot_claim_where_stock_fell_below_the_minimum(self):
        facility = make_facility(capacity=20, booked_count=5, stock_level_ml=MINIMUM_STOCK_THRESHOLD_ML)
        # Dispensed to someone else between ranking and booking.
        Facility.objects.filter(pk=facility.pk).update(stock_level_ml=MINIMUM_STOCK_THRESHOLD_ML - 1)

        self.assertFalse(claim_slot(facility, "RECIPIENT"))
        self.assertEqual(booked(facility), 5)

    def test_a_donor_is_never_refused_over_stock(self):
        facility = make_facility(capacity=20, booked_count=5, stock_level_ml=0)

        self.assertTrue(claim_slot(facility, "DONOR"))

    def test_the_claim_applies_exactly_the_gate_that_ranking_applied(self):
        """
        One definition of "eligible", used for both the candidate list and
        the claim. If the two ever diverged, a facility could be ranked
        but unclaimable, or -- worse -- claimable but never ranked.
        """
        states = [
            dict(name="open", capacity=10, booked_count=3, stock_level_ml=900),
            dict(name="full", capacity=10, booked_count=10, stock_level_ml=900),
            dict(name="closed", capacity=10, booked_count=3, stock_level_ml=900, is_operational=False),
            dict(name="no capacity", capacity=0, booked_count=0, stock_level_ml=900),
            dict(name="low stock", capacity=10, booked_count=3, stock_level_ml=MINIMUM_STOCK_THRESHOLD_ML - 1),
            dict(name="exactly minimum stock", capacity=10, booked_count=3, stock_level_ml=MINIMUM_STOCK_THRESHOLD_ML),
            dict(name="one slot left", capacity=10, booked_count=9, stock_level_ml=900),
        ]
        facilities = [make_facility(**state) for state in states]

        for request_type in ("DONOR", "RECIPIENT"):
            ranked_ids = set(
                Facility.objects.filter(**eligibility_filters(request_type)).values_list("pk", flat=True)
            )
            for facility in facilities:
                with self.subTest(request_type=request_type, facility=facility.name):
                    before = booked(facility)
                    claimed = claim_slot(facility, request_type)
                    self.assertEqual(claimed, facility.pk in ranked_ids)
                    # Put it back so the next request type sees the same state.
                    Facility.objects.filter(pk=facility.pk).update(booked_count=before)


class TwoMothersOneSlotTests(APITestCase):
    """The same thing through POST /milkbank/requests/, the way the app books."""

    def setUp(self):
        # Nearest first: `near` is where every mother here would be sent.
        self.near = make_facility(name="Near", capacity=20, booked_count=19, latitude=14.60, longitude=121.00)
        self.far = make_facility(name="Far", capacity=20, booked_count=0, latitude=14.90, longitude=121.30)

    def book(self, mother, stale_ranking=None, request_type="DONOR"):
        self.client.force_authenticate(user=mother)
        payload = {"request_type": request_type, "preferred_date": BOOKING_DATE, "preferred_time": "10:00 AM"}
        if stale_ranking is None:
            return self.client.post("/milkbank/requests/", payload, format="json")
        with patch("milkbank.views.get_ranked_facilities", return_value=stale_ranking):
            return self.client.post("/milkbank/requests/", payload, format="json")

    def ranking_read_now(self, mother, request_type="DONOR"):
        """What a mother's request reads at the moment it ranks -- before anyone books."""
        return get_ranked_facilities(mother, request_type)

    def test_one_mother_alone_is_booked_at_the_nearest_facility_as_before(self):
        response = self.book(make_mother(1))

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["allocated_facility_name"], "Near")
        self.assertEqual(booked(self.near), 20)
        self.assertEqual(booked(self.far), 0)

    def test_two_mothers_who_both_saw_the_last_slot_cannot_both_have_it(self):
        first, second = make_mother(1), make_mother(2)
        # Both rank while Near still shows 19 of 20.
        seen_by_first = self.ranking_read_now(first)
        seen_by_second = self.ranking_read_now(second)
        self.assertEqual(seen_by_first[0].name, "Near")
        self.assertEqual(seen_by_second[0].name, "Near")

        first_response = self.book(first, stale_ranking=seen_by_first)
        second_response = self.book(second, stale_ranking=seen_by_second)

        self.assertEqual(first_response.status_code, 201)
        self.assertEqual(first_response.data["allocated_facility_name"], "Near")
        self.assertEqual(booked(self.near), 20, "Near must stop at its capacity, not go to 21")

        # The second mother is not turned away -- she goes to the next
        # facility in her own ranking.
        self.assertEqual(second_response.status_code, 201)
        self.assertEqual(second_response.data["allocated_facility_name"], "Far")
        self.assertEqual(booked(self.far), 1)

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

    def test_when_no_other_facility_has_room_the_second_booking_is_refused(self):
        Facility.objects.filter(pk=self.far.pk).update(booked_count=20)
        first, second = make_mother(1), make_mother(2)
        seen_by_first = self.ranking_read_now(first)
        seen_by_second = self.ranking_read_now(second)
        self.assertEqual([f.name for f in seen_by_second], ["Near"])

        self.assertEqual(self.book(first, stale_ranking=seen_by_first).status_code, 201)
        refused = self.book(second, stale_ranking=seen_by_second)

        self.assertEqual(refused.status_code, 404)
        self.assertIn("fully booked", refused.data["detail"])
        self.assertEqual(booked(self.near), 20)

    def test_a_refused_booking_leaves_nothing_behind(self):
        Facility.objects.filter(pk=self.far.pk).update(booked_count=20)
        first, second = make_mother(1), make_mother(2)
        seen_by_first = self.ranking_read_now(first)
        seen_by_second = self.ranking_read_now(second)
        self.book(first, stale_ranking=seen_by_first)
        self.book(second, stale_ranking=seen_by_second)

        self.assertFalse(MilkBankRequest.objects.filter(owner=second).exists())
        self.assertFalse(NotificationItem.objects.filter(owner=second).exists())
        self.assertFalse(AuditLogEntry.objects.filter(actor=second, action="booking.created").exists())
        # ...and she is free to try again, not stuck behind a phantom request.
        Facility.objects.filter(pk=self.far.pk).update(booked_count=0)
        self.assertEqual(self.book(second).status_code, 201)

    def test_ten_mothers_racing_for_three_slots_fill_exactly_three(self):
        Facility.objects.filter(pk=self.near.pk).update(booked_count=17)  # 3 left of 20
        mothers = [make_mother(n) for n in range(10)]
        rankings = [self.ranking_read_now(mother) for mother in mothers]

        responses = [self.book(mother, stale_ranking=ranking) for mother, ranking in zip(mothers, rankings)]

        self.assertEqual([r.status_code for r in responses], [201] * 10)
        placed = [r.data["allocated_facility_name"] for r in responses]
        self.assertEqual(placed.count("Near"), 3)
        self.assertEqual(placed.count("Far"), 7)
        self.assertEqual(booked(self.near), 20)
        self.assertEqual(booked(self.far), 7)

    def test_no_facility_ever_ends_up_over_capacity_when_everything_is_scarce(self):
        Facility.objects.filter(pk=self.near.pk).update(capacity=2, booked_count=0)
        Facility.objects.filter(pk=self.far.pk).update(capacity=3, booked_count=0)
        mothers = [make_mother(n) for n in range(12)]
        rankings = [self.ranking_read_now(mother) for mother in mothers]

        codes = [self.book(m, stale_ranking=r).status_code for m, r in zip(mothers, rankings)]

        self.assertEqual(codes.count(201), 5, "2 + 3 slots existed, so exactly 5 bookings")
        self.assertEqual(codes.count(404), 7)
        for facility in Facility.objects.all():
            self.assertLessEqual(facility.booked_count, facility.capacity, facility.name)
        self.assertEqual(MilkBankRequest.objects.count(), 5)

    def test_the_count_always_matches_the_bookings_that_exist(self):
        Facility.objects.filter(pk=self.near.pk).update(capacity=4, booked_count=0)
        Facility.objects.filter(pk=self.far.pk).update(capacity=4, booked_count=0)
        mothers = [make_mother(n) for n in range(10)]
        rankings = [self.ranking_read_now(mother) for mother in mothers]
        for mother, ranking in zip(mothers, rankings):
            self.book(mother, stale_ranking=ranking)

        for facility in Facility.objects.all():
            self.assertEqual(
                facility.booked_count,
                MilkBankRequest.objects.filter(allocated_facility=facility).count(),
                facility.name,
            )

    def test_a_recipient_is_not_sent_where_stock_ran_out_while_she_was_booking(self):
        Facility.objects.filter(pk=self.near.pk).update(booked_count=0, stock_level_ml=MINIMUM_STOCK_THRESHOLD_ML)
        mother = make_mother(1)
        seen = self.ranking_read_now(mother, "RECIPIENT")
        self.assertEqual(seen[0].name, "Near")
        # Someone else's dispensing completes in between.
        Facility.objects.filter(pk=self.near.pk).update(stock_level_ml=10)

        response = self.book(mother, stale_ranking=seen, request_type="RECIPIENT")

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["allocated_facility_name"], "Far")
        self.assertEqual(booked(self.near), 0)

    def test_a_slot_is_not_lost_if_creating_the_booking_fails_after_it_was_claimed(self):
        """
        The claim and the booking are one transaction. Without that, an
        error between the two would leave the facility one slot short for
        a booking that does not exist -- and it would never come back.
        """
        mother = make_mother(1)
        self.client.force_authenticate(user=mother)
        payload = {"request_type": "DONOR", "preferred_date": BOOKING_DATE, "preferred_time": "10:00 AM"}

        with patch("milkbank.views.MilkBankRequest.objects.create", side_effect=RuntimeError("database hiccup")):
            with self.assertRaises(RuntimeError):
                self.client.post("/milkbank/requests/", payload, format="json")

        self.assertEqual(booked(self.near), 19, "the claimed slot must have been given back")
        self.assertFalse(MilkBankRequest.objects.filter(owner=mother).exists())
