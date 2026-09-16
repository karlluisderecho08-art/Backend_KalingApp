import logging
import threading
from datetime import timedelta

from django.conf import settings
from django.http import Http404
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import generics, permissions
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView

from core.audit import log_action
from core.throttling import ClientIPScopedRateThrottle

from .emails import (
    MAX_VERIFICATION_ATTEMPTS,
    RESEND_COOLDOWN_SECONDS,
    VERIFICATION_CODE_TTL_MINUTES,
    send_password_reset_email,
    send_verification_email,
)
from .models import PendingRegistration, User
from .serializers import (
    LocationConsentSerializer,
    RegisterSerializer,
    StaffUserListSerializer,
    UpdateProfileSerializer,
    UserSerializer,
)

logger = logging.getLogger(__name__)


def _send_verification_email_in_background(pending):
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
    _send_in_background(send_verification_email, pending, "verification")


def _send_password_reset_email_in_background(user):
    """Same fire-and-forget reasoning as _send_verification_email_in_background above."""
    _send_in_background(send_password_reset_email, user, "password_reset")


def _send_in_background(send_func, recipient, kind):
    """
    Runs one of the send_*_email() functions and records the outcome in
    the audit log, not just the Python logger.

    The logger alone turned out to be nearly useless in practice: these
    sends happen on a background thread in a hosted environment, so a
    failure only ever reached the platform's own log stream, which is
    awkward to read after the fact and impossible to correlate with a
    specific mother's signup. Days were lost to "the code says it sent,
    she says nothing arrived" with no way to tell which of the two was
    true. An audit row is queryable, and says plainly which host was
    actually used -- the thing that matters most here, since which
    provider is live depends entirely on which env vars happen to be set
    on the server (see settings/base.py).

    `recipient` is a User for password resets but a PendingRegistration
    for signup verification, where no account exists yet -- hence the
    actor below being None in that case, with the address recorded in
    the target text instead. AuditLogEntry.actor is a FK to User and
    documents null as "the system did it, not a person", which is
    exactly right for a signup that hasn't become anyone yet.

    Records the exception type and message on failure, and the host on
    success. Never the credentials: EMAIL_HOST_PASSWORD is not touched
    here, and the exception text from smtplib carries a status code and
    server reply, not the password that was offered.
    """
    host = getattr(settings, "EMAIL_HOST", "") or settings.EMAIL_BACKEND
    actor = recipient if isinstance(recipient, User) else None
    who = f" for {recipient.email}" if actor is None else ""

    try:
        send_func(recipient)
    except Exception as exc:
        logger.exception("Failed to send %s email to %s", kind, recipient.email)
        detail = f"{type(exc).__name__}: {exc}"
        log_action(actor, f"email.{kind}_failed", f"via {host}{who} -- {detail}"[:255])
    else:
        log_action(actor, f"email.{kind}_sent", f"via {host}{who}"[:255])


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


class ThrottledTokenObtainPairView(TokenObtainPairView):
    """
    simplejwt's login view, with a rate limit attached.

    Subclassed purely for the throttle: used bare, /auth/login/ would
    accept unlimited password guesses against any known email address,
    and every other guessable secret in this app (verification codes,
    reset codes) is capped while the password itself was not.
    """

    throttle_scope = "login"
    throttle_classes = [ClientIPScopedRateThrottle]


class RegisterView(generics.CreateAPIView):
    """
    POST /auth/register/  {email, password, mom_name, baby_name}

    Creates no account. The signup is held as a PendingRegistration and
    a 6-digit code is emailed; the User row is only created once
    VerifyEmailView sees that code come back correct. Until then there
    is nothing to log into, nothing occupying the users table, and
    nothing to clean up if she never finishes.

    Response shape is unchanged from when this did create an inactive
    account, so the mobile client needs no changes: it still shows the
    "enter your code" screen next.
    """

    serializer_class = RegisterSerializer
    permission_classes = [permissions.AllowAny]
    throttle_scope = "register"
    throttle_classes = [ClientIPScopedRateThrottle]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        pending = serializer.save()

        # Fire-and-forget: the pending row above is already committed,
        # so the response below is accurate regardless of how the send
        # itself turns out. See _send_verification_email_in_background's
        # docstring for why this can't just be a try/except here.
        threading.Thread(target=_send_verification_email_in_background, args=(pending,), daemon=True).start()

        return Response({
            "detail": "Account created. Check your email for a 6-digit verification code.",
            "email": pending.email,
        }, status=201)


