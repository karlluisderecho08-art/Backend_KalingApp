from django.core.cache import cache
from rest_framework.test import APITestCase

from accounts.models import User
from core.models import AuditLogEntry
from milkbank.tests import make_facility

from .models import Conversation, Message

FACILITY_THREAD = "/messaging/facility/messages/"
FACILITY_UNREAD = "/messaging/facility/unread/"
ADMIN_LIST = "/messaging/admin/conversations/"
ADMIN_UNREAD = "/messaging/admin/unread/"


def admin_thread(facility_id):
    return f"/messaging/admin/conversations/{facility_id}/messages/"


class MessagingTests(APITestCase):
    def setUp(self):
        cache.clear()  # throttle counters
        self.pgh = make_facility(name="PGH")
        self.stlukes = make_facility(name="St. Luke's")
        self.pgh_staff = User.objects.create_user(
            email="pgh@example.com", password="x", is_active=True,
            role=User.Role.FACILITY_STAFF, facility=self.pgh,
        )
        self.stlukes_staff = User.objects.create_user(
            email="sl@example.com", password="x", is_active=True,
            role=User.Role.FACILITY_STAFF, facility=self.stlukes,
        )
        self.admin = User.objects.create_user(email="admin@example.com", password="x", is_active=True, is_staff=True)
        self.mother = User.objects.create_user(email="mom@example.com", password="x", is_active=True)

    def as_user(self, user):
        self.client.force_authenticate(user=user)

    # --- who can reach what ---

    def test_anonymous_is_refused_everywhere(self):
        for url in (FACILITY_THREAD, FACILITY_UNREAD, ADMIN_LIST, ADMIN_UNREAD, admin_thread(self.pgh.id)):
            self.assertEqual(self.client.get(url).status_code, 401, url)

    def test_a_mother_is_refused_both_sides(self):
        self.as_user(self.mother)
        self.assertEqual(self.client.get(FACILITY_THREAD).status_code, 403)
        self.assertEqual(self.client.get(ADMIN_LIST).status_code, 403)
        self.assertEqual(self.client.post(FACILITY_THREAD, {"body": "hi"}).status_code, 403)

    def test_facility_staff_cannot_use_the_admin_side(self):
        self.as_user(self.pgh_staff)
        self.assertEqual(self.client.get(ADMIN_LIST).status_code, 403)
        self.assertEqual(self.client.get(admin_thread(self.stlukes.id)).status_code, 403)
        self.assertEqual(self.client.post(admin_thread(self.stlukes.id), {"body": "x"}).status_code, 403)
        self.assertEqual(self.client.delete(ADMIN_LIST).status_code, 403)

    def test_unassigned_facility_staff_gets_nothing(self):
        self.pgh_staff.facility = None
        self.pgh_staff.save()
        self.as_user(self.pgh_staff)
        self.assertEqual(self.client.get(FACILITY_THREAD).status_code, 403)

    def test_a_facility_only_sees_its_own_thread(self):
        self.as_user(self.admin)
        self.client.post(admin_thread(self.stlukes.id), {"body": "For St. Luke's only"})
        self.as_user(self.pgh_staff)
        response = self.client.get(FACILITY_THREAD)
        self.assertEqual(response.data["facility_name"], "PGH")
        self.assertEqual(response.data["messages"], [])

    # --- the conversation itself ---

    def test_round_trip(self):
        self.as_user(self.pgh_staff)
        self.assertEqual(self.client.post(FACILITY_THREAD, {"body": "Low on stock"}).status_code, 201)

        self.as_user(self.admin)
        self.assertEqual(self.client.get(ADMIN_UNREAD).data["unread"], 1)
        thread = self.client.get(admin_thread(self.pgh.id)).data["messages"]
        self.assertEqual([(m["side"], m["body"], m["sender_name"]) for m in thread],
                         [("facility", "Low on stock", "pgh@example.com")])
        self.assertEqual(self.client.get(ADMIN_UNREAD).data["unread"], 0)  # reading marked it read
        self.client.post(admin_thread(self.pgh.id), {"body": "Sending more Friday"})

        self.as_user(self.pgh_staff)
        self.assertEqual(self.client.get(FACILITY_UNREAD).data["unread"], 1)
        thread = self.client.get(FACILITY_THREAD).data["messages"]
        self.assertEqual([m["body"] for m in thread], ["Low on stock", "Sending more Friday"])
        self.assertEqual(self.client.get(FACILITY_UNREAD).data["unread"], 0)

    def test_a_facility_never_sees_an_admins_email(self):
        self.as_user(self.admin)
        self.client.post(admin_thread(self.pgh.id), {"body": "Hello"})
        self.as_user(self.pgh_staff)
        message = self.client.get(FACILITY_THREAD).data["messages"][0]
        self.assertEqual(message["sender_name"], "KalingApp Admin")
        self.assertNotIn("admin@example.com", str(message))

    def test_the_admin_inbox_lists_every_facility(self):
        self.as_user(self.pgh_staff)
        self.client.post(FACILITY_THREAD, {"body": "Hi"})
        self.as_user(self.admin)
        rows = self.client.get(ADMIN_LIST).data
        self.assertEqual([r["facility_name"] for r in rows], ["PGH", "St. Luke's"])
        self.assertEqual(rows[0]["unread"], 1)
        self.assertEqual(rows[0]["last_message_preview"], "Hi")

    def test_empty_and_oversized_messages_are_rejected(self):
        self.as_user(self.pgh_staff)
        self.assertEqual(self.client.post(FACILITY_THREAD, {"body": "   "}).status_code, 400)
        self.assertEqual(self.client.post(FACILITY_THREAD, {"body": "x" * 2001}).status_code, 400)
        self.assertEqual(Message.objects.count(), 0)

    def test_hidden_control_characters_are_stripped(self):
        self.as_user(self.pgh_staff)
        response = self.client.post(FACILITY_THREAD, {"body": "pay\u202eevil\x07 now"})
        self.assertEqual(response.data["body"], "payevil now")

    def test_markup_is_stored_as_plain_text(self):
        self.as_user(self.pgh_staff)
        self.client.post(FACILITY_THREAD, {"body": "<script>alert(1)</script>"})
        # Stored verbatim; the dashboards render it as text, never as HTML.
        self.assertEqual(Message.objects.get().body, "<script>alert(1)</script>")

    def test_responses_are_not_cacheable(self):
        self.as_user(self.pgh_staff)
        self.assertEqual(self.client.get(FACILITY_THREAD)["Cache-Control"], "no-store")

    def test_sending_is_rate_limited(self):
        self.as_user(self.pgh_staff)
        codes = [self.client.post(FACILITY_THREAD, {"body": f"m{i}"}).status_code for i in range(31)]
        self.assertEqual(codes[:30], [201] * 30)
        self.assertEqual(codes[30], 429)

    # --- clearing ---

    def test_facility_clear_purges_the_thread_for_both_sides(self):
        self.as_user(self.pgh_staff)
        self.client.post(FACILITY_THREAD, {"body": "secret"})
        self.as_user(self.admin)
        self.client.post(admin_thread(self.pgh.id), {"body": "reply"})
        self.client.post(admin_thread(self.stlukes.id), {"body": "other facility"})

        self.as_user(self.pgh_staff)
        self.assertEqual(self.client.delete(FACILITY_THREAD).data["deleted"], 2)

        self.assertFalse(Message.objects.filter(conversation__facility=self.pgh).exists())
        self.assertEqual(Message.objects.count(), 1)  # St. Luke's untouched
        self.as_user(self.admin)
        self.assertEqual(self.client.get(admin_thread(self.pgh.id)).data["messages"], [])

    def test_clearing_is_audited_without_the_content(self):
        self.as_user(self.pgh_staff)
        self.client.post(FACILITY_THREAD, {"body": "very private words"})
        self.client.delete(FACILITY_THREAD)
        entry = AuditLogEntry.objects.get(action="messaging.cleared")
        self.assertEqual(entry.actor, self.pgh_staff)
        self.assertNotIn("private", entry.target)

    def test_admin_can_clear_one_thread_or_all(self):
        for staff in (self.pgh_staff, self.stlukes_staff):
            self.as_user(staff)
            self.client.post(FACILITY_THREAD, {"body": "hi"})
        self.as_user(self.admin)
        self.assertEqual(self.client.delete(admin_thread(self.pgh.id)).data["deleted"], 1)
        self.assertEqual(Message.objects.count(), 1)
        self.assertEqual(self.client.delete(ADMIN_LIST).data["deleted"], 1)
        self.assertEqual(Message.objects.count(), 0)
        self.assertEqual(Conversation.objects.filter(last_message_at__isnull=False).count(), 0)

    def test_messages_survive_the_sender_account_being_deleted(self):
        self.as_user(self.pgh_staff)
        self.client.post(FACILITY_THREAD, {"body": "hello"})
        self.pgh_staff.delete()
        self.as_user(self.admin)
        message = self.client.get(admin_thread(self.pgh.id)).data["messages"][0]
        self.assertEqual((message["side"], message["sender_name"]), ("facility", "Former staff member"))
