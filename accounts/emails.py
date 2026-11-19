import secrets
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone
from django.utils.html import escape

from core.i18n import tr

# How long a code stays valid, and how long a mother must wait before
# requesting another one -- both measured off the same sent_at
# timestamp (see VerifyEmailView/ResendVerificationView in views.py).
VERIFICATION_CODE_TTL_MINUTES = 15
RESEND_COOLDOWN_SECONDS = 60
MAX_VERIFICATION_ATTEMPTS = 5


# The code emails' HTML version: one card, the code large in its own box (as
# the owner asked, 2026-11-20). KalingApp's colours (the app's ui/theme/Color.kt):
# deep raspberry headings and code, blush pink code box, warm beige page.
# Tables and inline styles only, so mail apps that strip <style> still show it.
# Every value is escaped: her name is hers to type.
_CODE_EMAIL_HTML = """<!DOCTYPE html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<meta name="supported-color-schemes" content="light">
<title>{heading}</title>
</head>
<body style="margin:0;padding:0;background-color:#FFF6F1;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#FFF6F1;">
<tr><td align="center" style="padding:32px 16px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:480px;background-color:#FFFFFF;border:1px solid #F7D3DF;border-radius:16px;">
<tr><td style="padding:32px 28px;font-family:Arial,Helvetica,sans-serif;color:#111111;">
<h1 style="margin:0 0 20px 0;font-size:24px;line-height:30px;font-weight:bold;color:#9E1C4B;">{heading}</h1>
<p style="margin:0 0 12px 0;font-size:16px;line-height:24px;">{greeting}</p>
<p style="margin:0 0 12px 0;font-size:16px;line-height:24px;">{intro}</p>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:0 0 20px 0;">
<tr><td align="center" style="padding:18px 12px 18px 22px;background-color:#FFE8EF;border:1px solid #D4A437;border-radius:12px;font-family:'Courier New',Courier,monospace;font-size:34px;line-height:40px;font-weight:bold;letter-spacing:10px;color:#9E1C4B;">{code}</td></tr>
</table>
<p style="margin:0 0 16px 0;font-size:16px;line-height:24px;">{expiry}</p>
{after}<p style="margin:0;font-size:13px;line-height:20px;color:#6B6B6B;">{ignore}</p>
</td></tr>
</table>
</td></tr>
</table>
</body>
</html>
"""


def code_email_html(*, heading, greeting, intro, code, minutes, ignore, after="", tagalog=False):
    """
    A code email's HTML version. Every line is given in English and sent
    through tr() (core/tagalog_catalog.py), so a mother who signed up in
    Tagalog gets it in Tagalog, like the plain-text version beside it.
    """
    expiry = escape(tr(f"This code expires in {minutes} minutes.", tagalog=tagalog))
    for unit in ("minutes", "minuto"):
        expiry = expiry.replace(f"{minutes} {unit}", f"<strong>{minutes} {unit}</strong>", 1)
    return _CODE_EMAIL_HTML.format(
        lang="fil" if tagalog else "en",
        heading=escape(tr(heading, tagalog=tagalog)),
        greeting=escape(tr(greeting, tagalog=tagalog)),
        intro=escape(tr(intro, tagalog=tagalog)),
        code=escape(code),
        expiry=expiry,
        after=(f'<p style="margin:0 0 16px 0;font-size:16px;line-height:24px;">{escape(tr(after, tagalog=tagalog))}</p>\n'
               if after else ""),
        ignore=escape(tr(ignore, tagalog=tagalog)),
    )


def _greeting(name):
    return f"Hi {name}," if name else "Hi there,"


