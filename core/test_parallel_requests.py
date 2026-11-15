"""
What the server answering several requests at once (gunicorn.conf.py) must
not break. Each of these was safe only while requests ran one at a time.

Same approach as milkbank/test_capacity_race.py: real simultaneity is hard to
stage inside a test, and a race that only sometimes reproduces proves nothing
on the runs where it doesn't. So each test stages the dangerous ordering
exactly -- usually by handing a request the stale copy it would have read in
the real race, or by letting "the other request" finish at the precise moment
it would have. The rate-limit test is the exception: that race lives in
Python, not in the database, so it uses real threads, made to overlap every
time by a cache read that takes a moment.
"""

import threading
import time
from datetime import datetime, timedelta
from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser
from django.core.cache.backends.locmem import LocMemCache
from django.db import IntegrityError
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts import lockout
from accounts.emails import MAX_VERIFICATION_ATTEMPTS
from accounts.models import PendingRegistration, User
from accounts.tests import ThrottleIsolatedTestCase, _SynchronousThread
from core.throttling import ClientIPAnonRateThrottle
from milkbank import views as milkbank_views
from milkbank.business_hours import BUSINESS_TZ
from milkbank.collection import REMINDER_TITLE, _appointment_day, send_delivery_link_reminders
from milkbank.models import Facility, MilkBankRequest, TransactionRecord
from milkbank.tests import BOOKED_DAY, make_facility, make_request
from milkbank.transitions import (
    SLA_EXPIRY_MESSAGES, InvalidTransition, advance_stage, apply_transition, sweep_expired_requests,
)
from notifications.models import NotificationItem

Status = MilkBankRequest.Status
Method = MilkBankRequest.CollectionMethod


# --- Rate limits -------------------------------------------------------------

class _SlowCache:
    """A real cache whose reads take a moment -- long enough that requests sent
    together are all between their read and their write at the same time."""

    def __init__(self):
        self.inner = LocMemCache("parallel-rate-limit-test", {})

    def get(self, *args, **kwargs):
        value = self.inner.get(*args, **kwargs)
        time.sleep(0.05)
        return value

    def set(self, *args, **kwargs):
        return self.inner.set(*args, **kwargs)


class ParallelRateLimitTests(SimpleTestCase):
    def test_a_burst_sent_all_at_once_is_held_to_the_limit(self):
        class FiveAMinute(ClientIPAnonRateThrottle):
            rate = "5/min"
            cache = _SlowCache()

        request = RequestFactory().post("/auth/login/", REMOTE_ADDR="203.0.113.7")
        request.user = AnonymousUser()
        start = threading.Barrier(8)
        allowed = []

        def attempt():
            start.wait()
            allowed.append(FiveAMinute().allow_request(request, None))

        threads = [threading.Thread(target=attempt) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        # Without the per-key lock all eight read the same empty history during
        # that slow read, and all eight got through.
        self.assertEqual(allowed.count(True), 5)
        self.assertEqual(allowed.count(False), 3)


# --- Sign-in lockout ---------------------------------------------------------

class OneSignInAtATimeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="mother@example.com", password=None, is_active=True)

    def fresh(self):
        return User.objects.get(pk=self.user.pk)

    def test_a_copy_read_before_its_turn_sees_the_guesses_made_meanwhile(self):
        waiting = self.fresh()  # read as it arrived, behind five guesses in line
        for _ in range(lockout.LOCK_THRESHOLD):
            lockout.register_failure(self.fresh())
        self.assertFalse(lockout.is_locked(waiting))  # what it would have gone on

        with lockout.one_attempt_at_a_time(waiting):
            self.assertTrue(lockout.is_locked(waiting))

    def test_every_guess_in_line_is_counted(self):
        arrived_together = [self.fresh() for _ in range(3)]
        for copy in arrived_together:
            with lockout.one_attempt_at_a_time(copy):
                lockout.register_failure(copy)

        # Each copy read 0; without the lock each wrote 1.
        self.assertEqual(self.fresh().failed_login_attempts, 3)

    def test_a_second_check_on_the_same_account_waits_its_turn(self):
        stripe = lockout._ACCOUNT_LOCKS[self.user.pk % len(lockout._ACCOUNT_LOCKS)]
        with lockout.one_attempt_at_a_time(self.user):
            self.assertFalse(stripe.acquire(blocking=False))
        self.assertTrue(stripe.acquire(blocking=False))
        stripe.release()


