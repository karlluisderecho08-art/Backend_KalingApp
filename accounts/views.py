import hashlib
import logging
import secrets
import threading
from datetime import timedelta
from functools import partial

from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import update_last_login
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import F
from django.http import Http404
from django.utils import timezone
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import generics, permissions, serializers
from rest_framework.exceptions import AuthenticationFailed, PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.settings import api_settings as jwt_settings
from rest_framework_simplejwt.utils import get_md5_hash_password
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from core.audit import log_action
from core.retention import maybe_purge
from core.throttling import ClientIPScopedRateThrottle
from directory.models import SupportContact
from notifications.models import NotificationItem
from core.i18n import wants_tagalog
from notifications.services import notify_many

from . import devices, lockout, owner
from .legacy_signups import email_is_taken, is_abandoned_legacy_signup
from .emails import (
    MAX_VERIFICATION_ATTEMPTS,
    RESEND_COOLDOWN_SECONDS,
    VERIFICATION_CODE_TTL_MINUTES,
    generate_code,
    send_email_change_code,
    send_email_changed_notice,
    send_email_in_use_notice,
    send_existing_account_notice,
    send_password_reset_email,
    send_verification_email,
)
from .models import PendingRegistration, User
from .passwords import mother_password_problems, staff_password_problems
from .serializers import (
    AdminUserListSerializer,
    LocationConsentSerializer,
    RegisterSerializer,
    StaffUserListSerializer,
    UpdateProfileSerializer,
    UserSerializer,
)

logger = logging.getLogger(__name__)


def _send_verification_email_in_background(pending, tagalog=False):
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
    _send_in_background(send_verification_email, pending, "verification", tagalog)


def _send_password_reset_email_in_background(user, tagalog=False):
    """Same fire-and-forget reasoning as _send_verification_email_in_background above."""
    _send_in_background(send_password_reset_email, user, "password_reset", tagalog)


def _send_existing_account_notice_in_background(user, tagalog=False):
    """Same fire-and-forget reasoning as _send_verification_email_in_background above."""
    _send_in_background(send_existing_account_notice, user, "existing_account_notice", tagalog)


def _send_in_background(send_func, recipient, kind, tagalog=False):
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
        send_func(recipient, tagalog=tagalog)
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
    """Issue a fresh access/refresh token pair for a user -- signing her in, so
    it counts as a login (verify email, password reset, demo), the same as
    POST /auth/login/ with SIMPLE_JWT's UPDATE_LAST_LOGIN."""
    update_last_login(None, user)
    refresh = RefreshToken.for_user(user)
    # Each of these proves the password (or the email address) just now, so
    # the device it came from is trusted from here on -- see accounts/devices.py.
    return {"access": str(refresh.access_token), "refresh": str(refresh), "device_token": devices.issue(user)}


class CheckedTokenRefreshSerializer(TokenRefreshSerializer):
    """
    simplejwt's refresh, refusing a refresh token whose session has ended.

    With CHECK_REVOKE_TOKEN on (settings.SIMPLE_JWT) an ACCESS token issued
    before a password change is refused, but simplejwt's own refresh would
    still happily mint new ones from an old refresh token -- each refused
    in turn, leaving the app stuck instead of back at the login screen. So
    the refresh is checked the same way: the account must still exist, be
    active, and have the password the token was issued under. (simplejwt's
    own check also raised a 500 for a deleted account.)
    """

    def validate(self, attrs):
        refresh = self.token_class(attrs["refresh"])
        user = User.objects.filter(pk=refresh.payload.get(jwt_settings.USER_ID_CLAIM)).first()
        if (
            user is None
            or not user.is_active
            or refresh.payload.get(jwt_settings.REVOKE_TOKEN_CLAIM) != get_md5_hash_password(user.password)
        ):
            raise AuthenticationFailed("This session has ended. Please sign in again.", "session_ended")
        return super().validate(attrs)


class CheckedTokenRefreshView(TokenRefreshView):
    """POST /auth/refresh/ -- see CheckedTokenRefreshSerializer."""

    serializer_class = CheckedTokenRefreshSerializer


