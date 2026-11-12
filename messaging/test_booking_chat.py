"""
The mother-facility chat on a booking, and the donor's Breastmilk Collection
method (Pick up / Walk-in / Drop off, with a courier link for Drop off).
"""

from django.core.cache import cache
from rest_framework.test import APITestCase

from accounts.models import User
from milkbank.models import MilkBankRequest
from milkbank.tests import BOOKED_DAY, make_facility, make_request
from milkbank.transitions import apply_transition
from notifications.models import NotificationItem

from .models import BookingChat, BookingMessage

Status = MilkBankRequest.Status
UNREAD = "/messaging/bookings/unread/"


def chat_url(request_id):
    return f"/messaging/bookings/{request_id}/messages/"


class BookingChatTests(APITestCase):
    def setUp(self):
        cache.clear()  # throttle counters
        self.pgh = make_facility(name="PGH")
        self.other = make_facility(name="St. Luke's")
        self.mother = User.objects.create_user(email="mom@example.com", password="x", is_active=True, mom_name="Ana")
        self.stranger = User.objects.create_user(email="other-mom@example.com", password="x", is_active=True)
        self.staff = User.objects.create_user(
            email="pgh@example.com", password="x", is_active=True,
            role=User.Role.FACILITY_STAFF, facility=self.pgh,
        )
        self.other_staff = User.objects.create_user(
            email="sl@example.com", password="x", is_active=True,
            role=User.Role.FACILITY_STAFF, facility=self.other,
        )
        self.req = make_request(self.mother, self.pgh, current_sub_status=Status.SCHEDULED)

    def as_user(self, user):
        self.client.force_authenticate(user=user)

    def send(self, user, body):
        self.as_user(user)
        return self.client.post(chat_url(self.req.id), {"body": body})

    def test_mother_and_her_facility_can_talk(self):
        self.assertEqual(self.send(self.mother, "Hello po").status_code, 201)
        self.assertEqual(self.send(self.staff, "Hi Ana!").status_code, 201)

        self.as_user(self.mother)
        data = self.client.get(chat_url(self.req.id)).data
        self.assertTrue(data["open"])
        self.assertEqual([m["body"] for m in data["messages"]], ["Hello po", "Hi Ana!"])
        # She sees the facility's name, never a staff member's account.
        self.assertEqual(data["messages"][1]["sender_name"], "PGH")

        self.as_user(self.staff)
        data = self.client.get(chat_url(self.req.id)).data
        self.assertEqual(data["messages"][0]["sender_name"], "Ana")
        self.assertEqual(data["messages"][1]["sender_name"], "pgh@example.com")

    def test_nobody_else_can_reach_the_chat(self):
        for user in (self.stranger, self.other_staff):
            self.as_user(user)
            self.assertEqual(self.client.get(chat_url(self.req.id)).status_code, 404)
            self.assertEqual(self.client.post(chat_url(self.req.id), {"body": "x"}).status_code, 404)
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get(chat_url(self.req.id)).status_code, 401)

    def test_unread_counts_and_reading_clears_them(self):
        self.send(self.staff, "One")
        self.send(self.staff, "Two")
        self.as_user(self.mother)
        self.assertEqual(self.client.get(UNREAD).data, {"unread": 2, "by_request": {str(self.req.id): 2}})
        self.client.get(chat_url(self.req.id))
        self.assertEqual(self.client.get(UNREAD).data["unread"], 0)

        self.send(self.mother, "Reply")
        self.as_user(self.staff)
        self.assertEqual(self.client.get(UNREAD).data["by_request"], {str(self.req.id): 1})
        self.as_user(self.other_staff)
        self.assertEqual(self.client.get(UNREAD).data["unread"], 0)

    def test_mother_is_notified_once_per_batch_of_unread_messages(self):
        self.send(self.staff, "One")
        self.send(self.staff, "Two")
        titles = list(NotificationItem.objects.filter(owner=self.mother).values_list("title", flat=True))
        self.assertEqual(titles, ["New message from PGH"])
        self.as_user(self.mother)
        self.client.get(chat_url(self.req.id))  # read
        self.send(self.staff, "Three")
        self.assertEqual(NotificationItem.objects.filter(owner=self.mother, title="New message from PGH").count(), 2)

    def test_chat_is_deleted_for_both_sides_when_the_booking_ends(self):
        for final in (Status.COMPLETED, Status.DECLINED):
            with self.subTest(final=final):
                req = make_request(self.mother, self.pgh, current_sub_status=Status.SCHEDULED)
                self.req = req
                self.send(self.mother, "Hi")
                self.send(self.staff, "Hello")
                self.assertEqual(BookingMessage.objects.filter(chat__request=req).count(), 2)

                apply_transition(req, final, self.staff, "test", amount_ml=100 if final == Status.COMPLETED else None)

                self.assertFalse(BookingChat.objects.filter(request=req).exists())
                self.assertFalse(BookingMessage.objects.filter(chat__request_id=req.id).exists())
                for user in (self.mother, self.staff):
                    self.as_user(user)
                    data = self.client.get(chat_url(req.id)).data
                    self.assertFalse(data["open"])
                    self.assertEqual(data["messages"], [])
                response = self.send(self.mother, "Still there?")
                self.assertEqual(response.status_code, 400)
                self.assertFalse(BookingChat.objects.filter(request=req).exists())

    def test_expired_booking_chat_is_deleted_too(self):
        req = make_request(self.mother, self.pgh)  # pending
        self.req = req
        self.send(self.mother, "Hello?")
        apply_transition(req, Status.EXPIRED, None, "expired_sla_timeout")
        self.assertFalse(BookingChat.objects.filter(request=req).exists())

    def test_empty_message_is_refused(self):
        self.assertEqual(self.send(self.mother, "   ").status_code, 400)


