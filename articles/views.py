from django.conf import settings
from django.db import IntegrityError, transaction
from drf_spectacular.utils import extend_schema
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import User
from core.audit import log_action
from notifications.models import NotificationItem
from notifications.services import notify, notify_many

from .models import Article, ArticleComment, CommentReport, ResourceLink, SavedArticle
from .serializers import (
    AdminArticleSerializer,
    ArticleCommentCreateSerializer,
    ArticleCommentSerializer,
    ArticleListSerializer,
    ArticleSerializer,
    ReportCommentSerializer,
    ReportedCommentSerializer,
    ResolveCommentSerializer,
    SavedArticleIdsSerializer,
    ResourceLinkSerializer,
)


class ArticleListView(generics.ListAPIView):
    """GET /articles/ -- replaces the hardcoded list in loadStaticData()."""

    queryset = Article.objects.all()
    serializer_class = ArticleListSerializer
    permission_classes = [permissions.AllowAny]


class ArticleDetailView(generics.RetrieveAPIView):
    """GET /articles/<id>/ -- full body + nested comments."""

    queryset = Article.objects.all()
    serializer_class = ArticleSerializer
    permission_classes = [permissions.AllowAny]


class AdminResourceLinkListCreateView(generics.ListCreateAPIView):
    """
    GET  /articles/resource-links/admin/ -- every educational video / guide link.
    POST /articles/resource-links/admin/ -- add one.
    Platform-admin only (is_staff). What is saved here is what mothers see in
    the Knowledge Hub, via the public GET /articles/resource-links/.
    """

    queryset = ResourceLink.objects.order_by("id")
    serializer_class = ResourceLinkSerializer
    permission_classes = [permissions.IsAdminUser]
    pagination_class = None

    def perform_create(self, serializer):
        link = serializer.save()
        log_action(self.request.user, "resource_link.created", f"ResourceLink:{link.id} ({link.title})"[:255])


class AdminResourceLinkDetailView(generics.RetrieveUpdateDestroyAPIView):
    """PATCH/PUT/DELETE /articles/resource-links/admin/<id>/ -- platform-admin only."""

    queryset = ResourceLink.objects.all()
    serializer_class = ResourceLinkSerializer
    permission_classes = [permissions.IsAdminUser]

    def perform_update(self, serializer):
        link = serializer.save()
        log_action(self.request.user, "resource_link.updated", f"ResourceLink:{link.id} ({link.title})"[:255])

    def perform_destroy(self, instance):
        log_action(self.request.user, "resource_link.deleted", f"ResourceLink:{instance.id} ({instance.title})"[:255])
        instance.delete()


class AdminArticleListCreateView(generics.ListCreateAPIView):
    """
    GET  /articles/admin/ -- every article, for the platform admin's
    Knowledge Base table (ArticleListView's lighter shape omits
    `content`, which the admin table needs for the edit form).
    POST /articles/admin/ -- create one.
    Platform-admin only (is_staff).
    """

    queryset = Article.objects.all()
    serializer_class = AdminArticleSerializer
    permission_classes = [permissions.IsAdminUser]


class AdminArticleDetailView(generics.RetrieveUpdateDestroyAPIView):
    """PATCH/PUT/DELETE /articles/admin/<id>/ -- platform-admin only."""

    queryset = Article.objects.all()
    serializer_class = AdminArticleSerializer
    permission_classes = [permissions.IsAdminUser]


class ArticleCommentCreateView(generics.CreateAPIView):
    """POST /articles/<id>/comments/  {text} -- add a comment. Requires login."""

    serializer_class = ArticleCommentCreateSerializer
    permission_classes = [permissions.IsAuthenticated]

    def perform_create(self, serializer):
        article = generics.get_object_or_404(Article, pk=self.kwargs["pk"])
        serializer.save(article=article, author=self.request.user)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        # Respond with the full read shape (incl. author_name), not the
        # bare {"text": ...} the create serializer would echo back.
        return Response(
            ArticleCommentSerializer(serializer.instance).data,
            status=status.HTTP_201_CREATED,
        )


