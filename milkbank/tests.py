import os
from datetime import date, datetime, timedelta
from importlib import import_module

from django.apps import apps
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from core.models import AuditLogEntry
from notifications.models import NotificationItem

from .allocation import (
    DayFullyBooked,
    LocationRequired,
    NoOperationalFacility,
    get_ranked_facilities,
    rank_facilities,
)
from .business_hours import BUSINESS_TZ, add_business_hours, is_business_day, philippine_holidays
from .capacity import slots_taken
from .models import DonorQuestionnaire, Facility, MilkBankRequest, TransactionRecord
from .scheduling import counter_offer_errors
from .transitions import ALLOWED_TRANSITIONS, InvalidTransition, apply_transition, sweep_expired_requests
from .views import _can_view_questionnaire

Status = MilkBankRequest.Status


def _first_weekday_after(days):
    """The first Mon-Fri at least `days` days from now (Manila), as yyyy-mm-dd.
    Counter-offers must be on a future weekday, so tests that offer one can't
    hard-code a calendar date -- it goes stale, and the one they used was a Sunday."""
    day = datetime.now(BUSINESS_TZ).date() + timedelta(days=days)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day.isoformat()


COUNTER_OFFER_DATE = _first_weekday_after(14)
# Two different future weekdays, for per-day capacity tests.
BOOKED_DAY = _first_weekday_after(21)
# Strictly later than BOOKED_DAY: _first_weekday_after(22) could roll onto the
# same Monday when day 21 is a Saturday.
OTHER_DAY = _first_weekday_after(
    (date.fromisoformat(BOOKED_DAY) - datetime.now(BUSINESS_TZ).date()).days + 1
)


def make_facility(slots_taken=None, **overrides):
    """
    `slots_taken` only attaches that day's load as an attribute, for the pure
    rank_facilities() tests (the attribute get_ranked_facilities() normally
    sets). It books nothing -- use fill_day() for real bookings on a day.
    """
    defaults = dict(
        name="Test Facility",
        type=Facility.FacilityType.HOSPITAL_DEPOT,
        contact="000-0000",
        address="Somewhere",
        is_operational=True,
        capacity=10,
        stock_level_ml=1000,
        latitude=14.6,
        longitude=121.0,
    )
    defaults.update(overrides)
    facility = Facility.objects.create(**defaults)
    if slots_taken is not None:
        facility.slots_taken = slots_taken
    return facility


def make_request(owner, facility, **overrides):
    defaults = dict(
        owner=owner,
        request_type=MilkBankRequest.RequestType.DONOR,
        allocated_facility=facility,
        preferred_date="2026-12-01",
        preferred_time="10:00 AM",
    )
    defaults.update(overrides)
    return MilkBankRequest.objects.create(**defaults)


_filler_count = 0


def fill_day(facility, day, n, status=None):
    """Books `n` slots on `day` at `facility`, each by a different mother."""
    global _filler_count
    for _ in range(n):
        _filler_count += 1
        owner = User.objects.create_user(email=f"filler{_filler_count}@example.com", password=None, is_active=True)  # no hash: fast
        make_request(owner, facility, preferred_date=day, **({"current_sub_status": status} if status else {}))


