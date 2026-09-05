import logging
import threading
from datetime import timedelta

from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import generics, permissions
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from core.audit import log_action

from .emails import (
    MAX_VERIFICATION_ATTEMPTS,
    RESEND_COOLDOWN_SECONDS,
    VERIFICATION_CODE_TTL_MINUTES,
    send_verification_email,
)
from .models import User
from .serializers import (
    LocationConsentSerializer,
    RegisterSerializer,
    StaffUserListSerializer,
    UpdateProfileSerializer,
    UserSerializer,
)

logger = logging.getLogger(__name__)


def _send_verification_email_in_background(user):
    """
    Fire-and-forget wrapper around send_verification_email(), run on a
    background thread so a slow or hung SMTP connection can never make
    the HTTP response wait on it.

    Found the hard way against the live backend: EMAIL_TIMEOUT bounds
    how long the SMTP *connection* can hang, but gunicorn's own worker
    timeout can still kill the whole request from outside Python before
    that ever matters -- no try/except inside the request/response cycle
    can catch that, because the process is gone. Moving the send off
    the request thread entirely sidesteps the problem instead of trying
    to out-race it with shorter and shorter timeouts.

    daemon=True: this thread must never block the worker process from
    shutting down. Real tradeoff, accepted deliberately: if the worker
    recycles before the send finishes, that one email is lost, same as
    any other fire-and-forget background job without a real task queue
    (Celery, etc.) in front of it -- a reasonable size fix for this
    project's current scale, not a claim that this is the fully robust
    long-term answer.
    """
    try:
        send_verification_email(user)
    except Exception:
        logger.exception("Failed to send verification email to %s", user.email)


class IsFacilityStaff(permissions.BasePermission):
    """
    Same check as milkbank.permissions.IsFacilityStaff -- duplicated
    (not imported) so accounts, the lower-level app, never has to
    depend on milkbank.
    """

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.role == User.Role.FACILITY_STAFF)


def _tokens_for(user):
    """Issue a fresh access/refresh token pair for a user."""
    refresh = RefreshToken.for_user(user)
    return {"access": str(refresh.access_token), "refresh": str(refresh)}


class RegisterView(generics.CreateAPIView):
    """
    POST /auth/register/  {email, password, mom_name, baby_name}

    No longer hands back JWTs directly: the account is created with
    is_active=False and a 6-digit code is emailed to her. is_active=False
    means TokenObtainPairView (login) and JWTAuthentication both refuse
    this account until VerifyEmailView flips it back on -- Django's
    ModelBackend and simplejwt's JWTAuthentication.get_user() both check
    is_active on their own, so nothing else has to police this. The
    client now shows an "enter your code" screen instead of logging her
    straight in; see VerifyEmailView for what happens once she does.
    """

    queryset = User.objects.all()
    serializer_class = RegisterSerializer
    permission_classes = [permissions.AllowAny]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save(is_active=False)

        # Fire-and-forget: the account row above is already committed,
        # so the response below is accurate regardless of how the send
        # itself turns out. See _send_verification_email_in_background's
        # docstring for why this can't just be a try/except here.
        threading.Thread(target=_send_verification_email_in_background, args=(user,), daemon=True).start()

        return Response({
            "detail": "Account created. Check your email for a 6-digit verification code.",
            "email": user.email,
        }, status=201)


class VerifyEmailView(APIView):
    """
    POST /auth/verify-email/  {email, code}

    Confirms the code RegisterView emailed, flips is_active and
    email_verified to True, then hands back the same {user, access,
    refresh} shape RegisterView used to return directly -- so the
    Kotlin app can log her in immediately once she's verified, instead
    of sending her back to Login to type her password again.
    """

    permission_classes = [permissions.AllowAny]

    def post(self, request):
        email = (request.data.get("email") or "").strip().lower()
        code = (request.data.get("code") or "").strip()

        try:
            user = User.objects.get(email__iexact=email, is_active=False)
        except User.DoesNotExist:
            return Response({"detail": "Invalid email, or this account is already verified."}, status=400)

        if (
            not user.email_verification_sent_at
            or timezone.now() - user.email_verification_sent_at > timedelta(minutes=VERIFICATION_CODE_TTL_MINUTES)
        ):
            return Response({"detail": "This code has expired. Please request a new one."}, status=400)

        if user.email_verification_attempts >= MAX_VERIFICATION_ATTEMPTS:
            return Response({"detail": "Too many incorrect attempts. Please request a new code."}, status=400)

        if not code or not user.email_verification_code or code != user.email_verification_code:
            user.email_verification_attempts += 1
            user.save(update_fields=["email_verification_attempts"])
            return Response({"detail": "Incorrect verification code."}, status=400)

        user.is_active = True
        user.email_verified = True
        user.email_verification_code = ""
        user.email_verification_attempts = 0
        user.save(update_fields=[
            "is_active", "email_verified", "email_verification_code", "email_verification_attempts",
        ])

        log_action(user, "account.email_verified", f"User:{user.id}")

        return Response({
            "user": UserSerializer(user).data,
            **_tokens_for(user),
        })


class ResendVerificationView(APIView):
    """
    POST /auth/resend-verification/  {email}

    Same response whether the account doesn't exist, is already
    verified, or a code really was just sent -- deliberately doesn't
    reveal which emails have KalingApp accounts.
    """

    permission_classes = [permissions.AllowAny]

    def post(self, request):
        email = (request.data.get("email") or "").strip().lower()
        generic_response = Response({"detail": "If that email needs verifying, a new code has been sent."})

        try:
            user = User.objects.get(email__iexact=email, is_active=False)
        except User.DoesNotExist:
            return generic_response

        if (
            user.email_verification_sent_at
            and timezone.now() - user.email_verification_sent_at < timedelta(seconds=RESEND_COOLDOWN_SECONDS)
        ):
            return generic_response

        threading.Thread(target=_send_verification_email_in_background, args=(user,), daemon=True).start()
        return generic_response


