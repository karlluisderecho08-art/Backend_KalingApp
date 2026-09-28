from rest_framework.test import APITestCase

from accounts.models import User
from notifications.models import NotificationItem

from .models import Article, ArticleComment


def make_article(**overrides):
    defaults = dict(
        title="Latching Basics",
        category=Article.Category.LATCHING,
        read_time="5 min read",
        teaser="A short teaser.",
        content="Full article content.",
        author="Dr. Santos",
        date="May 2026",
    )
    defaults.update(overrides)
    return Article.objects.create(**defaults)


class ReportCommentNotifiesAdminsTests(APITestCase):
    """
    A reported comment used to just sit in the Moderation tab until an
    admin happened to open it -- nothing told them one had come in. See
    ArticleCommentReportView.
    """

    def setUp(self):
        self.article = make_article()
        self.comment_author = User.objects.create_user(
            email="mother@example.com", password="x", is_active=True,
        )
        self.reporter = User.objects.create_user(
            email="reporter@example.com", password="x", is_active=True,
        )
        self.admin = User.objects.create_user(
            email="admin@example.com", password="x", is_active=True, is_staff=True,
        )
        self.other_admin = User.objects.create_user(
            email="admin2@example.com", password="x", is_active=True, is_staff=True,
        )
        self.comment = ArticleComment.objects.create(
            article=self.article, author=self.comment_author, text="A comment.",
        )

    def test_reporting_a_comment_notifies_every_admin(self):
        self.client.force_authenticate(user=self.reporter)
        response = self.client.post(
            f"/articles/comments/{self.comment.id}/report/", {"reason": "Spam"}
        )
        self.assertEqual(response.status_code, 200)
        for admin in (self.admin, self.other_admin):
            self.assertTrue(
                NotificationItem.objects.filter(owner=admin, title="Comment Reported").exists()
            )

    def test_reporting_a_comment_does_not_notify_non_admins(self):
        self.client.force_authenticate(user=self.reporter)
        self.client.post(f"/articles/comments/{self.comment.id}/report/", {"reason": "Spam"})
        self.assertFalse(
            NotificationItem.objects.filter(owner=self.reporter, title="Comment Reported").exists()
        )
        self.assertFalse(
            NotificationItem.objects.filter(
                owner=self.comment_author, title="Comment Reported"
            ).exists()
        )

    def test_notification_names_the_article_and_reason(self):
        self.client.force_authenticate(user=self.reporter)
        self.client.post(
            f"/articles/comments/{self.comment.id}/report/", {"reason": "Misinformation"}
        )
        notification = NotificationItem.objects.get(owner=self.admin, title="Comment Reported")
        self.assertIn("Latching Basics", notification.description)
        self.assertIn("Misinformation", notification.description)
        self.assertEqual(notification.category, NotificationItem.Category.ARTICLES)