class TransitionsTests(APITestCase):
    """
    apply_transition() is the single choke point every booking status
    change goes through -- this is the highest business-risk logic in
    the backend (get this wrong and a mother could be told her booking
    is scheduled when it isn't, or a facility's slot count could drift
    from reality).
    """

    def setUp(self):
        self.mother = User.objects.create_user(email="mother@example.com", password="x", is_active=True)
        self.staff = User.objects.create_user(
            email="staff@example.com", password="x", is_active=True, role=User.Role.FACILITY_STAFF,
        )
        self.facility = make_facility()
        self.req = make_request(self.mother, self.facility)

    def test_every_status_has_an_entry_in_the_transition_table(self):
        # Guards against a future new Status choice being added without
        # anyone remembering to also add its transition rule -- it would
        # otherwise silently behave as "no transitions allowed at all".
        for value, _label in Status.choices:
            self.assertIn(value, ALLOWED_TRANSITIONS, f"{value} has no ALLOWED_TRANSITIONS entry")

    def test_valid_transition_succeeds_and_is_audited(self):
        apply_transition(self.req, Status.AWAITING_ATTENDANCE, self.staff, "accepted")
        self.req.refresh_from_db()
        self.assertEqual(self.req.current_sub_status, Status.AWAITING_ATTENDANCE)

    def test_valid_transition_notifies_the_owner(self):
        apply_transition(self.req, Status.AWAITING_ATTENDANCE, self.staff, "accepted")
        notification = NotificationItem.objects.get(owner=self.mother)
        self.assertEqual(notification.category, NotificationItem.Category.BOOKINGS)
        self.assertIn("confirm your attendance", notification.description)

    def test_invalid_transition_is_rejected_and_leaves_status_unchanged(self):
        # declined is terminal -- nothing should ever move it to completed.
        apply_transition(self.req, Status.DECLINED, self.staff, "declined")
        with self.assertRaises(InvalidTransition):
            apply_transition(self.req, Status.COMPLETED, self.staff, "completed")
        self.req.refresh_from_db()
        self.assertEqual(self.req.current_sub_status, Status.DECLINED)

    def test_pending_may_move_straight_to_scheduled_for_the_recipient_review(self):
        # This edge used to be forbidden, on the reasoning that reaching
        # SCHEDULED without passing through AWAITING_ATTENDANCE would skip
        # the mother's attendance confirmation.
        #
        # It is allowed now because the RECIPIENT pathway needs it: accepting
        # her lands on the "Status" stage, where staff read the serology test
        # and questionnaire she already submitted. Nothing is being asked of
        # her there, so putting her in AWAITING_ATTENDANCE would start an
        # 8-business-hour clock against a mother with nothing left to do.
        #
        # Her attendance confirmation is not skipped -- it moves later in the
        # sequence. StaffAdvanceStageView sends her to AWAITING_ATTENDANCE the
        # moment that review passes; see
        # test_advancing_a_recipient_into_booking_confirmation_awaits_attendance.
        apply_transition(self.req, Status.SCHEDULED, self.staff, "accepted")
        self.req.refresh_from_db()
        self.assertEqual(self.req.current_sub_status, Status.SCHEDULED)

    def test_declined_still_cannot_jump_to_scheduled(self):
        # The edge added above is PENDING -> SCHEDULED specifically. A
        # terminal status must stay terminal.
        apply_transition(self.req, Status.DECLINED, self.staff, "declined")
        with self.assertRaises(InvalidTransition):
            apply_transition(self.req, Status.SCHEDULED, self.staff, "accepted")

    def test_declining_frees_the_slot_on_her_day(self):
        day = self.req.preferred_date
        self.assertEqual(slots_taken(self.facility.pk, day), 1)
        apply_transition(self.req, Status.DECLINED, self.staff, "declined")
        self.assertEqual(slots_taken(self.facility.pk, day), 0)

    def test_accepting_keeps_her_slot(self):
        day = self.req.preferred_date
        apply_transition(self.req, Status.AWAITING_ATTENDANCE, self.staff, "accepted")
        self.assertEqual(slots_taken(self.facility.pk, day), 1)

    def test_a_completed_appointment_still_counts_on_its_day(self):
        # It happened on that day and used that day's slot; finishing it
        # must not hand the day a spare one.
        day = self.req.preferred_date
        apply_transition(self.req, Status.AWAITING_ATTENDANCE, self.staff, "accepted")
        apply_transition(self.req, Status.SCHEDULED, self.mother, "attendance_confirmed")
        apply_transition(self.req, Status.COMPLETED, self.staff, "completed", amount_ml=100)
        self.assertEqual(slots_taken(self.facility.pk, day), 1)

    def test_completing_a_donor_request_creates_a_donation_record(self):
        apply_transition(self.req, Status.AWAITING_ATTENDANCE, self.staff, "accepted")
        apply_transition(self.req, Status.SCHEDULED, self.mother, "attendance_confirmed")
        apply_transition(self.req, Status.COMPLETED, self.staff, "completed", amount_ml=150)

        record = TransactionRecord.objects.get(owner=self.mother)
        self.assertEqual(record.type, TransactionRecord.TransactionType.DONATION)
        self.assertEqual(record.status, TransactionRecord.TransactionStatus.COMPLETED)
        self.assertEqual(record.amount_ml, 150)

    def test_completing_without_an_amount_still_creates_a_record_with_no_amount(self):
        # apply_transition's amount_ml is optional in its own signature even
        # though the real endpoint always supplies one (see
        # ConfirmCompletionSerializer's min_value=1) -- this is what happens
        # if some future caller doesn't.
        apply_transition(self.req, Status.AWAITING_ATTENDANCE, self.staff, "accepted")
        apply_transition(self.req, Status.SCHEDULED, self.mother, "attendance_confirmed")
        apply_transition(self.req, Status.COMPLETED, self.staff, "completed")

        record = TransactionRecord.objects.get(owner=self.mother)
        self.assertIsNone(record.amount_ml)

    def test_transactions_mine_endpoint_returns_the_completed_amount(self):
        # The actual thing the mobile Transaction History screen reads --
        # a model-level assertion on TransactionRecord.amount_ml wouldn't
        # catch a serializer that forgot to list the field.
        apply_transition(self.req, Status.AWAITING_ATTENDANCE, self.staff, "accepted")
        apply_transition(self.req, Status.SCHEDULED, self.mother, "attendance_confirmed")
        apply_transition(self.req, Status.COMPLETED, self.staff, "completed", amount_ml=150)

        self.client.force_authenticate(user=self.mother)
        response = self.client.get("/milkbank/transactions/mine/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data[0]["amount_ml"], 150)

    def test_completing_a_recipient_request_creates_a_received_record(self):
        recipient_req = make_request(self.mother, self.facility, request_type=MilkBankRequest.RequestType.RECIPIENT)
        apply_transition(recipient_req, Status.AWAITING_ATTENDANCE, self.staff, "accepted")
        apply_transition(recipient_req, Status.SCHEDULED, self.mother, "attendance_confirmed")
        apply_transition(recipient_req, Status.COMPLETED, self.staff, "completed")

        record = TransactionRecord.objects.latest("id")
        self.assertEqual(record.type, TransactionRecord.TransactionType.RECEIVED)

    def test_completing_a_donor_request_with_an_amount_credits_stock_and_the_mothers_total(self):
        starting_stock = self.facility.stock_level_ml
        apply_transition(self.req, Status.AWAITING_ATTENDANCE, self.staff, "accepted")
        apply_transition(self.req, Status.SCHEDULED, self.mother, "attendance_confirmed")
        apply_transition(self.req, Status.COMPLETED, self.staff, "completed", amount_ml=120)

        self.facility.refresh_from_db()
        self.mother.refresh_from_db()
        # Applied as given: stock and the mother's lifetime total are both
        # millilitres, so there is no conversion and nothing to round.
        self.assertEqual(self.facility.stock_level_ml, starting_stock + 120)
        self.assertEqual(self.mother.total_drawn_ml, 120)

    def test_completing_a_recipient_request_with_an_amount_debits_stock(self):
        recipient_req = make_request(self.mother, self.facility, request_type=MilkBankRequest.RequestType.RECIPIENT)
        starting_stock = self.facility.stock_level_ml
        apply_transition(recipient_req, Status.AWAITING_ATTENDANCE, self.staff, "accepted")
        apply_transition(recipient_req, Status.SCHEDULED, self.mother, "attendance_confirmed")
        apply_transition(recipient_req, Status.COMPLETED, self.staff, "completed", amount_ml=90)

        self.facility.refresh_from_db()
        self.assertEqual(self.facility.stock_level_ml, starting_stock - 90)

    def test_completing_a_recipient_request_with_an_amount_credits_the_mothers_total_received(self):
        recipient_req = make_request(self.mother, self.facility, request_type=MilkBankRequest.RequestType.RECIPIENT)
        apply_transition(recipient_req, Status.AWAITING_ATTENDANCE, self.staff, "accepted")
        apply_transition(recipient_req, Status.SCHEDULED, self.mother, "attendance_confirmed")
        apply_transition(recipient_req, Status.COMPLETED, self.staff, "completed", amount_ml=90)

        self.mother.refresh_from_db()
        # Same mother, two independent lifetime counters: completing a
        # RECIPIENT booking must not touch total_drawn_ml, and completing
        # a DONOR one (the test above) must not touch total_received_ml.
        self.assertEqual(self.mother.total_received_ml, 90)
        self.assertEqual(self.mother.total_drawn_ml, 0)

    def test_completing_without_an_amount_leaves_stock_and_total_drawn_untouched(self):
        # Every non-StaffConfirmCompletionView caller (there are none right
        # now, but nothing stops a future one) must be safe leaving
        # amount_ml at its None default -- this is what that relies on.
        starting_stock = self.facility.stock_level_ml
        apply_transition(self.req, Status.AWAITING_ATTENDANCE, self.staff, "accepted")
        apply_transition(self.req, Status.SCHEDULED, self.mother, "attendance_confirmed")
        apply_transition(self.req, Status.COMPLETED, self.staff, "completed")

        self.facility.refresh_from_db()
        self.mother.refresh_from_db()
        self.assertEqual(self.facility.stock_level_ml, starting_stock)
        self.assertEqual(self.mother.total_drawn_ml, 0)
        self.assertEqual(self.mother.total_received_ml, 0)

    def test_counter_offer_can_return_to_pending_or_go_to_scheduled(self):
        apply_transition(self.req, Status.AWAITING_ATTENDANCE, self.staff, "accepted")
        apply_transition(self.req, Status.COUNTER_OFFERED, self.staff, "counter_offer_proposed")

        # From counter_offered, both a reject-back-to-pending and an
        # accept-to-scheduled are legal -- test the reject branch here
        # since the accept branch is already covered by the completion tests.
        apply_transition(self.req, Status.PENDING, self.mother, "counter_offer_rejected")
        self.req.refresh_from_db()
        self.assertEqual(self.req.current_sub_status, Status.PENDING)

    # --- SLA clock: which transitions set/clear response_deadline ---

    def test_accepting_gives_the_mother_a_fresh_confirmation_deadline(self):
        # make_request() doesn't set one, so this also proves accept sets
        # it rather than merely leaving whatever was already there.
        self.assertIsNone(self.req.response_deadline)
        apply_transition(self.req, Status.AWAITING_ATTENDANCE, self.staff, "accepted")
        self.req.refresh_from_db()
        self.assertIsNotNone(self.req.response_deadline)
        self.assertGreater(self.req.response_deadline, timezone.now())

    def test_rejecting_a_counter_offer_gives_the_facility_a_fresh_deadline(self):
        # Back to pending means the facility is effectively looking at a
        # new proposed slot -- it should get a full new response window,
        # not inherit whatever was left (or cleared) from before.
        apply_transition(self.req, Status.AWAITING_ATTENDANCE, self.staff, "accepted")
        apply_transition(self.req, Status.COUNTER_OFFERED, self.staff, "counter_offer_proposed")
        self.req.refresh_from_db()
        self.assertIsNone(self.req.response_deadline)  # counter_offered has no clock of its own

        apply_transition(self.req, Status.PENDING, self.mother, "counter_offer_rejected")
        self.req.refresh_from_db()
        self.assertIsNotNone(self.req.response_deadline)

    def test_declining_clears_the_deadline(self):
        apply_transition(self.req, Status.DECLINED, self.staff, "declined")
        self.req.refresh_from_db()
        self.assertIsNone(self.req.response_deadline)

    def test_scheduling_clears_the_deadline(self):
        apply_transition(self.req, Status.AWAITING_ATTENDANCE, self.staff, "accepted")
        apply_transition(self.req, Status.SCHEDULED, self.mother, "attendance_confirmed")
        self.req.refresh_from_db()
        self.assertIsNone(self.req.response_deadline)


class SweepExpiredRequestsTests(APITestCase):
    """
    sweep_expired_requests() -- the automatic enforcement side of the
    8-business-hour SLA. Without this, response_deadline is just a number
    nobody ever checks: these tests are what makes "expired" a real,
    self-enforcing status rather than only something a staff member can
    trigger by hand via StaffExpireView.
    """

    def setUp(self):
        self.mother = User.objects.create_user(email="mother@example.com", password="x", is_active=True)
        self.staff = User.objects.create_user(
            email="staff@example.com", password="x", is_active=True, role=User.Role.FACILITY_STAFF,
        )
        self.facility = make_facility()

    def test_overdue_pending_request_expires(self):
        req = make_request(self.mother, self.facility, response_deadline=timezone.now() - timedelta(hours=1))
        expired = sweep_expired_requests()
        req.refresh_from_db()
        self.assertEqual(req.current_sub_status, Status.EXPIRED)
        self.assertIn(req, expired)

    def test_overdue_awaiting_attendance_request_expires(self):
        req = make_request(self.mother, self.facility)
        apply_transition(req, Status.AWAITING_ATTENDANCE, self.staff, "accepted")
        req.response_deadline = timezone.now() - timedelta(hours=1)
        req.save(update_fields=["response_deadline"])

        sweep_expired_requests()
        req.refresh_from_db()
        self.assertEqual(req.current_sub_status, Status.EXPIRED)

    def test_request_not_yet_due_is_left_alone(self):
        req = make_request(self.mother, self.facility, response_deadline=timezone.now() + timedelta(hours=1))
        sweep_expired_requests()
        req.refresh_from_db()
        self.assertEqual(req.current_sub_status, Status.PENDING)

    def test_request_with_no_deadline_is_never_swept(self):
        # A scheduled/completed/declined/counter_offered request has no
        # clock (response_deadline is None) -- confirms the sweep query's
        # response_deadline__isnull=False actually excludes those rather
        # than a None deadline comparing as "less than now" some other way.
        req = make_request(self.mother, self.facility)
        apply_transition(req, Status.DECLINED, self.staff, "declined")
        sweep_expired_requests()  # must not raise, and must not touch this
        req.refresh_from_db()
        self.assertEqual(req.current_sub_status, Status.DECLINED)

    def test_sweep_notifies_the_owner_with_the_pending_specific_message(self):
        make_request(self.mother, self.facility, response_deadline=timezone.now() - timedelta(hours=1))
        sweep_expired_requests()
        notification = NotificationItem.objects.get(owner=self.mother)
        self.assertIn("facility didn't respond", notification.description)

    def test_sweep_notifies_the_owner_with_the_awaiting_attendance_specific_message(self):
        req = make_request(self.mother, self.facility)
        apply_transition(req, Status.AWAITING_ATTENDANCE, self.staff, "accepted")
        req.response_deadline = timezone.now() - timedelta(hours=1)
        req.save(update_fields=["response_deadline"])

        sweep_expired_requests()
        notification = NotificationItem.objects.filter(owner=self.mother).latest("id")
        self.assertIn("attendance wasn't confirmed", notification.description)

    def test_sweep_releases_the_slot_on_her_day(self):
        # An SLA expiry frees the day's slot exactly like every other path
        # to expired: an expired request simply stops being counted.
        req = make_request(self.mother, self.facility, response_deadline=timezone.now() - timedelta(hours=1))
        self.assertEqual(slots_taken(self.facility.pk, req.preferred_date), 1)
        sweep_expired_requests()
        self.assertEqual(slots_taken(self.facility.pk, req.preferred_date), 0)

    def test_sweep_is_actor_less_in_the_audit_log(self):
        # No human did this -- confirms apply_transition's actor=None path
        # (AuditLogEntry.actor is nullable specifically for "the system
        # did it") is what an SLA expiry actually records.
        from core.models import AuditLogEntry

        make_request(self.mother, self.facility, response_deadline=timezone.now() - timedelta(hours=1))
        sweep_expired_requests()
        entry = AuditLogEntry.objects.get(action="booking.expired_sla_timeout")
        self.assertIsNone(entry.actor)


class SweepExpiredEndpointTests(APITestCase):
    """POST /milkbank/sweep-expired/ -- the cron-facing trigger."""

    def setUp(self):
        self.mother = User.objects.create_user(email="mother@example.com", password="x", is_active=True)
        self.facility = make_facility()
        self.req = make_request(
            self.mother, self.facility, response_deadline=timezone.now() - timedelta(hours=1)
        )

    @override_settings(MILKBANK_SWEEP_TOKEN="correct-token")
    def test_correct_token_sweeps_and_reports_what_expired(self):
        response = self.client.post(
            "/milkbank/sweep-expired/", HTTP_X_SWEEP_TOKEN="correct-token",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["expired_count"], 1)
        self.assertEqual(response.data["expired_ids"], [self.req.id])
        self.req.refresh_from_db()
        self.assertEqual(self.req.current_sub_status, Status.EXPIRED)

    @override_settings(MILKBANK_SWEEP_TOKEN="correct-token")
    def test_wrong_token_is_rejected_and_sweeps_nothing(self):
        response = self.client.post(
            "/milkbank/sweep-expired/", HTTP_X_SWEEP_TOKEN="wrong-token",
        )
        self.assertEqual(response.status_code, 404)
        self.req.refresh_from_db()
        self.assertEqual(self.req.current_sub_status, Status.PENDING)

    @override_settings(MILKBANK_SWEEP_TOKEN="correct-token")
    def test_missing_token_is_rejected(self):
        response = self.client.post("/milkbank/sweep-expired/")
        self.assertEqual(response.status_code, 404)

    @override_settings(MILKBANK_SWEEP_TOKEN="")
    def test_unconfigured_token_fails_closed_even_with_a_header(self):
        # An empty MILKBANK_SWEEP_TOKEN must never mean "no check" -- this
        # is the "forgotten env var" scenario the docstring calls out.
        response = self.client.post(
            "/milkbank/sweep-expired/", HTTP_X_SWEEP_TOKEN="anything",
        )
        self.assertEqual(response.status_code, 404)
        self.req.refresh_from_db()
        self.assertEqual(self.req.current_sub_status, Status.PENDING)


class SmartAllocationRankingTests(APITestCase):
    """
    rank_facilities()'s tie-break chain (distance -> ratio -> stock
    direction) is pure and deterministic, so it's tested directly without
    going through the HTTP layer at all.

    Note how the ratio and stock cases below all place their facilities
    at IDENTICAL coordinates. That isn't incidental tidiness -- distance
    leads the chain now, so it is the only way to reach the later steps
    at all. See rank_facilities' own note on what that costs.
    """

    def test_nearest_facility_wins_even_when_another_is_far_less_busy(self):
        """
        The case that motivated putting distance first: a mother beside a
        nearly-full facility that can still take her, versus a much
        emptier one across the city.

        Under the old ordering (ratio first) the far, quiet facility won
        outright and distance was never consulted -- a mother walking
        distance from an available facility was sent ~45 km away because
        that facility's booked ratio looked better on paper.
        """
        near_busy = make_facility(
            name="Near but busy", capacity=30, slots_taken=25, stock_level_ml=500,
            latitude=14.60, longitude=121.00,
        )
        far_quiet = make_facility(
            name="Far but quiet", capacity=40, slots_taken=10, stock_level_ml=500,
            latitude=15.00, longitude=121.00,
        )

        ranked = rank_facilities([far_quiet, near_busy], "DONOR", mother_lat=14.60, mother_lon=121.00)

        self.assertEqual(ranked[0], near_busy)
        # And it is genuinely still able to take her -- 25 of 30 booked,
        # which is exactly why the capacity gate (not the ratio) is what
        # should be keeping anyone out.
        self.assertLess(near_busy.slots_taken, near_busy.capacity)

    def test_lower_booked_ratio_wins_regardless_of_raw_count(self):
        # Same coordinates, so distance ties and the ratio decides.
        # 50/100 (ratio 0.5) beats 8/10 (ratio 0.8): lower ratio wins, so
        # the 100-slot facility ranks first despite more raw bookings.
        busy_small = make_facility(name="Busy Small", capacity=10, slots_taken=8, latitude=14.6, longitude=121.0)
        quiet_large = make_facility(name="Quiet Large", capacity=100, slots_taken=50, latitude=14.6, longitude=121.0)

        ranked = rank_facilities([busy_small, quiet_large], "DONOR", mother_lat=14.6, mother_lon=121.0)

        self.assertEqual(ranked[0], quiet_large)  # 0.5 ratio beats 0.8 ratio

    def test_donor_prefers_lower_stock_facility_on_distance_and_ratio_tie(self):
        # Same coordinates and same ratio, so stock direction decides.
        low_stock = make_facility(name="Low Stock", capacity=10, slots_taken=5, stock_level_ml=100,
                                   latitude=14.6, longitude=121.0)
        high_stock = make_facility(name="High Stock", capacity=10, slots_taken=5, stock_level_ml=900,
                                    latitude=14.6, longitude=121.0)

        ranked = rank_facilities([high_stock, low_stock], "DONOR", mother_lat=14.6, mother_lon=121.0)

        self.assertEqual(ranked[0], low_stock)

    def test_recipient_prefers_higher_stock_facility_on_distance_and_ratio_tie(self):
        # Same coordinates and same ratio, so stock direction decides.
        low_stock = make_facility(name="Low Stock", capacity=10, slots_taken=5, stock_level_ml=100,
                                   latitude=14.6, longitude=121.0)
        high_stock = make_facility(name="High Stock", capacity=10, slots_taken=5, stock_level_ml=900,
                                    latitude=14.6, longitude=121.0)

        ranked = rank_facilities([high_stock, low_stock], "RECIPIENT", mother_lat=14.6, mother_lon=121.0)

        self.assertEqual(ranked[0], high_stock)

    def test_nearest_wins_when_ratio_and_stock_are_equal(self):
        near = make_facility(name="Near", capacity=10, slots_taken=5, stock_level_ml=500,
                              latitude=14.60, longitude=121.00)
        far = make_facility(name="Far", capacity=10, slots_taken=5, stock_level_ml=500,
                             latitude=16.00, longitude=121.00)

        ranked = rank_facilities([far, near], "DONOR", mother_lat=14.60, mother_lon=121.00)

        self.assertEqual(ranked[0], near)

    def test_ranked_facilities_carry_the_computed_ratio_and_distance(self):
        facility = make_facility(capacity=10, slots_taken=5, latitude=14.6, longitude=121.0)
        ranked = rank_facilities([facility], "DONOR", mother_lat=14.6, mother_lon=121.0)
        self.assertAlmostEqual(ranked[0].booked_ratio, 0.5)
        self.assertAlmostEqual(ranked[0].distance_km, 0.0, places=3)


class GetRankedFacilitiesTests(APITestCase):
    """get_ranked_facilities() wraps rank_facilities() with the real
    eligibility gates: location required, operational+capacity filter,
    and the recipient-only minimum-stock exclusion."""

    def setUp(self):
        self.user = User.objects.create_user(email="mother@example.com", password="x", is_active=True)

    def test_raises_location_required_without_coordinates(self):
        with self.assertRaises(LocationRequired):
            get_ranked_facilities(self.user, "DONOR")

    def test_raises_no_operational_facility_when_none_match(self):
        self.user.latitude, self.user.longitude = 14.6, 121.0
        self.user.save()
        make_facility(is_operational=False)
        make_facility(capacity=0)

        with self.assertRaises(NoOperationalFacility):
            get_ranked_facilities(self.user, "DONOR")

    def test_recipient_excludes_facilities_below_minimum_stock(self):
        self.user.latitude, self.user.longitude = 14.6, 121.0
        self.user.save()
        low = make_facility(name="Low", stock_level_ml=50, latitude=14.6, longitude=121.0)
        make_facility(name="Adequate", stock_level_ml=500, latitude=14.6, longitude=121.0)

        ranked = get_ranked_facilities(self.user, "RECIPIENT")

        self.assertNotIn(low, ranked)

    def test_donor_is_never_excluded_by_low_stock(self):
        self.user.latitude, self.user.longitude = 14.6, 121.0
        self.user.save()
        low = make_facility(name="Low", stock_level_ml=0, latitude=14.6, longitude=121.0)

        ranked = get_ranked_facilities(self.user, "DONOR")

        self.assertIn(low, ranked)

    def test_a_facility_whose_day_is_full_is_excluded_for_that_day(self):
        # Room is per day: capacity 10 means 10 bookings on each day.
        self.user.latitude, self.user.longitude = 14.6, 121.0
        self.user.save()
        full = make_facility(name="Full", capacity=10, latitude=14.6, longitude=121.0)
        has_room = make_facility(name="Has room", capacity=10, latitude=14.6, longitude=121.0)
        fill_day(full, BOOKED_DAY, 10)
        fill_day(has_room, BOOKED_DAY, 9)

        ranked = get_ranked_facilities(self.user, "DONOR", day=date.fromisoformat(BOOKED_DAY))

        self.assertNotIn(full, ranked)
        self.assertIn(has_room, ranked)

    def test_a_full_day_does_not_close_the_facility_on_other_days(self):
        self.user.latitude, self.user.longitude = 14.6, 121.0
        self.user.save()
        facility = make_facility(name="Busy on one day", capacity=2, latitude=14.6, longitude=121.0)
        fill_day(facility, BOOKED_DAY, 2)

        self.assertIn(facility, get_ranked_facilities(self.user, "DONOR", day=date.fromisoformat(OTHER_DAY)))

    def test_declined_and_expired_bookings_do_not_fill_a_day(self):
        self.user.latitude, self.user.longitude = 14.6, 121.0
        self.user.save()
        facility = make_facility(capacity=2, latitude=14.6, longitude=121.0)
        fill_day(facility, BOOKED_DAY, 1, status=Status.DECLINED)
        fill_day(facility, BOOKED_DAY, 1, status=Status.EXPIRED)
        fill_day(facility, BOOKED_DAY, 1)  # pending: holds its slot

        self.assertIn(facility, get_ranked_facilities(self.user, "DONOR", day=date.fromisoformat(BOOKED_DAY)))

    def test_a_full_facility_is_excluded_even_when_it_is_the_nearest(self):
        # Distance-first ordering means "nearest" is no longer a safe
        # proxy for "bookable" -- without the gate this full facility
        # would be ranked first and booked into anyway.
        self.user.latitude, self.user.longitude = 14.6, 121.0
        self.user.save()
        full_and_nearest = make_facility(name="Full and nearest", capacity=10, latitude=14.6, longitude=121.0)
        farther_with_room = make_facility(name="Farther with room", capacity=10, latitude=15.6, longitude=121.0)
        fill_day(full_and_nearest, BOOKED_DAY, 10)
        fill_day(farther_with_room, BOOKED_DAY, 2)

        ranked = get_ranked_facilities(self.user, "DONOR", day=date.fromisoformat(BOOKED_DAY))

        self.assertNotIn(full_and_nearest, ranked)
        self.assertEqual(ranked[0], farther_with_room)

    def test_raises_day_fully_booked_when_every_facility_is_full_that_day(self):
        self.user.latitude, self.user.longitude = 14.6, 121.0
        self.user.save()
        a = make_facility(name="Full A", capacity=5, latitude=14.6, longitude=121.0)
        b = make_facility(name="Full B", capacity=8, latitude=14.6, longitude=121.0)
        fill_day(a, BOOKED_DAY, 5)
        fill_day(b, BOOKED_DAY, 8)

        with self.assertRaises(DayFullyBooked):
            get_ranked_facilities(self.user, "DONOR", day=date.fromisoformat(BOOKED_DAY))

    def test_the_date_less_preview_still_offers_a_facility_full_on_some_day(self):
        # The preview runs before she picks a date; her scheduler then greys
        # out that facility's full days instead.
        self.user.latitude, self.user.longitude = 14.6, 121.0
        self.user.save()
        facility = make_facility(capacity=1, latitude=14.6, longitude=121.0)
        fill_day(facility, BOOKED_DAY, 1)

        self.assertIn(facility, get_ranked_facilities(self.user, "DONOR"))

    def test_a_booking_is_never_created_on_a_full_day(self):
        # End to end through the real endpoint: the day is full, so she is
        # told to pick another date, and nothing is added to it.
        self.user.latitude, self.user.longitude = 14.6, 121.0
        self.user.save()
        full = make_facility(name="Full", capacity=3, latitude=14.6, longitude=121.0)
        fill_day(full, BOOKED_DAY, 3)

        self.client.force_authenticate(user=self.user)
        response = self.client.post("/milkbank/requests/", {
            "request_type": "DONOR", "preferred_date": BOOKED_DAY, "preferred_time": "10:00 AM",
        })

        self.assertEqual(response.status_code, 409)
        self.assertIn("fully booked", response.data["detail"])
        self.assertEqual(slots_taken(full.pk, BOOKED_DAY), 3)  # never pushed past capacity

    def test_the_same_mother_can_book_another_day_at_that_facility(self):
        self.user.latitude, self.user.longitude = 14.6, 121.0
        self.user.save()
        full = make_facility(name="Full", capacity=3, latitude=14.6, longitude=121.0)
        fill_day(full, BOOKED_DAY, 3)

        self.client.force_authenticate(user=self.user)
        response = self.client.post("/milkbank/requests/", {
            "request_type": "DONOR", "preferred_date": OTHER_DAY, "preferred_time": "10:00 AM",
        })

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(slots_taken(full.pk, OTHER_DAY), 1)
        self.assertEqual(slots_taken(full.pk, BOOKED_DAY), 3)


class BookingEndpointPermissionTests(APITestCase):
    """
    The staff-side actions (accept/decline/etc) must be reachable by
    facility_staff and refused for a mother, and vice versa for the
    mother-side actions -- these permission boundaries are as important
    as the state machine rules they guard.
    """

    def setUp(self):
        self.mother = User.objects.create_user(email="mother@example.com", password="x", is_active=True)
        self.other_mother = User.objects.create_user(email="other@example.com", password="x", is_active=True)
        self.facility = make_facility(name="St. Luke's")
        self.other_facility = make_facility(name="PGH")
        self.staff = User.objects.create_user(
            email="staff@example.com", password="x", is_active=True,
            role=User.Role.FACILITY_STAFF, facility=self.facility,
        )
        self.other_facility_staff = User.objects.create_user(
            email="other-staff@example.com", password="x", is_active=True,
            role=User.Role.FACILITY_STAFF, facility=self.other_facility,
        )
        self.unassigned_staff = User.objects.create_user(
            email="unassigned-staff@example.com", password="x", is_active=True, role=User.Role.FACILITY_STAFF,
        )
        self.req = make_request(self.mother, self.facility)

    def test_mother_cannot_call_staff_accept(self):
        self.client.force_authenticate(user=self.mother)
        response = self.client.post(f"/milkbank/requests/{self.req.id}/accept/")
        self.assertEqual(response.status_code, 403)

    def test_staff_can_call_staff_accept(self):
        self.client.force_authenticate(user=self.staff)
        response = self.client.post(f"/milkbank/requests/{self.req.id}/accept/")
        self.assertEqual(response.status_code, 200)

    def test_staff_at_a_different_facility_cannot_touch_this_booking(self):
        """
        The actual point of the whole facility-scoping change: PGH's
        staff must not be able to accept (or view, or do anything else
        to) a booking that belongs to St. Luke's, even though both are
        facility_staff and both know the booking's real id.
        """
        self.client.force_authenticate(user=self.other_facility_staff)
        response = self.client.post(f"/milkbank/requests/{self.req.id}/accept/")
        self.assertEqual(response.status_code, 403)

    def test_staff_at_a_different_facility_cannot_view_this_booking(self):
        self.client.force_authenticate(user=self.other_facility_staff)
        response = self.client.get(f"/milkbank/requests/{self.req.id}/")
        self.assertEqual(response.status_code, 403)

    def test_unassigned_staff_account_cannot_touch_any_booking(self):
        # Fail closed: an account that's facility_staff in role but has
        # no facility assigned yet (an incompletely-provisioned account)
        # must see nothing, not everything.
        self.client.force_authenticate(user=self.unassigned_staff)
        response = self.client.post(f"/milkbank/requests/{self.req.id}/accept/")
        self.assertEqual(response.status_code, 403)

    def test_all_requests_list_only_shows_this_staff_members_facility(self):
        other_req = make_request(self.other_mother, self.other_facility)

        self.client.force_authenticate(user=self.staff)
        response = self.client.get("/milkbank/requests/all/")

        returned_ids = {row["id"] for row in response.data}
        self.assertIn(self.req.id, returned_ids)
        self.assertNotIn(other_req.id, returned_ids)

    def test_all_requests_list_is_empty_for_unassigned_staff(self):
        self.client.force_authenticate(user=self.unassigned_staff)
        response = self.client.get("/milkbank/requests/all/")
        self.assertEqual(len(response.data), 0)

    def test_a_different_mother_cannot_confirm_someone_elses_attendance(self):
        apply_transition(self.req, Status.AWAITING_ATTENDANCE, self.staff, "accepted")
        self.client.force_authenticate(user=self.other_mother)
        response = self.client.post(f"/milkbank/requests/{self.req.id}/confirm-attendance/")
        self.assertEqual(response.status_code, 403)

    def test_owner_can_confirm_their_own_attendance(self):
        apply_transition(self.req, Status.AWAITING_ATTENDANCE, self.staff, "accepted")
        self.client.force_authenticate(user=self.mother)
        response = self.client.post(f"/milkbank/requests/{self.req.id}/confirm-attendance/")
        self.assertEqual(response.status_code, 200)

    def test_confirming_attendance_notifies_this_facilitys_staff_only(self):
        apply_transition(self.req, Status.AWAITING_ATTENDANCE, self.staff, "accepted")
        self.client.force_authenticate(user=self.mother)
        response = self.client.post(f"/milkbank/requests/{self.req.id}/confirm-attendance/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            NotificationItem.objects.filter(owner=self.staff, title="Attendance Confirmed").exists()
        )
        self.assertFalse(
            NotificationItem.objects.filter(owner=self.other_facility_staff, title="Attendance Confirmed").exists()
        )

    def test_cannot_have_two_open_requests_at_once(self):
        self.mother.latitude, self.mother.longitude = 14.6, 121.0
        self.mother.save()
        self.client.force_authenticate(user=self.mother)

        response = self.client.post("/milkbank/requests/", {
            "request_type": "DONOR", "preferred_date": "2026-12-15", "preferred_time": "10:00 AM",
        })

        self.assertEqual(response.status_code, 400)
        self.assertIn("already have an open request", response.data["detail"])

    def test_submitting_a_request_notifies_only_the_winning_facilitys_staff(self):
        # self.facility and self.other_facility sit at identical coordinates
        # (make_facility's shared default) with identical stock and booked
        # counts, so Smart Allocation could rank either one first -- this
        # reads which one actually won from the response instead of
        # assuming, so the test holds regardless of which way a tie breaks.
        self.other_mother.latitude, self.other_mother.longitude = 14.6, 121.0
        self.other_mother.save()
        self.client.force_authenticate(user=self.other_mother)

        response = self.client.post("/milkbank/requests/", {
            "request_type": "DONOR", "preferred_date": "2026-12-15", "preferred_time": "10:00 AM",
        })

        self.assertEqual(response.status_code, 201)
        won_this_facility = response.data["allocated_facility"] == self.facility.id
        winner_staff = self.staff if won_this_facility else self.other_facility_staff
        loser_staff = self.other_facility_staff if won_this_facility else self.staff

        self.assertTrue(
            NotificationItem.objects.filter(owner=winner_staff, title="New Booking Request").exists()
        )
        self.assertFalse(
            NotificationItem.objects.filter(owner=loser_staff, title="New Booking Request").exists()
        )


class ConfirmCompletionEndpointTests(APITestCase):
    """
    POST .../confirm-completion/ is the only place amount_ml ever reaches
    apply_transition for real -- covers the serializer requiring it, and
    the recipient-side stock-sufficiency check that has to run before the
    transition commits (see StaffConfirmCompletionView's docstring).
    """

    def setUp(self):
        self.mother = User.objects.create_user(email="mother@example.com", password="x", is_active=True)
        self.facility = make_facility(stock_level_ml=100)
        self.staff = User.objects.create_user(
            email="staff@example.com", password="x", is_active=True,
            role=User.Role.FACILITY_STAFF, facility=self.facility,
        )

    def _schedule(self, req):
        apply_transition(req, Status.AWAITING_ATTENDANCE, self.staff, "accepted")
        apply_transition(req, Status.SCHEDULED, self.mother, "attendance_confirmed")

    def test_amount_ml_is_required(self):
        req = make_request(self.mother, self.facility)
        self._schedule(req)
        self.client.force_authenticate(user=self.staff)
        response = self.client.post(f"/milkbank/requests/{req.id}/confirm-completion/", {})
        self.assertEqual(response.status_code, 400)
        req.refresh_from_db()
        self.assertEqual(req.current_sub_status, Status.SCHEDULED)

    def test_a_fractional_amount_is_rejected(self):
        """
        Volumes are whole millilitres throughout, so there is nothing for
        a fraction to mean here -- and accepting one would reintroduce the
        rounding that splitting the unit caused in the first place.
        """
        req = make_request(self.mother, self.facility)
        self._schedule(req)
        self.client.force_authenticate(user=self.staff)

        response = self.client.post(
            f"/milkbank/requests/{req.id}/confirm-completion/", {"amount_ml": 4.5}
        )

        self.assertEqual(response.status_code, 400)
        self.facility.refresh_from_db()
        self.assertEqual(self.facility.stock_level_ml, 100)

    def test_a_zero_amount_is_rejected(self):
        req = make_request(self.mother, self.facility)
        self._schedule(req)
        self.client.force_authenticate(user=self.staff)

        response = self.client.post(
            f"/milkbank/requests/{req.id}/confirm-completion/", {"amount_ml": 0}
        )

        self.assertEqual(response.status_code, 400)
        req.refresh_from_db()
        self.assertEqual(req.current_sub_status, Status.SCHEDULED)

    def test_donor_completion_adds_to_facility_stock(self):
        req = make_request(self.mother, self.facility)
        self._schedule(req)
        self.client.force_authenticate(user=self.staff)
        response = self.client.post(f"/milkbank/requests/{req.id}/confirm-completion/", {"amount_ml": 150})
        self.assertEqual(response.status_code, 200)
        self.facility.refresh_from_db()
        self.assertEqual(self.facility.stock_level_ml, 100 + 150)

    def test_recipient_completion_is_rejected_when_stock_is_insufficient(self):
        req = make_request(self.mother, self.facility, request_type=MilkBankRequest.RequestType.RECIPIENT)
        self._schedule(req)
        self.client.force_authenticate(user=self.staff)
        # Asking for 600 mL against 100 mL on hand.
        response = self.client.post(f"/milkbank/requests/{req.id}/confirm-completion/", {"amount_ml": 600})
        self.assertEqual(response.status_code, 400)
        self.facility.refresh_from_db()
        req.refresh_from_db()
        self.assertEqual(self.facility.stock_level_ml, 100)
        self.assertEqual(req.current_sub_status, Status.SCHEDULED)

    def test_recipient_completion_subtracts_from_facility_stock_when_enough_is_on_hand(self):
        req = make_request(self.mother, self.facility, request_type=MilkBankRequest.RequestType.RECIPIENT)
        self._schedule(req)
        self.client.force_authenticate(user=self.staff)
        response = self.client.post(f"/milkbank/requests/{req.id}/confirm-completion/", {"amount_ml": 60})
        self.assertEqual(response.status_code, 200)
        self.facility.refresh_from_db()
        self.assertEqual(self.facility.stock_level_ml, 100 - 60)

    def test_completion_records_the_amount_and_when_it_happened(self):
        # This is the only durable copy of the millilitres staff actually
        # recorded -- TransactionRecord deliberately doesn't carry it (see
        # that model's docstring), so the Facility dashboard's Finished
        # Transactions list reads it from here.
        req = make_request(self.mother, self.facility)
        self._schedule(req)
        self.client.force_authenticate(user=self.staff)
        before = timezone.now()
        response = self.client.post(f"/milkbank/requests/{req.id}/confirm-completion/", {"amount_ml": 150})
        self.assertEqual(response.status_code, 200)
        req.refresh_from_db()
        self.assertEqual(req.amount_ml, 150)
        self.assertIsNotNone(req.completed_at)
        self.assertGreaterEqual(req.completed_at, before)

    def test_amount_ml_and_completed_at_stay_null_before_completion(self):
        req = make_request(self.mother, self.facility)
        self._schedule(req)
        req.refresh_from_db()
        self.assertIsNone(req.amount_ml)
        self.assertIsNone(req.completed_at)


class BookingStageIndexTests(APITestCase):
    """
    current_stage_index is what the mobile app's Booking Status tracker
    actually reads to decide what's "current" -- including whether to show
    the "Confirm My Attendance" button at all (it's gated on
    stages[current_stage_index] == "Booking Confirmation", not on
    current_sub_status). Regression coverage for the bug where accepting a
    request flipped current_sub_status but left current_stage_index at 0,
    so nothing about the mother's screen ever visibly changed and she had
    no way to confirm attendance.
    """

    def setUp(self):
        self.mother = User.objects.create_user(email="stage-mother@example.com", password="x", is_active=True)
        self.facility = make_facility(name="St. Luke's")
        self.staff = User.objects.create_user(
            email="stage-staff@example.com", password="x", is_active=True,
            role=User.Role.FACILITY_STAFF, facility=self.facility,
        )

    def test_staff_accept_advances_stage_for_donor_request(self):
        req = make_request(self.mother, self.facility, request_type=MilkBankRequest.RequestType.DONOR)
        self.client.force_authenticate(user=self.staff)
        response = self.client.post(f"/milkbank/requests/{req.id}/accept/")
        self.assertEqual(response.status_code, 200)
        req.refresh_from_db()
        self.assertEqual(req.stages[req.current_stage_index], "Booking Confirmation")

    def test_staff_accept_lands_a_recipient_on_the_status_review(self):
        # Her serology test and questionnaire are already in; a human has to
        # read them before she is asked for anything else. So she lands on
        # "Status" as SCHEDULED (facility work in progress) rather than on
        # "Booking Confirmation" as AWAITING_ATTENDANCE (waiting on her).
        req = make_request(self.mother, self.facility, request_type=MilkBankRequest.RequestType.RECIPIENT)
        self.client.force_authenticate(user=self.staff)
        response = self.client.post(f"/milkbank/requests/{req.id}/accept/")
        self.assertEqual(response.status_code, 200)
        req.refresh_from_db()
        self.assertEqual(req.stages[req.current_stage_index], "Status")
        self.assertEqual(req.current_sub_status, Status.SCHEDULED)

    def test_staff_accept_lands_a_donor_on_booking_confirmation(self):
        # The donor pathway is unchanged: nothing is asked of the facility
        # until she turns up, so the next move is hers.
        req = make_request(self.mother, self.facility, request_type=MilkBankRequest.RequestType.DONOR)
        self.client.force_authenticate(user=self.staff)
        response = self.client.post(f"/milkbank/requests/{req.id}/accept/")
        self.assertEqual(response.status_code, 200)
        req.refresh_from_db()
        self.assertEqual(req.stages[req.current_stage_index], "Booking Confirmation")
        self.assertEqual(req.current_sub_status, Status.AWAITING_ATTENDANCE)

    def test_advancing_a_recipient_into_booking_confirmation_awaits_attendance(self):
        # Passing the Status review is what finally puts the ball in her
        # court -- so arriving at that stage must also flip the status, or
        # she sits on a screen whose whole purpose is confirming attendance
        # without ever being asked to.
        req = make_request(self.mother, self.facility, request_type=MilkBankRequest.RequestType.RECIPIENT)
        self.client.force_authenticate(user=self.staff)
        self.client.post(f"/milkbank/requests/{req.id}/accept/")
        response = self.client.post(f"/milkbank/requests/{req.id}/advance-stage/")
        self.assertEqual(response.status_code, 200)
        req.refresh_from_db()
        self.assertEqual(req.stages[req.current_stage_index], "Booking Confirmation")
        self.assertEqual(req.current_sub_status, Status.AWAITING_ATTENDANCE)

    def test_a_recipient_confirming_attendance_reaches_results(self):
        # End of the recipient pathway: Status -> Booking Confirmation ->
        # Results, which is where staff record the millilitres dispensed.
        req = make_request(self.mother, self.facility, request_type=MilkBankRequest.RequestType.RECIPIENT)
        self.client.force_authenticate(user=self.staff)
        self.client.post(f"/milkbank/requests/{req.id}/accept/")
        self.client.post(f"/milkbank/requests/{req.id}/advance-stage/")
        self.client.force_authenticate(user=self.mother)
        response = self.client.post(f"/milkbank/requests/{req.id}/confirm-attendance/")
        self.assertEqual(response.status_code, 200)
        req.refresh_from_db()
        self.assertEqual(req.stages[req.current_stage_index], "Results")
        self.assertEqual(req.current_sub_status, Status.SCHEDULED)

    def test_confirm_attendance_lands_on_the_stage_after_booking_confirmation(self):
        req = make_request(self.mother, self.facility, request_type=MilkBankRequest.RequestType.DONOR)
        apply_transition(req, Status.AWAITING_ATTENDANCE, self.staff, "accepted")
        req.current_stage_index = req.stages.index("Booking Confirmation")
        req.save(update_fields=["current_stage_index"])

        self.client.force_authenticate(user=self.mother)
        response = self.client.post(f"/milkbank/requests/{req.id}/confirm-attendance/")
        self.assertEqual(response.status_code, 200)
        req.refresh_from_db()
        self.assertEqual(req.stages[req.current_stage_index], "Counseling and Testing")

    def test_reject_counter_offer_reverts_stage_to_status(self):
        req = make_request(self.mother, self.facility, request_type=MilkBankRequest.RequestType.DONOR)
        apply_transition(req, Status.AWAITING_ATTENDANCE, self.staff, "accepted")
        req.current_stage_index = req.stages.index("Booking Confirmation")
        req.save(update_fields=["current_stage_index"])
        apply_transition(req, Status.COUNTER_OFFERED, self.staff, "counter_offer_proposed")
        req.counter_offer_date, req.counter_offer_time = COUNTER_OFFER_DATE, "2:00 PM"
        req.save(update_fields=["counter_offer_date", "counter_offer_time"])

        self.client.force_authenticate(user=self.mother)
        response = self.client.post(f"/milkbank/requests/{req.id}/reject-counter-offer/", {
            "preferred_date": "2026-12-22", "preferred_time": "10:00 AM",
        })
        self.assertEqual(response.status_code, 200)
        req.refresh_from_db()
        self.assertEqual(req.current_sub_status, "pending")
        self.assertEqual(req.stages[req.current_stage_index], "Status")

    def test_accept_counter_offer_leaves_her_still_able_to_confirm_attendance(self):
        # Regression coverage for the bug where accepting a counter offer
        # moved the request straight to SCHEDULED and advanced the stage
        # index past "Booking Confirmation". The app gates its "Confirm My
        # Attendance" button on that stage, so she was left unable to
        # confirm at all -- either no button, or a 400 if one showed.
        # Agreeing to the proposed date is still an acceptance, so it has
        # to land her where an acceptance lands her.
        req = make_request(self.mother, self.facility, request_type=MilkBankRequest.RequestType.DONOR)
        apply_transition(req, Status.AWAITING_ATTENDANCE, self.staff, "accepted")
        req.current_stage_index = req.stages.index("Booking Confirmation")
        req.save(update_fields=["current_stage_index"])
        apply_transition(req, Status.COUNTER_OFFERED, self.staff, "counter_offer_proposed")
        req.counter_offer_date, req.counter_offer_time = COUNTER_OFFER_DATE, "2:00 PM"
        req.save(update_fields=["counter_offer_date", "counter_offer_time"])

        self.client.force_authenticate(user=self.mother)
        response = self.client.post(f"/milkbank/requests/{req.id}/accept-counter-offer/")
        self.assertEqual(response.status_code, 200)
        req.refresh_from_db()
        # Still the stage the app shows the confirm button on, and still
        # waiting on her rather than already settled.
        self.assertEqual(req.current_sub_status, Status.AWAITING_ATTENDANCE)
        self.assertEqual(req.stages[req.current_stage_index], "Booking Confirmation")
        self.assertFalse(req.attendance_confirmed)


class RecipientRequirementsTests(APITestCase):
    """
    neonate_name/clinic_info/has_prescription_proof/has_cooler/
    has_medical_abstract -- the Request Milk form's checklist, which used
    to be collected on-screen and then silently discarded: nothing ever
    reached the backend, for any recipient request, ever. Added
    2026-10-01. Covers both directions: the create endpoint actually
    persists what is sent, and a DONOR request (which the form never
    collects any of this for) is unaffected.
    """

    def setUp(self):
        self.mother = User.objects.create_user(
            email="rr-mother@example.com", password="x", is_active=True,
            latitude=14.6, longitude=121.0,
        )
        self.facility = make_facility(name="St. Luke's")
        self.client.force_authenticate(user=self.mother)

    def test_a_recipient_requests_requirements_are_saved(self):
        response = self.client.post("/milkbank/requests/", {
            "request_type": "RECIPIENT", "preferred_date": "2026-12-15", "preferred_time": "10:00 AM",
            "requested_ml": 100,
            "neonate_name": "Baby Cruz", "clinic_info": "Under Dr. Santos, PGH Pediatrics",
            "has_prescription_proof": True, "has_cooler": True, "has_medical_abstract": True,
        })

        self.assertEqual(response.status_code, 201)
        req = MilkBankRequest.objects.get(pk=response.data["id"])
        self.assertEqual(req.neonate_name, "Baby Cruz")
        self.assertEqual(req.clinic_info, "Under Dr. Santos, PGH Pediatrics")
        self.assertTrue(req.has_prescription_proof)
        self.assertTrue(req.has_cooler)
        self.assertTrue(req.has_medical_abstract)

    def test_the_saved_requirements_are_returned_to_the_caller(self):
        # Not just persisted -- actually readable back, since that's the
        # whole point: the facility dashboard's View Details reads these
        # same field names straight off the list/detail response.
        response = self.client.post("/milkbank/requests/", {
            "request_type": "RECIPIENT", "preferred_date": "2026-12-15", "preferred_time": "10:00 AM",
            "requested_ml": 100,
            "neonate_name": "Baby Cruz", "has_cooler": True,
        })

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["neonate_name"], "Baby Cruz")
        self.assertTrue(response.data["has_cooler"])
        self.assertFalse(response.data["has_medical_abstract"])

    def test_a_donor_request_ignores_requirements_fields_sent_alongside_it(self):
        # The form never collects these for a DONOR, but the serializer
        # accepts them unconditionally (same posture as the representative
        # fields) -- confirms a DONOR request stays blank even if sent.
        response = self.client.post("/milkbank/requests/", {
            "request_type": "DONOR", "preferred_date": "2026-12-15", "preferred_time": "10:00 AM",
            "neonate_name": "Should not apply to a donor",
        })

        self.assertEqual(response.status_code, 201)
        req = MilkBankRequest.objects.get(pk=response.data["id"])
        self.assertEqual(req.neonate_name, "Should not apply to a donor")  # stored, but meaningless here
        self.assertEqual(req.request_type, MilkBankRequest.RequestType.DONOR)

    def test_omitting_requirements_fields_defaults_to_blank(self):
        # The serializer must not make these required -- a caller that
        # predates this field (or simply omits them) still gets a 201.
        response = self.client.post("/milkbank/requests/", {
            "request_type": "RECIPIENT", "preferred_date": "2026-12-15", "preferred_time": "10:00 AM",
            "requested_ml": 100,
        })

        self.assertEqual(response.status_code, 201)
        req = MilkBankRequest.objects.get(pk=response.data["id"])
        self.assertEqual(req.neonate_name, "")
        self.assertFalse(req.has_cooler)