VERIFY_IGNORE = "If you didn't try to create a KalingApp account, you can safely ignore this email."
RESET_IGNORE = (
    "If you didn't ask to reset your KalingApp password, you can safely ignore this email — "
    "your password hasn't been changed."
)


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
    # Tagalog (core/tagalog_catalog.py has both versions of each email). The
    # HTML version is what mail apps show; the plain text is for any that can't.
    send_mail(
        subject=tr("Your KalingApp verification code", tagalog=tagalog),
        message=tr((
            f"Hi {pending.mom_name or 'there'},\n\n"
            f"Your KalingApp verification code is {pending.code}.\n"
            f"It expires in {VERIFICATION_CODE_TTL_MINUTES} minutes.\n\n"
            f"{VERIFY_IGNORE}"
        ), tagalog=tagalog),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[pending.email],
        fail_silently=False,
        html_message=code_email_html(
            heading="Verify your KalingApp account",
            greeting=_greeting(pending.mom_name),
            intro="Your 6-digit verification code is:",
            code=pending.code,
            minutes=VERIFICATION_CODE_TTL_MINUTES,
            ignore=VERIFY_IGNORE,
            tagalog=tagalog,
        ),
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
            f"{RESET_IGNORE}"
        ), tagalog=tagalog),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user.email],
        fail_silently=False,
        html_message=code_email_html(
            heading="Reset your KalingApp password",
            greeting=_greeting(user.mom_name),
            intro="Your 6-digit password reset code is:",
            code=user.password_reset_code,
            minutes=VERIFICATION_CODE_TTL_MINUTES,
            ignore=RESET_IGNORE,
            tagalog=tagalog,
        ),
    )


# --- Changing an account's sign-in email (accounts.views.EmailChangeRequestView).
# Only admins can change their email so far, from the admin portal, so these
# are in English only. `tagalog` is accepted to fit views._send_in_background. ---

def send_email_change_code(user, tagalog=False):
    """
    Emails the code that confirms an email change to the NEW address
    (user.pending_email). The code was generated and saved by the view before
    it answered, so the confirm step never races this send. The account's
    current address is left out: if the new one was mistyped, a stranger
    reads this.
    """
    send_mail(
        subject="Confirm your new KalingApp email address",
        message=(
            "Hi,\n\n"
            f"Your KalingApp code to confirm this email address is {user.pending_email_code}.\n"
            f"It expires in {VERIFICATION_CODE_TTL_MINUTES} minutes.\n\n"
            "Enter it in the KalingApp admin portal to make this the account's sign-in email.\n\n"
            "If you didn't ask for this, you can safely ignore this email — nothing will change."
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user.pending_email],
        fail_silently=False,
        html_message=code_email_html(
            heading="Confirm your new KalingApp email address",
            greeting="Hi,",
            intro="Your 6-digit code to confirm this email address is:",
            code=user.pending_email_code,
            minutes=VERIFICATION_CODE_TTL_MINUTES,
            after="Enter it in the KalingApp admin portal to make this the account's sign-in email.",
            ignore="If you didn't ask for this, you can safely ignore this email — nothing will change.",
        ),
    )


def send_email_in_use_notice(user, tagalog=False):
    """
    Sent to the new address INSTEAD of a code when it already belongs to
    another account. The admin portal is answered exactly as if a code had
    gone out, so the form can't be used to find out which addresses have
    accounts; whoever really reads this inbox learns why no code came.
    """
    send_mail(
        subject="Your KalingApp email address",
        message=(
            "Hi,\n\n"
            "Someone asked to move a KalingApp admin account to this email address. This address "
            "already has a KalingApp account, so it can't be used for another one, and nothing was "
            "changed.\n\n"
            "If that was you, go back to the admin portal and choose a different address. If it "
            "wasn't you, you can safely ignore this email."
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user.pending_email],
        fail_silently=False,
    )


def send_email_changed_notice(user, old_email, changed_at, tagalog=False):
    """
    Tells the OLD address that the account now signs in with user.email, once
    the change is done -- so a change its owner didn't make doesn't go unseen.
    """
    when = changed_at.astimezone(ZoneInfo("Asia/Manila"))
    send_mail(
        subject="Your KalingApp email address was changed",
        message=(
            "Hi,\n\n"
            f"The KalingApp admin account that used this email address now signs in with "
            f"{user.email} instead. The change was made on {when:%B} {when.day}, {when.year} at "
            f"{when.hour % 12 or 12}:{when:%M %p} (Philippine time), confirmed with the account's password and a code "
            "sent to the new address.\n\n"
            "If you made this change, there's nothing else to do. If you didn't, contact another "
            "KalingApp admin right away."
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[old_email],
        fail_silently=False,
    )