# --- Code guesses, signup, verification -------------------------------------

class CodeGuessAndDoubleTapTests(ThrottleIsolatedTestCase):
    def setUp(self):
        super().setUp()
        patcher = patch("accounts.views.threading.Thread", new=_SynchronousThread)
        patcher.start()
        self.addCleanup(patcher.stop)

    def pending(self, **extra):
        return PendingRegistration.objects.create(
            email="new@example.com", password="x", code="123456", sent_at=timezone.now(), **extra,
        )

    def verify(self, code="123456"):
        return self.client.post("/auth/verify-email/", {"email": "new@example.com", "code": code})

    def test_a_verification_guess_read_alongside_the_last_allowed_one_is_not_compared(self):
        # The last allowed guess has just been counted; this one read the count
        # a moment before that -- and it carries the right code.
        pending = self.pending(attempts=MAX_VERIFICATION_ATTEMPTS)
        read_earlier = PendingRegistration.objects.get(pk=pending.pk)
        read_earlier.attempts = MAX_VERIFICATION_ATTEMPTS - 1

        with patch.object(PendingRegistration.objects, "get", return_value=read_earlier):
            response = self.verify()

        self.assertEqual(response.status_code, 400)
        self.assertIn("Too many incorrect attempts", response.data["detail"])
        self.assertFalse(User.objects.filter(email="new@example.com").exists())

    def test_a_wrong_verification_guess_is_still_counted(self):
        pending = self.pending()
        self.assertEqual(self.verify(code="000000").status_code, 400)
        pending.refresh_from_db()
        self.assertEqual(pending.attempts, 1)

    def test_two_taps_on_verify_make_one_account(self):
        pending = self.pending()

        def other_tap_gets_there_first(email):
            # A moment ahead, it passed this same check, claimed the pending
            # signup and made the account.
            PendingRegistration.objects.filter(pk=pending.pk).delete()
            User.objects.create_user(email=email, password=None, is_active=True)
            return False  # what this tap's own check saw, a moment earlier

        with patch("accounts.views.email_is_taken", side_effect=other_tap_gets_there_first):
            response = self.verify()

        self.assertEqual(response.status_code, 400)
        self.assertIn("already verified", response.data["detail"])
        self.assertEqual(User.objects.filter(email="new@example.com").count(), 1)

    def test_two_taps_on_sign_up_make_one_pending_signup(self):
        real_create = PendingRegistration.objects.create
        tries = []

        def create(**fields):
            tries.append(fields["email"])
            if len(tries) == 1:
                # The other tap's row landed between this one's delete and create.
                raise IntegrityError("duplicate key value violates unique constraint")
            return real_create(**fields)

        with patch.object(PendingRegistration.objects, "create", side_effect=create):
            response = self.client.post("/auth/register/", {
                "email": "new@example.com", "password": "correct-horse-battery-staple",
                "mom_name": "Rachel", "baby_name": "James",
            })

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(len(tries), 2)
        self.assertEqual(PendingRegistration.objects.filter(email="new@example.com").count(), 1)

    def reset_user(self, attempts=0):
        user = User.objects.create_user(email="mother@example.com", password="old-password", is_active=True)
        User.objects.filter(pk=user.pk).update(
            password_reset_code="654321", password_reset_sent_at=timezone.now(), password_reset_attempts=attempts,
        )
        return user

    def reset(self, new_password="a-brand-new-password"):
        return self.client.post("/auth/reset-password/", {
            "email": "mother@example.com", "code": "654321", "new_password": new_password,
        })

    def test_a_reset_guess_read_alongside_the_last_allowed_one_is_not_compared(self):
        user = self.reset_user(attempts=MAX_VERIFICATION_ATTEMPTS)
        read_earlier = User.objects.get(pk=user.pk)
        read_earlier.password_reset_attempts = MAX_VERIFICATION_ATTEMPTS - 1

        with patch.object(User.objects, "get", return_value=read_earlier):
            response = self.reset()

        self.assertEqual(response.status_code, 400)
        self.assertIn("Too many incorrect attempts", response.data["detail"])
        user.refresh_from_db()
        self.assertTrue(user.check_password("old-password"))

    def test_a_right_reset_code_with_a_short_password_costs_no_guess(self):
        user = self.reset_user(attempts=2)
        response = self.reset(new_password="short")
        self.assertEqual(response.status_code, 400)
        self.assertIn("at least 8 characters", response.data["detail"])
        user.refresh_from_db()
        self.assertEqual(user.password_reset_attempts, 2)

    def test_a_wrong_reset_guess_is_still_counted(self):
        user = self.reset_user()
        response = self.client.post("/auth/reset-password/", {
            "email": "mother@example.com", "code": "000000", "new_password": "a-brand-new-password",
        })
        self.assertEqual(response.status_code, 400)
        user.refresh_from_db()
        self.assertEqual(user.password_reset_attempts, 1)


