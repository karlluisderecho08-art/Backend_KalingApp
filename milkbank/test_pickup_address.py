"""
A Pick up must say where: the pin from her phone, her complete address and a
landmark (milkbank/serializers.py _check_pickup). Seen by her and her booking's
facility only, and cleared a week after the booking closes (core/retention.py).
"""

from datetime import timedelta

from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from core.retention import purge_expired_data

from .models import MilkBankRequest
from .serializers import (
    PICKUP_ADDRESS_REQUIRED, PICKUP_LANDMARK_REQUIRED, PICKUP_PIN_OUTSIDE, PICKUP_PIN_REQUIRED,
)
from .tests import make_facility, make_request

Status = MilkBankRequest.Status
Method = MilkBankRequest.CollectionMethod

NEW_APP = {"HTTP_X_KALINGAPP_VERSION_CODE": "6"}
OLD_APP = {"HTTP_X_KALINGAPP_VERSION_CODE": "5"}
PIN = {
    "pickup_latitude": 14.6201, "pickup_longitude": 121.0302,
    "pickup_address": "Unit 4B, 123 Mabini St, Brgy. San Roque, Quezon City",
    "pickup_landmark": "Beside the blue sari-sari store",
}


class PickupAddressTests(APITestCase):
    URL = "/milkbank/requests/"

    def setUp(self):
        self.facility = make_facility(name="PGH")
        self.mother = User.objects.create_user(
            email="donor@example.com", password="x", is_active=True, latitude=14.6, longitude=121.0,
        )
        self.client.force_authenticate(user=self.mother)

    def book(self, headers=NEW_APP, **extra):
        payload = {
            "request_type": "DONOR", "preferred_date": "2026-12-15", "preferred_time": "10:00 AM",
            "collection_method": "pickup",
        }
        payload.update(extra)
        return self.client.post(self.URL, payload, format="json", **headers)

    # --- Booking ----------------------------------------------------------

    def test_a_pickup_is_saved_with_its_pin_address_and_landmark(self):
        response = self.book(**PIN)
        self.assertEqual(response.status_code, 201, response.data)
        for field, value in PIN.items():
            self.assertEqual(response.data[field], value)
        req = MilkBankRequest.objects.get(pk=response.data["id"])
        self.assertEqual((req.pickup_latitude, req.pickup_longitude), (14.6201, 121.0302))

    def test_each_part_is_required(self):
        cases = [
            ("pickup_latitude", PICKUP_PIN_REQUIRED, {k: v for k, v in PIN.items() if not k.startswith("pickup_l") or k == "pickup_landmark"}),
            ("pickup_address", PICKUP_ADDRESS_REQUIRED, {**PIN, "pickup_address": "   "}),
            ("pickup_landmark", PICKUP_LANDMARK_REQUIRED, {**PIN, "pickup_landmark": ""}),
        ]
        for field, message, payload in cases:
            with self.subTest(field=field):
                response = self.book(**payload)
                self.assertEqual(response.status_code, 400, response.data)
                self.assertEqual(response.data[field], [message])
        self.assertFalse(MilkBankRequest.objects.exists())

    def test_half_a_pin_is_refused(self):
        response = self.book(**{**PIN, "pickup_longitude": None})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["pickup_latitude"], [PICKUP_PIN_REQUIRED])

    def test_a_pin_outside_the_philippines_is_refused(self):
        response = self.book(**{**PIN, "pickup_latitude": 35.68, "pickup_longitude": 139.69})  # Tokyo
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["pickup_latitude"], [PICKUP_PIN_OUTSIDE])

    def test_another_method_keeps_no_pickup_details(self):
        response = self.book(collection_method="walk_in", **PIN)
        self.assertEqual(response.status_code, 201, response.data)
        req = MilkBankRequest.objects.get(pk=response.data["id"])
        self.assertEqual((req.pickup_address, req.pickup_landmark), ("", ""))
        self.assertIsNone(req.pickup_latitude)

    def test_an_app_too_old_to_ask_can_still_book_a_pickup(self):
        response = self.book(headers=OLD_APP)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["pickup_address"], "")

    @override_settings(APP_MIN_VERSION_CODE=6)
    def test_once_every_app_asks_an_old_app_cannot_book_at_all(self):
        # Below the minimum the version gate answers first: update required.
        self.assertEqual(self.book(headers=OLD_APP).status_code, 426)

    def test_the_error_comes_in_tagalog_when_asked(self):
        response = self.book(headers={**NEW_APP, "HTTP_ACCEPT_LANGUAGE": "tl"}, **{**PIN, "pickup_landmark": ""})
        self.assertEqual(response.status_code, 400)
        self.assertIn("palatandaan", response.content.decode("utf-8"))

    # --- Who sees it ----------------------------------------------------------

    def test_her_facility_sees_where_to_collect_and_another_facility_does_not(self):
        req_id = self.book(**PIN).data["id"]
        own = User.objects.create_user(
            email="own@example.com", password="x", is_active=True,
            role=User.Role.FACILITY_STAFF, facility=self.facility,
        )
        other = User.objects.create_user(
            email="other@example.com", password="x", is_active=True,
            role=User.Role.FACILITY_STAFF, facility=make_facility(name="Fabella"),
        )
        self.client.force_authenticate(user=own)
        rows = self.client.get("/milkbank/requests/all/").data
        rows = rows.get("results", rows) if isinstance(rows, dict) else rows
        row = next(r for r in rows if r["id"] == req_id)
        self.assertEqual((row["pickup_address"], row["pickup_landmark"]), (PIN["pickup_address"], PIN["pickup_landmark"]))
        self.assertEqual((row["pickup_latitude"], row["pickup_longitude"]), (14.6201, 121.0302))

        self.client.force_authenticate(user=other)
        rows = self.client.get("/milkbank/requests/all/").data
        rows = rows.get("results", rows) if isinstance(rows, dict) else rows
        self.assertFalse(any(r["id"] == req_id for r in rows))
        self.assertIn(self.client.get(f"/milkbank/requests/{req_id}/").status_code, (403, 404))


