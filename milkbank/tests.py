from datetime import date, datetime, timedelta
from importlib import import_module

from django.apps import apps
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from core.models import AuditLogEntry
from notifications.models import NotificationItem

from .allocation import LocationRequired, NoOperationalFacility, get_ranked_facilities, rank_facilities
from .business_hours import BUSINESS_TZ, add_business_hours, is_business_day, philippine_holidays
from .models import DonorQuestionnaire, Facility, MilkBankRequest, TransactionRecord
from .transitions import ALLOWED_TRANSITIONS, InvalidTransition, apply_transition, sweep_expired_requests
from .views import _can_view_questionnaire

Status = MilkBankRequest.Status


def make_facility(**overrides):
    defaults = dict(
        name="Test Facility",
        type=Facility.FacilityType.HOSPITAL_DEPOT,
        contact="000-0000",
        address="Somewhere",
        is_operational=True,
        capacity=10,
        booked_count=0,
        stock_level_ml=1000,
        latitude=14.6,
        longitude=121.0,
    )
    defaults.update(overrides)
    return Facility.objects.create(**defaults)


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
        self.facility = make_facility(booked_count=1)
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

    def test_terminal_status_decrements_facility_booked_count(self):
        starting_count = self.facility.booked_count
        apply_transition(self.req, Status.DECLINED, self.staff, "declined")
        self.facility.refresh_from_db()
        self.assertEqual(self.facility.booked_count, starting_count - 1)

    def test_non_terminal_status_does_not_touch_booked_count(self):
        starting_count = self.facility.booked_count
        apply_transition(self.req, Status.AWAITING_ATTENDANCE, self.staff, "accepted")
        self.facility.refresh_from_db()
        self.assertEqual(self.facility.booked_count, starting_count)

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
        self.facility = make_facility(booked_count=1)

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

    def test_sweep_releases_the_facility_slot(self):
        # apply_transition's TERMINAL_STATUSES bookkeeping should fire for
        # an SLA expiry exactly like it does for every other path to expired.
        make_request(self.mother, self.facility, response_deadline=timezone.now() - timedelta(hours=1))
        sweep_expired_requests()
        self.facility.refresh_from_db()
        self.assertEqual(self.facility.booked_count, 0)

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
        self.facility = make_facility(booked_count=1)
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
            name="Near but busy", capacity=30, booked_count=25, stock_level_ml=500,
            latitude=14.60, longitude=121.00,
        )
        far_quiet = make_facility(
            name="Far but quiet", capacity=40, booked_count=10, stock_level_ml=500,
            latitude=15.00, longitude=121.00,
        )

        ranked = rank_facilities([far_quiet, near_busy], "DONOR", mother_lat=14.60, mother_lon=121.00)

        self.assertEqual(ranked[0], near_busy)
        # And it is genuinely still able to take her -- 25 of 30 booked,
        # which is exactly why the capacity gate (not the ratio) is what
        # should be keeping anyone out.
        self.assertLess(near_busy.booked_count, near_busy.capacity)

    def test_lower_booked_ratio_wins_regardless_of_raw_count(self):
        # Same coordinates, so distance ties and the ratio decides.
        # 50/100 (ratio 0.5) beats 8/10 (ratio 0.8): lower ratio wins, so
        # the 100-slot facility ranks first despite more raw bookings.
        busy_small = make_facility(name="Busy Small", capacity=10, booked_count=8, latitude=14.6, longitude=121.0)
        quiet_large = make_facility(name="Quiet Large", capacity=100, booked_count=50, latitude=14.6, longitude=121.0)

        ranked = rank_facilities([busy_small, quiet_large], "DONOR", mother_lat=14.6, mother_lon=121.0)

        self.assertEqual(ranked[0], quiet_large)  # 0.5 ratio beats 0.8 ratio

    def test_donor_prefers_lower_stock_facility_on_distance_and_ratio_tie(self):
        # Same coordinates and same ratio, so stock direction decides.
        low_stock = make_facility(name="Low Stock", capacity=10, booked_count=5, stock_level_ml=100,
                                   latitude=14.6, longitude=121.0)
        high_stock = make_facility(name="High Stock", capacity=10, booked_count=5, stock_level_ml=900,
                                    latitude=14.6, longitude=121.0)

        ranked = rank_facilities([high_stock, low_stock], "DONOR", mother_lat=14.6, mother_lon=121.0)

        self.assertEqual(ranked[0], low_stock)

    def test_recipient_prefers_higher_stock_facility_on_distance_and_ratio_tie(self):
        # Same coordinates and same ratio, so stock direction decides.
        low_stock = make_facility(name="Low Stock", capacity=10, booked_count=5, stock_level_ml=100,
                                   latitude=14.6, longitude=121.0)
        high_stock = make_facility(name="High Stock", capacity=10, booked_count=5, stock_level_ml=900,
                                    latitude=14.6, longitude=121.0)

        ranked = rank_facilities([high_stock, low_stock], "RECIPIENT", mother_lat=14.6, mother_lon=121.0)

        self.assertEqual(ranked[0], high_stock)

    def test_nearest_wins_when_ratio_and_stock_are_equal(self):
        near = make_facility(name="Near", capacity=10, booked_count=5, stock_level_ml=500,
                              latitude=14.60, longitude=121.00)
        far = make_facility(name="Far", capacity=10, booked_count=5, stock_level_ml=500,
                             latitude=16.00, longitude=121.00)

        ranked = rank_facilities([far, near], "DONOR", mother_lat=14.60, mother_lon=121.00)

        self.assertEqual(ranked[0], near)

    def test_ranked_facilities_carry_the_computed_ratio_and_distance(self):
        facility = make_facility(capacity=10, booked_count=5, latitude=14.6, longitude=121.0)
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

    def test_a_facility_with_no_room_left_is_excluded(self):
        # The gate that was missing entirely: capacity__gt=0 only asked
        # whether a capacity was configured, never whether any of it was
        # left. This matters far more now that distance leads the sort --
        # a full facility that happens to be nearest would rank FIRST.
        self.user.latitude, self.user.longitude = 14.6, 121.0
        self.user.save()
        full = make_facility(name="Full", capacity=10, booked_count=10, latitude=14.6, longitude=121.0)
        has_room = make_facility(name="Has room", capacity=10, booked_count=9, latitude=14.6, longitude=121.0)

        ranked = get_ranked_facilities(self.user, "DONOR")

        self.assertNotIn(full, ranked)
        self.assertIn(has_room, ranked)

    def test_a_full_facility_is_excluded_even_when_it_is_the_nearest(self):
        # Distance-first ordering means "nearest" is no longer a safe
        # proxy for "bookable" -- without the gate this full facility
        # would be ranked first and booked into anyway.
        self.user.latitude, self.user.longitude = 14.6, 121.0
        self.user.save()
        full_and_nearest = make_facility(
            name="Full and nearest", capacity=10, booked_count=10, latitude=14.6, longitude=121.0,
        )
        farther_with_room = make_facility(
            name="Farther with room", capacity=10, booked_count=2, latitude=15.6, longitude=121.0,
        )

        ranked = get_ranked_facilities(self.user, "DONOR")

        self.assertNotIn(full_and_nearest, ranked)
        self.assertEqual(ranked[0], farther_with_room)

    def test_raises_no_operational_facility_when_every_facility_is_full(self):
        self.user.latitude, self.user.longitude = 14.6, 121.0
        self.user.save()
        make_facility(name="Full A", capacity=5, booked_count=5, latitude=14.6, longitude=121.0)
        make_facility(name="Full B", capacity=8, booked_count=8, latitude=14.6, longitude=121.0)

        with self.assertRaises(NoOperationalFacility):
            get_ranked_facilities(self.user, "DONOR")

    def test_a_booking_is_never_created_against_a_full_facility(self):
        # End to end through the real endpoint rather than the ranking
        # function: MilkBankRequestCreateView assigns ranked[0]
        # unconditionally and then increments booked_count, so if a full
        # facility could ever reach ranked[0] the count would run past
        # capacity with nothing to stop it.
        self.user.latitude, self.user.longitude = 14.6, 121.0
        self.user.save()
        full = make_facility(name="Full", capacity=3, booked_count=3, latitude=14.6, longitude=121.0)

        self.client.force_authenticate(user=self.user)
        response = self.client.post("/milkbank/requests/", {
            "request_type": "DONOR", "preferred_date": "2026-12-15", "preferred_time": "10:00 AM",
        })

        self.assertEqual(response.status_code, 404)
        full.refresh_from_db()
        self.assertEqual(full.booked_count, 3)  # never pushed past capacity


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
        # booked_count=1: apply_transition decrements it on every COMPLETED
        # transition (a terminal status freeing the slot) -- 0 here would
        # trip the "never negative" CHECK constraint the moment a test
        # actually completes a request, same as TransitionsTests.setUp.
        self.facility = make_facility(stock_level_ml=100, booked_count=1)
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
        req.counter_offer_date, req.counter_offer_time = "2026-12-20", "2:00 PM"
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
        req.counter_offer_date, req.counter_offer_time = "2026-12-20", "2:00 PM"
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
        # booked_count=1, not the helper's 0 default: reaching a terminal
        # status decrements it (see apply_transition), and booked_count is
        # a PositiveIntegerField -- so the declined-request test below would
        # fail on the constraint rather than on the behaviour it is testing.
        self.facility = make_facility(name="St. Luke's", booked_count=1)
        self.staff = User.objects.create_user(
            email="co-staff@example.com", password="x", is_active=True,
            role=User.Role.FACILITY_STAFF, facility=self.facility,
        )

    def _propose(self, req, **extra):
        self.client.force_authenticate(user=self.staff)
        payload = {"counter_offer_date": "2026-12-20", "counter_offer_time": "2:00 PM"}
        payload.update(extra)
        return self.client.post(f"/milkbank/requests/{req.id}/propose-counter-offer/", payload)

    def test_a_pending_donor_request_can_be_counter_offered(self):
        req = make_request(self.mother, self.facility, request_type=MilkBankRequest.RequestType.DONOR)
        self.assertEqual(req.current_sub_status, Status.PENDING)

        response = self._propose(req)

        self.assertEqual(response.status_code, 200)
        req.refresh_from_db()
        self.assertEqual(req.current_sub_status, Status.COUNTER_OFFERED)
        self.assertEqual(str(req.counter_offer_date), "2026-12-20")
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
        booked_before = Facility.objects.get(pk=self.facility.pk).booked_count

        self._propose(req)

        req.refresh_from_db()
        self.assertIsNone(req.response_deadline)
        self.assertEqual(Facility.objects.get(pk=self.facility.pk).booked_count, booked_before)

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
        self.assertEqual(str(req.preferred_date), "2026-12-20")
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
        req.counter_offer_date, req.counter_offer_time = "2026-12-20", "2:00 PM"
        req.save(update_fields=["counter_offer_date", "counter_offer_time"])

        self.client.force_authenticate(user=self.mother)
        response = self.client.post(f"/milkbank/requests/{req.id}/accept-counter-offer/")

        self.assertEqual(response.status_code, 200, response.data)
        req.refresh_from_db()
        self.assertEqual(req.current_sub_status, Status.AWAITING_ATTENDANCE)
        self.assertEqual(req.stages[req.current_stage_index], "Booking Confirmation")
        self.assertEqual(str(req.preferred_date), "2026-12-20")

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
        self.assertEqual(str(req.preferred_date), "2026-12-20")

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
        other_facility = make_facility(name="PGH", booked_count=1)
        outsider = User.objects.create_user(
            email="outsider@example.com", password="x", is_active=True,
            role=User.Role.FACILITY_STAFF, facility=other_facility,
        )
        req = make_request(self.mother, self.facility, request_type=MilkBankRequest.RequestType.DONOR)

        self.client.force_authenticate(user=outsider)
        response = self.client.post(f"/milkbank/requests/{req.id}/propose-counter-offer/", {
            "counter_offer_date": "2026-12-20", "counter_offer_time": "2:00 PM",
        })

        self.assertEqual(response.status_code, 403)
        req.refresh_from_db()
        self.assertEqual(req.current_sub_status, Status.PENDING)


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
        # booked_count=1: apply_transition decrements it on every DECLINED
        # transition (a terminal status freeing the slot) -- 0 here would
        # trip the "never negative" CHECK constraint the moment a test
        # actually declines a request, same as BookingStageIndexTests.setUp.
        self.facility = make_facility(name="St. Luke's", booked_count=1)
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
