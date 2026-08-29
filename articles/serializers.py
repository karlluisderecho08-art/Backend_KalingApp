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
        fields = ["id", "article", "author_name", "text", "created_at", "is_reported"]
        read_only_fields = ["id", "author_name", "created_at", "is_reported"]

    def get_author_name(self, obj) -> str:
        return obj.author.mom_name or obj.author.email


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


class ResourceLinkSerializer(serializers.ModelSerializer):
    class Meta:
        model = ResourceLink
        fields = ["id", "title", "description", "url", "type"]


class ReportCommentSerializer(serializers.Serializer):
    reason = serializers.ChoiceField(choices=ArticleComment.ReportReason.choices)


class ResolveCommentSerializer(serializers.Serializer):
    ACTION_CHOICES = [("remove", "remove"), ("no_violation", "no_violation")]
    action = serializers.ChoiceField(choices=ACTION_CHOICES)
