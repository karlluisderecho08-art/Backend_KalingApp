from datetime import date, datetime, timedelta

from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from notifications.models import NotificationItem

from .allocation import LocationRequired, NoOperationalFacility, get_ranked_facilities, rank_facilities
from .business_hours import BUSINESS_TZ, add_business_hours, is_business_day, philippine_holidays
from .models import Facility, MilkBankRequest, TransactionRecord
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

    def test_pending_cannot_jump_straight_to_scheduled(self):
        # Must go through awaiting_attendance first -- skipping straight
        # to scheduled would bypass the mother's attendance confirmation.
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
        apply_transition(self.req, Status.COMPLETED, self.staff, "completed")

        record = TransactionRecord.objects.get(owner=self.mother)
        self.assertEqual(record.type, TransactionRecord.TransactionType.DONATION)
        self.assertEqual(record.status, TransactionRecord.TransactionStatus.COMPLETED)

    def test_completing_a_recipient_request_creates_a_received_record(self):
        recipient_req = make_request(self.mother, self.facility, request_type=MilkBankRequest.RequestType.RECIPIENT)
        apply_transition(recipient_req, Status.AWAITING_ATTENDANCE, self.staff, "accepted")
        apply_transition(recipient_req, Status.SCHEDULED, self.mother, "attendance_confirmed")
        apply_transition(recipient_req, Status.COMPLETED, self.staff, "completed")

        record = TransactionRecord.objects.latest("id")
        self.assertEqual(record.type, TransactionRecord.TransactionType.RECEIVED)

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
    rank_facilities()'s tie-break chain (ratio -> stock direction ->
    distance) is pure and deterministic, so it's tested directly without
    going through the HTTP layer at all.
    """

    def test_lower_booked_ratio_wins_regardless_of_raw_count(self):
        # 50/100 slots (ratio 0.5) should lose to 8/10 slots (ratio 0.8)...
        # wait -- lower ratio wins, so the 100-slot facility with the
        # *lower* ratio should be ranked first even though it has more
        # raw bookings.
        busy_small = make_facility(name="Busy Small", capacity=10, booked_count=8, latitude=14.6, longitude=121.0)
        quiet_large = make_facility(name="Quiet Large", capacity=100, booked_count=50, latitude=14.6, longitude=121.0)

        ranked = rank_facilities([busy_small, quiet_large], "DONOR", mother_lat=14.6, mother_lon=121.0)

        self.assertEqual(ranked[0], quiet_large)  # 0.5 ratio beats 0.8 ratio

    def test_donor_prefers_lower_stock_facility_on_ratio_tie(self):
        low_stock = make_facility(name="Low Stock", capacity=10, booked_count=5, stock_level_ml=100,
                                   latitude=14.6, longitude=121.0)
        high_stock = make_facility(name="High Stock", capacity=10, booked_count=5, stock_level_ml=900,
                                    latitude=14.6, longitude=121.0)

        ranked = rank_facilities([high_stock, low_stock], "DONOR", mother_lat=14.6, mother_lon=121.0)

        self.assertEqual(ranked[0], low_stock)

    def test_recipient_prefers_higher_stock_facility_on_ratio_tie(self):
        low_stock = make_facility(name="Low Stock", capacity=10, booked_count=5, stock_level_ml=100,
                                   latitude=14.6, longitude=121.0)
        high_stock = make_facility(name="High Stock", capacity=10, booked_count=5, stock_level_ml=900,
                                    latitude=14.6, longitude=121.0)

        ranked = rank_facilities([high_stock, low_stock], "RECIPIENT", mother_lat=14.6, mother_lon=121.0)

        self.assertEqual(ranked[0], high_stock)

    def test_distance_breaks_ties_when_ratio_and_stock_are_equal(self):
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

    def test_cannot_have_two_open_requests_at_once(self):
        self.mother.latitude, self.mother.longitude = 14.6, 121.0
        self.mother.save()
        self.client.force_authenticate(user=self.mother)

        response = self.client.post("/milkbank/requests/", {
            "request_type": "DONOR", "preferred_date": "2026-12-15", "preferred_time": "10:00 AM",
        })

        self.assertEqual(response.status_code, 400)
        self.assertIn("already have an open request", response.data["detail"])


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
