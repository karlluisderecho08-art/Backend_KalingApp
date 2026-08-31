import secrets

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

# How long a code stays valid, and how long a mother must wait before
# requesting another one -- both measured off the same
# email_verification_sent_at timestamp (see VerifyEmailView/
# ResendVerificationView in views.py).
VERIFICATION_CODE_TTL_MINUTES = 15
RESEND_COOLDOWN_SECONDS = 60
MAX_VERIFICATION_ATTEMPTS = 5


def send_verification_email(user):
    """
    (Re)generates a 6-digit verification code for `user`, saves it, and
    emails it. secrets.randbelow (not `random`) because this code is a
    short-lived credential, not a cosmetic ID -- it should be as hard to
    predict as the 1-in-a-million odds on paper suggest.

    Called from both RegisterView (first code) and ResendVerificationView
    (a fresh one) -- either way, wipes the previous code and attempt
    count so an old, possibly-already-guessed-at code can't still work
    alongside the new one.
    """
    user.email_verification_code = f"{secrets.randbelow(1_000_000):06d}"
    user.email_verification_sent_at = timezone.now()
    user.email_verification_attempts = 0
    user.save(update_fields=[
        "email_verification_code", "email_verification_sent_at", "email_verification_attempts",
    ])

    send_mail(
        subject="Your KalingApp verification code",
        message=(
            f"Hi {user.mom_name or 'there'},\n\n"
            f"Your KalingApp verification code is {user.email_verification_code}.\n"
            f"It expires in {VERIFICATION_CODE_TTL_MINUTES} minutes.\n\n"
            "If you didn't try to create a KalingApp account, you can safely ignore this email."
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user.email],
        fail_silently=False,
    )
