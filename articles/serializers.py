from rest_framework import serializers

from .formatting import normalize_markers
from .models import Article, ArticleComment, CommentReport, ResourceLink


class ArticleCommentSerializer(serializers.ModelSerializer):
    """
    Read shape for a comment. `author_name` stands in for the Kotlin
    model's free-text `authorName` field -- except here it's derived from
    the real logged-in user instead of a string the client could fake.
    """

    author_name = serializers.SerializerMethodField()
    # Whether the viewer wrote it (needs the request in the context), so the
    # app no longer guesses by comparing names -- two mothers can share one.
    is_mine = serializers.SerializerMethodField()

    class Meta:
        model = ArticleComment
        fields = ["id", "article", "author_name", "is_mine", "text", "created_at", "is_reported", "report_reason"]
        read_only_fields = ["id", "author_name", "is_mine", "created_at", "is_reported", "report_reason"]

    def get_author_name(self, obj) -> str:
        # Her chosen name, or nothing -- NEVER her email address, which other
        # mothers have no business seeing. The app shows a neutral label for
        # a comment with no name.
        return (obj.author.mom_name or "").strip()

    def get_is_mine(self, obj) -> bool:
        request = self.context.get("request")
        user = getattr(request, "user", None)
        return bool(user and user.is_authenticated and obj.author_id == user.id)


class ReportedCommentSerializer(ArticleCommentSerializer):
    """Same shape as ArticleCommentSerializer plus the article's title,
    for the admin Moderation screen -- one flat list across every
    article instead of the client fetching each article to find its
    comments."""

    article_title = serializers.CharField(source="article.title", read_only=True)
    # Platform admins only (ReportedCommentListView): who wrote it, so a
    # moderator can act on a comment whose author set no name.
    author_email = serializers.EmailField(source="author.email", read_only=True)
    # How many different users have reported it, and for what -- so a
    # moderator can tell one complaint from twenty-nine. A comment reaching
    # settings.COMMENT_AUTO_REMOVE_REPORTS is deleted before it gets here.
    report_count = serializers.SerializerMethodField()
    report_reasons = serializers.SerializerMethodField()

    class Meta(ArticleCommentSerializer.Meta):
        fields = ArticleCommentSerializer.Meta.fields + ["author_email", "article_title", "report_count", "report_reasons"]

    def get_report_count(self, obj) -> int:
        return len(obj.reports.all())

    def get_report_reasons(self, obj) -> dict:
        counts = {}
        for report in obj.reports.all():
            counts[report.reason] = counts.get(report.reason, 0) + 1
        return counts


class ArticleCommentCreateSerializer(serializers.ModelSerializer):
    """Write shape for POSTing a new comment -- only `text` is client input."""

    # The model's TextField has no limit of its own; one comment can't be a novel.
    text = serializers.CharField(max_length=2000)

    class Meta:
        model = ArticleComment
        fields = ["text"]


class ArticleListSerializer(serializers.ModelSerializer):
    """
    Lighter shape for GET /articles/ -- no full `content` body and no
    nested comments, so the list endpoint stays cheap (mirrors the
    ArticleCard summary view in KnowledgeHubScreen, not the detail view).
    """

    class Meta:
        model = Article
        fields = [
            "id", "title", "category", "read_time", "teaser",
            "author", "rating", "evidence_label", "date",
        ]


class ArticleSerializer(serializers.ModelSerializer):
    """Full shape for GET /articles/<id>/ -- includes body + comments."""

    # Comments carry other mothers' names, so they are shown only to someone
    # signed in -- articles themselves stay public. An empty list (not a
    # missing key) for anyone else, which older app versions read safely.
    comments = serializers.SerializerMethodField()

    class Meta:
        model = Article
        fields = [
            "id", "title", "category", "read_time", "teaser", "content",
            "author", "rating", "evidence_label", "date", "comments",
        ]

    def get_comments(self, obj) -> list:
        request = self.context.get("request")
        if not (request and request.user and request.user.is_authenticated):
            return []
        return ArticleCommentSerializer(obj.comments.all(), many=True, context=self.context).data


class AdminArticleSerializer(serializers.ModelSerializer):
    """Read/write shape for the platform admin's Knowledge Base CRUD --
    no nested comments (unlike ArticleSerializer), since writing an
    article never touches its comments. `created_at` is read-only (the
    model sets it), so a client cannot backdate an article."""

    class Meta:
        model = Article
        fields = [
            "id", "title", "category", "read_time", "teaser", "content",
            "author", "rating", "evidence_label", "date", "created_at",
        ]
        read_only_fields = ["created_at"]

    def validate_content(self, value):
        # Bold/italic markers split across a line break render as literal
        # asterisks in the app -- repaired here so no client can store them.
        # See articles/formatting.py.
        return normalize_markers(value)


class ResourceLinkSerializer(serializers.ModelSerializer):
    class Meta:
        model = ResourceLink
        fields = ["id", "title", "description", "url", "type"]

    def validate_url(self, value):
        # The app opens this in whatever handles the link (a browser, the
        # YouTube app). Only web links belong here -- Django's URLField also
        # lets ftp:// through, which nothing on a phone would play.
        if not value.lower().startswith(("http://", "https://")):
            raise serializers.ValidationError("Enter a web link starting with http:// or https://")
        return value


class ReportCommentSerializer(serializers.Serializer):
    reason = serializers.ChoiceField(choices=ArticleComment.ReportReason.choices)


class ResolveCommentSerializer(serializers.Serializer):
    ACTION_CHOICES = [("remove", "remove"), ("no_violation", "no_violation")]
    action = serializers.ChoiceField(choices=ACTION_CHOICES)


class SavedArticleIdsSerializer(serializers.Serializer):
    """Response shape of the saved-articles endpoints (documentation only)."""

    article_ids = serializers.ListField(child=serializers.IntegerField())
