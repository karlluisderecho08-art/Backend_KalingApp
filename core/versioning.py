"""
The mother app's version gate: forced updates.

The app sends its versionCode on every request in the
X-KalingApp-Version-Code header. GET /app/version/ tells it the latest and
the minimum supported code, and the app blocks itself below the minimum.

That alone can't reach a copy installed before the app knew to ask (1.0
sends no header at all). So the endpoints only the mother app writes to --
matching a facility, booking, the donor questionnaire and Kali -- refuse an
out-of-date or headerless client with 426 Upgrade Required and a plain
message naming where to download the new version. 1.0 shows a server's
"detail" text to the mother as-is, so the message is what she reads.

Only unsafe methods are gated: the facility console reads some of the same
endpoints (a donor questionnaire) with GET and never sends the header.
"""

from django.conf import settings
from rest_framework import permissions, status
from rest_framework.exceptions import APIException

APP_VERSION_HEADER = "X-KalingApp-Version-Code"


def client_version_code(request):
    """The app's versionCode from the request header; 0 when absent or unreadable."""
    raw = request.headers.get(APP_VERSION_HEADER, "")
    try:
        return max(int(raw), 0)
    except (TypeError, ValueError):
        return 0


class UpdateRequired(APIException):
    status_code = status.HTTP_426_UPGRADE_REQUIRED
    default_code = "update_required"

    def __init__(self):
        super().__init__(detail={
            "detail": (
                "A new version of KalingApp is required. Please download and install it from "
                f"{settings.APP_DOWNLOAD_URL} to continue."
            ),
            "code": "update_required",
            "download_url": settings.APP_DOWNLOAD_URL,
            "min_version_code": settings.APP_MIN_VERSION_CODE,
        })


class RequiresSupportedApp(permissions.BasePermission):
    """Refuses writes from a mother-app version below APP_MIN_VERSION_CODE."""

    def has_permission(self, request, view):
        if request.method in permissions.SAFE_METHODS:
            return True
        if client_version_code(request) < settings.APP_MIN_VERSION_CODE:
            raise UpdateRequired()
        return True