class LogoutView(APIView):
    """
    POST /auth/logout/  {refresh}

    Signs this device out on the server as well: its refresh token goes on
    the blacklist (simplejwt's token_blacklist app), so it can never be
    traded for a new access token again -- not even by someone who copied
    it. Signing out used to only forget the tokens on the device, and a
    copied refresh token kept working until it expired. The access token
    that goes with it is short-lived and runs out within minutes.

    Always answers 204: a token that is malformed, expired or already
    revoked has nothing left to revoke, and the client signs out locally
    either way. No authentication is required or read -- the refresh token
    is the proof -- so an expired access token in the header can't make
    signing out fail.
    """

    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    @extend_schema(
        request=inline_serializer("Logout", {"refresh": serializers.CharField()}),
        responses={204: None},
    )
    def post(self, request):
        token = request.data.get("refresh")
        if isinstance(token, str) and token:
            try:
                RefreshToken(token).blacklist()
            except TokenError:
                pass
        return Response(status=204)


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

    def post(self, request, *args, **kwargs):
        """
        Wrong passwords lock the account for longer and longer (see
        accounts/lockout.py). A locked account answers 423 with how long is
        left and who to contact -- before the password is even looked at, so
        a correct guess made during a lock gains nothing.

        Only an account that exists and is active can be locked. Someone
        typing an unknown address just gets the ordinary 401 every time.

        Sign-ins to one account are checked one at a time (see
        lockout.one_attempt_at_a_time), so guesses sent all at once can't all
        slip in before the lock.

        A device she has signed in on before sends its trusted-device token
        (`device_token`, see accounts/devices.py): the account-wide lock
        doesn't apply to it, and its own wrong passwords lock only that
        device -- so a stranger who knows her email can't keep her out of
        her own phone. Every successful sign-in answers with a fresh
        `device_token` for the client to keep.
        """
        email = str(request.data.get("email") or "").strip()
        user = User.objects.filter(email__iexact=email, is_active=True).first() if email else None
        if user is None:
            return super().post(request, *args, **kwargs)

        presented = request.data.get("device_token")
        with lockout.one_attempt_at_a_time(user):
            device = devices.device_id(presented, user)
            if device is not None:
                until = lockout.device_locked_until(device)
                if until:
                    return self._locked_response(user, until)
            elif lockout.is_locked(user):
                return self._locked_response(user)

            try:
                response = super().post(request, *args, **kwargs)
            except AuthenticationFailed:
                # simplejwt reports wrong credentials by raising, not by returning.
                if device is not None:
                    attempts = lockout.register_device_failure(device)
                    until = lockout.device_locked_until(device)
                    if until is None:
                        raise
                    log_action(None, "account.device_locked", f"User:{user.id} ({user.email}) one trusted device, "
                                                              f"after {attempts} failed sign-ins, until {until.isoformat()}")
                    return self._locked_response(user, until)
                attempts, newly_locked = lockout.register_failure(user)
                if newly_locked:
                    self._tell_admins(user)
                if not lockout.is_locked(user):
                    raise
                log_action(None, "account.locked", f"User:{user.id} ({user.email}) after {attempts} failed sign-ins, "
                                                   f"until {user.locked_until.isoformat()}")
                return self._locked_response(user)

            if response.status_code == 200:
                if device is not None:
                    # Only this device's count: what strangers did to the
                    # account stays on it, for the devices it was meant for.
                    lockout.clear_device(device)
                else:
                    lockout.clear(user)
                response.data["device_token"] = devices.issue(user, presented)
            return response

    @staticmethod
    def _tell_admins(user):
        notify_many(
            User.objects.filter(is_staff=True, is_active=True),
            "Account Locked",
            f"{user.email} was locked after {lockout.LOCK_THRESHOLD} wrong passwords in a row. "
            "You can unlock it under User Management.",
            NotificationItem.Category.SECURITY,
        )

    @staticmethod
    def _locked_response(user, until=None):
        """423 for a locked account -- or, with `until`, for one trusted device's own lock."""
        until = until or user.locked_until
        # Rounded up, so "0 seconds left" never shows while still locked.
        seconds = max(0, int((until - timezone.now()).total_seconds()) + 1)
        contact = SupportContact.objects.filter(name__icontains="arugaan").first()
        body = {
            "code": "account_locked",
            "detail": "Too many incorrect passwords, so this account is locked for now. "
                      "Please contact the admin for help and assistance.",
            "locked_until": until.isoformat(),
            "retry_after_seconds": seconds,
            "contact": {
                "name": contact.name if contact else "",
                "email": contact.email if contact else "",
                "phone": contact.phone if contact else "",
            },
        }
        return Response(body, status=423, headers={"Retry-After": str(seconds)})


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

    An address that already has an account gets the very same answer, as
    fast: no signup is started, and its owner is emailed instead (at most
    once an hour). It used to be refused with "An account with this email
    already exists", which let anyone find out who has a KalingApp account
    -- a mother of a newborn, a facility's staff login -- one address at a
    time. An existing account is never replaced either way: a deactivated
    mother must not get a fresh one (that would undo the deactivation and
    erase her history). The one exception is an abandoned signup from
    before verification existed, which nothing can verify any more;
    VerifyEmailView clears it when the replacement account is created. See
    accounts/legacy_signups.py.
    """

    serializer_class = RegisterSerializer
    permission_classes = [permissions.AllowAny]
    throttle_scope = "register"
    throttle_classes = [ClientIPScopedRateThrottle]

    # How long one address's owner hears about signup attempts only once, so
    # signing up with someone else's address can't flood their inbox.
    EXISTING_NOTICE_SECONDS = 60 * 60

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]
        answer = Response({
            "detail": "Account created. Check your email for a 6-digit verification code.",
            "email": email,
        }, status=201)

        existing = next(
            (user for user in User.objects.filter(email__iexact=email) if not is_abandoned_legacy_signup(user)),
            None,
        )
        if existing is not None:
            # The password is hashed all the same and thrown away: a new
            # address spends seconds on it, and answering this one at once
            # would tell the two apart just by how long the reply took.
            make_password(serializer.validated_data["password"])
            notice_key = "signup-existing-notice:" + hashlib.sha256(email.encode()).hexdigest()
            if cache.add(notice_key, True, self.EXISTING_NOTICE_SECONDS):
                threading.Thread(
                    target=_send_existing_account_notice_in_background,
                    args=(existing, wants_tagalog(request)), daemon=True,
                ).start()
            return answer

        pending = serializer.save()

        # Fire-and-forget: the pending row above is already committed,
        # so the response below is accurate regardless of how the send
        # itself turns out. See _send_verification_email_in_background's
        # docstring for why this can't just be a try/except here.
        threading.Thread(target=_send_verification_email_in_background, args=(pending, wants_tagalog(request)), daemon=True).start()

        return answer


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

        # The guess is counted BEFORE it is compared, in one UPDATE that only
        # succeeds while guesses are left. Counting after a wrong guess (read
        # the count, add one, save) let guesses sent all at once each read the
        # same count: every one of them was compared, and most went uncounted.
        # A right code uses up a guess too, which changes nothing -- the
        # pending signup is deleted below.
        if not PendingRegistration.objects.filter(
            pk=pending.pk, attempts__lt=MAX_VERIFICATION_ATTEMPTS,
        ).update(attempts=F("attempts") + 1):
            return Response({"detail": "Too many incorrect attempts. Please request a new code."}, status=400)

        if not code or not pending.code or code != pending.code:
            return Response({"detail": "Incorrect verification code."}, status=400)

        # Checked again here, not only at signup: the address may have gained
        # an account since the code was sent (another signup finished first).
        if email_is_taken(pending.email):
            pending.delete()
            return Response({"detail": "An account with this email already exists."}, status=400)

        with transaction.atomic():
            # Claimed by deleting it. Two taps on Verify arriving together both
            # get this far; only one of them deletes the pending signup, so only
            # one account is made -- the other would have failed on the unique
            # email with a server error. If making the account fails, the
            # pending signup comes back with the rollback.
            if not PendingRegistration.objects.filter(pk=pending.pk).delete()[0]:
                return Response({"detail": "Invalid email, or this account is already verified."}, status=400)
            user = self._create_account(pending)

        log_action(user, "account.email_verified", f"User:{user.id}")

        return Response({
            "user": UserSerializer(user).data,
            **_tokens_for(user),
        })

    @staticmethod
    def _create_account(pending):
        # Clears out an abandoned signup from before this flow existed (an
        # inactive, never-used row that would otherwise collide with
        # User.email's unique constraint). Only that: a deactivated account
        # is never deleted here -- the caller has already refused the
        # signup if one exists. See accounts/legacy_signups.py.
        for leftover in User.objects.filter(email__iexact=pending.email, is_active=False):
            if is_abandoned_legacy_signup(leftover):
                leftover.delete()

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

        threading.Thread(target=_send_verification_email_in_background, args=(pending, wants_tagalog(request)), daemon=True).start()
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
        email = str(request.data.get("email") or "").strip().lower()
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

        threading.Thread(target=_send_password_reset_email_in_background, args=(user, wants_tagalog(request)), daemon=True).start()
        return generic_response


class ResetPasswordView(APIView):
    """
    POST /auth/reset-password/  {email, code, new_password}

    Confirms the code ForgotPasswordView emailed and sets the new
    password. Same expiry/attempt-lockout shape as VerifyEmailView, and
    like it, hands back a fresh {user, access, refresh} on success --
    no reason to make her go type the password she just set into a
    separate Login screen right after.

    Used by the mother app and by the facility console's "Forgot password?"
    page (which ignores the tokens and sends staff back to sign in).
    """

    permission_classes = [permissions.AllowAny]
    throttle_scope = "verify"
    throttle_classes = [ClientIPScopedRateThrottle]

    def post(self, request):
        email = str(request.data.get("email") or "").strip().lower()
        code = str(request.data.get("code") or "").strip()
        new_password = request.data.get("new_password") or ""
        if not isinstance(new_password, str):
            new_password = ""

        try:
            user = User.objects.get(email__iexact=email, is_active=True)
        except User.DoesNotExist:
            return Response({"detail": "Invalid email or code."}, status=400)

        if (
            not user.password_reset_sent_at
            or timezone.now() - user.password_reset_sent_at > timedelta(minutes=VERIFICATION_CODE_TTL_MINUTES)
        ):
            return Response({"detail": "This code has expired. Please request a new one."}, status=400)

        # Counted before it is compared, in one UPDATE that only succeeds while
        # guesses are left -- see VerifyEmailView. This is the code that lets
        # someone set a new password on her account, so guesses sent all at
        # once must not get more tries than guesses sent one at a time.
        guesses_left = User.objects.filter(pk=user.pk, password_reset_attempts__lt=MAX_VERIFICATION_ATTEMPTS)
        if not guesses_left.update(password_reset_attempts=F("password_reset_attempts") + 1):
            return Response({"detail": "Too many incorrect attempts. Please request a new code."}, status=400)

        if not code or not user.password_reset_code or code != user.password_reset_code:
            return Response({"detail": "Incorrect reset code."}, status=400)

        # A mother: the same rule RegisterSerializer's password field enforces
        # (checked here, since this never goes through that serializer).
        # Facility staff -- who reset from the facility console's sign-in page
        # -- and admins get the staff rules (accounts/passwords.py): the
        # same 12 characters and checks as a password an admin sets for them,
        # so a reset can't be the way round them.
        staff = user.role == User.Role.FACILITY_STAFF or user.is_staff
        if staff:
            problems = staff_password_problems(new_password, user)
        else:
            # A mother: 8 characters, and not one of the passwords guessed first.
            problems = mother_password_problems(new_password, user)
        if problems:
            # Her code was right, so this try doesn't count against her.
            User.objects.filter(pk=user.pk, password_reset_attempts__gt=0).update(
                password_reset_attempts=F("password_reset_attempts") - 1,
            )
            # A mother is shown one reason at a time: each one has its own
            # Tagalog line, where several joined into one sentence would not.
            return Response({"detail": " ".join(problems) if staff else problems[0]}, status=400)

        user.set_password(new_password)
        user.password_reset_code = ""
        user.password_reset_attempts = 0
        user.save(update_fields=["password", "password_reset_code", "password_reset_attempts"])
        # Proving she owns the email address is how a locked-out mother gets back in.
        lockout.clear(user)

        log_action(user, "account.password_reset", f"User:{user.id}")

        return Response({
            "user": UserSerializer(user).data,
            **_tokens_for(user),
        })


class DemoLoginView(APIView):
    """
    POST /auth/demo-login/

    Preserves the Kotlin WelcomeScreen's "Bypass / Quick-Access Demo Mode"
    button: no credentials, straight into the seeded demo account
    (settings.DEMO_ACCOUNT_EMAIL), for panel demos. The account itself is created by the seed_demo_user
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
            user = User.objects.get(email=settings.DEMO_ACCOUNT_EMAIL)
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
        # Every app and console loads this on start, so it is where data
        # retention gets its regular chance to run (at most hourly).
        maybe_purge()
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


