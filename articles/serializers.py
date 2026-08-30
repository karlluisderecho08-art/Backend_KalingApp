from rest_framework import serializers

from .models import Article, ArticleComment, ResourceLink


class ArticleCommentSerializer(serializers.ModelSerializer):
    """
    Read shape for a comment. `author_name` stands in for the Kotlin
    model's free-text `authorName` field -- except here it's derived from
    the real logged-in user instead of a string the client could fake.
    """

    author_name = serializers.SerializerMethodField()

    class Meta:
        model = ArticleComment
        fields = ["id", "article", "author_name", "text", "created_at", "is_reported", "report_reason"]
        read_only_fields = ["id", "author_name", "created_at", "is_reported", "report_reason"]

    def get_author_name(self, obj) -> str:
        return obj.author.mom_name or obj.author.email


class ReportedCommentSerializer(ArticleCommentSerializer):
    """Same shape as ArticleCommentSerializer plus the article's title,
    for the admin Moderation screen -- one flat list across every
    article instead of the client fetching each article to find its
    comments."""

    article_title = serializers.CharField(source="article.title", read_only=True)

    class Meta(ArticleCommentSerializer.Meta):
        fields = ArticleCommentSerializer.Meta.fields + ["article_title"]


class ArticleCommentCreateSerializer(serializers.ModelSerializer):
    """Write shape for POSTing a new comment -- only `text` is client input."""

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

    comments = ArticleCommentSerializer(many=True, read_only=True)

    class Meta:
        model = Article
        fields = [
            "id", "title", "category", "read_time", "teaser", "content",
            "author", "rating", "evidence_label", "date", "comments",
        ]


class AdminArticleSerializer(serializers.ModelSerializer):
    """Read/write shape for the platform admin's Knowledge Base CRUD --
    no nested comments (unlike ArticleSerializer), since writing an
    article never touches its comments."""

    class Meta:
        model = Article
        fields = [
            "id", "title", "category", "read_time", "teaser", "content",
            "author", "rating", "evidence_label", "date",
        ]


class ResourceLinkSerializer(serializers.ModelSerializer):
    class Meta:
        model = ResourceLink
        fields = ["id", "title", "description", "url", "type"]


class ReportCommentSerializer(serializers.Serializer):
    reason = serializers.ChoiceField(choices=ArticleComment.ReportReason.choices)


class ResolveCommentSerializer(serializers.Serializer):
    ACTION_CHOICES = [("remove", "remove"), ("no_violation", "no_violation")]
    action = serializers.ChoiceField(choices=ACTION_CHOICES)