# --- Bookings ----------------------------------------------------------------

class OneStatusChangeWinsTests(TestCase):
    def setUp(self):
        self.mother = User.objects.create_user(email="mother@example.com", password=None, is_active=True)
        self.staff = User.objects.create_user(
            email="staff@example.com", password=None, is_active=True, role=User.Role.FACILITY_STAFF,
        )
        self.facility = make_facility(stock_level_ml=1000)

    def scheduled(self, **extra):
        req = make_request(self.mother, self.facility, **extra)
        apply_transition(req, Status.AWAITING_ATTENDANCE, self.staff, "accepted")
        apply_transition(req, Status.SCHEDULED, self.mother, "attendance_confirmed")
        return req

    def copies(self, req, n=2):
        return [MilkBankRequest.objects.get(pk=req.pk) for _ in range(n)]

    def test_completing_twice_at_once_adds_the_milk_once(self):
        first, second = self.copies(self.scheduled())  # both clicks read Scheduled

        apply_transition(first, Status.COMPLETED, self.staff, "completed", amount_ml=150)
        notifications = NotificationItem.objects.count()
        with self.assertRaises(InvalidTransition):
            apply_transition(second, Status.COMPLETED, self.staff, "completed", amount_ml=150)

        self.facility.refresh_from_db()
        self.mother.refresh_from_db()
        self.assertEqual(self.facility.stock_level_ml, 1150)
        self.assertEqual(self.mother.total_drawn_ml, 150)
        self.assertEqual(TransactionRecord.objects.filter(owner=self.mother).count(), 1)
        self.assertEqual(NotificationItem.objects.count(), notifications)

    def test_the_refused_change_names_the_status_the_booking_really_has(self):
        accept, decline = self.copies(make_request(self.mother, self.facility))
        apply_transition(decline, Status.DECLINED, self.staff, "declined")
        with self.assertRaisesMessage(InvalidTransition, f"Cannot move from {Status.DECLINED} to"):
            apply_transition(accept, Status.AWAITING_ATTENDANCE, self.staff, "accepted")
        self.assertEqual(MilkBankRequest.objects.get(pk=accept.pk).current_sub_status, Status.DECLINED)

    def test_two_sweeps_expire_an_overdue_booking_once(self):
        req = make_request(self.mother, self.facility)
        apply_transition(req, Status.AWAITING_ATTENDANCE, self.staff, "accepted")
        MilkBankRequest.objects.filter(pk=req.pk).update(response_deadline=timezone.now() - timedelta(minutes=1))
        message = SLA_EXPIRY_MESSAGES[Status.AWAITING_ATTENDANCE]

        def other_sweep_expires_it_first(found):
            apply_transition(
                MilkBankRequest.objects.get(pk=found.pk), Status.EXPIRED, None, "expired_sla_timeout",
                message_override=message,
            )
            return False  # not a Drop off

        with patch("milkbank.transitions.confirms_by_delivery_link", side_effect=other_sweep_expires_it_first):
            expired_here = sweep_expired_requests()

        self.assertEqual(expired_here, [])
        self.assertEqual(MilkBankRequest.objects.get(pk=req.pk).current_sub_status, Status.EXPIRED)
        self.assertEqual(NotificationItem.objects.filter(owner=self.mother, description=message).count(), 1)

    def test_a_double_click_on_next_phase_moves_one_phase(self):
        req = self.scheduled()
        start = req.current_stage_index
        first, second = self.copies(req)

        self.assertTrue(advance_stage(first))
        self.assertFalse(advance_stage(second))
        self.assertEqual(MilkBankRequest.objects.get(pk=req.pk).current_stage_index, start + 1)

    def test_two_sweeps_send_one_drop_off_reminder(self):
        req = self.scheduled(
            collection_method=Method.DROP_OFF, preferred_date="2026-12-01", preferred_time="10:00 AM",
        )
        key = f"{_appointment_day(req).isoformat()} {req.preferred_time}"

        def other_sweep_reminds_her_first(found, now):
            MilkBankRequest.objects.filter(pk=found.pk).update(delivery_reminder_for=key)
            return True

        with patch("milkbank.collection.delivery_link_due", side_effect=other_sweep_reminds_her_first):
            reminded_here = send_delivery_link_reminders(datetime(2026, 12, 1, 10, 5, tzinfo=BUSINESS_TZ))

        self.assertEqual(reminded_here, [])
        self.assertFalse(NotificationItem.objects.filter(owner=self.mother, title=REMINDER_TITLE).exists())