class CollectionMethodTests(APITestCase):
    def setUp(self):
        self.mother = User.objects.create_user(
            email="donor@example.com", password="x", is_active=True, latitude=14.6, longitude=121.0,
        )
        self.facility = make_facility(name="PGH")
        self.client.force_authenticate(user=self.mother)

    def book(self, **extra):
        return self.client.post("/milkbank/requests/", {
            "request_type": "DONOR", "preferred_date": BOOKED_DAY, "preferred_time": "10:00 AM", **extra,
        })

    def test_each_method_is_saved(self):
        for method in ("pickup", "walk_in"):
            with self.subTest(method=method):
                MilkBankRequest.objects.all().delete()
                response = self.book(collection_method=method)
                self.assertEqual(response.status_code, 201, response.data)
                self.assertEqual(response.data["collection_method"], method)
                self.assertEqual(response.data["delivery_link"], "")

    def test_drop_off_is_booked_before_its_link(self):
        # The courier is booked on the appointment day, so the link comes later.
        response = self.book(collection_method="drop_off")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["delivery_link"], "")
        self.assertFalse(response.data["delivery_link_due"])
        MilkBankRequest.objects.all().delete()

        for bad in ("not a link", "javascript:alert(1)", "ftp://example.com/x"):
            with self.subTest(bad=bad):
                response = self.book(collection_method="drop_off", delivery_link=bad)
                self.assertEqual(response.status_code, 400)

        link = "https://share.lalamove.com/?ABC123"
        response = self.book(collection_method="drop_off", delivery_link=link)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["delivery_link"], link)

    def test_a_link_without_drop_off_is_not_kept(self):
        response = self.book(collection_method="walk_in", delivery_link="https://example.com")
        self.assertEqual(response.data["delivery_link"], "")

    def test_older_app_without_the_question_can_still_book(self):
        response = self.book()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["collection_method"], "")

    def test_recipient_never_gets_a_method(self):
        response = self.client.post("/milkbank/requests/", {
            "request_type": "RECIPIENT", "preferred_date": BOOKED_DAY, "preferred_time": "10:00 AM",
            "requested_ml": 100, "collection_method": "pickup",
        })
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["collection_method"], "")

    def test_donor_can_change_it_until_her_milk_is_collected(self):
        req = make_request(self.mother, self.facility, collection_method="walk_in")
        url = f"/milkbank/requests/{req.id}/collection-method/"

        # Before the appointment hour a Drop off needs no link yet...
        response = self.client.post(url, {"collection_method": "drop_off"})
        self.assertEqual(response.status_code, 200, response.data)
        response = self.client.post(url, {"collection_method": "drop_off", "delivery_link": "https://grab.com/t/1"})
        self.assertEqual(response.status_code, 200, response.data)
        req.refresh_from_db()
        self.assertEqual((req.collection_method, req.delivery_link), ("drop_off", "https://grab.com/t/1"))

        req.current_stage_index = req.stages.index("Results")
        req.save()
        self.assertEqual(self.client.post(url, {"collection_method": "pickup"}).status_code, 400)

    def test_only_the_owner_can_change_it(self):
        req = make_request(self.mother, self.facility)
        other = User.objects.create_user(email="x@example.com", password="x", is_active=True)
        self.client.force_authenticate(user=other)
        response = self.client.post(f"/milkbank/requests/{req.id}/collection-method/", {"collection_method": "pickup"})
        self.assertEqual(response.status_code, 403)
