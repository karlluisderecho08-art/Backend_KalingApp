"""
A Drop off donor confirms her booking by pasting the courier's delivery link
instead of confirming attendance: any time after the facility accepts, until 2
hours after her appointment time. She has no Counseling and Testing stage, so
confirming takes her straight to Breastmilk Collection. After confirmation the
collection method is locked; only a Drop off's link can still be replaced.
"""

import importlib
from datetime import datetime
from unittest.mock import patch

from django.apps import apps
from django.test import override_settings
from rest_framework.test import APITestCase

from accounts.models import User
from core.i18n import translate
from notifications.models import NotificationItem

from .business_hours import BUSINESS_TZ
from .collection import STEPS, STEPS_DROP_OFF_LINKED, STEPS_TITLE
from .models import MilkBankRequest
from .results import EXPIRED_DROP_OFF, mother_result
from .serializers import DELIVERY_LINK_INVALID, DELIVERY_LINK_REQUIRED
from .tests import make_facility, make_request
from .transitions import (
    DROP_OFF_ACCEPTED_NOTIFICATION, DROP_OFF_EXPIRY_MESSAGE, sweep_expired_requests,
)
from .views import COLLECTION_LOCKED

Status = MilkBankRequest.Status
Method = MilkBankRequest.CollectionMethod

NEW_APP = {"HTTP_X_KALINGAPP_VERSION_CODE": "6"}
OLD_APP = {"HTTP_X_KALINGAPP_VERSION_CODE": "5"}
LINK = "https://share.lalamove.com/?PH123456"
PIN = {
    "pickup_latitude": 14.62, "pickup_longitude": 121.03,
    "pickup_address": "123 Mabini St, Quezon City", "pickup_landmark": "Blue gate",
}


def manila(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=BUSINESS_TZ)


