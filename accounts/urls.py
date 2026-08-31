from django.conf import settings
from django.http import JsonResponse
from django.urls import path
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from .views import (
    CheckInView,
    DemoLoginView,
    LocationConsentView,
    MeView,
    RegisterView,
    ResendVerificationView,
    StaffUserListView,
    StaffUserSetActiveView,
    VerifyEmailView,
)


# TEMPORARY -- diagnosing why verification emails aren't reaching an
# inbox despite the backend reporting success. Exposes zero secrets
# (never the key itself, just whether one is configured), but this
# view and its urls.py entry get deleted once the SendGrid issue is
# root-caused -- not meant to stay in the codebase.
def _debug_email_config(request):
    return JsonResponse({
        "EMAIL_BACKEND": settings.EMAIL_BACKEND,
        "DEFAULT_FROM_EMAIL": settings.DEFAULT_FROM_EMAIL,
        "SENDGRID_API_KEY_set": bool(settings.SENDGRID_API_KEY),
        "SENDGRID_API_KEY_length": len(settings.SENDGRID_API_KEY),
    })


urlpatterns = [
    path("register/", RegisterView.as_view(), name="register"),
    path("verify-email/", VerifyEmailView.as_view(), name="verify-email"),
    path("resend-verification/", ResendVerificationView.as_view(), name="resend-verification"),
    path("_debug-email-config/", _debug_email_config, name="debug-email-config"),
    # TokenObtainPairView is simplejwt's built-in "login": it checks
    # email+password (USERNAME_FIELD="email" on our model) and returns
    # {access, refresh}. We don't need to write login logic ourselves.
    # Also doubles as the is_active gate: ModelBackend refuses to
    # authenticate an unverified (is_active=False) account here.
    path("login/", TokenObtainPairView.as_view(), name="login"),
    path("refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("demo-login/", DemoLoginView.as_view(), name="demo_login"),
    path("me/", MeView.as_view(), name="me"),
    path("check-in/", CheckInView.as_view(), name="check-in"),
    path("location/", LocationConsentView.as_view(), name="location-consent"),
    path("users/", StaffUserListView.as_view(), name="staff-user-list"),
    path("users/<int:pk>/activate/", StaffUserSetActiveView.as_view(active=True), name="staff-user-activate"),
    path("users/<int:pk>/deactivate/", StaffUserSetActiveView.as_view(active=False), name="staff-user-deactivate"),
]