class IsCommentAuthor(permissions.BasePermission):
    """Only the person who wrote a comment may delete it."""

    def has_object_permission(self, request, view, obj):
        return obj.author_id == request.user.id


class ArticleCommentDeleteView(generics.DestroyAPIView):
    """DELETE /articles/comments/<id>/ -- author-only delete."""

    queryset = ArticleComment.objects.all()
    serializer_class = ArticleCommentSerializer
    permission_classes = [permissions.IsAuthenticated, IsCommentAuthor]

    @extend_schema(request=None)
    def delete(self, request, *args, **kwargs):
        return super().delete(request, *args, **kwargs)


class ArticleCommentReportView(APIView):
    """
    POST /articles/comments/<id>/report/  {reason}

    Any authenticated user can report a comment. Each user counts once per
    comment (CommentReport); reporting it again changes nothing. Deciding
    what happens next is normally a staff decision (ArticleCommentResolveView
    below) -- except that once settings.COMMENT_AUTO_REMOVE_REPORTS different
    users have reported it, the comment is deleted straight away, without
    waiting for an admin to get to it.

    Always answers with the comment as it stood, plus `report_count` and
    `removed` (true when this report was the one that removed it), so the
    app can drop it from the list immediately. A client that predates
    `removed` just sees a reported comment that is gone on its next refresh.
    """

    permission_classes = [permissions.IsAuthenticated]
    serializer_class = ReportCommentSerializer

    def post(self, request, pk):
        serializer = ReportCommentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        reason = serializer.validated_data["reason"]

        with transaction.atomic():
            # Locked so two reports landing together are counted one after
            # the other, and the comment can only be removed once.
            comment = generics.get_object_or_404(
                ArticleComment.objects.select_for_update().select_related("article", "author"), pk=pk
            )
            # The flag, not the report rows: comments flagged before reports
            # were counted have no rows, and admins already know about those.
            first_report = not comment.is_reported
            try:
                with transaction.atomic():
                    CommentReport.objects.create(comment=comment, reporter=request.user, reason=reason)
                newly_reported = True
            except IntegrityError:
                newly_reported = False  # she already reported it; it still counts once

            comment.is_reported = True
            comment.report_reason = reason if newly_reported else (comment.report_reason or reason)
            comment.save(update_fields=["is_reported", "report_reason"])
            report_count = comment.reports.count()
            data = dict(ArticleCommentSerializer(comment).data, report_count=report_count, removed=False)

            if newly_reported:
                log_action(request.user, "comment.reported", f"ArticleComment:{comment.id} ({reason})")

            if report_count >= settings.COMMENT_AUTO_REMOVE_REPORTS:
                self._remove(comment, report_count)
                data["removed"] = True
                return Response(data)

        # Admins hear about a comment once, when it is first reported -- not
        # once per report, which at the removal threshold would be dozens of
        # notifications for a single comment. The Moderation queue shows the
        # running count.
        if newly_reported and first_report:
            notify_many(
                User.objects.filter(is_staff=True, is_active=True),
                "Comment Reported",
                f"A comment on \"{comment.article.title}\" was reported for {reason}.",
                NotificationItem.Category.ARTICLES,
            )
        return Response(data)

    @staticmethod
    def _remove(comment, report_count):
        target = f"ArticleComment:{comment.id}"
        article_title = comment.article.title
        author = comment.author
        comment.delete()
        # No human actor: the threshold did this, which is what a null actor
        # means in the audit log.
        log_action(None, "comment.auto_removed", f"{target} after {report_count} reports")
        notify(
            author,
            "Comment Removed",
            f"Your comment on \"{article_title}\" was removed after it was reported by "
            f"{report_count} members of the community.",
            NotificationItem.Category.ARTICLES,
        )
        notify_many(
            User.objects.filter(is_staff=True, is_active=True),
            "Comment Removed Automatically",
            f"A comment on \"{article_title}\" reached {report_count} reports and was removed.",
            NotificationItem.Category.ARTICLES,
        )


