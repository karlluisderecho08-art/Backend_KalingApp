"""
Platform endpoints that belong to no one feature:

  GET /app/version/          public: what the mother app checks for updates
  GET /core/audit-log/       platform admins: the audit trail, read-only
  GET /core/system-status/   platform admins: is storage, email and the
                             expiry pinger actually configured -- answers
                             without exposing any secret
"""

from django.conf import settings
from django.db import connection
from django.db.models import Q
from drf_spectacular.utils import extend_schema
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import AuditLogEntry
from .retention import last_run

AUDIT_PAGE_SIZE = 50


class AppVersionView(APIView):
    """
    GET /app/version/ -- no sign-in needed, so the app can check before
    anything else. The app blocks itself when its own versionCode is below
    min_version_code (a forced update) and opens download_url to update.
    """

    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    @extend_schema(request=None)
    def get(self, request):
        return Response({
            "product_version": settings.KALINGAPP_VERSION,
            "latest_version_name": settings.APP_LATEST_VERSION_NAME,
            "latest_version_code": settings.APP_LATEST_VERSION_CODE,
            "min_version_code": settings.APP_MIN_VERSION_CODE,
            "download_url": settings.APP_DOWNLOAD_URL,
        })


class AuditLogView(APIView):
    """
    GET /core/audit-log/?page=1&action=booking.accepted&q=text

    Read-only: there is deliberately no way to add, change or delete an entry
    through the API (only the retention purge removes old ones). `action`
    filters on an exact action, or on a whole family when it ends in a dot
    ("booking."). `q` searches the target text and the actor's email.
    Newest first, 50 per page.
    """

    permission_classes = [permissions.IsAdminUser]

    @extend_schema(request=None)
    def get(self, request):
        entries = AuditLogEntry.objects.select_related("actor").order_by("-created_at", "-id")

        action = (request.query_params.get("action") or "").strip()
        if action.endswith("."):
            entries = entries.filter(action__startswith=action)
        elif action:
            entries = entries.filter(action=action)

        query = (request.query_params.get("q") or "").strip()
        if query:
            entries = entries.filter(Q(target__icontains=query) | Q(actor__email__icontains=query))

        try:
            page = max(int(request.query_params.get("page", 1)), 1)
        except (TypeError, ValueError):
            page = 1
        total = entries.count()
        start = (page - 1) * AUDIT_PAGE_SIZE
        rows = entries[start:start + AUDIT_PAGE_SIZE]

        return Response({
            "count": total,
            "page": page,
            "page_size": AUDIT_PAGE_SIZE,
            "results": [
                {
                    "id": entry.id,
                    "created_at": entry.created_at,
                    # Null when the system did it, or the account was since deleted.
                    "actor_email": entry.actor.email if entry.actor else None,
                    "action": entry.action,
                    "target": entry.target,
                }
                for entry in rows
            ],
            # Every action in the log, for the filter menu.
            "actions": sorted(AuditLogEntry.objects.values_list("action", flat=True).distinct()),
            "retention_days": settings.RETENTION_AUDIT_DAYS,
        })


def _storage_status():
    backend = settings.STORAGES["default"]["BACKEND"]
    if backend.endswith("S3Boto3Storage"):
        return {
            "kind": "supabase",
            "label": "Supabase Storage (private bucket)",
            "bucket": settings.SUPABASE_STORAGE_BUCKET,
            "ok": True,
        }
    return {
        "kind": "local",
        "label": "Server disk. Uploads are lost on every redeploy.",
        "bucket": None,
        "ok": settings.DEBUG,  # fine on a laptop, a problem on the live server
    }


def _email_status():
    backend = settings.EMAIL_BACKEND
    if backend.endswith("BrevoApiEmailBackend"):
        return {"kind": "brevo_api", "label": "Brevo API", "ok": True}
    if backend.endswith("console.EmailBackend"):
        return {"kind": "console", "label": "Not configured. Emails are only printed to the server log.", "ok": settings.DEBUG}
    # An SMTP provider. Render's free plan blocks outgoing SMTP ports, so on
    # the live server this cannot deliver -- say so rather than show "ok".
    return {
        "kind": "smtp",
        "label": f"SMTP via {getattr(settings, 'EMAIL_HOST', 'unknown host')} (blocked on Render's free plan)",
        "ok": settings.DEBUG,
    }


class SystemStatusView(APIView):
    """GET /core/system-status/ -- configuration health, no secrets in it."""

    permission_classes = [permissions.IsAdminUser]

    @extend_schema(request=None)
    def get(self, request):
        return Response({
            "version": settings.KALINGAPP_VERSION,
            "app": {
                "latest_version_name": settings.APP_LATEST_VERSION_NAME,
                "latest_version_code": settings.APP_LATEST_VERSION_CODE,
                "min_version_code": settings.APP_MIN_VERSION_CODE,
                "download_url": settings.APP_DOWNLOAD_URL,
            },
            "database": connection.vendor,
            "file_storage": _storage_status(),
            "email": _email_status(),
            "expiry_pinger_token_set": bool(settings.MILKBANK_SWEEP_TOKEN),
            "django_admin_enabled": settings.DJANGO_ADMIN_ENABLED,
            "demo_login_enabled": settings.DEMO_LOGIN_ENABLED,
            "retention": {
                "pending_signup_hours": settings.RETENTION_PENDING_SIGNUP_HOURS,
                "representative_days": settings.RETENTION_REPRESENTATIVE_DAYS,
                "chat_days": settings.RETENTION_CHAT_DAYS,
                "audit_days": settings.RETENTION_AUDIT_DAYS,
                "last_run": last_run(),
            },
        })