class VerifyEmailView(APIView):
    """
    POST /auth/verify-email/  {email, code}

    Where the account actually gets created. Confirms the code
    RegisterView emailed, turns the PendingRegistration into a real
    User, and hands back {user, access, refresh} so the Kotlin app can
    log her straight in rather than sending her to Login to retype the
    password she just chose.
    """

    permission_classes = [permissions.AllowAny]
    throttle_scope = "verify"
    throttle_classes = [ClientIPScopedRateThrottle]

    def post(self, request):
        email = (request.data.get("email") or "").strip().lower()
        code = (request.data.get("code") or "").strip()

        try:
            pending = PendingRegistration.objects.get(email__iexact=email)
        except PendingRegistration.DoesNotExist:
            return Response({"detail": "Invalid email, or this account is already verified."}, status=400)

        # No sent_at means the first send hasn't completed yet (it runs on
        # a background thread), so there is no code to match against --
        # treated the same as expired rather than letting an empty code
        # compare equal to an empty submission.
        if (
            not pending.sent_at
            or timezone.now() - pending.sent_at > timedelta(minutes=VERIFICATION_CODE_TTL_MINUTES)
        ):
            return Response({"detail": "This code has expired. Please request a new one."}, status=400)

        if pending.attempts >= MAX_VERIFICATION_ATTEMPTS:
            return Response({"detail": "Too many incorrect attempts. Please request a new code."}, status=400)

        if not code or not pending.code or code != pending.code:
            pending.attempts += 1
            pending.save(update_fields=["attempts"])
            return Response({"detail": "Incorrect verification code."}, status=400)

        user = self._create_account(pending)
        pending.delete()

        log_action(user, "account.email_verified", f"User:{user.id}")

        return Response({
            "user": UserSerializer(user).data,
            **_tokens_for(user),
        })

    @staticmethod
    def _create_account(pending):
        # Clears out any leftover inactive row for this address. Under
        # the current design nothing creates one, but accounts from
        # before this flow existed (signups that were stored as
        # is_active=False users and never verified) are still out there,
        # and they'd otherwise collide with User.email's unique
        # constraint and make those addresses permanently unusable.
        User.objects.filter(email__iexact=pending.email, is_active=False).delete()

        user = User(
            email=pending.email,
            mom_name=pending.mom_name,
            baby_name=pending.baby_name,
            is_active=True,
            email_verified=True,
        )
        # Assigned directly, not via set_password(): the value was
        # already hashed at signup (RegisterSerializer), and re-hashing
        # a hash would lock her out of the password she actually chose.
        user.password = pending.password
        user.save()
        return user


class ResendVerificationView(APIView):
    """
    POST /auth/resend-verification/  {email}

    Same response whether there's no pending signup for that address,
    it's already been verified, or a code really was just sent --
    deliberately doesn't reveal which emails have KalingApp accounts.
    """

    permission_classes = [permissions.AllowAny]
    throttle_scope = "resend"
    throttle_classes = [ClientIPScopedRateThrottle]

    def post(self, request):
        email = (request.data.get("email") or "").strip().lower()
        generic_response = Response({"detail": "If that email needs verifying, a new code has been sent."})

        try:
            pending = PendingRegistration.objects.get(email__iexact=email)
        except PendingRegistration.DoesNotExist:
            return generic_response

        # A pending signup whose first send hasn't landed yet (sent_at is
        # null) is not inside any cooldown -- resending is exactly what
        # should happen if that first attempt failed.
        if pending.sent_at and timezone.now() - pending.sent_at < timedelta(seconds=RESEND_COOLDOWN_SECONDS):
            return generic_response

        threading.Thread(target=_send_verification_email_in_background, args=(pending,), daemon=True).start()
        return generic_response