class ReportedCommentListView(generics.ListAPIView):
    """
    GET /articles/comments/reported/ -- every currently-flagged comment,
    across all articles, newest first. The admin Moderation queue: a
    comment leaves this list the moment ArticleCommentResolveView below
    clears or deletes it, so there's no separate pending/approved/
    rejected status to track here -- "flagged" is the whole queue.
    """

    queryset = (
        ArticleComment.objects.filter(is_reported=True)
        .select_related("article", "author")
        .prefetch_related("reports")
    )
    serializer_class = ReportedCommentSerializer
    permission_classes = [permissions.IsAdminUser]


class ArticleCommentResolveView(APIView):
    """
    POST /articles/comments/<id>/resolve/  {"action": "remove"} or {"action": "no_violation"}

    Staff-only (Django's built-in is_staff flag -- the same flag that
    lets someone into /admin/). This is the real version of the Kotlin
    app's two demo buttons, "Simulate Moderation Remove" and "Simulate
    Moderation No Violation": a human with admin rights looks at a
    flagged comment and decides whether it actually breaks the rules.
    """

    permission_classes = [permissions.IsAdminUser]
    serializer_class = ResolveCommentSerializer

    def post(self, request, pk):
        comment = generics.get_object_or_404(ArticleComment, pk=pk)
        serializer = ResolveCommentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        action = serializer.validated_data["action"]

        if action == "remove":
            target = f"ArticleComment:{comment.id}"
            comment.delete()
            log_action(request.user, "comment.removed", target)
            return Response(status=status.HTTP_204_NO_CONTENT)

        comment.is_reported = False
        comment.report_reason = ""
        comment.save(update_fields=["is_reported", "report_reason"])
        # Staff looked and kept it, so the reports they judged no longer count
        # toward automatic removal -- it would take that many fresh reports.
        comment.reports.all().delete()
        log_action(request.user, "comment.dismissed_report", f"ArticleComment:{comment.id}")
        return Response(ArticleCommentSerializer(comment).data)


class ResourceLinkListView(generics.ListAPIView):
    """GET /articles/resource-links/ -- the Milk Expression Guides list."""

    queryset = ResourceLink.objects.all()
    serializer_class = ResourceLinkSerializer
    permission_classes = [permissions.AllowAny]


class SavedArticleListView(APIView):
    """
    GET /articles/saved/ -- the ids of the articles the signed-in mother has
    bookmarked, newest first: {"article_ids": [7, 3]}. The app already holds
    the article list, so ids are all it needs to mark and filter it.
    """

    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(responses={200: SavedArticleIdsSerializer})
    def get(self, request):
        ids = list(
            SavedArticle.objects.filter(user=request.user).values_list("article_id", flat=True)
        )
        return Response({"article_ids": ids})


class SavedArticleDetailView(APIView):
    """
    PUT    /articles/<id>/save/ -- bookmark the article.
    DELETE /articles/<id>/save/ -- remove the bookmark.

    Both are idempotent, so a retry after a dropped connection is harmless,
    and both answer with the full set of saved ids afterwards. That way the
    app ends up in step with the server even if two devices are tapping.
    """

    permission_classes = [permissions.IsAuthenticated]

    def _saved_ids(self, user):
        return list(SavedArticle.objects.filter(user=user).values_list("article_id", flat=True))

    @extend_schema(request=None, responses={200: SavedArticleIdsSerializer})
    def put(self, request, pk):
        article = generics.get_object_or_404(Article, pk=pk)
        SavedArticle.objects.get_or_create(user=request.user, article=article)
        return Response({"article_ids": self._saved_ids(request.user)})

    @extend_schema(responses={200: SavedArticleIdsSerializer})
    def delete(self, request, pk):
        SavedArticle.objects.filter(user=request.user, article_id=pk).delete()
        return Response({"article_ids": self._saved_ids(request.user)})