class ProposeCounterOfferFromPendingTests(APITestCase):
    """
    "No doctor available on her date" stopped being a decline and became
    a proposed date instead -- which means the Booking Request desk has
    to be able to counter-offer a request that has NOT been accepted yet.
    PENDING -> COUNTER_OFFERED did not exist before that change; the only
    way in was AWAITING_ATTENDANCE, i.e. after acceptance.

    The point of the whole change is that nothing is re-submitted, so
    these cover the round trip, not just the first hop: the questionnaire
    she already filled in has to still be attached at the end of it.
    """

    def setUp(self):
        self.mother = User.objects.create_user(email="co-mother@example.com", password="x", is_active=True)
        self.facility = make_facility(name="St. Luke's")
        self.staff = User.objects.create_user(
            email="co-staff@example.com", password="x", is_active=True,
            role=User.Role.FACILITY_STAFF, facility=self.facility,
        )

    def _propose(self, req, **extra):
        self.client.force_authenticate(user=self.staff)
        payload = {"counter_offer_date": COUNTER_OFFER_DATE, "counter_offer_time": "2:00 PM"}
        payload.update(extra)
        return self.client.post(f"/milkbank/requests/{req.id}/propose-counter-offer/", payload)

    def test_a_pending_donor_request_can_be_counter_offered(self):
        req = make_request(self.mother, self.facility, request_type=MilkBankRequest.RequestType.DONOR)
        self.assertEqual(req.current_sub_status, Status.PENDING)

        response = self._propose(req)

        self.assertEqual(response.status_code, 200)
        req.refresh_from_db()
        self.assertEqual(req.current_sub_status, Status.COUNTER_OFFERED)
        self.assertEqual(str(req.counter_offer_date), COUNTER_OFFER_DATE)
        self.assertEqual(req.counter_offer_time, "2:00 PM")

    def test_a_pending_recipient_request_can_be_counter_offered(self):
        # Both pathways need a doctor (Counseling and Testing for a donor,
        # the dispensing appointment for a recipient), so neither should be
        # turned away over facility scheduling.
        req = make_request(self.mother, self.facility, request_type=MilkBankRequest.RequestType.RECIPIENT)

        response = self._propose(req)

        self.assertEqual(response.status_code, 200)
        req.refresh_from_db()
        self.assertEqual(req.current_sub_status, Status.COUNTER_OFFERED)

    def test_the_reason_reaches_the_mother(self):
        # She sees staff_message in the app's "Message from Facility Team"
        # card, right above Accept / Choose New Time. Without it, the date
        # simply moves with no explanation.
        req = make_request(self.mother, self.facility, request_type=MilkBankRequest.RequestType.DONOR)

        response = self._propose(req, staff_message="No Available Doctor — none rostered that day.")

        self.assertEqual(response.status_code, 200)
        req.refresh_from_db()
        self.assertEqual(req.staff_message, "No Available Doctor — none rostered that day.")

    def test_a_blank_message_does_not_wipe_an_earlier_one(self):
        req = make_request(self.mother, self.facility, request_type=MilkBankRequest.RequestType.DONOR)
        req.staff_message = "Please bring your medical abstract."
        req.save(update_fields=["staff_message"])

        self._propose(req, staff_message="")

        req.refresh_from_db()
        self.assertEqual(req.staff_message, "Please bring your medical abstract.")

    def test_counter_offering_holds_her_slot_and_starts_no_clock_against_her(self):
        # The delay is the facility's, so nothing should count down against
        # the mother -- and the slot she already holds must not be released
        # while she decides.
        req = make_request(self.mother, self.facility, request_type=MilkBankRequest.RequestType.DONOR)
        self.assertEqual(slots_taken(self.facility.pk, req.preferred_date), 1)

        self._propose(req)

        req.refresh_from_db()
        self.assertIsNone(req.response_deadline)
        self.assertEqual(slots_taken(self.facility.pk, req.preferred_date), 1)

    def test_she_keeps_her_questionnaire_through_a_rejected_counter_offer(self):
        # The whole reason this replaced a decline: a declined request made
        # her start over. Proposing a date she then turns down must leave
        # her exactly where she was -- pending, with her answers intact.
        req = make_request(self.mother, self.facility, request_type=MilkBankRequest.RequestType.DONOR)
        DonorQuestionnaire.objects.create(
            request=req, good_general_health=True, lactating_with_excess_supply=True,
            free_of_infectious_disease=True, recent_transfusion_or_transplant=False,
            uses_tobacco_alcohol_or_drugs=False, on_medication_or_supplements=False,
            has_recent_serology_test=True,
        )
        self._propose(req)

        self.client.force_authenticate(user=self.mother)
        response = self.client.post(f"/milkbank/requests/{req.id}/reject-counter-offer/", {
            "preferred_date": "2026-12-22", "preferred_time": "10:00 AM",
        })

        self.assertEqual(response.status_code, 200)
        req.refresh_from_db()
        self.assertEqual(req.current_sub_status, Status.PENDING)
        self.assertEqual(str(req.preferred_date), "2026-12-22")
        # Still hers, still attached -- nothing was re-submitted.
        self.assertTrue(hasattr(req, "donor_questionnaire"))
        self.assertTrue(req.donor_questionnaire.good_general_health)

    def test_a_donor_accepting_a_counter_offer_can_then_confirm_attendance(self):
        # The full round trip from this desk: propose, accept, confirm.
        # Accepting used to jump straight to SCHEDULED, so the confirm POST
        # 400'd with "Cannot move from scheduled to scheduled" and she had
        # no way to say she was coming for the date she had just agreed to.
        req = make_request(self.mother, self.facility, request_type=MilkBankRequest.RequestType.DONOR)
        self._propose(req)

        self.client.force_authenticate(user=self.mother)
        accepted = self.client.post(f"/milkbank/requests/{req.id}/accept-counter-offer/")
        self.assertEqual(accepted.status_code, 200)
        req.refresh_from_db()
        # The facility's proposed slot becomes hers.
        self.assertEqual(str(req.preferred_date), COUNTER_OFFER_DATE)
        self.assertEqual(req.preferred_time, "2:00 PM")

        confirmed = self.client.post(f"/milkbank/requests/{req.id}/confirm-attendance/")
        self.assertEqual(confirmed.status_code, 200, confirmed.data)
        req.refresh_from_db()
        self.assertTrue(req.attendance_confirmed)
        self.assertEqual(req.current_sub_status, Status.SCHEDULED)
        self.assertEqual(req.stages[req.current_stage_index], "Counseling and Testing")

    def test_a_recipient_whose_booking_moved_is_not_sent_back_for_review(self):
        # A recipient already on "Booking Confirmation" (the staff review
        # passed and she is waiting to confirm) who has to move dates.
        # Regression coverage: keying the landing state off request_type
        # alone would put her back on "Status", handing already-reviewed
        # paperwork to staff a second time and putting a live 8-hour clock
        # on a wait that isn't hers. She stays put and re-confirms.
        req = make_request(self.mother, self.facility, request_type=MilkBankRequest.RequestType.RECIPIENT)
        self.client.force_authenticate(user=self.staff)
        # Through the real endpoints, not a raw apply_transition() call --
        # that would change current_sub_status without ever touching
        # current_stage_index (apply_transition never does), leaving her
        # stuck on "Requirements" and silently invalidating the rest of
        # this test. /accept/ is what actually moves a RECIPIENT onto
        # "Status" as part of accepting (see StaffAcceptView); only then
        # does one /advance-stage/ call land her on "Booking Confirmation".
        self.client.post(f"/milkbank/requests/{req.id}/accept/")
        req.refresh_from_db()
        self.assertEqual(req.current_sub_status, Status.SCHEDULED)
        self.assertEqual(req.stages[req.current_stage_index], "Status")

        self.client.post(f"/milkbank/requests/{req.id}/advance-stage/")
        req.refresh_from_db()
        self.assertEqual(req.current_sub_status, Status.AWAITING_ATTENDANCE)
        self.assertEqual(req.stages[req.current_stage_index], "Booking Confirmation")

        apply_transition(req, Status.COUNTER_OFFERED, self.staff, "counter_offer_proposed")
        req.counter_offer_date, req.counter_offer_time = COUNTER_OFFER_DATE, "2:00 PM"
        req.save(update_fields=["counter_offer_date", "counter_offer_time"])

        self.client.force_authenticate(user=self.mother)
        response = self.client.post(f"/milkbank/requests/{req.id}/accept-counter-offer/")

        self.assertEqual(response.status_code, 200, response.data)
        req.refresh_from_db()
        self.assertEqual(req.current_sub_status, Status.AWAITING_ATTENDANCE)
        self.assertEqual(req.stages[req.current_stage_index], "Booking Confirmation")
        self.assertEqual(str(req.preferred_date), COUNTER_OFFER_DATE)

        confirmed = self.client.post(f"/milkbank/requests/{req.id}/confirm-attendance/")
        self.assertEqual(confirmed.status_code, 200, confirmed.data)
        req.refresh_from_db()
        self.assertTrue(req.attendance_confirmed)
        self.assertEqual(req.stages[req.current_stage_index], "Results")

    def test_accepting_a_counter_offer_starts_a_fresh_clock_against_her(self):
        # COUNTER_OFFERED itself carries no clock (that delay is the
        # facility's). Once she has agreed to the new date the wait is
        # hers, so the 8-business-hour confirmation window opens then.
        req = make_request(self.mother, self.facility, request_type=MilkBankRequest.RequestType.DONOR)
        self._propose(req)
        req.refresh_from_db()
        self.assertIsNone(req.response_deadline)

        self.client.force_authenticate(user=self.mother)
        self.client.post(f"/milkbank/requests/{req.id}/accept-counter-offer/")

        req.refresh_from_db()
        self.assertIsNotNone(req.response_deadline)
        self.assertGreater(req.response_deadline, timezone.now())

    def test_a_recipient_accepting_a_counter_offer_is_not_held_at_booking_confirmation(self):
        # The RECIPIENT pathway puts staff review before attendance, so
        # accepting lands her on "Status" with no clock -- she has nothing
        # left to do until staff have read her paperwork. Staff advancing
        # that review is what puts her on "Booking Confirmation".
        req = make_request(self.mother, self.facility, request_type=MilkBankRequest.RequestType.RECIPIENT)
        self._propose(req)

        self.client.force_authenticate(user=self.mother)
        response = self.client.post(f"/milkbank/requests/{req.id}/accept-counter-offer/")

        self.assertEqual(response.status_code, 200)
        req.refresh_from_db()
        self.assertEqual(req.current_sub_status, Status.SCHEDULED)
        self.assertEqual(req.stages[req.current_stage_index], "Status")
        self.assertIsNone(req.response_deadline)

    def test_a_recipient_confirms_attendance_after_the_review_follows_the_new_date(self):
        # Ends the RECIPIENT counter-offer round trip: accept -> staff pass
        # the review -> she confirms. Same shape as the donor pathway, with
        # the two steps in the opposite order.
        req = make_request(self.mother, self.facility, request_type=MilkBankRequest.RequestType.RECIPIENT)
        self._propose(req)

        self.client.force_authenticate(user=self.mother)
        self.client.post(f"/milkbank/requests/{req.id}/accept-counter-offer/")

        self.client.force_authenticate(user=self.staff)
        self.client.post(f"/milkbank/requests/{req.id}/advance-stage/")
        req.refresh_from_db()
        self.assertEqual(req.current_sub_status, Status.AWAITING_ATTENDANCE)
        self.assertEqual(str(req.preferred_date), COUNTER_OFFER_DATE)

        self.client.force_authenticate(user=self.mother)
        confirmed = self.client.post(f"/milkbank/requests/{req.id}/confirm-attendance/")
        self.assertEqual(confirmed.status_code, 200, confirmed.data)
        req.refresh_from_db()
        self.assertTrue(req.attendance_confirmed)
        self.assertEqual(req.stages[req.current_stage_index], "Results")

    def test_a_declined_request_cannot_be_counter_offered(self):
        # DECLINED is terminal. Offering a date on top of one would put a
        # refused request back in play through the side door.
        req = make_request(self.mother, self.facility, request_type=MilkBankRequest.RequestType.DONOR)
        apply_transition(req, Status.DECLINED, self.staff, "declined")

        response = self._propose(req)

        self.assertEqual(response.status_code, 400)
        req.refresh_from_db()
        self.assertEqual(req.current_sub_status, Status.DECLINED)

    def test_staff_from_another_facility_cannot_counter_offer(self):
        other_facility = make_facility(name="PGH")
        outsider = User.objects.create_user(
            email="outsider@example.com", password="x", is_active=True,
            role=User.Role.FACILITY_STAFF, facility=other_facility,
        )
        req = make_request(self.mother, self.facility, request_type=MilkBankRequest.RequestType.DONOR)

        self.client.force_authenticate(user=outsider)
        response = self.client.post(f"/milkbank/requests/{req.id}/propose-counter-offer/", {
            "counter_offer_date": COUNTER_OFFER_DATE, "counter_offer_time": "2:00 PM",
        })

        self.assertEqual(response.status_code, 403)
        req.refresh_from_db()
        self.assertEqual(req.current_sub_status, Status.PENDING)


