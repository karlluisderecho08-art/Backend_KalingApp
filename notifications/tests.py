from django.test import TestCase

from accounts.models import User

from .models import NotificationItem
from .services import notify_many


class NotifyManyTests(TestCase):
    """
    notify_many() is the one INSERT every multi-recipient notification
    (a facility's staff, every admin) goes through -- see
    milkbank/views.py and articles/views.py for its callers.
    """

    def setUp(self):
        self.alice = User.objects.create_user(email="alice@example.com", password="x", is_active=True)
        self.bob = User.objects.create_user(email="bob@example.com", password="x", is_active=True)
        self.carol = User.objects.create_user(email="carol@example.com", password="x", is_active=True)

    def test_creates_one_notification_per_owner(self):
        notify_many(
            [self.alice, self.bob], "Title", "Description", NotificationItem.Category.BOOKINGS
        )
        self.assertTrue(NotificationItem.objects.filter(owner=self.alice, title="Title").exists())
        self.assertTrue(NotificationItem.objects.filter(owner=self.bob, title="Title").exists())

    def test_does_not_touch_an_owner_left_out_of_the_list(self):
        notify_many([self.alice], "Title", "Description", NotificationItem.Category.BOOKINGS)
        self.assertFalse(NotificationItem.objects.filter(owner=self.carol).exists())

    def test_accepts_a_queryset_not_just_a_list(self):
        # Every real caller passes a queryset (User.objects.filter(...)),
        # not a list -- bulk_create only needs something iterable, but
        # this is what actually gets called in production.
        notify_many(
            User.objects.filter(email__in=[self.alice.email, self.bob.email]),
            "Title", "Description", NotificationItem.Category.ARTICLES,
        )
        self.assertEqual(
            NotificationItem.objects.filter(title="Title", category=NotificationItem.Category.ARTICLES).count(),
            2,
        )

    def test_an_empty_iterable_creates_nothing_and_does_not_raise(self):
        notify_many([], "Title", "Description", NotificationItem.Category.REMINDERS)
        self.assertEqual(NotificationItem.objects.count(), 0)

    def test_each_notification_starts_unread(self):
        notify_many([self.alice], "Title", "Description", NotificationItem.Category.BOOKINGS)
        notification = NotificationItem.objects.get(owner=self.alice)
        self.assertFalse(notification.is_read)