class DropOffConfirmationTests(APITestCase):
    def setUp(self):
        # Every date here is written out (December 2026), so "now" is held at
        # a fixed moment before them -- otherwise these tests would start
        # failing the day those dates passed. A test needing another moment
        # patches it again, or passes `now`.
        clock = patch("django.utils.timezone.now", return_value=manila(2026, 11, 20, 8))
        clock.start()
        self.addCleanup(clock.stop)
        self.facility = make_facility(name="PGH")
        self.mother = User.objects.create_user(email="donor@example.com", password="x", is_active=True, mom_name="Ana")
        self.staff = User.objects.create_user(
            email="staff@example.com", password="x", is_active=True,
            role=User.Role.FACILITY_STAFF, facility=self.facility,
        )

    def booking(self, method=Method.DROP_OFF):
        # Appointment: December 1, 2026, 10:00 AM (Manila).
        return make_request(
            self.mother, self.facility, collection_method=method,
            preferred_date="2026-12-01", preferred_time="10:00 AM",
        )

    def accept(self, req):
        self.client.force_authenticate(user=self.staff)
        response = self.client.post(f"/milkbank/requests/{req.id}/accept/")
        self.assertEqual(response.status_code, 200, response.data)
        self.client.force_authenticate(user=self.mother)
        req.refresh_from_db()
        return req

    def confirm(self, req, headers=NEW_APP, **body):
        return self.client.post(f"/milkbank/requests/{req.id}/confirm-attendance/", body, format="json", **headers)

    def stage(self, req):
        req.refresh_from_db()
        return req.stages[req.current_stage_index]

    # --- Stages -----------------------------------------------------------------

    def test_a_drop_off_has_no_counseling_and_testing(self):
        self.assertNotIn("Counseling and Testing", self.booking().stages)
        self.assertIn("Counseling and Testing", self.booking(Method.WALK_IN).stages)
        self.assertEqual(self.booking().stages, ["Status", "Booking Confirmation", "Breastmilk Collection", "Results"])

    # --- Acceptance and deadline -----------------------------------------------

    def test_acceptance_waits_until_2_hours_after_the_appointment(self):
        req = self.accept(self.booking())
        self.assertEqual(req.current_sub_status, Status.AWAITING_ATTENDANCE)
        self.assertEqual(req.response_deadline, manila(2026, 12, 1, 12))
        accepted = NotificationItem.objects.filter(owner=self.mother, description=DROP_OFF_ACCEPTED_NOTIFICATION)
        self.assertTrue(accepted.exists())

    def test_other_methods_keep_the_8_business_hour_confirmation(self):
        req = self.accept(self.booking(Method.WALK_IN))
        self.assertLess(req.response_deadline, manila(2026, 11, 30, 0))

    # --- Pasting the link confirms ---------------------------------------------

    def test_the_link_is_required_to_confirm_and_must_be_a_web_link(self):
        req = self.accept(self.booking())
        self.assertEqual(self.confirm(req).data["delivery_link"], [DELIVERY_LINK_REQUIRED])
        self.assertEqual(self.confirm(req, delivery_link="lalamove order 123").data["delivery_link"], [DELIVERY_LINK_INVALID])
        req.refresh_from_db()
        self.assertEqual((req.current_sub_status, req.delivery_link), (Status.AWAITING_ATTENDANCE, ""))

    def test_the_link_confirms_and_goes_straight_to_breastmilk_collection(self):
        req = self.accept(self.booking())
        response = self.confirm(req, delivery_link=LINK)
        self.assertEqual(response.status_code, 200, response.data)
        req.refresh_from_db()
        self.assertEqual((req.current_sub_status, req.delivery_link), (Status.SCHEDULED, LINK))
        self.assertEqual(self.stage(req), "Breastmilk Collection")
        self.assertTrue(NotificationItem.objects.filter(owner=self.staff, title="Delivery Link Sent").exists())
        steps = NotificationItem.objects.filter(owner=self.mother, title=STEPS_TITLE).get()
        self.assertEqual(steps.description, STEPS_DROP_OFF_LINKED.format(date="December 1, 2026", time="10:00 AM"))
        # The facility sees her on its Breastmilk Collection queue right away.
        self.client.force_authenticate(user=self.staff)
        row = next(r for r in self.client.get("/milkbank/requests/all/").data if r["id"] == req.id)
        self.assertEqual(row["stages"][row["current_stage_index"]], "Breastmilk Collection")

    def test_pasting_the_link_in_booking_status_also_confirms(self):
        req = self.accept(self.booking())
        url = f"/milkbank/requests/{req.id}/collection-method/"
        response = self.client.post(url, {"collection_method": "drop_off", "delivery_link": LINK}, format="json", **NEW_APP)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.stage(req), "Breastmilk Collection")
        self.assertEqual(req.current_sub_status, Status.SCHEDULED)
        self.assertEqual(NotificationItem.objects.filter(owner=self.mother, title=STEPS_TITLE).count(), 1)

    def test_the_link_can_be_pasted_well_before_the_appointment_hour(self):
        req = self.accept(self.booking())
        with patch("milkbank.collection.timezone.now", return_value=manila(2026, 11, 20, 8)):
            self.assertEqual(self.confirm(req, delivery_link=LINK).status_code, 200)

    def test_an_older_app_still_confirms_a_drop_off_the_old_way(self):
        req = self.accept(self.booking())
        self.assertEqual(self.confirm(req, headers=OLD_APP).status_code, 200)

    @override_settings(APP_MIN_VERSION_CODE=6)
    def test_once_every_app_asks_a_link_is_always_required(self):
        req = self.accept(self.booking())
        self.assertEqual(self.confirm(req, headers=NEW_APP).status_code, 400)

    def test_walk_in_and_pickup_still_confirm_attendance(self):
        req = self.accept(self.booking(Method.WALK_IN))
        self.assertEqual(self.confirm(req).status_code, 200)
        self.assertEqual(self.stage(req), "Counseling and Testing")

    # --- Switching while waiting -------------------------------------------------

    def test_switching_to_drop_off_while_waiting_moves_the_deadline(self):
        req = self.accept(self.booking(Method.WALK_IN))
        url = f"/milkbank/requests/{req.id}/collection-method/"
        self.assertEqual(self.client.post(url, {"collection_method": "drop_off"}, format="json", **NEW_APP).status_code, 200)
        req.refresh_from_db()
        self.assertEqual(req.response_deadline, manila(2026, 12, 1, 12))
        self.assertEqual(req.current_sub_status, Status.AWAITING_ATTENDANCE)  # no link yet: not confirmed

    def test_a_counter_offer_moves_the_deadline_to_the_new_appointment(self):
        req = self.accept(self.booking())
        self.client.force_authenticate(user=self.staff)
        response = self.client.post(
            f"/milkbank/requests/{req.id}/propose-counter-offer/",
            {"counter_offer_date": "2026-12-08", "counter_offer_time": "2:00 PM"}, format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.client.force_authenticate(user=self.mother)
        self.assertEqual(self.client.post(f"/milkbank/requests/{req.id}/accept-counter-offer/").status_code, 200)
        req.refresh_from_db()
        self.assertEqual(req.response_deadline, manila(2026, 12, 8, 16))

    # --- Expiry -------------------------------------------------------------------

    def test_no_link_by_2_hours_after_the_appointment_expires_it(self):
        req = self.accept(self.booking())
        self.assertEqual(sweep_expired_requests(now=manila(2026, 12, 1, 11, 59)), [])
        expired = sweep_expired_requests(now=manila(2026, 12, 1, 12, 1))
        self.assertEqual([r.id for r in expired], [req.id])
        req.refresh_from_db()
        self.assertEqual(req.current_sub_status, Status.EXPIRED)
        self.assertTrue(NotificationItem.objects.filter(owner=self.mother, description=DROP_OFF_EXPIRY_MESSAGE).exists())
        self.assertEqual(mother_result(req)["message"], EXPIRED_DROP_OFF[1])

    # --- Locked after confirmation ------------------------------------------------

    def test_the_method_is_locked_after_confirmation(self):
        req = self.accept(self.booking(Method.WALK_IN))
        self.confirm(req)
        url = f"/milkbank/requests/{req.id}/collection-method/"
        for body in ({"collection_method": "drop_off", "delivery_link": LINK}, {"collection_method": "pickup", **PIN}):
            with self.subTest(body=body["collection_method"]):
                response = self.client.post(url, body, format="json", **NEW_APP)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.data["detail"], COLLECTION_LOCKED)
        req.refresh_from_db()
        self.assertEqual(req.collection_method, Method.WALK_IN)

    def test_a_confirmed_drop_off_can_replace_its_link_but_not_change_method(self):
        req = self.accept(self.booking())
        self.confirm(req, delivery_link=LINK)
        url = f"/milkbank/requests/{req.id}/collection-method/"
        new_link = "https://grab.com/express/track/ABC"
        response = self.client.post(url, {"collection_method": "drop_off", "delivery_link": new_link}, format="json", **NEW_APP)
        self.assertEqual(response.status_code, 200, response.data)
        req.refresh_from_db()
        self.assertEqual(req.delivery_link, new_link)
        self.assertEqual(self.stage(req), "Breastmilk Collection")
        response = self.client.post(url, {"collection_method": "drop_off", "delivery_link": ""}, format="json", **NEW_APP)
        self.assertEqual(response.data["delivery_link"], [DELIVERY_LINK_REQUIRED])
        response = self.client.post(url, {"collection_method": "walk_in"}, format="json", **NEW_APP)
        self.assertEqual(response.data["detail"], COLLECTION_LOCKED)

    def test_before_acceptance_she_can_still_change_anything(self):
        req = self.booking(Method.WALK_IN)
        self.client.force_authenticate(user=self.mother)
        url = f"/milkbank/requests/{req.id}/collection-method/"
        self.assertEqual(self.client.post(url, {"collection_method": "pickup", **PIN}, format="json", **NEW_APP).status_code, 200)
        self.assertEqual(self.client.post(url, {"collection_method": "drop_off"}, format="json", **NEW_APP).status_code, 200)