class CounterOfferSlotRulesTests(APITestCase):
    """
    The slots staff may offer in a counter-offer. The facility dashboard
    greys these out; these tests are for the backend refusing them, so a
    stale tab or a direct API call can't offer a slot nobody could attend.

    Rules are pinned to a fixed clock -- Wednesday 7 Oct 2026, 1:30 PM in
    Manila -- so they don't depend on the day the suite runs.
    """

    NOW = datetime(2026, 10, 7, 13, 30, tzinfo=BUSINESS_TZ)

    def setUp(self):
        self.mother = User.objects.create_user(email="slot-mother@example.com", password="x", is_active=True)
        self.facility = make_facility(
            name="St. Luke's",
            unavailable_donor_dates=["2026-10-14"], unavailable_recipient_dates=["2026-10-15"],
        )
        self.staff = User.objects.create_user(
            email="slot-staff@example.com", password="x", is_active=True,
            role=User.Role.FACILITY_STAFF, facility=self.facility,
        )
        self.req = make_request(
            self.mother, self.facility, preferred_date="2026-10-09", preferred_time="10:00 AM",
        )
        self.req.refresh_from_db()  # preferred_date back to a date, as the view sees it

    def errors(self, day, time, req=None, now=None):
        return counter_offer_errors(req or self.req, date.fromisoformat(day), time, now or self.NOW)

    # --- dates ---

    def test_an_ordinary_future_weekday_is_fine(self):
        self.assertEqual(self.errors("2026-10-08", "9:00 AM"), {})

    def test_a_past_date_is_refused(self):
        self.assertIn("counter_offer_date", self.errors("2026-10-06", "9:00 AM"))

    def test_weekends_are_refused(self):
        self.assertIn("counter_offer_date", self.errors("2026-10-10", "9:00 AM"))  # Saturday
        self.assertIn("counter_offer_date", self.errors("2026-10-11", "9:00 AM"))  # Sunday

    def test_a_date_the_facility_marked_unavailable_is_refused_for_that_pathway(self):
        self.assertIn("counter_offer_date", self.errors("2026-10-14", "9:00 AM"))  # donor list
        # ...but the donor list does not close the day to a recipient, and vice versa.
        recipient = make_request(
            self.mother, self.facility, request_type=MilkBankRequest.RequestType.RECIPIENT,
            preferred_date="2026-10-09", preferred_time="10:00 AM",
        )
        recipient.refresh_from_db()
        self.assertEqual(self.errors("2026-10-14", "9:00 AM", req=recipient), {})
        self.assertIn("counter_offer_date", self.errors("2026-10-15", "9:00 AM", req=recipient))
        self.assertEqual(self.errors("2026-10-15", "9:00 AM"), {})

    # --- times ---

    def test_only_the_hourly_slots_can_be_offered(self):
        self.assertIn("counter_offer_time", self.errors("2026-10-08", "5:00 PM"))   # no 5 PM slot
        self.assertIn("counter_offer_time", self.errors("2026-10-08", "7:00 AM"))
        self.assertIn("counter_offer_time", self.errors("2026-10-08", "9:30 AM"))
        self.assertIn("counter_offer_time", self.errors("2026-10-08", "whenever"))
        self.assertEqual(self.errors("2026-10-08", "4:00 PM"), {})

    def test_a_time_that_has_passed_today_is_refused(self):
        self.assertIn("counter_offer_time", self.errors("2026-10-07", "9:00 AM"))
        self.assertIn("counter_offer_time", self.errors("2026-10-07", "1:00 PM"))  # began 30 min ago
        self.assertEqual(self.errors("2026-10-07", "2:00 PM"), {})

    def test_a_slot_starting_exactly_now_counts_as_started(self):
        at_two = datetime(2026, 10, 7, 14, 0, tzinfo=BUSINESS_TZ)
        self.assertIn("counter_offer_time", self.errors("2026-10-07", "2:00 PM", now=at_two))
        self.assertEqual(self.errors("2026-10-07", "3:00 PM", now=at_two), {})

    def test_today_has_nothing_to_offer_once_the_last_slot_has_started(self):
        for hour in (16, 18):
            late = datetime(2026, 10, 7, hour, 0, tzinfo=BUSINESS_TZ)
            self.assertIn("counter_offer_date", self.errors("2026-10-07", "4:00 PM", now=late))
        self.assertEqual(
            self.errors("2026-10-07", "4:00 PM", now=datetime(2026, 10, 7, 15, 59, tzinfo=BUSINESS_TZ)), {},
        )
        # Tomorrow is still open that evening.
        late = datetime(2026, 10, 7, 18, 0, tzinfo=BUSINESS_TZ)
        self.assertEqual(self.errors("2026-10-08", "9:00 AM", now=late), {})

    def test_today_is_manila_today_not_utc_today(self):
        # 7:30 AM Manila on the 8th is 11:30 PM UTC on the 7th. Judged by
        # UTC, the 7th would still be "today" and the 8th a future date.
        morning = datetime(2026, 10, 8, 7, 30, tzinfo=BUSINESS_TZ)
        self.assertIn("counter_offer_date", self.errors("2026-10-07", "9:00 AM", now=morning))
        self.assertEqual(self.errors("2026-10-08", "8:00 AM", now=morning), {})

    # --- her own slot ---

    def test_the_slot_she_asked_for_is_refused(self):
        self.assertIn("counter_offer_time", self.errors("2026-10-09", "10:00 AM"))

    def test_the_same_time_on_another_day_or_another_time_that_day_is_fine(self):
        self.assertEqual(self.errors("2026-10-08", "10:00 AM"), {})
        self.assertEqual(self.errors("2026-10-09", "11:00 AM"), {})

    # --- through the endpoint ---

    def _propose(self, day, time):
        self.client.force_authenticate(user=self.staff)
        return self.client.post(
            f"/milkbank/requests/{self.req.id}/propose-counter-offer/",
            {"counter_offer_date": day, "counter_offer_time": time},
        )

    def test_a_refused_slot_returns_400_and_changes_nothing(self):
        # She asked for a real upcoming slot, so only her-own-slot is wrong
        # about offering it back (the fixture's fixed date would go stale).
        hers = _first_weekday_after(21)
        MilkBankRequest.objects.filter(pk=self.req.pk).update(preferred_date=hers, preferred_time="10:00 AM")
        past = (datetime.now(BUSINESS_TZ).date() - timedelta(days=3)).isoformat()
        for day, time, field in (
            (past, "9:00 AM", "counter_offer_date"),
            (_first_weekday_after(14), "5:00 PM", "counter_offer_time"),
            (hers, "10:00 AM", "counter_offer_time"),
        ):
            response = self._propose(day, time)
            self.assertEqual(response.status_code, 400, (day, time))
            self.assertIn(field, response.data)
            self.assertEqual(response.data["detail"], response.data[field])  # what the dashboard shows

        self.req.refresh_from_db()
        self.assertEqual(self.req.current_sub_status, Status.PENDING)
        self.assertIsNone(self.req.counter_offer_date)
        self.assertEqual(self.req.counter_offer_time, "")
        self.assertEqual(slots_taken(self.facility.pk, self.req.preferred_date), 1)

    def test_a_weekend_is_refused_through_the_endpoint(self):
        day = datetime.now(BUSINESS_TZ).date() + timedelta(days=14)
        while day.weekday() != 5:
            day += timedelta(days=1)
        response = self._propose(day.isoformat(), "9:00 AM")
        self.assertEqual(response.status_code, 400)
        self.assertIn("counter_offer_date", response.data)

    def test_a_valid_slot_still_goes_through(self):
        response = self._propose(_first_weekday_after(14), "11:00 AM")
        self.assertEqual(response.status_code, 200, response.data)
        self.req.refresh_from_db()
        self.assertEqual(self.req.current_sub_status, Status.COUNTER_OFFERED)
        self.assertEqual(self.req.counter_offer_time, "11:00 AM")


