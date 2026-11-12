"""
Breastmilk Collection timing (milkbank/collection.py): the delivery link is
wanted only from the appointment's hour, and each step is in her notifications.
"""

from datetime import datetime
from unittest.mock import patch

from rest_framework.test import APITestCase

from accounts.models import User
from notifications.models import NotificationItem

from .business_hours import BUSINESS_TZ
from .collection import (
    REMINDER_TITLE, STEPS, STEPS_TITLE, appointment_start, delivery_link_due, send_delivery_link_reminders,
)
from .models import MilkBankRequest
from .tests import make_facility, make_request
from .transitions import apply_transition

Status = MilkBankRequest.Status
Method = MilkBankRequest.CollectionMethod


def manila(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=BUSINESS_TZ)


class CollectionTimingTests(APITestCase):
    def setUp(self):
        self.mother = User.objects.create_user(email="donor@example.com", password="x", is_active=True)
        self.facility = make_facility(name="PGH")
        self.client.force_authenticate(user=self.mother)

    def drop_off(self, **extra):
        return make_request(
            self.mother, self.facility, collection_method=Method.DROP_OFF,
            preferred_date="2026-12-01", preferred_time="10:00 AM", **extra,
        )

    def steps(self):
        return NotificationItem.objects.filter(owner=self.mother, title=STEPS_TITLE)

    def test_appointment_start_is_manila_time(self):
        self.assertEqual(appointment_start(self.drop_off()), manila(2026, 12, 1, 10))

    def test_link_is_due_from_the_appointment_hour(self):
        req = self.drop_off()
        self.assertFalse(delivery_link_due(req, manila(2026, 12, 1, 9, 59)))
        self.assertTrue(delivery_link_due(req, manila(2026, 12, 1, 10, 0)))
        self.assertTrue(delivery_link_due(req, manila(2026, 12, 1, 10, 40)))
        req.current_stage_index = req.stages.index("Results")
        self.assertFalse(delivery_link_due(req, manila(2026, 12, 1, 10, 40)))  # milk already collected

    def test_link_required_only_once_due(self):
        req = self.drop_off()
        url = f"/milkbank/requests/{req.id}/collection-method/"
        with patch("milkbank.collection.timezone.now", return_value=manila(2026, 12, 1, 10, 5)):
            response = self.client.post(url, {"collection_method": "drop_off"})
            self.assertEqual(response.status_code, 400)
            self.assertIn("delivery_link", response.data)
            response = self.client.post(url, {"collection_method": "drop_off", "delivery_link": "https://share.lalamove.com/?A1"})
            self.assertEqual(response.status_code, 200, response.data)
            self.assertTrue(response.data["delivery_link_due"])

    def test_api_gives_the_app_the_appointment_time(self):
        req = self.drop_off()
        data = self.client.get(f"/milkbank/requests/{req.id}/").data
        self.assertEqual(data["appointment_start_ms"], int(manila(2026, 12, 1, 10).timestamp() * 1000))

    def test_steps_sent_when_appointment_is_confirmed(self):
        for method in Method:
            with self.subTest(method=method):
                NotificationItem.objects.all().delete()
                MilkBankRequest.objects.all().delete()
                req = make_request(self.mother, self.facility, collection_method=method,
                                   current_sub_status=Status.AWAITING_ATTENDANCE)
                apply_transition(req, Status.SCHEDULED, self.mother, "attendance_confirmed")
                self.assertEqual(self.steps().count(), 1)
                body = self.steps().get().description
                self.assertEqual(body, STEPS[method].format(date="December 1, 2026", time="10:00 AM"))

    def test_no_steps_without_a_method(self):
        req = make_request(self.mother, self.facility, current_sub_status=Status.AWAITING_ATTENDANCE)
        apply_transition(req, Status.SCHEDULED, self.mother, "attendance_confirmed")
        self.assertFalse(self.steps().exists())

    def test_steps_sent_when_she_changes_the_method_not_when_she_pastes_a_link(self):
        req = self.drop_off(current_sub_status=Status.SCHEDULED)
        url = f"/milkbank/requests/{req.id}/collection-method/"
        self.client.post(url, {"collection_method": "walk_in"})
        self.assertEqual(self.steps().count(), 1)
        self.client.post(url, {"collection_method": "drop_off"})
        self.assertEqual(self.steps().count(), 2)
        self.client.post(url, {"collection_method": "drop_off", "delivery_link": "https://grab.com/t/1"})
        self.assertEqual(self.steps().count(), 2)

    def test_reminder_at_the_hour_once(self):
        req = self.drop_off(current_sub_status=Status.SCHEDULED)
        reminders = NotificationItem.objects.filter(owner=self.mother, title=REMINDER_TITLE)
        self.assertEqual(send_delivery_link_reminders(manila(2026, 12, 1, 9, 30)), [])
        self.assertEqual(len(send_delivery_link_reminders(manila(2026, 12, 1, 10, 1))), 1)
        self.assertEqual(len(send_delivery_link_reminders(manila(2026, 12, 1, 10, 30))), 0)  # once
        self.assertEqual(reminders.count(), 1)
        self.assertIn("10:00 AM", reminders.get().description)

        # A new appointment time gets its own reminder.
        req.preferred_time = "2:00 PM"
        req.save()
        self.assertEqual(len(send_delivery_link_reminders(manila(2026, 12, 1, 14, 0))), 1)

    def test_no_reminder_once_the_link_is_in_or_for_other_methods(self):
        self.drop_off(current_sub_status=Status.SCHEDULED, delivery_link="https://grab.com/t/1")
        make_request(self.mother, self.facility, collection_method=Method.WALK_IN, current_sub_status=Status.SCHEDULED)
        self.assertEqual(send_delivery_link_reminders(manila(2026, 12, 1, 10, 5)), [])
