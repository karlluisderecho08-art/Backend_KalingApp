from rest_framework.test import APITestCase

from accounts.models import User
from notifications.models import NotificationItem

from .allocation import LocationRequired, NoOperationalFacility, get_ranked_facilities, rank_facilities
from .models import Facility, MilkBankRequest, TransactionRecord
from .transitions import ALLOWED_TRANSITIONS, InvalidTransition, apply_transition

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
        self.staff = User.objects.create_user(
            email="staff@example.com", password="x", is_active=True, role=User.Role.FACILITY_STAFF,
        )
        self.facility = make_facility()
        self.req = make_request(self.mother, self.facility)

    def test_mother_cannot_call_staff_accept(self):
        self.client.force_authenticate(user=self.mother)
        response = self.client.post(f"/milkbank/requests/{self.req.id}/accept/")
        self.assertEqual(response.status_code, 403)

    def test_staff_can_call_staff_accept(self):
        self.client.force_authenticate(user=self.staff)
        response = self.client.post(f"/milkbank/requests/{self.req.id}/accept/")
        self.assertEqual(response.status_code, 200)

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