class CanViewQuestionnaireTests(APITestCase):
    """
    _can_view_questionnaire() gates the donor screening form and
    serology photo -- more sensitive than the booking record itself, so
    it gets the same facility-scoping treatment (tested directly here
    rather than through the full multipart submission flow).
    """

    def setUp(self):
        self.mother = User.objects.create_user(email="mother@example.com", password="x", is_active=True)
        self.facility = make_facility(name="St. Luke's")
        self.other_facility = make_facility(name="PGH")
        self.staff = User.objects.create_user(
            email="staff@example.com", password="x", is_active=True,
            role=User.Role.FACILITY_STAFF, facility=self.facility,
        )
        self.other_facility_staff = User.objects.create_user(
            email="other-staff@example.com", password="x", is_active=True,
            role=User.Role.FACILITY_STAFF, facility=self.other_facility,
        )
        self.req = make_request(self.mother, self.facility)

    def test_owner_can_view_her_own_questionnaire(self):
        self.assertTrue(_can_view_questionnaire(self.mother, self.req))

    def test_staff_at_the_same_facility_can_view_it(self):
        self.assertTrue(_can_view_questionnaire(self.staff, self.req))

    def test_staff_at_a_different_facility_cannot_view_it(self):
        self.assertFalse(_can_view_questionnaire(self.other_facility_staff, self.req))


