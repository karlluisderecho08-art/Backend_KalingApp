"""
Which inactive accounts may be replaced by a new signup for the same email.

Before signups were held as PendingRegistration rows, registering created an
inactive User straight away and verifying the code switched it on. Signups
abandoned under that old flow left inactive rows behind that nothing can
verify any more, and their owners must still be able to register.

An inactive account is otherwise NOT free to replace. Facility staff
deactivate mothers on purpose, and replacing such an account would both
undo that decision and erase her history (bookings, questionnaires,
transactions -- everything CASCADEs from User). So a row only counts as an
abandoned old signup when everything about it says it was never used:

  * inactive, never email-verified, never logged in;
  * an ordinary mother -- never a staff or admin account;
  * no deactivation on record (core.AuditLogEntry "user.deactivated");
  * nothing of hers anywhere: no booking, no transaction, no comment, no
    Kali chat.
"""

from .models import User


def is_abandoned_legacy_signup(user):
    if user.is_active or user.email_verified or user.last_login is not None:
        return False
    if user.role != User.Role.MOTHER or user.is_staff or user.is_superuser:
        return False

    # Imported here, not at module level: accounts is the lower-level app.
    from articles.models import ArticleComment
    from chat.models import ChatSession
    from core.models import AuditLogEntry
    from milkbank.models import MilkBankRequest, TransactionRecord

    if AuditLogEntry.objects.filter(action="user.deactivated", target=f"User:{user.id}").exists():
        return False
    return not (
        MilkBankRequest.objects.filter(owner=user).exists()
        or TransactionRecord.objects.filter(owner=user).exists()
        or ArticleComment.objects.filter(author=user).exists()
        or ChatSession.objects.filter(owner=user).exists()
    )


def email_is_taken(email):
    """True when this address belongs to an account a new signup must not replace."""
    return any(
        not is_abandoned_legacy_signup(user)
        for user in User.objects.filter(email__iexact=email)
    )