class ForgotPasswordView(APIView):
    """
    POST /auth/forgot-password/  {email}

    Same "always generic response" discipline as ResendVerificationView,
    and for the same reason: whether this email doesn't have a
    KalingApp account, has one that's never been verified (password
    reset isn't the right flow there -- verify-email/resend-verification
    is), or genuinely got a code just now, the response is identical
    either way. Only targets is_active=True accounts, unlike
    ResendVerificationView's is_active=False.
    """

    permission_classes = [permissions.AllowAny]
    throttle_scope = "resend"
    throttle_classes = [ClientIPScopedRateThrottle]

    def post(self, request):
        email = (request.data.get("email") or "").strip().lower()
        generic_response = Response({"detail": "If that email has a KalingApp account, a reset code has been sent."})

        try:
            user = User.objects.get(email__iexact=email, is_active=True)
        except User.DoesNotExist:
            return generic_response

        if (
            user.password_reset_sent_at
            and timezone.now() - user.password_reset_sent_at < timedelta(seconds=RESEND_COOLDOWN_SECONDS)
        ):
            return generic_response

        threading.Thread(target=_send_password_reset_email_in_background, args=(user,), daemon=True).start()
        return generic_response


class ResetPasswordView(APIView):
    """
    POST /auth/reset-password/  {email, code, new_password}

    Confirms the code ForgotPasswordView emailed and sets the new
    password. Same expiry/attempt-lockout shape as VerifyEmailView, and
    like it, hands back a fresh {user, access, refresh} on success --
    no reason to make her go type the password she just set into a
    separate Login screen right after.
    """

    permission_classes = [permissions.AllowAny]
    throttle_scope = "verify"
    throttle_classes = [ClientIPScopedRateThrottle]

    def post(self, request):
        email = (request.data.get("email") or "").strip().lower()
        code = (request.data.get("code") or "").strip()
        new_password = request.data.get("new_password") or ""

        try:
            user = User.objects.get(email__iexact=email, is_active=True)
        except User.DoesNotExist:
            return Response({"detail": "Invalid email or code."}, status=400)

        if (
            not user.password_reset_sent_at
            or timezone.now() - user.password_reset_sent_at > timedelta(minutes=VERIFICATION_CODE_TTL_MINUTES)
        ):
            return Response({"detail": "This code has expired. Please request a new one."}, status=400)

        if user.password_reset_attempts >= MAX_VERIFICATION_ATTEMPTS:
            return Response({"detail": "Too many incorrect attempts. Please request a new code."}, status=400)

        if not code or not user.password_reset_code or code != user.password_reset_code:
            user.password_reset_attempts += 1
            user.save(update_fields=["password_reset_attempts"])
            return Response({"detail": "Incorrect reset code."}, status=400)

        # Same rule RegisterSerializer's password field enforces --
        # checked here, not there, since this never goes through that
        # serializer.
        if len(new_password) < 8:
            return Response({"detail": "Password must be at least 8 characters."}, status=400)

        user.set_password(new_password)
        user.password_reset_code = ""
        user.password_reset_attempts = 0
        user.save(update_fields=["password", "password_reset_code", "password_reset_attempts"])

        log_action(user, "account.password_reset", f"User:{user.id}")

        return Response({
            "user": UserSerializer(user).data,
            **_tokens_for(user),
        })


class DemoLoginView(APIView):
    """
    POST /auth/demo-login/

    Preserves the Kotlin WelcomeScreen's "Bypass / Quick-Access Demo Mode"
    button: no credentials, straight into the seeded "Rachel" account, for
    panel demos. The account itself is created by the seed_demo_user
    management command, not here -- this view only ever logs in.

    Now gated behind DEMO_LOGIN_ENABLED, off by default. This hands out
    real tokens to anyone who can reach the URL, with no credential of
    any kind -- fine pointed at a laptop, not something to leave
    reachable on the public internet. It was only ever harmless in
    production by accident (the demo account isn't seeded there, so it
    errored), which is not the same as being safe: seeding it once would
    have quietly turned it into an open door.
    """

    permission_classes = [permissions.AllowAny]

    # responses=UserSerializer is an approximation -- the real response
    # also includes access/refresh tokens alongside the user fields, but
    # drf-spectacular needs a concrete serializer to document at all.
    @extend_schema(request=None, responses=UserSerializer)
    def post(self, request):
        if not settings.DEMO_LOGIN_ENABLED:
            raise Http404

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
