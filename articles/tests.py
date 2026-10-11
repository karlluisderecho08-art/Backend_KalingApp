from importlib import import_module

from django.apps import apps
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APITestCase

from accounts.models import User
from core.models import AuditLogEntry
from notifications.models import NotificationItem

from .formatting import normalize_markers
from .models import Article, ArticleComment, CommentReport


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

class NormalizeMarkersTests(SimpleTestCase):
    """
    articles.formatting.normalize_markers -- repairs a bold/italic pair split
    across a line break (the old admin editor wrapped a triple-clicked line,
    line break included, giving "**heading\n**", which the app shows as
    literal asterisks), and touches nothing else.
    """

    def assertNormalized(self, src, want):
        self.assertEqual(normalize_markers(src), want)

    def test_the_live_defect_a_closer_left_on_its_own_line(self):
        self.assertNormalized(
            "intro\n\n**1. Getting a good latch\n**\nA proper latch",
            "intro\n\n**1. Getting a good latch**\nA proper latch",
        )

    def test_an_opener_left_at_the_end_of_the_previous_line(self):
        self.assertNormalized("p\n**\nHeading**\nbody", "p\n**Heading**\nbody")

    def test_a_multi_line_selection_is_applied_per_line(self):
        self.assertNormalized("**one\ntwo\n**\nnext", "**one**\n**two**\nnext")
        self.assertNormalized("**one\ntwo**", "**one**\n**two**")

    def test_italics_are_repaired_the_same_way(self):
        self.assertNormalized("*it\n*", "*it*")

    def test_well_formed_text_is_returned_unchanged(self):
        for src in ("plain", "a **b** c\n*d*", "keep\r\nCRLF **ok**", "* a\n* b\n* c", ""):
            with self.subTest(src=src):
                self.assertNormalized(src, src)

    def test_never_spans_a_paragraph(self):
        self.assertNormalized("**unclosed\n\nnext **para**", "**unclosed\n\nnext **para**")

    def test_ambiguous_markers_are_left_alone(self):
        for src in ("**a\nb **c**", "5 *x\ny* z", "**l1\n* bullet\n**"):
            with self.subTest(src=src):
                self.assertNormalized(src, src)


class AdminArticleSaveRepairsMarkersTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            email="kb-admin@example.com", password="x", is_active=True, is_staff=True,
        )
        self.client.force_authenticate(user=self.admin)
        self.article = make_article()

    def test_saving_split_markers_stores_them_repaired(self):
        response = self.client.patch(
            f"/articles/admin/{self.article.id}/",
            {"content": "Intro\n\n**1. Getting a good latch\n**\nBody"},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.article.refresh_from_db()
        self.assertEqual(self.article.content, "Intro\n\n**1. Getting a good latch**\nBody")

    def test_the_migration_repairs_stored_articles(self):
        broken = make_article(content="Intro\n\n**Heading\n**\nBody")
        fine = make_article(content="Already **fine**.")

        repair = import_module("articles.migrations.0003_repair_split_formatting_markers").repair
        repair(apps, None)

        broken.refresh_from_db()
        fine.refresh_from_db()
        self.assertEqual(broken.content, "Intro\n\n**Heading**\nBody")
        self.assertEqual(fine.content, "Already **fine**.")


@override_settings(COMMENT_AUTO_REMOVE_REPORTS=3)
class CommentAutoRemovalTests(APITestCase):
    """
    A comment reported by settings.COMMENT_AUTO_REMOVE_REPORTS different users
    is deleted automatically (30 in production; 3 here so each test stays
    small -- the default itself is checked separately).
    """

    def setUp(self):
        self.article = make_article()
        self.author = User.objects.create_user(email="author@example.com", password=None, is_active=True)
        self.admin = User.objects.create_user(
            email="mod@example.com", password=None, is_active=True, is_staff=True,
        )
        self.comment = ArticleComment.objects.create(article=self.article, author=self.author, text="Buy now!")
        self.reporters = [
            User.objects.create_user(email=f"r{n}@example.com", password=None, is_active=True) for n in range(5)
        ]

    def report(self, user, reason="Spam", comment=None):
        self.client.force_authenticate(user=user)
        return self.client.post(f"/articles/comments/{(comment or self.comment).id}/report/", {"reason": reason})

    def test_the_threshold_report_removes_the_comment(self):
        first = self.report(self.reporters[0])
        second = self.report(self.reporters[1])
        third = self.report(self.reporters[2])

        self.assertEqual((first.data["report_count"], first.data["removed"]), (1, False))
        self.assertEqual((second.data["report_count"], second.data["removed"]), (2, False))
        self.assertEqual(third.status_code, 200)
        self.assertEqual((third.data["report_count"], third.data["removed"]), (3, True))
        self.assertEqual(third.data["id"], self.comment.id)  # still the comment, for older apps
        self.assertFalse(ArticleComment.objects.filter(pk=self.comment.pk).exists())
        self.assertFalse(CommentReport.objects.exists())

    def test_one_person_reporting_again_and_again_counts_once(self):
        for _ in range(5):
            response = self.report(self.reporters[0])
        self.assertEqual(response.data["report_count"], 1)
        self.assertFalse(response.data["removed"])
        self.assertTrue(ArticleComment.objects.filter(pk=self.comment.pk).exists())

    def test_below_the_threshold_it_stays_and_waits_for_an_admin(self):
        self.report(self.reporters[0])
        self.report(self.reporters[1])

        self.comment.refresh_from_db()
        self.assertTrue(self.comment.is_reported)
        self.assertEqual(self.comment.reports.count(), 2)

    def test_the_author_and_admins_are_told_and_it_is_audit_logged(self):
        for user in self.reporters[:3]:
            self.report(user)

        notice = NotificationItem.objects.get(owner=self.author, title="Comment Removed")
        self.assertIn("Latching Basics", notice.description)
        self.assertIn("3", notice.description)
        self.assertTrue(
            NotificationItem.objects.filter(owner=self.admin, title="Comment Removed Automatically").exists()
        )
        entry = AuditLogEntry.objects.get(action="comment.auto_removed")
        self.assertIsNone(entry.actor)
        self.assertIn(f"ArticleComment:{self.comment.id}", entry.target)

    def test_admins_are_notified_on_the_first_report_only(self):
        self.report(self.reporters[0])
        self.report(self.reporters[1])
        self.assertEqual(NotificationItem.objects.filter(owner=self.admin, title="Comment Reported").count(), 1)

    def test_no_violation_resets_the_count(self):
        self.report(self.reporters[0])
        self.report(self.reporters[1])
        self.client.force_authenticate(user=self.admin)
        self.client.post(f"/articles/comments/{self.comment.id}/resolve/", {"action": "no_violation"})

        # Two earlier reports no longer count: it takes three fresh ones.
        self.report(self.reporters[2])
        self.report(self.reporters[3])
        self.assertTrue(ArticleComment.objects.filter(pk=self.comment.pk).exists())
        self.assertTrue(self.report(self.reporters[4]).data["removed"])

    def test_reports_on_other_comments_do_not_add_up(self):
        other = ArticleComment.objects.create(article=self.article, author=self.author, text="Fine.")
        self.report(self.reporters[0])
        self.report(self.reporters[1], comment=other)
        self.report(self.reporters[2], comment=other)

        self.assertTrue(ArticleComment.objects.filter(pk=self.comment.pk).exists())
        self.assertTrue(ArticleComment.objects.filter(pk=other.pk).exists())

    def test_the_moderation_queue_shows_the_count_and_reasons(self):
        self.report(self.reporters[0], reason="Spam")
        self.report(self.reporters[1], reason="Misinformation")
        self.client.force_authenticate(user=self.admin)

        data = self.client.get("/articles/comments/reported/").data
        rows = data["results"] if isinstance(data, dict) else data

        self.assertEqual(rows[0]["report_count"], 2)
        self.assertEqual(rows[0]["report_reasons"], {"Spam": 1, "Misinformation": 1})

    def test_reporting_a_comment_that_is_already_gone_is_a_404(self):
        for user in self.reporters[:3]:
            self.report(user)
        self.assertEqual(self.report(self.reporters[3]).status_code, 404)


class CommentAutoRemovalDefaultTests(SimpleTestCase):
    def test_the_placeholder_threshold_is_30(self):
        from django.conf import settings

        self.assertEqual(settings.COMMENT_AUTO_REMOVE_REPORTS, 30)