def _mothers_of_my_facility(staff):
    """
    The mothers a facility's staff may see and act on: those with at least
    one booking at THEIR facility, past or present. Never every mother in
    the system -- a hospital has no business with the email, baby details
    or clinic of a mother who never booked there (RA 10173, data
    minimisation). A staff account with no facility sees nobody.
    """
    from django.db.models import Q, Sum
    from django.db.models.functions import Coalesce

    from milkbank.models import MilkBankRequest

    if not staff.facility_id:
        return User.objects.none()
    # The filter comes before the annotations on purpose: the sums then run
    # over this facility's bookings only, and grouping by user returns each
    # mother once. Her totals are what she gave and received HERE -- a
    # hospital must not learn what she did at another one.
    completed = Q(milkbank_requests__current_sub_status=MilkBankRequest.Status.COMPLETED)
    return User.objects.filter(
        role=User.Role.MOTHER,
        milkbank_requests__allocated_facility_id=staff.facility_id,
    ).annotate(
        facility_drawn_ml=Coalesce(Sum(
            "milkbank_requests__amount_ml",
            filter=completed & Q(milkbank_requests__request_type=MilkBankRequest.RequestType.DONOR),
        ), 0),
        facility_received_ml=Coalesce(Sum(
            "milkbank_requests__amount_ml",
            filter=completed & Q(milkbank_requests__request_type=MilkBankRequest.RequestType.RECIPIENT),
        ), 0),
    )