class OneBookingPerTapTests(APITestCase):
    def setUp(self):
        self.mother = User.objects.create_user(
            email="mother@example.com", password=None, is_active=True, latitude=14.6, longitude=121.0,
        )
        self.facility = make_facility(latitude=14.6, longitude=121.0)

    def test_a_double_tap_on_book_makes_one_booking(self):
        real_rank = milkbank_views.get_ranked_facilities

        def other_tap_books_first(*args, **kwargs):
            # A moment ahead, it passed the same open-request check and booked.
            make_request(self.mother, self.facility, preferred_date=BOOKED_DAY)
            return real_rank(*args, **kwargs)

        self.client.force_authenticate(user=self.mother)
        with patch("milkbank.views.get_ranked_facilities", side_effect=other_tap_books_first):
            response = self.client.post("/milkbank/requests/", {
                "request_type": "DONOR", "preferred_date": BOOKED_DAY, "preferred_time": "10:00 AM",
            })

        self.assertEqual(response.status_code, 400)
        self.assertIn("already have an open request", response.data["detail"])
        self.assertEqual(MilkBankRequest.objects.filter(owner=self.mother).count(), 1)

    def test_two_dispensings_at_once_cannot_take_stock_below_zero(self):
        facility = make_facility(name="Depot", stock_level_ml=100)
        staff = User.objects.create_user(
            email="staff@example.com", password=None, is_active=True,
            role=User.Role.FACILITY_STAFF, facility=facility,
        )
        req = make_request(self.mother, facility, request_type=MilkBankRequest.RequestType.RECIPIENT)
        apply_transition(req, Status.AWAITING_ATTENDANCE, staff, "accepted")
        apply_transition(req, Status.SCHEDULED, self.mother, "attendance_confirmed")
        # Read while the facility still had 100 mL ...
        read_earlier = MilkBankRequest.objects.select_related("allocated_facility").get(pk=req.pk)
        # ... then another dispensing took 70 of it.
        Facility.objects.filter(pk=facility.pk).update(stock_level_ml=30)

        self.client.force_authenticate(user=staff)
        with patch.object(milkbank_views.StaffConfirmCompletionView, "get_object", return_value=read_earlier):
            response = self.client.post(f"/milkbank/requests/{req.id}/confirm-completion/", {"amount_ml": 80})

        self.assertEqual(response.status_code, 400)
        self.assertIn("only has 30 mL", response.data["detail"])
        facility.refresh_from_db()
        self.assertEqual(facility.stock_level_ml, 30)
        self.assertEqual(MilkBankRequest.objects.get(pk=req.pk).current_sub_status, Status.SCHEDULED)
