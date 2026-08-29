from .models import AuditLogEntry


def log_action(actor, action, target=""):
    """
    Write one line to the activity notebook. Call this from any view,
    right after the thing you want a record of actually happens.

        log_action(request.user, "comment.resolved", f"ArticleComment:{comment.id}")
    """
    AuditLogEntry.objects.create(actor=actor, action=action, target=target)