class StaffUserListView(generics.ListAPIView):
    """GET /auth/users/ -- the mothers who have booked at this staff member's
    facility, newest first, for the facility dashboard's User Management
    table and reports."""

    serializer_class = StaffUserListSerializer
    permission_classes = [permissions.IsAuthenticated, IsFacilityStaff]

    def get_queryset(self):
        return _mothers_of_my_facility(self.request.user).order_by("-date_joined")


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
        # Only a mother who has booked at this staff member's own facility.
        user = generics.get_object_or_404(_mothers_of_my_facility(request.user), pk=pk)
        user.is_active = self.active
        user.save(update_fields=["is_active"])
        log_action(request.user, "user.activated" if self.active else "user.deactivated", f"User:{user.id}")
        return Response(StaffUserListSerializer(user).data)


# --- Platform-admin account management (Django's is_staff, NOT the
# facility_staff role gated above by IsFacilityStaff) ---

class AdminUserListView(generics.ListAPIView):
    """
    GET /auth/admin/users/ -- every account in the system, for the admin
    dashboard's User Management page. Deliberately crosses every role
    (mother, facility_staff, admin) -- a platform admin is the one role
    meant to see who the facility-staff accounts actually are and which
    facility each belongs to, not just mothers like StaffUserListView
    above.
    """

    serializer_class = AdminUserListSerializer
    permission_classes = [permissions.IsAdminUser]

    def get_queryset(self):
        return User.objects.select_related("facility").order_by("-date_joined")