class ChangeToPickupTests(APITestCase):
    def setUp(self):
        self.facility = make_facility(name="PGH")
        self.mother = User.objects.create_user(email="donor@example.com", password="x", is_active=True)
        self.client.force_authenticate(user=self.mother)
        self.req = make_request(self.mother, self.facility, collection_method=Method.WALK_IN,
                                current_sub_status=Status.SCHEDULED)
        self.url = f"/milkbank/requests/{self.req.id}/collection-method/"

    def test_switching_to_pickup_needs_where(self):
        response = self.client.post(self.url, {"collection_method": "pickup"}, format="json", **NEW_APP)
        self.assertEqual(response.status_code, 400)
        response = self.client.post(self.url, {"collection_method": "pickup", **PIN}, format="json", **NEW_APP)
        self.assertEqual(response.status_code, 200, response.data)
        self.req.refresh_from_db()
        self.assertEqual(self.req.collection_method, Method.PICKUP)
        self.assertEqual(self.req.pickup_landmark, PIN["pickup_landmark"])

    def test_an_old_app_can_still_switch_to_pickup(self):
        response = self.client.post(self.url, {"collection_method": "pickup"}, format="json", **OLD_APP)
        self.assertEqual(response.status_code, 200, response.data)

    @override_settings(APP_MIN_VERSION_CODE=6)
    def test_once_every_app_asks_switching_to_pickup_always_needs_where(self):
        response = self.client.post(self.url, {"collection_method": "pickup"}, format="json", **OLD_APP)
        self.assertEqual(response.status_code, 400)
        self.assertIn("pickup_latitude", response.data)

    def test_she_can_correct_her_address(self):
        self.client.post(self.url, {"collection_method": "pickup", **PIN}, format="json", **NEW_APP)
        response = self.client.post(
            self.url, {"collection_method": "pickup", **PIN, "pickup_address": "Blk 2 Lot 9, Sampaloc, Manila"},
            format="json", **NEW_APP,
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.req.refresh_from_db()
        self.assertEqual(self.req.pickup_address, "Blk 2 Lot 9, Sampaloc, Manila")

    def test_switching_away_clears_where(self):
        self.client.post(self.url, {"collection_method": "pickup", **PIN}, format="json", **NEW_APP)
        response = self.client.post(self.url, {"collection_method": "walk_in"}, format="json", **NEW_APP)
        self.assertEqual(response.status_code, 200, response.data)
        self.req.refresh_from_db()
        self.assertEqual((self.req.pickup_address, self.req.pickup_landmark), ("", ""))
        self.assertIsNone(self.req.pickup_longitude)


class PickupAddressRetentionTests(APITestCase):
    def setUp(self):
        self.now = timezone.now()
        self.mother = User.objects.create_user(email="donor@example.com", password="x", is_active=True)
        self.facility = make_facility(name="PGH")

    def pickup(self, closed_days_ago=None):
        req = make_request(
            self.mother, self.facility, collection_method=Method.PICKUP,
            pickup_address=PIN["pickup_address"], pickup_landmark=PIN["pickup_landmark"],
            pickup_latitude=PIN["pickup_latitude"], pickup_longitude=PIN["pickup_longitude"],
        )
        if closed_days_ago is not None:
            MilkBankRequest.objects.filter(pk=req.pk).update(
                current_sub_status=Status.COMPLETED, closed_at=self.now - timedelta(days=closed_days_ago),
            )
        return req

    def test_the_address_goes_7_days_after_the_booking_closes(self):
        expired, recent, still_open = self.pickup(closed_days_ago=8), self.pickup(closed_days_ago=6), self.pickup()

        self.assertEqual(purge_expired_data(now=self.now)["pickup_addresses"], 1)

        expired.refresh_from_db()
        self.assertEqual((expired.pickup_address, expired.pickup_landmark), ("", ""))
        self.assertIsNone(expired.pickup_latitude)
        self.assertEqual(expired.collection_method, Method.PICKUP)  # the booking itself stays
        for req in (recent, still_open):
            req.refresh_from_db()
            self.assertEqual(req.pickup_address, PIN["pickup_address"])
        self.assertEqual(purge_expired_data(now=self.now)["pickup_addresses"], 0)
