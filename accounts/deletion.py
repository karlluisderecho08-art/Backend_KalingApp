"""
Deleting a mother's account -- by herself (DeleteMyAccountView) or by a
platform admin (AdminUserDeleteView). Both go through delete_mother_account(),
so what happens to her bookings can't differ with who pressed the button:

  * a booking still open is cancelled: her facility's staff are told which
    slot was freed (without her name), and the slot frees itself, because
    capacity is counted from the bookings that exist;
  * a COMPLETED booking stays in the facility's records, anonymised: what was
    donated or dispensed, where and when, under a random reference shared by
    all her kept bookings -- so donor milk can still be traced, and stock
    history still adds up -- but nothing that leads back to her: no account,
    no name, no representative, pickup address, courier link, staff note or
    screening answers (her serology photo file goes too, milkbank/signals.py);
  * everything else she had is deleted with the account (CASCADE): other
    bookings and their questionnaires, transactions, comments and reports,
    saved articles, notifications, Kali chat.

Audit entries stay (their actor is cleared) until the retention period ends.
"""

import secrets

from django.db import transaction

from core.audit import log_action
from notifications.models import NotificationItem
from notifications.services import notify_many

from .models import User

# No 0/O, 1/I: a reference staff may read aloud or copy by hand.
_REF_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

CANCELLED_TITLE = "Booking Cancelled"
SELF_DELETED = (
    "A {kind} booking for {when} was cancelled because the mother deleted her KalingApp account. "
    "The slot is free again."
)
ADMIN_DELETED = (
    "A {kind} booking for {when} was cancelled because a KalingApp admin deleted the mother's account. "
    "The slot is free again."
)


def new_former_owner_ref():
    """'M-7F3K2A' -- random, so it can't be traced back to the account it replaced."""
    return "M-" + "".join(secrets.choice(_REF_ALPHABET) for _ in range(6))


def _open_statuses():
    from milkbank.transitions import FINAL_STATUSES
    from milkbank.models import MilkBankRequest

    return [s for s in MilkBankRequest.Status.values if s not in FINAL_STATUSES]


def _tell_staff_about_open_bookings(user, by_admin):
    from milkbank.models import MilkBankRequest

    template = ADMIN_DELETED if by_admin else SELF_DELETED
    bookings = MilkBankRequest.objects.filter(owner=user, current_sub_status__in=_open_statuses()).select_related(
        "allocated_facility"
    )
    for req in bookings:
        kind = "donation" if req.request_type == MilkBankRequest.RequestType.DONOR else "milk request"
        day = req.preferred_date
        when = f"{day:%B} {day.day}, {day.year} at {req.preferred_time}"
        notify_many(
            User.objects.filter(facility=req.allocated_facility, role=User.Role.FACILITY_STAFF, is_active=True),
            CANCELLED_TITLE,
            template.format(kind=kind, when=when),
            NotificationItem.Category.BOOKINGS,
        )
        log_action(None, "booking.cancelled_account_deleted", f"MilkBankRequest:{req.id}")


def keep_completed_bookings(user):
    """
    Detaches her COMPLETED bookings from the account, anonymised (see the
    module docstring), before the account is deleted. Returns how many.
    """
    from milkbank.models import DonorQuestionnaire, MilkBankRequest

    completed = MilkBankRequest.objects.filter(owner=user, current_sub_status=MilkBankRequest.Status.COMPLETED)
    ids = list(completed.values_list("id", flat=True))
    if not ids:
        return 0
    # Deleted one by one, not with a bulk update, so each serology photo's
    # file is removed too (milkbank/signals.py runs on post_delete).
    for questionnaire in DonorQuestionnaire.objects.filter(request_id__in=ids):
        questionnaire.delete()
    MilkBankRequest.objects.filter(id__in=ids).update(
        owner=None,
        former_owner_ref=new_former_owner_ref(),
        staff_message="",
        needs_representative=False,
        representative_name="",
        representative_birthday=None,
        representative_contact_number="",
        neonate_name="",
        clinic_info="",
        delivery_link="",
        pickup_address="",
        pickup_landmark="",
        pickup_latitude=None,
        pickup_longitude=None,
        delivery_reminder_for="",
    )
    return len(ids)


def delete_mother_account(user, by_admin, actor=None):
    """
    Deletes a mother's account as described in the module docstring, in one
    transaction. `actor` is the admin doing it (None when she deletes her own).
    """
    with transaction.atomic():
        _tell_staff_about_open_bookings(user, by_admin)
        kept = keep_completed_bookings(user)
        if kept:
            log_action(actor, "booking.kept_anonymised", f"User:{user.id} -- {kept} completed booking(s)")
        if not by_admin:
            # Logged without her email: she is deleting her personal data.
            log_action(None, "user.self_deleted", f"User:{user.id} (mother)")
        user.delete()