class AdminUserUnlockView(APIView):
    """
    POST /auth/admin/users/<id>/unlock/ -- platform-admin only.

    Lifts a sign-in lockout and forgets the failed attempts, for a mother
    who contacted the admin because she is locked out. Safe to press on an
    account that isn't locked.
    """

    permission_classes = [permissions.IsAdminUser]

    @extend_schema(request=None, responses=AdminUserListSerializer)
    def post(self, request, pk):
        user = generics.get_object_or_404(User, pk=pk)
        lockout.clear(user)
        log_action(request.user, "account.unlocked", f"User:{user.id} ({user.email})")
        return Response(AdminUserListSerializer(user).data)


class AdminUserSetPasswordView(APIView):
    """
    POST /auth/admin/users/<id>/set-password/  {new_password} -- platform-admin only.

    Gives a facility staff account a new password: for a facility that lost
    it, a staff member who left, or a password that may have leaked. The admin
    then hands it to the facility, the same way as a new staff login made from
    the facility form.

    Facility staff accounts only. A mother resets her own password by email
    (ForgotPasswordView) -- an admin must never know it -- and an admin
    account's password is not another admin's to change.

    What it does besides the password:
      * signs that account out everywhere: each session carries a fingerprint
        of the password (SIMPLE_JWT CHECK_REVOKE_TOKEN), so every one issued
        under the old password stops working at once;
      * lifts any sign-in lockout, as a password reset does;
      * voids any password-reset code still outstanding for it;
      * records who did it in the audit log -- never the password itself.
    """

    permission_classes = [permissions.IsAdminUser]

    @extend_schema(
        request=inline_serializer("AdminSetPassword", {"new_password": serializers.CharField()}),
        responses=AdminUserListSerializer,
    )
    def post(self, request, pk):
        user = generics.get_object_or_404(User, pk=pk)
        if user.is_staff or user.is_superuser or user.role != User.Role.FACILITY_STAFF:
            return Response(
                {"detail": "Only a facility staff account's password can be set here. "
                           "Mothers reset their own password from the app's sign-in screen."},
                status=400,
            )
        new_password = request.data.get("new_password")
        if not isinstance(new_password, str) or not new_password:
            return Response({"new_password": ["Enter a new password."]}, status=400)
        problems = staff_password_problems(new_password, user)
        if problems:
            return Response({"new_password": problems}, status=400)

        user.set_password(new_password)
        user.password_reset_code = ""
        user.password_reset_attempts = 0
        user.save(update_fields=["password", "password_reset_code", "password_reset_attempts"])
        lockout.clear(user)
        log_action(request.user, "account.password_set_by_admin", f"User:{user.id} ({user.email})")
        return Response(AdminUserListSerializer(user).data)


class AdminUserDeleteView(generics.DestroyAPIView):
    """
    DELETE /auth/admin/users/<id>/ -- platform-admin only, permanently
    removes an account.

    Mothers and facility staff: any admin. Another admin account: only the
    owner admin (accounts/owner.py), and the owner admin itself: nobody.

    A hard delete, not a deactivate (StaffUserSetActiveView above is the
    reversible version, left in place for facility staff to use on a
    mother account). Every other model's FK to User is CASCADE (milkbank
    requests and their questionnaires, transactions, chat history,
    notifications, article comments -- see each model's own FK), so this
    also removes everything that account ever did. core.AuditLogEntry.
    actor is the one exception (SET_NULL), so the "who did what" audit
    trail survives the account itself being gone.

    Logged BEFORE the delete runs, not after: once the row is gone
    there's no User left to log_action() against, and the target string
    has to say who this was while it still can.
    """

    queryset = User.objects.all()
    permission_classes = [permissions.IsAdminUser]

    def perform_destroy(self, instance):
        # Checked here, not as a queryset exclusion, so deleting yourself
        # fails with a clear reason instead of a bare 404 -- and so an
        # admin can never delete the only account that could undo it.
        if instance.id == self.request.user.id:
            raise ValidationError("You cannot delete your own account.")
        # Admins can't delete one another: one stolen admin login could
        # otherwise remove every other admin. Only the owner admin (the one
        # Render's ADMIN_EMAIL names -- accounts/owner.py) may, and nobody may
        # delete the owner admin. A refused try is worth a line in the audit log.
        if owner.is_admin(instance):
            if owner.is_owner(instance):
                reason = "This is the owner admin account (the one Render's ADMIN_EMAIL names). It can't be deleted."
            elif not owner.is_owner(self.request.user):
                reason = ("Only the owner admin (the account Render's ADMIN_EMAIL names) can delete another "
                          "admin account.")
            else:
                reason = None
            if reason:
                log_action(self.request.user, "user.delete_refused", f"User:{instance.id} ({instance.email}, admin)")
                raise PermissionDenied(reason)
        log_action(self.request.user, "user.deleted", f"User:{instance.id} ({instance.email}, {instance.role})")
        instance.delete()


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

        if user.location_consent_given:
            # She already consented. The app re-sends her position at the
            # start of every booking so Smart Allocation ranks from where she
            # is now, not from the first place she ever shared it. That is a
            # refresh, not a new consent: leave location_consent_at as the
            # original evidence and don't log a second "consent given".
            user.save(update_fields=["latitude", "longitude"])
            log_action(user, "location.updated", f"User:{user.id}")
        else:
            user.location_consent_given = True
            user.location_consent_at = timezone.now()
            user.save(update_fields=[
                "latitude", "longitude", "location_consent_given", "location_consent_at",
            ])
            log_action(user, "location.consent_given", f"User:{user.id}")

        return Response(UserSerializer(user).data)


