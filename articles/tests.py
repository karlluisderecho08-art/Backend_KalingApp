from importlib import import_module

from django.apps import apps
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APITestCase

from accounts.models import User
from core.models import AuditLogEntry
from notifications.models import NotificationItem

from .formatting import normalize_markers
from .models import Article, ArticleComment, CommentReport, ResourceLink, SavedArticle


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


class AdminArticleDateAddedTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            email="kb-dates@example.com", password="x", is_active=True, is_staff=True,
        )
        self.client.force_authenticate(user=self.admin)
        self.payload = dict(
            title="New Guide", category=Article.Category.NEWBORN, read_time="3 min read",
            teaser="Teaser.", content="Body.", author="Dr. Cruz", date="October 2026",
        )

    def test_a_new_article_records_when_it_was_added(self):
        response = self.client.post("/articles/admin/", self.payload, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertIsNotNone(response.data["created_at"])
        listed = self.client.get("/articles/admin/").data
        self.assertEqual(listed[0]["created_at"], response.data["created_at"])

    def test_the_client_cannot_set_or_change_it(self):
        response = self.client.post(
            "/articles/admin/", {**self.payload, "created_at": "2001-01-01T00:00:00Z"}, format="json",
        )
        self.assertFalse(response.data["created_at"].startswith("2001"))

        added = response.data["created_at"]
        self.client.patch(
            f"/articles/admin/{response.data['id']}/", {"created_at": "2001-01-01T00:00:00Z"}, format="json",
        )
        self.assertEqual(self.client.get("/articles/admin/").data[0]["created_at"], added)

    def test_articles_from_before_the_field_have_none(self):
        old = make_article()
        Article.objects.filter(pk=old.pk).update(created_at=None)

        self.assertIsNone(self.client.get("/articles/admin/").data[0]["created_at"])


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


class SavedArticleTests(APITestCase):
    """
    Bookmarks used to live only in the app's memory, so signing out lost
    them. They are stored per account on the server instead.
    """

    def setUp(self):
        self.mother = User.objects.create_user(email="mother@example.com", password="x", is_active=True)
        self.other = User.objects.create_user(email="other@example.com", password="x", is_active=True)
        self.first = make_article(title="First")
        self.second = make_article(title="Second")

    def save_article(self, article):
        return self.client.put(f"/articles/{article.pk}/save/")

    def test_a_saved_article_is_still_there_after_signing_in_again(self):
        self.client.force_authenticate(self.mother)
        self.save_article(self.first)

        self.client.force_authenticate(None)
        self.client.force_authenticate(User.objects.get(email="mother@example.com"))
        response = self.client.get("/articles/saved/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["article_ids"], [self.first.pk])

    def test_saving_twice_keeps_one_bookmark(self):
        self.client.force_authenticate(self.mother)
        self.save_article(self.first)
        response = self.save_article(self.first)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["article_ids"], [self.first.pk])
        self.assertEqual(SavedArticle.objects.count(), 1)

    def test_newest_save_comes_first(self):
        self.client.force_authenticate(self.mother)
        self.save_article(self.first)
        self.save_article(self.second)

        self.assertEqual(
            self.client.get("/articles/saved/").data["article_ids"], [self.second.pk, self.first.pk]
        )

    def test_unsaving_removes_it_and_answers_with_what_is_left(self):
        self.client.force_authenticate(self.mother)
        self.save_article(self.first)
        self.save_article(self.second)

        response = self.client.delete(f"/articles/{self.first.pk}/save/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["article_ids"], [self.second.pk])

    def test_unsaving_something_never_saved_is_fine(self):
        self.client.force_authenticate(self.mother)

        self.assertEqual(self.client.delete(f"/articles/{self.first.pk}/save/").status_code, 200)

    def test_one_mother_never_sees_anothers_bookmarks(self):
        self.client.force_authenticate(self.mother)
        self.save_article(self.first)

        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get("/articles/saved/").data["article_ids"], [])
        self.client.delete(f"/articles/{self.first.pk}/save/")

        self.client.force_authenticate(self.mother)
        self.assertEqual(self.client.get("/articles/saved/").data["article_ids"], [self.first.pk])

    def test_saving_an_article_that_does_not_exist_is_a_404(self):
        self.client.force_authenticate(self.mother)

        self.assertEqual(self.client.put("/articles/99999/save/").status_code, 404)

    def test_signed_out_requests_are_rejected(self):
        self.assertEqual(self.client.get("/articles/saved/").status_code, 401)
        self.assertEqual(self.client.put(f"/articles/{self.first.pk}/save/").status_code, 401)
        self.assertEqual(self.client.delete(f"/articles/{self.first.pk}/save/").status_code, 401)

    def test_deleting_an_article_drops_its_bookmarks(self):
        self.client.force_authenticate(self.mother)
        self.save_article(self.first)
        self.first.delete()

        self.assertEqual(self.client.get("/articles/saved/").data["article_ids"], [])


class AdminResourceLinkTests(APITestCase):
    """
    Educational video / guide links used to be editable only in Django's own
    admin site. The dashboard's Knowledge Base now manages them, and whatever
    it saves is what the app's public list returns.
    """

    URL = "/articles/resource-links/admin/"

    def setUp(self):
        self.admin = User.objects.create_user(email="admin@example.com", password="x", is_staff=True, is_active=True)
        self.mother = User.objects.create_user(email="mother@example.com", password="x", is_active=True)
        self.client.force_authenticate(self.admin)

    def video(self, **overrides):
        data = {
            "title": "Hand expression",
            "description": "How to express milk by hand.",
            "url": "https://www.youtube.com/watch?v=abc123",
            "type": "Video",
        }
        data.update(overrides)
        return data

    def test_an_admin_can_add_a_video_link(self):
        response = self.client.post(self.URL, self.video(), format="json")

        self.assertEqual(response.status_code, 201)
        self.assertEqual(ResourceLink.objects.get().url, "https://www.youtube.com/watch?v=abc123")

    def test_what_the_admin_adds_reaches_the_public_list_the_app_reads(self):
        self.client.post(self.URL, self.video(), format="json")

        self.client.force_authenticate(None)
        shown = self.client.get("/articles/resource-links/")

        self.assertEqual(shown.status_code, 200)
        self.assertEqual([link["title"] for link in shown.data], ["Hand expression"])
        self.assertEqual(shown.data[0]["type"], "Video")

    def test_an_admin_can_edit_a_link(self):
        link = ResourceLink.objects.create(**self.video())

        response = self.client.patch(f"{self.URL}{link.id}/", {"url": "https://example.org/new"}, format="json")

        self.assertEqual(response.status_code, 200)
        link.refresh_from_db()
        self.assertEqual(link.url, "https://example.org/new")

    def test_an_admin_can_delete_a_link(self):
        link = ResourceLink.objects.create(**self.video())

        self.assertEqual(self.client.delete(f"{self.URL}{link.id}/").status_code, 204)
        self.assertFalse(ResourceLink.objects.exists())

    def test_the_admin_list_returns_every_link_oldest_first(self):
        ResourceLink.objects.create(**self.video(title="First"))
        ResourceLink.objects.create(**self.video(title="Second", type="Infographic"))

        titles = [link["title"] for link in self.client.get(self.URL).data]

        self.assertEqual(titles, ["First", "Second"])

    def test_only_web_links_are_accepted(self):
        for bad in ("ftp://example.org/video", "javascript:alert(1)", "not a link", ""):
            response = self.client.post(self.URL, self.video(url=bad), format="json")
            self.assertEqual(response.status_code, 400, bad)
            self.assertIn("url", response.data)
        self.assertFalse(ResourceLink.objects.exists())

    def test_the_type_must_be_video_or_infographic(self):
        response = self.client.post(self.URL, self.video(type="Podcast"), format="json")

        self.assertEqual(response.status_code, 400)

    def test_a_title_and_a_link_are_required(self):
        response = self.client.post(self.URL, {"type": "Video"}, format="json")

        self.assertEqual(response.status_code, 400)
        self.assertIn("title", response.data)
        self.assertIn("url", response.data)

    def test_mothers_and_signed_out_visitors_cannot_manage_links(self):
        link = ResourceLink.objects.create(**self.video())

        self.client.force_authenticate(self.mother)
        self.assertEqual(self.client.post(self.URL, self.video(), format="json").status_code, 403)
        self.assertEqual(self.client.patch(f"{self.URL}{link.id}/", {"title": "x"}, format="json").status_code, 403)
        self.assertEqual(self.client.delete(f"{self.URL}{link.id}/").status_code, 403)

        self.client.force_authenticate(None)
        self.assertEqual(self.client.post(self.URL, self.video(), format="json").status_code, 401)
        self.assertEqual(ResourceLink.objects.count(), 1)

    def test_changes_are_audit_logged(self):
        created = self.client.post(self.URL, self.video(), format="json").data
        self.client.patch(f"{self.URL}{created['id']}/", {"title": "Renamed"}, format="json")
        self.client.delete(f"{self.URL}{created['id']}/")

        actions = list(AuditLogEntry.objects.filter(actor=self.admin).order_by("id").values_list("action", flat=True))
        self.assertEqual(actions, ["resource_link.created", "resource_link.updated", "resource_link.deleted"])
