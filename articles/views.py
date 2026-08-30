from drf_spectacular.utils import extend_schema
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from core.audit import log_action

from .models import Article, ArticleComment, ResourceLink
from .serializers import (
    AdminArticleSerializer,
    ArticleCommentCreateSerializer,
    ArticleCommentSerializer,
    ArticleListSerializer,
    ArticleSerializer,
    ReportCommentSerializer,
    ReportedCommentSerializer,
    ResolveCommentSerializer,
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

    Any authenticated user (not just the comment's author) can flag a
    comment -- this only raises the flag. Deciding what happens next is
    a staff-only decision, made in ArticleCommentResolveView below.
    """

    permission_classes = [permissions.IsAuthenticated]
    serializer_class = ReportCommentSerializer

    def post(self, request, pk):
        comment = generics.get_object_or_404(ArticleComment, pk=pk)
        serializer = ReportCommentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        comment.is_reported = True
        comment.report_reason = serializer.validated_data["reason"]
        comment.save(update_fields=["is_reported", "report_reason"])
        return Response(ArticleCommentSerializer(comment).data)


class ReportedCommentListView(generics.ListAPIView):
    """
    GET /articles/comments/reported/ -- every currently-flagged comment,
    across all articles, newest first. The admin Moderation queue: a
    comment leaves this list the moment ArticleCommentResolveView below
    clears or deletes it, so there's no separate pending/approved/
    rejected status to track here -- "flagged" is the whole queue.
    """

    queryset = ArticleComment.objects.filter(is_reported=True).select_related("article", "author")
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
        log_action(request.user, "comment.dismissed_report", f"ArticleComment:{comment.id}")
        return Response(ArticleCommentSerializer(comment).data)


class ResourceLinkListView(generics.ListAPIView):
    """GET /articles/resource-links/ -- the Milk Expression Guides list."""

    queryset = ResourceLink.objects.all()
    serializer_class = ResourceLinkSerializer
    permission_classes = [permissions.AllowAny]