def _check_password_again(request, user):
    """
    For an action that asks a signed-in account for its password again
    (deleting a mother's account, changing an admin's email): checks
    request.data's `password` under the same lockout as signing in
    (accounts/lockout.py). So a device left signed in can't be used for the
    action without the password, and the action can't be used to guess the
    password either.

    One check at a time per account, as at sign-in -- and, as at sign-in, a
    trusted device (request.data's `device_token`) isn't held to an
    account-wide lock that someone else's guesses set off; its own wrong
    passwords lock only itself.

    Returns None when the password is right, otherwise the response to send:
    423 while locked, or 400 {"code": "wrong_password"}.
    """
    password = request.data.get("password") or ""
    with lockout.one_attempt_at_a_time(user):
        device = devices.device_id(request.data.get("device_token"), user)
        if device is not None:
            until = lockout.device_locked_until(device)
            if until:
                return ThrottledTokenObtainPairView._locked_response(user, until)
        elif lockout.is_locked(user):
            return ThrottledTokenObtainPairView._locked_response(user)
        if not isinstance(password, str) or not user.check_password(password):
            if device is not None:
                lockout.register_device_failure(device)
                until = lockout.device_locked_until(device)
                if until:
                    return ThrottledTokenObtainPairView._locked_response(user, until)
            else:
                lockout.register_failure(user)
                if lockout.is_locked(user):
                    return ThrottledTokenObtainPairView._locked_response(user)
            return Response({"detail": "That password is incorrect.", "code": "wrong_password"}, status=400)
    return None


class DeleteMyAccountView(APIView):
    """
    POST /auth/me/delete/  {password}

    A mother permanently deletes her own account from the app.

    She confirms with her password, under the same lockout as signing in
    (accounts/lockout.py), so a phone left unlocked can't be used to wipe her
    account and this can't be used to guess her password either.

    Any booking still open is cancelled: the facility's staff are told which
    slot was freed (without her name -- she is deleting her data), and the
    slot frees itself because capacity is counted from the requests that
    exist. Then the account is deleted, and with it, through CASCADE, her
    requests and their questionnaires (the serology photo files go too --
    milkbank/signals.py), pickup representative details, transactions,
    comments and reports, saved articles, notifications and Kali chat. Audit
    entries stay (their actor is cleared) until the retention period ends.

    Facility staff and platform admin accounts are removed by a platform admin
    instead: one of them deleting itself could leave a facility, or the whole
    platform, with nobody able to sign in.
    """

    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "login"
    throttle_classes = [ClientIPScopedRateThrottle]

    @extend_schema(request=None)
    def post(self, request):
        from milkbank.models import MilkBankRequest

        user = request.user
        if user.is_staff or user.role != User.Role.MOTHER:
            return Response(
                {"detail": "Staff and admin accounts can only be removed by a platform admin."},
                status=403,
            )
        refusal = _check_password_again(request, user)
        if refusal is not None:
            return refusal

        open_statuses = [
            MilkBankRequest.Status.PENDING,
            MilkBankRequest.Status.AWAITING_ATTENDANCE,
            MilkBankRequest.Status.SCHEDULED,
            MilkBankRequest.Status.COUNTER_OFFERED,
        ]
        with transaction.atomic():
            for req in MilkBankRequest.objects.filter(owner=user, current_sub_status__in=open_statuses).select_related(
                "allocated_facility"
            ):
                kind = "donation" if req.request_type == MilkBankRequest.RequestType.DONOR else "milk request"
                when = f"{req.preferred_date:%B} {req.preferred_date.day}, {req.preferred_date.year} at {req.preferred_time}"
                notify_many(
                    User.objects.filter(
                        facility=req.allocated_facility, role=User.Role.FACILITY_STAFF, is_active=True,
                    ),
                    "Booking Cancelled",
                    f"A {kind} booking for {when} was cancelled because the mother deleted her KalingApp "
                    "account. The slot is free again.",
                    NotificationItem.Category.BOOKINGS,
                )
                log_action(None, "booking.cancelled_account_deleted", f"MilkBankRequest:{req.id}")
            # Logged without her email: she is deleting her personal data.
            log_action(None, "user.self_deleted", f"User:{user.id} (mother)")
            user.delete()
        return Response(status=204)


