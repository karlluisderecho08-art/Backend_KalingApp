import secrets

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

from core.i18n import tr

# How long a code stays valid, and how long a mother must wait before
# requesting another one -- both measured off the same sent_at
# timestamp (see VerifyEmailView/ResendVerificationView in views.py).
VERIFICATION_CODE_TTL_MINUTES = 15
RESEND_COOLDOWN_SECONDS = 60
MAX_VERIFICATION_ATTEMPTS = 5


def generate_code():
    """
    secrets.randbelow (not `random`) because these codes are short-lived
    credentials, not cosmetic IDs -- they should be as hard to predict
    as the 1-in-a-million odds on paper suggest.
    """
    return f"{secrets.randbelow(1_000_000):06d}"


def send_verification_email(pending, tagalog=False):
    """
    (Re)generates a 6-digit verification code on a PendingRegistration,
    saves it, and emails it.

    Takes a PendingRegistration rather than a User on purpose: no
    account exists yet at this point in the flow, and won't until the
    code comes back correct (see accounts/models.py).

    Called from both RegisterView (first code) and
    ResendVerificationView (a fresh one) -- either way, wipes the
    previous code and attempt count so an old, possibly-already-
    guessed-at code can't still work alongside the new one.
    """
    pending.code = generate_code()
    pending.sent_at = timezone.now()
    pending.attempts = 0
    pending.save(update_fields=["code", "sent_at", "attempts"])

    # Written in English, sent in Tagalog when she signed up from the app in
    # Tagalog (core/tagalog_catalog.py has both versions of each email).
    send_mail(
        subject=tr("Your KalingApp verification code", tagalog=tagalog),
        message=tr((
            f"Hi {pending.mom_name or 'there'},\n\n"
            f"Your KalingApp verification code is {pending.code}.\n"
            f"It expires in {VERIFICATION_CODE_TTL_MINUTES} minutes.\n\n"
            "If you didn't try to create a KalingApp account, you can safely ignore this email."
        ), tagalog=tagalog),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[pending.email],
        fail_silently=False,
    )


def send_existing_account_notice(user, tagalog=False):
    """
    Sent instead of a verification code when someone signs up with an
    address that already has an account. The signup is answered exactly as
    for a new address (RegisterView), so it can't be used to find out which
    addresses have KalingApp accounts -- this email is how the real owner of
    the address hears about it. Touches no database row.
    """
    send_mail(
        subject=tr("Your KalingApp account", tagalog=tagalog),
        message=tr((
            f"Hi {user.mom_name or 'there'},\n\n"
            "Someone just tried to create a new KalingApp account with this email address. "
            "There's already an account with this email, so no new one was made.\n\n"
            "If that was you, just sign in with your password. If you've forgotten it, you can set a "
            "new one from the sign-in screen. If you still can't sign in, contact the KalingApp admin "
            "for help.\n\n"
            "If it wasn't you, you can safely ignore this email — nothing about your account has changed."
        ), tagalog=tagalog),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user.email],
        fail_silently=False,
    )


def send_password_reset_email(user, tagalog=False):
    """
    (Re)generates a 6-digit password reset code for `user`, saves it, and
    emails it. Mirrors send_verification_email() above, but takes a real
    User: unlike registration, a password reset only ever applies to an
    account that already exists.
    Called from ForgotPasswordView -- each call wipes any previous reset
    code/attempt count so an old, possibly-already-guessed-at code can't
    still work alongside the new one.
    """
    user.password_reset_code = generate_code()
    user.password_reset_sent_at = timezone.now()
    user.password_reset_attempts = 0
    user.save(update_fields=["password_reset_code", "password_reset_sent_at", "password_reset_attempts"])

    send_mail(
        subject=tr("Your KalingApp password reset code", tagalog=tagalog),
        message=tr((
            f"Hi {user.mom_name or 'there'},\n\n"
            f"Your KalingApp password reset code is {user.password_reset_code}.\n"
            f"It expires in {VERIFICATION_CODE_TTL_MINUTES} minutes.\n\n"
            "If you didn't ask to reset your KalingApp password, you can safely ignore this "
            "email — your password hasn't been changed."
        ), tagalog=tagalog),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user.email],
        fail_silently=False,
    )
