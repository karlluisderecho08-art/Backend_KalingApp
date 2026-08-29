from django.conf import settings
from django.db import models


class AuditLogEntry(models.Model):
    """
    A single "who did what, when" line. Nothing ever edits or deletes a
    row here (append-only) -- that's what makes it useful as evidence
    later, e.g. answering "who removed this comment and when" under
    RA 10173.

    Deliberately generic (a short action string + a free-text description
    of what it happened to) instead of a foreign key to every model that
    might ever need logging -- that would mean this app importing from
    articles/milkbank/accounts, which is a dependency pointing the wrong
    way. Any app can log an entry without core needing to know it exists.
    """

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_log_entries",
        help_text="Who performed the action. Null means the system did it, not a person.",
    )
    action = models.CharField(max_length=100, help_text='e.g. "comment.resolved", "booking.accepted"')
    target = models.CharField(max_length=255, blank=True, help_text='e.g. "ArticleComment:42"')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "audit log entries"

    def __str__(self):
        return f"{self.actor} {self.action} {self.target}"