class TranslationTests(APITestCase):
    def test_every_new_message_has_tagalog(self):
        for text in (
            DROP_OFF_ACCEPTED_NOTIFICATION, DROP_OFF_EXPIRY_MESSAGE, EXPIRED_DROP_OFF[1], COLLECTION_LOCKED,
            STEPS[Method.DROP_OFF].format(date="December 1, 2026", time="10:00 AM"),
            STEPS_DROP_OFF_LINKED.format(date="December 1, 2026", time="10:00 AM"),
        ):
            with self.subTest(text=text[:40]):
                self.assertNotEqual(translate(text), text)


class ExistingDropOffsMigrationTests(APITestCase):
    def test_drop_offs_past_confirmation_keep_their_place(self):
        facility = make_facility(name="PGH")
        mother = User.objects.create_user(email="m@example.com", password="x", is_active=True)
        made = {}
        for old_index in (1, 2, 3, 4):
            made[old_index] = make_request(mother, facility, collection_method=Method.DROP_OFF, current_stage_index=old_index)
        walk_in = make_request(mother, facility, collection_method=Method.WALK_IN, current_stage_index=3)

        migration = importlib.import_module("milkbank.migrations.0022_drop_off_skips_counseling")
        migration.forwards(apps, None)

        expected = {1: "Booking Confirmation", 2: "Breastmilk Collection", 3: "Breastmilk Collection", 4: "Results"}
        for old_index, req in made.items():
            req.refresh_from_db()
            self.assertEqual(req.stages[req.current_stage_index], expected[old_index], old_index)
        walk_in.refresh_from_db()
        self.assertEqual(walk_in.current_stage_index, 3)
