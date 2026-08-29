from django.db.models import F

from core.audit import log_action
from notifications.models import NotificationItem
from notifications.services import notify

from .models import Facility, MilkBankRequest, TransactionRecord

Status = MilkBankRequest.Status

# A request in any of these statuses no longer occupies a booking slot.
TERMINAL_STATUSES = {Status.DECLINED, Status.EXPIRED, Status.COMPLETED}

# What to tell the mother when her booking reaches each status. This is
# the server-side replacement for the Kotlin app hand-writing a
# notification at each call site (e.g. finalizeAppointment()) -- one
# table here instead of scattered addNotification() calls.
STATUS_NOTIFICATIONS = {
    Status.AWAITING_ATTENDANCE: "The facility accepted your request. Please confirm your attendance.",
    Status.SCHEDULED: "Your appointment is scheduled.",
    Status.DECLINED: "The facility declined your request.",
    Status.EXPIRED: "Your request has expired.",
    Status.COUNTER_OFFERED: "The facility proposed a new date for your appointment.",
    Status.COMPLETED: "Your booking is complete. Thank you!",
}

# Which status a request is allowed to move to from each current status.
# The roadmap mentions django-fsm as an option for this; a plain dict is
# enough for seven statuses and keeps this readable without adding a new
# dependency -- worth revisiting only if the rules get much more complex.
ALLOWED_TRANSITIONS = {
    Status.PENDING: {Status.AWAITING_ATTENDANCE, Status.DECLINED, Status.EXPIRED},
    Status.AWAITING_ATTENDANCE: {Status.SCHEDULED, Status.COUNTER_OFFERED, Status.EXPIRED},
    Status.COUNTER_OFFERED: {Status.SCHEDULED, Status.PENDING},
    Status.SCHEDULED: {Status.COMPLETED},
    Status.DECLINED: set(),
    Status.EXPIRED: set(),
    Status.COMPLETED: set(),
}


class InvalidTransition(Exception):
    pass


def apply_transition(req, new_status, actor, action_name):
    """
    The one place a MilkBankRequest's status is ever allowed to change.
    Rejects illegal jumps (e.g. declined -> completed), and writes an
    audit log entry for every transition that succeeds -- this is the
    RA 10173 evidence trail for "who changed this booking, and to what."
    """
    if new_status not in ALLOWED_TRANSITIONS.get(req.current_sub_status, set()):
        raise InvalidTransition(f"Cannot move from {req.current_sub_status} to {new_status}")

    req.current_sub_status = new_status
    req.save(update_fields=["current_sub_status"])
    log_action(actor, f"booking.{action_name}", f"MilkBankRequest:{req.id}")

    message = STATUS_NOTIFICATIONS.get(new_status)
    if message:
        notify(req.owner, f"Milk Bank Request: {req.get_current_sub_status_display()}", message, NotificationItem.Category.BOOKINGS)

    # Keep Facility.booked_count -- the number Smart Allocation's ratio
    # tier depends on -- in sync with reality as requests close out.
    # F() does the +1/-1 as one atomic UPDATE in the database, so two
    # requests finishing at the same moment can't race and undercount.
    if new_status in TERMINAL_STATUSES:
        Facility.objects.filter(pk=req.allocated_facility_id).update(booked_count=F("booked_count") - 1)

    if new_status == Status.COMPLETED:
        TransactionRecord.objects.create(
            owner=req.owner,
            type=TransactionRecord.TransactionType.DONATION
            if req.request_type == MilkBankRequest.RequestType.DONOR
            else TransactionRecord.TransactionType.RECEIVED,
            facility_name=req.allocated_facility.name,
            date=req.preferred_date,
            status=TransactionRecord.TransactionStatus.COMPLETED,
        )