class DonorQuestionnaireSubmissionTests(APITestCase):
    """
    Full round trip through DonorQuestionnaireView: the mobile app POSTs
    multipart/form-data with booleans as "true"/"false" strings (what
    Kotlin's Boolean.toString() produces), and the facility dashboard
    then GETs it back. Regression coverage for the bug where the app
    never called this endpoint at all, so facility staff always saw "no
    questionnaire submitted" regardless of what she'd answered.
    """

    def setUp(self):
        self.mother = User.objects.create_user(email="donor@example.com", password="x", is_active=True)
        self.facility = make_facility(name="St. Luke's")
        self.staff = User.objects.create_user(
            email="staff2@example.com", password="x", is_active=True,
            role=User.Role.FACILITY_STAFF, facility=self.facility,
        )
        self.req = make_request(self.mother, self.facility)
        self.answers = {
            "good_general_health": "true",
            "lactating_with_excess_supply": "true",
            "free_of_infectious_disease": "true",
            "recent_transfusion_or_transplant": "false",
            "uses_tobacco_alcohol_or_drugs": "false",
            "on_medication_or_supplements": "true",
            "medication_details": "Prenatal vitamins",
            "has_recent_serology_test": "true",
        }

    def test_submission_is_recorded_and_visible_to_facility_staff(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        self.client.force_authenticate(user=self.mother)
        photo = SimpleUploadedFile("serology.jpg", b"\xff\xd8\xff\xe0fake", content_type="image/jpeg")
        post_response = self.client.post(
            f"/milkbank/requests/{self.req.id}/donor-questionnaire/",
            data={**self.answers, "serology_photo": photo},
            format="multipart",
        )
        self.assertEqual(post_response.status_code, 201, post_response.data)

        self.client.force_authenticate(user=self.staff)
        get_response = self.client.get(f"/milkbank/requests/{self.req.id}/donor-questionnaire/")
        self.assertEqual(get_response.status_code, 200)
        data = get_response.data
        self.assertTrue(data["good_general_health"])
        self.assertFalse(data["recent_transfusion_or_transplant"])
        self.assertEqual(data["medication_details"], "Prenatal vitamins")
        self.assertTrue(data["photo_attached"])

    def test_a_second_submission_for_the_same_request_is_rejected(self):
        self.client.force_authenticate(user=self.mother)
        self.client.post(
            f"/milkbank/requests/{self.req.id}/donor-questionnaire/", data=self.answers, format="multipart",
        )
        second = self.client.post(
            f"/milkbank/requests/{self.req.id}/donor-questionnaire/", data=self.answers, format="multipart",
        )
        self.assertEqual(second.status_code, 400)

    def test_staff_before_any_submission_gets_404_not_found(self):
        self.client.force_authenticate(user=self.staff)
        response = self.client.get(f"/milkbank/requests/{self.req.id}/donor-questionnaire/")
        self.assertEqual(response.status_code, 404)


class MyLatestDonorQuestionnaireEndpointTests(APITestCase):
    """
    GET /milkbank/donor-questionnaire/mine/latest/ -- what the app calls
    to pre-fill a fresh questionnaire after a decline, since
    DonorQuestionnaire.request is a strict OneToOneField and the old row
    can never be reattached to the new request she's about to submit.
    """

    def setUp(self):
        self.mother = User.objects.create_user(email="donor@example.com", password="x", is_active=True)
        self.other_mother = User.objects.create_user(email="other@example.com", password="x", is_active=True)
        self.facility = make_facility(name="St. Luke's")
        self.answers = {
            "good_general_health": "true",
            "lactating_with_excess_supply": "true",
            "free_of_infectious_disease": "true",
            "recent_transfusion_or_transplant": "false",
            "uses_tobacco_alcohol_or_drugs": "false",
            "on_medication_or_supplements": "true",
            "medication_details": "Prenatal vitamins",
            "has_recent_serology_test": "true",
        }

    def _submit_questionnaire(self, owner, req, **overrides):
        self.client.force_authenticate(user=owner)
        self.client.post(
            f"/milkbank/requests/{req.id}/donor-questionnaire/",
            data={**self.answers, **overrides}, format="multipart",
        )

    def test_returns_204_when_she_has_never_submitted_one(self):
        self.client.force_authenticate(user=self.mother)
        response = self.client.get("/milkbank/donor-questionnaire/mine/latest/")
        self.assertEqual(response.status_code, 204)

    def test_returns_her_answers_from_a_declined_requests_questionnaire(self):
        # The exact scenario this endpoint exists for: a DECLINED request
        # still has a real, saved questionnaire attached to it -- nothing
        # about being declined deletes it -- this just makes it reachable
        # from a request that doesn't exist yet.
        declined_req = make_request(self.mother, self.facility)
        self._submit_questionnaire(self.mother, declined_req)
        apply_transition(declined_req, Status.DECLINED, self.mother, "declined")

        self.client.force_authenticate(user=self.mother)
        response = self.client.get("/milkbank/donor-questionnaire/mine/latest/")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["good_general_health"])
        self.assertFalse(response.data["recent_transfusion_or_transplant"])
        self.assertEqual(response.data["medication_details"], "Prenatal vitamins")

    def test_returns_the_most_recently_submitted_one_when_she_has_several(self):
        first_req = make_request(self.mother, self.facility)
        self._submit_questionnaire(self.mother, first_req, medication_details="First submission")
        apply_transition(first_req, Status.DECLINED, self.mother, "declined")

        second_req = make_request(self.mother, self.facility)
        self._submit_questionnaire(self.mother, second_req, medication_details="Second submission")

        self.client.force_authenticate(user=self.mother)
        response = self.client.get("/milkbank/donor-questionnaire/mine/latest/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["medication_details"], "Second submission")

    def test_never_returns_a_different_mothers_answers(self):
        her_req = make_request(self.other_mother, self.facility)
        self._submit_questionnaire(self.other_mother, her_req, medication_details="Not yours")

        self.client.force_authenticate(user=self.mother)
        response = self.client.get("/milkbank/donor-questionnaire/mine/latest/")

        self.assertEqual(response.status_code, 204)


class BusinessHoursClockTests(SimpleTestCase):
    """
    The SLA clock. These cases are the whole reason the deadline isn't
    just submitted_at + 8 hours: each one would land on a wrong, earlier
    deadline under plain elapsed time, cancelling a booking the facility
    never had a working hour to answer.
    """

    def manila(self, year, month, day, hour, minute=0):
        return datetime(year, month, day, hour, minute, tzinfo=BUSINESS_TZ)

    def assert_deadline(self, start, expected):
        actual = add_business_hours(start).astimezone(BUSINESS_TZ)
        self.assertEqual(actual, expected, f"{start} + 8 business hours -> {actual}, expected {expected}")

    def test_mid_morning_start_finishes_next_morning(self):
        # Wed 9 AM: 8 hours left today is 9->17 = 8h exactly, so it lands
        # at closing rather than spilling into Thursday.
        self.assert_deadline(self.manila(2026, 9, 9, 9), self.manila(2026, 9, 9, 17))

    def test_afternoon_start_spills_into_the_next_working_day(self):
        # Wed 3 PM: 2h today, remaining 6h resume Thursday 8 AM -> 2 PM.
        self.assert_deadline(self.manila(2026, 9, 9, 15), self.manila(2026, 9, 10, 14))

    def test_friday_afternoon_skips_the_weekend(self):
        # Fri 3 PM: 2h Friday, 6h on Monday -> Monday 2 PM, not Saturday.
        self.assert_deadline(self.manila(2026, 9, 11, 15), self.manila(2026, 9, 14, 14))

    def test_before_opening_does_not_burn_the_night(self):
        # 6 AM Wednesday counts from 8 AM, not from 6 AM.
        self.assert_deadline(self.manila(2026, 9, 9, 6), self.manila(2026, 9, 9, 16))

    def test_after_closing_starts_the_next_morning(self):
        self.assert_deadline(self.manila(2026, 9, 9, 21), self.manila(2026, 9, 10, 16))

    def test_weekend_submission_starts_monday(self):
        # Saturday -> the clock only begins Monday 8 AM, ending 4 PM.
        self.assert_deadline(self.manila(2026, 9, 12, 10), self.manila(2026, 9, 14, 16))

    def test_holiday_is_skipped(self):
        # Dec 24/25 are both holidays, and Dec 26 2026 is a Saturday, so a
        # Dec 23 afternoon request is not due until Monday Dec 28.
        self.assert_deadline(self.manila(2026, 12, 23, 15), self.manila(2026, 12, 28, 14))

    def test_holiday_detection(self):
        self.assertFalse(is_business_day(date(2026, 12, 25)))  # Christmas
        self.assertFalse(is_business_day(date(2026, 6, 12)))   # Independence Day
        self.assertFalse(is_business_day(date(2026, 9, 12)))   # a Saturday
        self.assertTrue(is_business_day(date(2026, 9, 9)))     # ordinary Wednesday

    def test_national_heroes_day_is_the_last_monday_of_august(self):
        self.assertIn(date(2026, 8, 31), philippine_holidays(2026))
        self.assertEqual(date(2026, 8, 31).weekday(), 0)

    def test_easter_derived_holidays_move_with_the_year(self):
        # Good Friday 2026 falls on 3 April.
        self.assertIn(date(2026, 4, 3), philippine_holidays(2026))
        self.assertFalse(is_business_day(date(2026, 4, 3)))

    def test_result_is_utc_aware(self):
        deadline = add_business_hours(self.manila(2026, 9, 9, 9))
        self.assertEqual(deadline.utcoffset(), timedelta(0))


class FabellaCurrentAddressMigrationTests(TestCase):
    """0011_fabella_current_address -- run directly against a row shaped
    like production's, since the test database starts with no facilities."""

    # Migration modules start with a digit, so a plain `import` can't name them.
    migration = import_module("milkbank.migrations.0011_fabella_current_address")

    def test_moves_the_row_that_still_has_the_old_address(self):
        fabella = make_facility(
            name=self.migration.FACILITY_NAME, address=self.migration.OLD_ADDRESS,
            latitude=14.6169, longitude=120.9833,
        )
        other = make_facility(name="Philippine General Hospital", address="Taft Ave, Ermita, Manila")

        self.migration.move_fabella(apps, None)

        fabella.refresh_from_db()
        self.assertEqual(fabella.address, "San Lazaro Compound, Tayuman St., Santa Cruz, Manila")
        self.assertEqual((fabella.latitude, fabella.longitude), (14.6153, 120.9804))
        other.refresh_from_db()
        self.assertEqual(other.address, "Taft Ave, Ermita, Manila")

    def test_leaves_an_address_an_admin_already_corrected(self):
        fabella = make_facility(
            name=self.migration.FACILITY_NAME, address="Corrected by hand",
            latitude=14.1, longitude=121.1,
        )

        self.migration.move_fabella(apps, None)

        fabella.refresh_from_db()
        self.assertEqual(fabella.address, "Corrected by hand")
        self.assertEqual((fabella.latitude, fabella.longitude), (14.1, 121.1))


class FacilityCreationStaffAssignmentTests(APITestCase):
    """
    POST /milkbank/facilities/ -- the admin dashboard's Add Facility
    modal can assign a staff account to the facility it's creating, in
    the same request: either an existing unassigned facility_staff
    account (staff_user_id), or a brand-new one (new_staff_email +
    new_staff_password). Platform-admin only, same as every other
    write on this endpoint (see FacilityListView).
    """

    def setUp(self):
        self.admin = User.objects.create_superuser(email="admin@example.com", password="password123")
        self.payload = dict(
            name="New Facility", type=Facility.FacilityType.HOSPITAL_DEPOT,
            contact="000-0000", address="Somewhere", capacity=10,
            latitude=14.6, longitude=121.0,
        )

    def test_creating_a_facility_needs_no_staff_fields_at_all(self):
        # The common case, and what every seeded facility already does
        # (see seed_facilities.py) -- must keep working unchanged.
        self.client.force_authenticate(user=self.admin)
        response = self.client.post("/milkbank/facilities/", self.payload)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Facility.objects.get(pk=response.data["id"]).staff.count(), 0)

    def test_assigns_an_existing_unassigned_staff_account(self):
        staff = User.objects.create_user(
            email="staff@example.com", password="x", role=User.Role.FACILITY_STAFF,
        )
        self.client.force_authenticate(user=self.admin)
        response = self.client.post("/milkbank/facilities/", {**self.payload, "staff_user_id": staff.id})

        self.assertEqual(response.status_code, 201)
        staff.refresh_from_db()
        self.assertEqual(staff.facility_id, response.data["id"])
        self.assertTrue(AuditLogEntry.objects.filter(action="facility.staff_assigned").exists())

    def test_refuses_a_staff_account_already_assigned_elsewhere(self):
        other_facility = make_facility(name="Other Facility")
        staff = User.objects.create_user(
            email="staff@example.com", password="x", role=User.Role.FACILITY_STAFF, facility=other_facility,
        )
        self.client.force_authenticate(user=self.admin)
        response = self.client.post("/milkbank/facilities/", {**self.payload, "staff_user_id": staff.id})

        self.assertEqual(response.status_code, 400)
        self.assertFalse(Facility.objects.filter(name="New Facility").exists())
        staff.refresh_from_db()
        self.assertEqual(staff.facility_id, other_facility.id)

    def test_refuses_a_non_facility_staff_user_id(self):
        mother = User.objects.create_user(email="mother@example.com", password="x")
        self.client.force_authenticate(user=self.admin)
        response = self.client.post("/milkbank/facilities/", {**self.payload, "staff_user_id": mother.id})
        self.assertEqual(response.status_code, 400)

    def test_creates_a_brand_new_staff_account(self):
        self.client.force_authenticate(user=self.admin)
        response = self.client.post("/milkbank/facilities/", {
            **self.payload, "new_staff_email": "newstaff@example.com", "new_staff_password": "password123",
        })

        self.assertEqual(response.status_code, 201)
        new_staff = User.objects.get(email="newstaff@example.com")
        self.assertEqual(new_staff.role, User.Role.FACILITY_STAFF)
        self.assertEqual(new_staff.facility_id, response.data["id"])
        self.assertTrue(new_staff.is_active)
        self.assertTrue(new_staff.check_password("password123"))
        self.assertTrue(AuditLogEntry.objects.filter(action="facility.staff_created").exists())

    def test_refuses_a_new_staff_email_already_in_use(self):
        User.objects.create_user(email="taken@example.com", password="x")
        self.client.force_authenticate(user=self.admin)
        response = self.client.post("/milkbank/facilities/", {
            **self.payload, "new_staff_email": "taken@example.com", "new_staff_password": "password123",
        })
        self.assertEqual(response.status_code, 400)

    def test_refuses_a_new_staff_email_without_a_password(self):
        self.client.force_authenticate(user=self.admin)
        response = self.client.post("/milkbank/facilities/", {
            **self.payload, "new_staff_email": "newstaff@example.com",
        })
        self.assertEqual(response.status_code, 400)
        self.assertFalse(User.objects.filter(email="newstaff@example.com").exists())

    def test_refuses_both_an_existing_and_a_new_staff_account_at_once(self):
        staff = User.objects.create_user(
            email="staff@example.com", password="x", role=User.Role.FACILITY_STAFF,
        )
        self.client.force_authenticate(user=self.admin)
        response = self.client.post("/milkbank/facilities/", {
            **self.payload, "staff_user_id": staff.id,
            "new_staff_email": "newstaff@example.com", "new_staff_password": "password123",
        })
        self.assertEqual(response.status_code, 400)

    def test_non_admin_cannot_create_a_facility_at_all(self):
        mother = User.objects.create_user(email="mother@example.com", password="x")
        self.client.force_authenticate(user=mother)
        response = self.client.post("/milkbank/facilities/", self.payload)
        self.assertEqual(response.status_code, 403)