class DemoLoginView(APIView):
    """
    POST /auth/demo-login/

    Preserves the Kotlin WelcomeScreen's "Bypass / Quick-Access Demo Mode"
    button: no credentials, straight into the seeded "Rachel" account, for
    panel demos. The account itself is created by the seed_demo_user
    management command, not here -- this view only ever logs in.
    """

    permission_classes = [permissions.AllowAny]

    # responses=UserSerializer is an approximation -- the real response
    # also includes access/refresh tokens alongside the user fields, but
    # drf-spectacular needs a concrete serializer to document at all.
    @extend_schema(request=None, responses=UserSerializer)
    def post(self, request):
        try:
            user = User.objects.get(email="rachel@kalingapp.demo")
        except User.DoesNotExist:
            return Response(
                {"detail": "Demo account not seeded. Run: manage.py seed_demo_user"},
                status=500,
            )
        return Response({
            "user": UserSerializer(user).data,
            **_tokens_for(user),
        })


class MeView(generics.RetrieveUpdateAPIView):
    """
    GET /auth/me/ -- the profile of whoever the access token belongs to.
    PATCH/PUT /auth/me/ -- update her own mom/baby profile info (see
    UpdateProfileSerializer for exactly which fields). Responds with
    the full UserSerializer shape either way, so the client doesn't
    need a second GET after saving.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get_object(self):
        return self.request.user

    def get_serializer_class(self):
        return UpdateProfileSerializer if self.request.method in ("PUT", "PATCH") else UserSerializer

    def update(self, request, *args, **kwargs):
        super().update(request, *args, **kwargs)
        # Respond with the full read shape, not UpdateProfileSerializer's
        # narrower write shape -- the client's UserInfo parsing expects
        # email/role/etc. to always be present.
        return Response(UserSerializer(self.get_object()).data)


class CheckInView(APIView):
    """
    POST /auth/check-in/ -- called once per app session, right after
    login/the cold-start session check confirms she's authenticated,
    to advance the "breastfeeding journey streak" on the Home
    Dashboard by a calendar day. Compares today against
    last_active_date:
      - same day already -> no-op (calling this more than once today
        doesn't inflate the count)
      - exactly one day later -> streak += 1
      - anything else (first ever check-in, or a gap) -> streak reset
        to 1, not left at whatever it was
    """

    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(request=None, responses=UserSerializer)
    def post(self, request):
        user = request.user
        today = timezone.localdate()
        last = user.last_active_date

        if last != today:
            user.tracking_streaks = user.tracking_streaks + 1 if last == today - timedelta(days=1) else 1
            user.last_active_date = today
            user.save(update_fields=["tracking_streaks", "last_active_date"])

        return Response(UserSerializer(user).data)


class StaffUserListView(generics.ListAPIView):
    """GET /auth/users/ -- every mother account, newest first, for the
    facility dashboard's User Management table."""

    serializer_class = StaffUserListSerializer
    permission_classes = [permissions.IsAuthenticated, IsFacilityStaff]

    def get_queryset(self):
        return User.objects.filter(role=User.Role.MOTHER).order_by("-date_joined")


class StaffUserSetActiveView(APIView):
    """
    POST /auth/users/<id>/activate/
    POST /auth/users/<id>/deactivate/

    Toggles Django's own is_active flag -- an inactive user's tokens
    still decode fine (JWTs aren't looked up in the DB per request),
    but simplejwt's default OutstandingToken check isn't enabled here,
    so this is enforced the usual Django way: every DRF view already
    requires IsAuthenticated, and Django's ModelBackend refuses to
    authenticate (and thus issue a new login) for is_active=False.
    Already-issued tokens keep working until they expire -- fine for a
    facility dashboard action, not a "lock this account out instantly"
    control.
    """

    permission_classes = [permissions.IsAuthenticated, IsFacilityStaff]
    # Set per-URL via as_view(active=True/False) -- see urls.py.
    active = None

    @extend_schema(request=None, responses=StaffUserListSerializer)
    def post(self, request, pk):
        user = generics.get_object_or_404(User, pk=pk, role=User.Role.MOTHER)
        user.is_active = self.active
        user.save(update_fields=["is_active"])
        log_action(request.user, "user.activated" if self.active else "user.deactivated", f"User:{user.id}")
        return Response(StaffUserListSerializer(user).data)


class LocationConsentView(APIView):
    """
    POST /auth/location/  {latitude, longitude, consent: true}

    The Phase 1 decision: location comes from device GPS, not a typed
    address (see roadmap gap #3). Chosen deliberately because Phase 3's
    Smart Allocation needs a real distance calculation -- but GPS is
    personal data under RA 10173, so storing it requires an explicit,
    logged consent event, not just a privacy-policy paragraph nobody
    reads. The audit log entry is that evidence trail.
    """

    permission_classes = [permissions.IsAuthenticated]
    serializer_class = LocationConsentSerializer

    def post(self, request):
        serializer = LocationConsentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        user = request.user
        user.latitude = data["latitude"]
        user.longitude = data["longitude"]
        user.location_consent_given = True
        user.location_consent_at = timezone.now()
        user.save(update_fields=[
            "latitude", "longitude", "location_consent_given", "location_consent_at",
        ])

        log_action(user, "location.consent_given", f"User:{user.id}")

        return Response(UserSerializer(user).data)
