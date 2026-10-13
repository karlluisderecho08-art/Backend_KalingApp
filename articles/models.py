from django.conf import settings
from django.db import models


class Article(models.Model):
    """
    A Knowledge Hub article. Ported field-for-field from the Kotlin
    Article data class (CODEBASE-1.md section 3) -- this replaces the
    hardcoded literals in loadStaticData().
    """

    class Category(models.TextChoices):
        LATCHING = "Latching Techniques", "Latching Techniques"
        STORAGE = "Milk Storage & Safety", "Milk Storage & Safety"
        NUTRITION = "Maternal Nutrition", "Maternal Nutrition"
        NEWBORN = "Newborn Health", "Newborn Health"

    title = models.CharField(max_length=255)
    category = models.CharField(max_length=50, choices=Category.choices)
    read_time = models.CharField(max_length=50, help_text='Display string, e.g. "5 min read"')
    teaser = models.CharField(max_length=500)
    content = models.TextField()
    author = models.CharField(max_length=150)
    rating = models.CharField(max_length=20, default="4.9 ★")
    evidence_label = models.CharField(
        max_length=255, default="Organization-Verified & Peer-Reviewed"
    )
    # Kept as a display string (like the Kotlin model) rather than a real
    # DateField, since the frontend never parses or sorts by it -- it's
    # shown as-is ("May 2026").
    date = models.CharField(max_length=50)

    def __str__(self):
        return self.title


class ArticleComment(models.Model):
    """
    A comment on an article. Unlike the Kotlin model (which stores a
    free-text authorName), this points at a real User -- now that
    accounts are real, "who posted this" can be an actual foreign key
    instead of a trusted string the client made up.
    """

    class ReportReason(models.TextChoices):
        SPAM = "Spam", "Spam"
        INAPPROPRIATE = "Inappropriate", "Inappropriate"
        MISINFORMATION = "Misinformation", "Misinformation"
        OTHER = "Other", "Other"

    article = models.ForeignKey(Article, related_name="comments", on_delete=models.CASCADE)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    is_reported = models.BooleanField(default=False)
    report_reason = models.CharField(
        max_length=20, choices=ReportReason.choices, blank=True
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.author} on {self.article_id}"


class CommentReport(models.Model):
    """
    One user's report of one comment.

    A comment used to carry only a reported flag and the latest reason, so
    there was nothing to count and one person could "report" it any number
    of times. One row per (comment, reporter) makes the count mean "how many
    different people reported this" -- which is what the automatic removal
    threshold (settings.COMMENT_AUTO_REMOVE_REPORTS) is measured against.

    Deleted with the comment. An admin's "No violation" also clears them, so
    a comment staff have reviewed and kept starts counting from zero again.
    """

    comment = models.ForeignKey(ArticleComment, related_name="reports", on_delete=models.CASCADE)
    reporter = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    reason = models.CharField(max_length=20, choices=ArticleComment.ReportReason.choices)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["comment", "reporter"], name="one_report_per_user_per_comment"),
        ]

    def __str__(self):
        return f"{self.reporter} reported comment {self.comment_id} ({self.reason})"


class SavedArticle(models.Model):
    """
    One mother's bookmark on one article.

    Kept on the server, per account, so a bookmark follows her across
    sign-outs, reinstalls and devices -- the app used to hold them only in
    memory. One row per (user, article); saving twice changes nothing.
    Deleted with either the user or the article.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, related_name="saved_articles", on_delete=models.CASCADE
    )
    article = models.ForeignKey(Article, related_name="saved_by", on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["user", "article"], name="one_save_per_user_per_article"),
        ]

    def __str__(self):
        return f"{self.user} saved {self.article_id}"


class ResourceLink(models.Model):
    """
    An external "Milk Expression Guide" link shown on the Knowledge Hub
    (WHO, La Leche League, UNICEF, etc). Lives alongside Article rather
    than in its own app since it's the same admin-managed content domain
    and the same screen -- unlike SupportContact, which is a distinct
    directory/ concern.
    """

    class LinkType(models.TextChoices):
        VIDEO = "Video", "Video"
        INFOGRAPHIC = "Infographic", "Infographic"

    title = models.CharField(max_length=255)
    description = models.CharField(max_length=500)
    url = models.URLField()
    type = models.CharField(max_length=20, choices=LinkType.choices)

    def __str__(self):
        return self.title