# --- Changing the sign-in email, keeping the same account -----------------
#
# Two steps, like a password reset: the password (step 1) sends a code to
# the new address, and the code (step 2) makes the change. The account stays
# the same row, so everything attached to it -- its audit log entries,
# messages, notifications -- stays attached. Admin accounts only for now
# (the admin portal's Settings); the same two views can later serve
# facility staff, or the mother app.

PENDING_EMAIL_FIELDS = ["pending_email", "pending_email_code", "pending_email_sent_at", "pending_email_attempts"]


def _clear_pending_email(user):
    user.pending_email = ""
    user.pending_email_code = ""
    user.pending_email_sent_at = None
    user.pending_email_attempts = 0


def _end_every_session(user):
    """
    Revokes every refresh token the account has been given (simplejwt's
    token_blacklist), so every device signed in to it is signed out once its
    short-lived access token runs out. The caller hands the device it is
    answering a fresh pair, issued after this.
    """
    from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken

    live = OutstandingToken.objects.filter(user=user, expires_at__gt=timezone.now(), blacklistedtoken__isnull=True)
    BlacklistedToken.objects.bulk_create([BlacklistedToken(token=token) for token in live], ignore_conflicts=True)


class EmailChangeRequestView(APIView):
    """
    POST /auth/me/email/  {new_email, password, device_token?} -- admin accounts only.

    Step 1 of changing the email this account signs in with. The password is
    checked first, under the sign-in lockout and trusted-device rules
    (_check_password_again), then a 6-digit code goes to the NEW address --
    the same lifetime (15 minutes), guess limit (5) and one-a-minute resend
    clock as a password reset code. Nothing about the account changes until
    EmailChangeConfirmView gets that code back.

    An address that already belongs to another account gets the very same
    answer: its inbox is sent a notice that it can't be used instead of a
    code, so this can't be used to find out which addresses have accounts.

    Asking again replaces the earlier code (and address).
    """

    permission_classes = [permissions.IsAdminUser]
    throttle_scope = "login"
    throttle_classes = [ClientIPScopedRateThrottle]

    @extend_schema(
        request=inline_serializer("EmailChangeRequest", {
            "new_email": serializers.EmailField(),
            "password": serializers.CharField(),
            "device_token": serializers.CharField(required=False),
        }),
        responses={200: None},
    )
    def post(self, request):
        user = request.user
        raw = request.data.get("new_email")
        if not isinstance(raw, str) or not raw.strip():
            return Response({"new_email": ["Enter the new email address."]}, status=400)
        try:
            # The same as a signup's address: trimmed and lower-cased.
            new_email = serializers.EmailField(max_length=254).run_validation(raw.strip()).lower()
        except ValidationError as exc:
            return Response({"new_email": exc.detail}, status=400)
        if new_email == user.email.lower():
            return Response({"new_email": ["That is already this account's email address."]}, status=400)

        refusal = _check_password_again(request, user)
        if refusal is not None:
            return refusal

        # Decided before the code is made, and answered the same either way.
        in_use = email_is_taken(new_email)
        now = timezone.now()
        with transaction.atomic():
            # Locked so two requests at once can't both send.
            account = User.objects.select_for_update().get(pk=user.pk)
            if account.pending_email_sent_at:
                wait = RESEND_COOLDOWN_SECONDS - int((now - account.pending_email_sent_at).total_seconds())
                if wait > 0:
                    wait = min(wait, RESEND_COOLDOWN_SECONDS)
                    return Response(
                        {
                            "detail": f"A code was sent less than a minute ago. Wait {wait} seconds, then try again.",
                            "code": "code_recently_sent",
                            "retry_after_seconds": wait,
                        },
                        status=429,
                        headers={"Retry-After": str(wait)},
                    )
            account.pending_email = new_email
            account.pending_email_code = "" if in_use else generate_code()
            account.pending_email_sent_at = now
            account.pending_email_attempts = 0
            account.save(update_fields=PENDING_EMAIL_FIELDS)

        if in_use:
            send, kind = send_email_in_use_notice, "email_in_use_notice"
        else:
            send, kind = send_email_change_code, "email_change_code"
        # Fire-and-forget, like every other email here (_send_in_background).
        threading.Thread(target=_send_in_background, args=(send, account, kind), daemon=True).start()
        log_action(user, "account.email_change_requested", f"User:{user.id} to {new_email}"[:255])

        return Response({
            "detail": f"We sent an email to {new_email}. Enter the 6-digit code from it to finish the change.",
            "pending_email": new_email,
            "code_minutes": VERIFICATION_CODE_TTL_MINUTES,
            "resend_after_seconds": RESEND_COOLDOWN_SECONDS,
        })


