from django.db.models import F
from django.utils import timezone

from core.audit import log_action
from notifications.models import NotificationItem
from notifications.services import notify

from .business_hours import add_business_hours
from .models import Facility, MilkBankRequest, TransactionRecord

Status = MilkBankRequest.Status

# Which statuses have an active SLA clock, and therefore get a fresh
# response_deadline (8 business hours from *now*) the moment a request
# enters them: PENDING because the facility hasn't answered yet (set both
# here, for the counter-offer-rejected/rebook path, and in
# MilkBankRequestCreateView, for a brand-new request), AWAITING_ATTENDANCE
# because the mother hasn't confirmed yet. Every other status means
# nobody is waiting on anyone, so the clock is cleared instead.
STATUSES_WITH_SLA_CLOCK = {Status.PENDING, Status.AWAITING_ATTENDANCE}

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


def apply_transition(req, new_status, actor, action_name, message_override=None):
    """
    The one place a MilkBankRequest's status is ever allowed to change.
    Rejects illegal jumps (e.g. declined -> completed), and writes an
    audit log entry for every transition that succeeds -- this is the
    RA 10173 evidence trail for "who changed this booking, and to what."

    `message_override` exists for exactly one caller (sweep_expired_requests
    below): "expired" reached by a facility never responding and "expired"
    reached by the mother never confirming are the same status but very
    different things to tell her, and STATUS_NOTIFICATIONS only has room
    for one message per status.
    """
    if new_status not in ALLOWED_TRANSITIONS.get(req.current_sub_status, set()):
        raise InvalidTransition(f"Cannot move from {req.current_sub_status} to {new_status}")

    req.current_sub_status = new_status
    req.response_deadline = add_business_hours(timezone.now()) if new_status in STATUSES_WITH_SLA_CLOCK else None
    req.save(update_fields=["current_sub_status", "response_deadline"])
    log_action(actor, f"booking.{action_name}", f"MilkBankRequest:{req.id}")

    message = message_override or STATUS_NOTIFICATIONS.get(new_status)
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


# Different wording depending on *whose* clock ran out, even though both
# land on the same EXPIRED status -- see apply_transition's message_override.
SLA_EXPIRY_MESSAGES = {
    Status.PENDING: (
        "The facility didn't respond to your request within 8 business hours, "
        "so it has expired. Please submit a new request."
    ),
    Status.AWAITING_ATTENDANCE: (
        "Your request expired because attendance wasn't confirmed within "
        "8 business hours of the facility's acceptance. Please submit a new request."
    ),
}


def sweep_expired_requests(now=None):
    """
    Expires every PENDING/AWAITING_ATTENDANCE request whose response_deadline
    has passed, and returns the list of requests that were actually expired.

    There's no Celery/cron worker in front of this app (Render's free tier
    web service is the only process that ever runs), so this has two
    callers rather than one: milkbank/views.py's read endpoints call it
    inline before serving a mother's or facility's own requests, so nobody
    is ever shown a stale pending/awaiting_attendance status past its
    deadline just because nothing else happened to trigger a check. The
    token-protected /milkbank/sweep-expired/ endpoint (see StaffSweepExpiredView)
    is for an external scheduler (e.g. a free cron-job.org ping) to hit on a
    timer, so an expiry -- and the notification telling her about it -- can
    fire close to the actual deadline instead of waiting for a coincidental
    page load.

    `now` is overridable for tests; every real caller leaves it as
    timezone.now().
    """
    now = now or timezone.now()
    overdue = MilkBankRequest.objects.filter(
        current_sub_status__in=STATUSES_WITH_SLA_CLOCK,
        response_deadline__isnull=False,
        response_deadline__lt=now,
    )

    expired = []
    for req in overdue:
        was_status = req.current_sub_status
        try:
            apply_transition(
                req, Status.EXPIRED, None, "expired_sla_timeout",
                message_override=SLA_EXPIRY_MESSAGES.get(was_status),
            )
        except InvalidTransition:
            # Another request in this same sweep (or a concurrent request
            # from the owner/facility) already moved it off PENDING/
            # AWAITING_ATTENDANCE between the query above and this save --
            # nothing to do, it's no longer overdue in whatever it became.
            continue
        expired.append(req)
    return expired