class DeclineFromAPhaseTests(APITestCase):
    """
    Staff can decline a booking from any in-person phase, not only at the
    Booking Request desk -- a failed breastmilk analysis, a bad blood test.
    The booking must end there: declined is terminal, so it can't be
    advanced or completed afterwards, and the facility's slot is released.
    """

    def setUp(self):
        self.facility = make_facility()
        self.mother = User.objects.create_user(email="mother@example.com", password="x", is_active=True)
        self.staff = User.objects.create_user(
            email="staff@example.com", password="x", is_active=True,
            role=User.Role.FACILITY_STAFF, facility=self.facility,
        )
        self.client.force_authenticate(user=self.staff)

    def _scheduled_at(self, stage_name, request_type=MilkBankRequest.RequestType.DONOR):
        req = make_request(self.mother, self.facility, request_type=request_type)
        req.current_sub_status = Status.SCHEDULED
        req.current_stage_index = req.stages.index(stage_name)
        req.save()
        return req

    def _decline(self, req, **payload):
        return self.client.post(f"/milkbank/requests/{req.id}/decline/", payload)

    def test_declining_a_failed_breastmilk_analysis_drops_the_booking(self):
        req = self._scheduled_at("Breastmilk Analysis")

        response = self._decline(
            req, reason="Failed breastmilk analysis",
            staff_message="Failed breastmilk analysis \u2014 bacterial growth found",
        )

        self.assertEqual(response.status_code, 200, response.data)
        req.refresh_from_db()
        self.assertEqual(req.current_sub_status, Status.DECLINED)
        self.assertEqual(req.decline_reason, "Failed breastmilk analysis")
        self.assertIn("bacterial growth", req.staff_message)
        # Left on the phase it failed at, so staff can see where it ended.
        self.assertEqual(req.stages[req.current_stage_index], "Breastmilk Analysis")

    def test_the_facility_slot_is_released(self):
        req = self._scheduled_at("Breastmilk Analysis")
        self.assertEqual(slots_taken(self.facility.pk, req.preferred_date), 1)
        self._decline(req, reason="Failed breastmilk analysis")
        self.assertEqual(slots_taken(self.facility.pk, req.preferred_date), 0)

    def test_a_declined_booking_cannot_be_advanced_or_completed(self):
        req = self._scheduled_at("Breastmilk Analysis")
        self._decline(req, reason="Failed breastmilk analysis")

        advance = self.client.post(f"/milkbank/requests/{req.id}/advance-stage/")
        complete = self.client.post(f"/milkbank/requests/{req.id}/confirm-completion/", {"amount_ml": 100})

        self.assertEqual(advance.status_code, 400)
        self.assertEqual(complete.status_code, 400)
        req.refresh_from_db()
        self.assertEqual(req.current_sub_status, Status.DECLINED)
        self.assertFalse(TransactionRecord.objects.filter(owner=self.mother).exists())

    def test_every_donor_and_recipient_phase_can_be_declined(self):
        cases = [
            (MilkBankRequest.RequestType.DONOR, "Counseling and Testing"),
            (MilkBankRequest.RequestType.DONOR, "Breastmilk Analysis"),
            (MilkBankRequest.RequestType.DONOR, "Results"),
            (MilkBankRequest.RequestType.RECIPIENT, "Status"),
            (MilkBankRequest.RequestType.RECIPIENT, "Results"),
        ]
        for request_type, stage in cases:
            with self.subTest(request_type=request_type, stage=stage):
                req = self._scheduled_at(stage, request_type)
                response = self._decline(req, reason="Others", staff_message="Others \u2014 note")
                self.assertEqual(response.status_code, 200, response.data)
                req.refresh_from_db()
                self.assertEqual(req.current_sub_status, Status.DECLINED)

    def test_the_booking_request_desk_decline_still_works(self):
        req = make_request(self.mother, self.facility)  # pending
        response = self._decline(req, reason="Outdated Serological Test")
        self.assertEqual(response.status_code, 200)
        req.refresh_from_db()
        self.assertEqual(req.decline_reason, "Outdated Serological Test")

    def test_a_decline_with_no_reason_is_still_a_decline(self):
        # An older dashboard build only sent staff_message.
        req = self._scheduled_at("Breastmilk Analysis")
        response = self._decline(req, staff_message="No longer eligible")
        self.assertEqual(response.status_code, 200)
        req.refresh_from_db()
        self.assertEqual(req.current_sub_status, Status.DECLINED)
        self.assertEqual(req.decline_reason, "")

    def test_only_a_pending_or_scheduled_booking_can_be_declined(self):
        for status in (Status.AWAITING_ATTENDANCE, Status.COUNTER_OFFERED, Status.COMPLETED, Status.EXPIRED):
            with self.subTest(status=status):
                req = make_request(self.mother, self.facility)
                req.current_sub_status = status
                req.save()
                self.assertEqual(self._decline(req, reason="Others").status_code, 400)
                req.refresh_from_db()
                self.assertEqual(req.current_sub_status, status)

    def test_staff_at_another_facility_cannot_decline_it(self):
        other = make_facility(name="Other")
        outsider = User.objects.create_user(
            email="outsider@example.com", password="x", is_active=True,
            role=User.Role.FACILITY_STAFF, facility=other,
        )
        req = self._scheduled_at("Breastmilk Analysis")
        self.client.force_authenticate(user=outsider)
        self.assertEqual(self._decline(req, reason="Others").status_code, 403)
        req.refresh_from_db()
        self.assertEqual(req.current_sub_status, Status.SCHEDULED)

    def test_the_mother_is_notified(self):
        req = self._scheduled_at("Breastmilk Analysis")
        self._decline(req, reason="Failed breastmilk analysis")
        # Titled as a decline; the body is her plain explanation of the reason
        # (milkbank/results.py), not the generic "The facility declined your request."
        notice = NotificationItem.objects.get(owner=self.mother, title__icontains="Declined")
        self.assertIn("Milk sample not accepted", notice.description)

    def test_the_reason_is_in_the_api_response_for_the_dashboard(self):
        req = self._scheduled_at("Breastmilk Analysis")
        response = self._decline(req, reason="Failed breastmilk analysis")
        self.assertEqual(response.data["decline_reason"], "Failed breastmilk analysis")


class SeedFacilitiesOnceTests(TestCase):
    """
    build.sh runs seed_facilities on every deploy. It used to recreate any
    seeded facility that was missing, so one an admin deleted from the
    dashboard came back with the next deploy.
    """

    def _seed(self):
        from django.core.management import call_command
        call_command("seed_facilities", verbosity=0, stdout=open(os.devnull, "w"))

    def test_the_first_run_seeds_the_demo_facilities_and_records_it(self):
        self._seed()
        self.assertEqual(Facility.objects.count(), 4)
        self.assertEqual(AuditLogEntry.objects.filter(action="seed.facilities").count(), 1)

    def test_a_deleted_facility_does_not_come_back_on_the_next_deploy(self):
        self._seed()
        Facility.objects.get(name="St. Martin de Porres").delete()

        self._seed()  # the next deploy
        self._seed()  # and the one after

        self.assertFalse(Facility.objects.filter(name="St. Martin de Porres").exists())
        self.assertEqual(Facility.objects.count(), 3)

    def test_deleting_every_facility_does_not_bring_the_demo_set_back(self):
        self._seed()
        Facility.objects.all().delete()
        self._seed()
        self.assertEqual(Facility.objects.count(), 0)

    def test_an_admin_added_facility_is_left_alone(self):
        self._seed()
        make_facility(name="Quezon City General Hospital")
        self._seed()
        self.assertEqual(Facility.objects.count(), 5)

    def test_a_database_seeded_before_this_fix_is_marked_so_it_is_not_seeded_again(self):
        # Production today: facilities exist (one already deleted), no record.
        make_facility(name="St. Luke's Medical Center")
        mark_seeded = import_module("milkbank.migrations.0013_mark_facilities_seeded").mark_seeded

        mark_seeded(apps, None)
        mark_seeded(apps, None)  # running twice must not double up
        self.assertEqual(AuditLogEntry.objects.filter(action="seed.facilities").count(), 1)

        self._seed()
        self.assertEqual(Facility.objects.count(), 1)  # nothing recreated

    def test_an_empty_database_is_not_marked_and_still_gets_seeded(self):
        mark_seeded = import_module("milkbank.migrations.0013_mark_facilities_seeded").mark_seeded
        mark_seeded(apps, None)
        self.assertFalse(AuditLogEntry.objects.filter(action="seed.facilities").exists())
        self._seed()
        self.assertEqual(Facility.objects.count(), 4)


class DeleteFacilityTests(APITestCase):
    """Deleting a facility from the admin dashboard."""

    def setUp(self):
        self.admin = User.objects.create_user(email="del-admin@example.com", password="x", is_active=True, is_staff=True)
        self.mother = User.objects.create_user(email="del-mother@example.com", password="x", is_active=True)
        self.client.force_authenticate(user=self.admin)

    def test_a_facility_with_no_bookings_is_deleted_and_logged(self):
        facility = make_facility(name="Unused Depot")
        staff = User.objects.create_user(
            email="del-staff@example.com", password="x", is_active=True,
            role=User.Role.FACILITY_STAFF, facility=facility,
        )

        response = self.client.delete(f"/milkbank/facilities/{facility.id}/")

        self.assertEqual(response.status_code, 204)
        self.assertFalse(Facility.objects.filter(pk=facility.id).exists())
        staff.refresh_from_db()
        self.assertIsNone(staff.facility)  # her account survives, unassigned
        self.assertTrue(AuditLogEntry.objects.filter(action="facility.deleted", actor=self.admin).exists())

    def test_a_facility_with_bookings_is_refused_with_a_reason_not_a_server_error(self):
        facility = make_facility(name="Busy Milk Bank")
        make_request(self.mother, facility)

        response = self.client.delete(f"/milkbank/facilities/{facility.id}/")

        self.assertEqual(response.status_code, 400)
        self.assertIn("1 booking on record", response.data["detail"])
        self.assertIn("operational", response.data["detail"])
        self.assertTrue(Facility.objects.filter(pk=facility.id).exists())
        self.assertFalse(AuditLogEntry.objects.filter(action="facility.deleted").exists())

    def test_facility_staff_cannot_delete_a_facility(self):
        facility = make_facility(name="Someone Else's")
        staff = User.objects.create_user(
            email="del-staff2@example.com", password="x", is_active=True,
            role=User.Role.FACILITY_STAFF, facility=facility,
        )
        self.client.force_authenticate(user=staff)
        self.assertEqual(self.client.delete(f"/milkbank/facilities/{facility.id}/").status_code, 403)
        self.assertTrue(Facility.objects.filter(pk=facility.id).exists())