class EmailChangeConfirmView(APIView):
    """
    POST /auth/me/email/confirm/  {code} -- admin accounts only.

    Step 2: the code EmailChangeRequestView sent to the new address. Only now
    does the account's email change. Then:
      * every session of the account is signed out (its refresh tokens are
        revoked), and this one is handed a fresh pair in the answer
        (`access`, `refresh`) to carry on with;
      * the OLD address is told about the change;
      * the audit log records `account.email_changed` with both addresses;
      * any password reset code still out (it went to the old address) stops
        working.

    `admin_email_setting_outdated` in the answer is true when the old address
    is the one Render's ADMIN_EMAIL names: that setting needs the new address
    now. It also marks the owner admin (accounts/owner.py), so until it's
    updated there is none; ensure_admin won't recreate an admin under the old
    address, but it warns on every deploy.
    """

    permission_classes = [permissions.IsAdminUser]
    throttle_scope = "verify"
    throttle_classes = [ClientIPScopedRateThrottle]

    @extend_schema(
        request=inline_serializer("EmailChangeConfirm", {"code": serializers.CharField()}),
        responses={200: None},
    )
    def post(self, request):
        user = request.user
        code = str(request.data.get("code") or "").strip()
        user.refresh_from_db(fields=PENDING_EMAIL_FIELDS)

        if not user.pending_email or not user.pending_email_sent_at:
            return Response(
                {"detail": "There's no email change waiting to be confirmed. Start again.", "code": "no_pending_change"},
                status=400,
            )
        if timezone.now() - user.pending_email_sent_at > timedelta(minutes=VERIFICATION_CODE_TTL_MINUTES):
            return Response({"detail": "This code has expired. Ask for a new one.", "code": "code_expired"}, status=400)

        # Counted before it is compared, in one UPDATE that only succeeds while
        # guesses are left -- see VerifyEmailView.
        guesses_left = User.objects.filter(pk=user.pk, pending_email_attempts__lt=MAX_VERIFICATION_ATTEMPTS)
        if not guesses_left.update(pending_email_attempts=F("pending_email_attempts") + 1):
            return Response(
                {"detail": "Too many incorrect codes. Ask for a new one.", "code": "too_many_attempts"}, status=400,
            )
        if (
            not code
            or not user.pending_email_code
            or not secrets.compare_digest(code.encode(), user.pending_email_code.encode())
        ):
            return Response({"detail": "Incorrect code.", "code": "wrong_code"}, status=400)

        unusable = Response(
            {"detail": "That address now belongs to another KalingApp account, so it can't be used. "
                       "Choose a different one.", "code": "email_in_use"},
            status=400,
        )
        changed_at = timezone.now()
        with transaction.atomic():
            account = User.objects.select_for_update().get(pk=user.pk)
            # A newer request may have replaced the code since it was read above.
            if account.pending_email != user.pending_email or account.pending_email_code != user.pending_email_code:
                return Response(
                    {"detail": "A newer code was sent. Enter the code from the latest email.", "code": "code_replaced"},
                    status=400,
                )
            old_email, new_email = account.email, account.pending_email

            # Checked again: the address may have gained an account since the
            # code was sent. An abandoned signup from before verification
            # existed is the one kind of account that gives way (it would
            # otherwise block the address forever) -- see legacy_signups.py.
            for leftover in User.objects.filter(email__iexact=new_email, is_active=False).exclude(pk=account.pk):
                if is_abandoned_legacy_signup(leftover):
                    leftover.delete()
            if email_is_taken(new_email):
                _clear_pending_email(account)
                account.save(update_fields=PENDING_EMAIL_FIELDS)
                return unusable

            account.email = new_email
            # Proved just now, by the code that reached it.
            account.email_verified = True
            _clear_pending_email(account)
            # A reset code still out went to the old address; it mustn't
            # unlock the account at its new one.
            account.password_reset_code = ""
            account.password_reset_attempts = 0
            try:
                with transaction.atomic():
                    account.save(update_fields=[
                        "email", "email_verified", *PENDING_EMAIL_FIELDS,
                        "password_reset_code", "password_reset_attempts",
                    ])
            except IntegrityError:
                # Another account took the address at this very moment.
                account.refresh_from_db()
                _clear_pending_email(account)
                account.save(update_fields=PENDING_EMAIL_FIELDS)
                return unusable

        _end_every_session(account)
        refresh = RefreshToken.for_user(account)
        notice = partial(send_email_changed_notice, old_email=old_email, changed_at=changed_at)
        threading.Thread(
            target=_send_in_background, args=(notice, account, "email_changed_notice"), daemon=True,
        ).start()
        log_action(account, "account.email_changed", f"User:{account.id} from {old_email} to {new_email}"[:255])

        return Response({
            "detail": f"Your sign-in email is now {new_email}. Every other device signed in to this account "
                      "has been signed out.",
            "user": UserSerializer(account).data,
            "access": str(refresh.access_token),
            "refresh": str(refresh),
            "admin_email_setting_outdated": owner.owner_email() == old_email.lower(),
        })