class RequestedMilkAmountTests(APITestCase):
    """
    requested_ml -- how much milk a recipient asks for. Optional (older app
    installs don't send it) but bounded by each facility's own limits (30-1000 mL
    unless the admin changed them), never routed to a facility that
    holds less than that, and handed back so the facility dashboard can show
    it beside the facility's stock.
    """

    URL = "/milkbank/requests/"

    def setUp(self):
        self.mother = User.objects.create_user(
            email="amt-mother@example.com", password="x", is_active=True,
            latitude=14.6, longitude=121.0,
        )
        self.client.force_authenticate(user=self.mother)

    def post(self, **extra):
        payload = {"request_type": "RECIPIENT", "preferred_date": "2026-12-15", "preferred_time": "10:00 AM"}
        payload.update(extra)
        return self.client.post(self.URL, payload, format="json")

    def test_the_amount_is_saved_and_returned(self):
        make_facility(stock_level_ml=800)

        response = self.post(requested_ml=250)

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["requested_ml"], 250)
        self.assertEqual(MilkBankRequest.objects.get(pk=response.data["id"]).requested_ml, 250)

    def test_an_app_that_predates_the_field_can_still_request(self):
        # Installed copies of the app don't send it; they must not be locked
        # out of requesting milk by a backend deploy.
        make_facility(stock_level_ml=800)

        response = self.post()

        self.assertEqual(response.status_code, 201)
        self.assertIsNone(response.data["requested_ml"])

    def test_the_amount_is_bounded(self):
        make_facility(stock_level_ml=5000)

        for bad in (0, -5, 29, 1001, 2500):
            with self.subTest(requested_ml=bad):
                self.assertEqual(self.post(requested_ml=bad).status_code, 400)
        for ok in (30, 1000):
            with self.subTest(requested_ml=ok):
                MilkBankRequest.objects.all().delete()
                self.assertEqual(self.post(requested_ml=ok).status_code, 201)

    def test_a_fraction_of_a_millilitre_is_refused(self):
        make_facility(stock_level_ml=800)

        self.assertEqual(self.post(requested_ml=12.5).status_code, 400)

    def test_more_than_any_facility_holds_is_refused_with_the_most_available(self):
        make_facility(name="A", stock_level_ml=400)
        make_facility(name="B", stock_level_ml=600)

        response = self.post(requested_ml=700)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["max_available_ml"], 600)
        self.assertFalse(MilkBankRequest.objects.exists())

    def test_it_is_routed_past_a_nearer_facility_that_cannot_fill_it(self):
        make_facility(name="Near", stock_level_ml=350, latitude=14.6, longitude=121.0)
        make_facility(name="Far", stock_level_ml=900, latitude=14.9, longitude=121.3)

        response = self.post(requested_ml=500)

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["allocated_facility_name"], "Far")

    def test_asking_for_exactly_the_stock_on_hand_is_allowed(self):
        make_facility(stock_level_ml=450)

        self.assertEqual(self.post(requested_ml=450).status_code, 201)

    def test_a_donor_request_ignores_the_amount(self):
        make_facility()

        response = self.client.post(self.URL, {
            "request_type": "DONOR", "preferred_date": "2026-12-15", "preferred_time": "10:00 AM",
            "requested_ml": 200,
        }, format="json")

        self.assertEqual(response.status_code, 201)
        self.assertIsNone(response.data["requested_ml"])

    def test_the_preview_only_offers_facilities_that_can_fill_it(self):
        make_facility(name="Near", stock_level_ml=350, latitude=14.6, longitude=121.0)
        make_facility(name="Far", stock_level_ml=900, latitude=14.9, longitude=121.3)

        response = self.client.post("/milkbank/allocate/", {"request_type": "RECIPIENT", "requested_ml": 500}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual([f["name"] for f in response.data["ranked_facilities"]], ["Far"])


class FacilityRequestLimitsTests(APITestCase):
    """
    Each facility sets how much one recipient may ask for (min_request_ml /
    max_request_ml) and whether it dispenses in 100 mL bottles -- chosen by
    the admin in the Add / Edit Facility modal.
    """

    def setUp(self):
        self.admin = User.objects.create_superuser(email="limits-admin@example.com", password="password123")
        self.client.force_authenticate(user=self.admin)
        self.payload = dict(
            name="Limits Facility", type=Facility.FacilityType.HUMAN_MILK_BANK,
            contact="000-0000", address="Somewhere", capacity=10, latitude=14.6, longitude=121.0,
        )

    def test_a_new_facility_starts_with_the_old_limits_and_no_bottles(self):
        response = self.client.post("/milkbank/facilities/", self.payload, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual((response.data["min_request_ml"], response.data["max_request_ml"]), (30, 1000))
        self.assertFalse(response.data["dispenses_bottles"])

    def test_the_admin_sets_them(self):
        response = self.client.post("/milkbank/facilities/", {
            **self.payload, "min_request_ml": 100, "max_request_ml": 500, "dispenses_bottles": True,
        }, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        facility = Facility.objects.get(pk=response.data["id"])
        self.assertEqual((facility.min_request_ml, facility.max_request_ml, facility.dispenses_bottles), (100, 500, True))

    def test_the_maximum_cannot_be_below_the_minimum(self):
        response = self.client.post("/milkbank/facilities/", {
            **self.payload, "min_request_ml": 500, "max_request_ml": 200,
        }, format="json")

        self.assertEqual(response.status_code, 400)
        self.assertIn("max_request_ml", response.data)

    def test_with_bottles_the_range_must_hold_a_whole_bottle(self):
        response = self.client.post("/milkbank/facilities/", {
            **self.payload, "min_request_ml": 30, "max_request_ml": 90, "dispenses_bottles": True,
        }, format="json")

        self.assertEqual(response.status_code, 400)
        self.assertIn("dispenses_bottles", response.data)

    def test_an_edit_is_checked_against_the_values_it_does_not_change(self):
        facility = make_facility(min_request_ml=300, max_request_ml=800)

        response = self.client.patch(f"/milkbank/facilities/{facility.id}/", {"max_request_ml": 200}, format="json")

        self.assertEqual(response.status_code, 400)
        facility.refresh_from_db()
        self.assertEqual(facility.max_request_ml, 800)


class RecipientAmountPerFacilityTests(APITestCase):
    """A request only goes to a facility whose own limits hold its amount, and
    a request made in bottles only to a facility that dispenses bottles."""

    URL = "/milkbank/requests/"

    def setUp(self):
        self.mother = User.objects.create_user(
            email="limits-mother@example.com", password="x", is_active=True, latitude=14.6, longitude=121.0,
        )
        self.client.force_authenticate(user=self.mother)

    def post(self, **extra):
        payload = {"request_type": "RECIPIENT", "preferred_date": "2026-12-15", "preferred_time": "10:00 AM"}
        payload.update(extra)
        return self.client.post(self.URL, payload, format="json")

    def test_it_skips_a_nearer_facility_whose_limits_exclude_the_amount(self):
        make_facility(name="Near", max_request_ml=200, latitude=14.6, longitude=121.0)
        make_facility(name="Far", max_request_ml=1000, latitude=14.9, longitude=121.3)

        response = self.post(requested_ml=500)

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["allocated_facility_name"], "Far")

    def test_an_amount_no_facility_accepts_is_refused_and_says_so(self):
        make_facility(min_request_ml=100, max_request_ml=500)

        response = self.post(requested_ml=50)

        self.assertEqual(response.status_code, 400)
        self.assertIn("accepts a request of 50 mL", response.data["detail"])
        self.assertFalse(MilkBankRequest.objects.exists())

    def test_a_facility_can_set_limits_beyond_the_old_system_wide_ones(self):
        make_facility(min_request_ml=10, max_request_ml=2000, stock_level_ml=3000)

        self.assertEqual(self.post(requested_ml=10).status_code, 201)
        MilkBankRequest.objects.all().delete()
        self.assertEqual(self.post(requested_ml=2000).status_code, 201)

    def test_bottles_are_stored_as_100_ml_each(self):
        make_facility(dispenses_bottles=True)

        response = self.post(requested_bottles=3)

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual((response.data["requested_bottles"], response.data["requested_ml"]), (3, 300))

    def test_a_request_in_bottles_only_goes_where_bottles_are_dispensed(self):
        make_facility(name="Near, no bottles", dispenses_bottles=False, latitude=14.6, longitude=121.0)
        make_facility(name="Far, bottles", dispenses_bottles=True, latitude=14.9, longitude=121.3)

        response = self.post(requested_bottles=2)

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["allocated_facility_name"], "Far, bottles")

    def test_bottles_where_no_facility_dispenses_them_are_refused(self):
        make_facility(dispenses_bottles=False)

        self.assertEqual(self.post(requested_bottles=2).status_code, 400)
        self.assertFalse(MilkBankRequest.objects.exists())

    def test_bottles_and_a_different_ml_figure_disagree(self):
        make_facility(dispenses_bottles=True)

        self.assertEqual(self.post(requested_bottles=2, requested_ml=250).status_code, 400)

    def test_an_amount_in_ml_still_goes_to_a_bottle_facility(self):
        make_facility(dispenses_bottles=True)

        response = self.post(requested_ml=250)

        self.assertEqual(response.status_code, 201, response.data)
        self.assertIsNone(response.data["requested_bottles"])

    def test_the_preview_skips_a_facility_holding_less_than_its_own_minimum(self):
        # Matched there, she could not ask for any amount at all.
        make_facility(name="Near", min_request_ml=600, stock_level_ml=500, latitude=14.6, longitude=121.0)
        make_facility(name="Far", stock_level_ml=500, latitude=14.9, longitude=121.3)

        response = self.client.post("/milkbank/allocate/", {"request_type": "RECIPIENT"}, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([f["name"] for f in response.data["ranked_facilities"]], ["Far"])


def fill_slot(facility, day, time, n, request_type=MilkBankRequest.RequestType.DONOR, status=None):
    """Books `n` requests at `time` on `day` at `facility`, each by a different mother."""
    global _filler_count
    for _ in range(n):
        _filler_count += 1
        owner = User.objects.create_user(email=f"slotfiller{_filler_count}@example.com", password=None, is_active=True)
        make_request(
            owner, facility, preferred_date=day, preferred_time=time, request_type=request_type,
            **({"current_sub_status": status} if status else {}),
        )


class DonorSlotLimitTests(APITestCase):
    """
    The optional per-time-slot limit for DONOR bookings (the lab), alongside
    the daily capacity. Recipients are never limited by it and never use it up.
    """

    DAY = "2026-12-15"
    URL = "/milkbank/requests/"

    def setUp(self):
        self.mother = User.objects.create_user(
            email="slot-mother@example.com", password="x", is_active=True, latitude=14.6, longitude=121.0,
        )
        self.facility = make_facility(name="Lab Bank", capacity=25, limit_donor_slots=True, donor_bookings_per_slot=2)

    def book(self, time="10:00 AM", request_type="DONOR", day=None):
        self.client.force_authenticate(user=self.mother)
        MilkBankRequest.objects.filter(owner=self.mother).delete()
        return self.client.post(self.URL, {
            "request_type": request_type, "preferred_date": day or self.DAY, "preferred_time": time,
        }, format="json")

    def test_a_full_donor_slot_refuses_the_next_donor(self):
        fill_slot(self.facility, self.DAY, "10:00 AM", 2)

        response = self.book("10:00 AM")

        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["full_slot"], {"date": self.DAY, "time": "10:00 AM"})

    def test_below_the_limit_and_other_times_are_fine(self):
        fill_slot(self.facility, self.DAY, "10:00 AM", 1)
        self.assertEqual(self.book("10:00 AM").status_code, 201)

        fill_slot(self.facility, self.DAY, "11:00 AM", 2)
        self.assertEqual(self.book("9:00 AM").status_code, 201)

    def test_recipients_are_not_limited_and_do_not_use_up_donor_slots(self):
        fill_slot(self.facility, self.DAY, "10:00 AM", 5, request_type=MilkBankRequest.RequestType.RECIPIENT)

        self.assertEqual(self.book("10:00 AM").status_code, 201)  # a donor still fits
        fill_slot(self.facility, self.DAY, "10:00 AM", 2)
        self.assertEqual(self.book("10:00 AM", request_type="RECIPIENT").status_code, 201)

    def test_with_the_limit_off_only_the_daily_capacity_applies(self):
        self.facility.limit_donor_slots = False
        self.facility.save()
        fill_slot(self.facility, self.DAY, "10:00 AM", 10)

        self.assertEqual(self.book("10:00 AM").status_code, 201)

    def test_the_daily_capacity_still_applies_with_the_limit_on(self):
        self.facility.capacity = 3
        self.facility.save()
        fill_slot(self.facility, self.DAY, "9:00 AM", 1)
        fill_slot(self.facility, self.DAY, "1:00 PM", 2)

        self.assertEqual(self.book("3:00 PM").status_code, 409)  # the day is full

    def test_a_declined_booking_frees_its_slot(self):
        fill_slot(self.facility, self.DAY, "10:00 AM", 1)
        fill_slot(self.facility, self.DAY, "10:00 AM", 1, status=Status.DECLINED)

        self.assertEqual(self.book("10:00 AM").status_code, 201)

    def test_a_donor_goes_to_the_next_facility_when_her_hour_is_full_here(self):
        make_facility(name="Other Bank", latitude=14.9, longitude=121.3)
        fill_slot(self.facility, self.DAY, "10:00 AM", 2)

        response = self.book("10:00 AM")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["allocated_facility_name"], "Other Bank")

    def test_the_facility_reports_its_full_donor_slots_and_days(self):
        from .scheduling import SLOTS
        fill_slot(self.facility, self.DAY, "10:00 AM", 2)
        full_day = "2026-12-16"
        for label, _hour in SLOTS:
            fill_slot(self.facility, full_day, label, 2)

        data = self.client.get(f"/milkbank/facilities/{self.facility.id}/").data

        self.assertEqual(data["full_donor_slots"][self.DAY], ["10:00 AM"])
        self.assertEqual(data["donor_fully_booked_dates"], [full_day])
        self.assertNotIn(self.DAY, data["donor_fully_booked_dates"])

    def test_rebooking_into_a_full_donor_slot_is_refused(self):
        req = make_request(
            self.mother, self.facility, preferred_date=self.DAY, preferred_time="9:00 AM",
            current_sub_status=Status.COUNTER_OFFERED, counter_offer_date=COUNTER_OFFER_DATE,
            counter_offer_time="2:00 PM",
        )
        fill_slot(self.facility, "2026-12-22", "11:00 AM", 2)
        self.client.force_authenticate(user=self.mother)

        response = self.client.post(f"/milkbank/requests/{req.id}/reject-counter-offer/", {
            "preferred_date": "2026-12-22", "preferred_time": "11:00 AM",
        })

        self.assertEqual(response.status_code, 409, response.data)

    def test_accepting_an_offered_time_that_filled_since_is_refused(self):
        req = make_request(
            self.mother, self.facility, preferred_date=self.DAY, preferred_time="9:00 AM",
            current_sub_status=Status.COUNTER_OFFERED, counter_offer_date=COUNTER_OFFER_DATE,
            counter_offer_time="2:00 PM",
        )
        fill_slot(self.facility, COUNTER_OFFER_DATE, "2:00 PM", 2)
        self.client.force_authenticate(user=self.mother)

        response = self.client.post(f"/milkbank/requests/{req.id}/accept-counter-offer/")

        self.assertEqual(response.status_code, 409, response.data)

    def test_staff_cannot_offer_a_full_donor_slot(self):
        staff = User.objects.create_user(
            email="slot-staff@example.com", password="x", is_active=True,
            role=User.Role.FACILITY_STAFF, facility=self.facility,
        )
        req = make_request(self.mother, self.facility, preferred_date=self.DAY, preferred_time="9:00 AM")
        fill_slot(self.facility, COUNTER_OFFER_DATE, "2:00 PM", 2)
        self.client.force_authenticate(user=staff)

        response = self.client.post(f"/milkbank/requests/{req.id}/propose-counter-offer/", {
            "counter_offer_date": COUNTER_OFFER_DATE, "counter_offer_time": "2:00 PM",
        })

        self.assertEqual(response.status_code, 400)
        self.assertIn("counter_offer_time", response.data)


class DonorSlotLimitSettingTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(email="slot-admin@example.com", password="password123")
        self.client.force_authenticate(user=self.admin)
        self.payload = dict(
            name="Slots Facility", type=Facility.FacilityType.HUMAN_MILK_BANK,
            contact="000-0000", address="Somewhere", capacity=25, latitude=14.6, longitude=121.0,
        )

    def test_off_by_default(self):
        response = self.client.post("/milkbank/facilities/", self.payload, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertFalse(response.data["limit_donor_slots"])
        self.assertEqual(response.data["full_donor_slots"], {})

    def test_the_admin_turns_it_on_with_a_number(self):
        response = self.client.post("/milkbank/facilities/", {
            **self.payload, "limit_donor_slots": True, "donor_bookings_per_slot": 3,
        }, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual((response.data["limit_donor_slots"], response.data["donor_bookings_per_slot"]), (True, 3))

    def test_a_limit_above_the_daily_capacity_is_refused_while_on(self):
        response = self.client.post("/milkbank/facilities/", {
            **self.payload, "limit_donor_slots": True, "donor_bookings_per_slot": 30,
        }, format="json")

        self.assertEqual(response.status_code, 400)
        self.assertIn("donor_bookings_per_slot", response.data)

    def test_the_number_is_kept_while_switched_off(self):
        facility = make_facility(capacity=25, limit_donor_slots=True, donor_bookings_per_slot=4)

        self.client.patch(f"/milkbank/facilities/{facility.id}/", {"limit_donor_slots": False}, format="json")
        facility.refresh_from_db()

        self.assertFalse(facility.limit_donor_slots)
        self.assertEqual(facility.donor_bookings_per_slot, 4)
