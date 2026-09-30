from django.db.models import F
from django.utils import timezone

from accounts.models import User
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
#
# Two of these edges exist for the RECIPIENT pathway only, and are worth
# spelling out because they look odd next to the DONOR one:
#
#   PENDING -> SCHEDULED
#     Accepting a RECIPIENT lands her on the "Status" stage, where staff
#     review the serology test and questionnaire she submitted. That is
#     facility work, not a wait on the mother, so it cannot be
#     AWAITING_ATTENDANCE -- which would start the 8-business-hour clock
#     against her for something she has already done. SCHEDULED is the
#     same status the DONOR screening stages use for exactly this shape
#     of "active, staff is working on it, nobody is being waited on."
#
#   SCHEDULED -> AWAITING_ATTENDANCE
#     Once that review passes, she moves to "Booking Confirmation", which
#     IS a wait on the mother. This is the only backwards-looking edge in
#     the graph, and it is deliberate: for a RECIPIENT the staff review
#     happens BEFORE attendance is confirmed, so the two statuses occur
#     in the opposite order to the DONOR pathway.
#
#   PENDING -> COUNTER_OFFERED
#     "No doctor available on the date she asked for" is not a reason to
#     refuse a mother -- it is a reason to offer her a different date. It
#     used to be one of the Booking Request desk's decline reasons, which
#     ended the request outright and made her submit the whole thing
#     again (questionnaire, serology photo and all) just to change one
#     date. Staff now propose a date a doctor IS available instead, from
#     the same desk, before the request has been accepted -- so this edge
#     has to exist alongside DECLINED rather than after it.
#
#     Applies to both pathways: a DONOR needs a doctor for Counseling and
#     Testing, a RECIPIENT for her dispensing appointment, and neither
#     should be turned away over staff scheduling.
#
#     COUNTER_OFFERED deliberately carries no SLA clock (it is not in
#     STATUSES_WITH_SLA_CLOCK above) and is not terminal, so proposing a
#     date keeps her booking slot held at the facility while she decides,
#     and does not start a countdown against her for a delay that was
#     never hers.
#
#   COUNTER_OFFERED -> AWAITING_ATTENDANCE
#     Accepting the proposed slot is the same decision as accepting the
#     original request, so it has to land her where acceptance lands her
#     -- which for a DONOR is AWAITING_ATTENDANCE, still waiting on her to
#     confirm the new date. Sending her to SCHEDULED instead would skip
#     the confirmation entirely: the mobile app gates its "Confirm My
#     Attendance" button on the Booking Confirmation stage, so she would
#     either never be shown it or get a 400 when she was.
ALLOWED_TRANSITIONS = {
    Status.PENDING: {
        Status.AWAITING_ATTENDANCE, Status.SCHEDULED, Status.COUNTER_OFFERED,
        Status.DECLINED, Status.EXPIRED,
    },
    Status.AWAITING_ATTENDANCE: {Status.SCHEDULED, Status.COUNTER_OFFERED, Status.EXPIRED},
    Status.COUNTER_OFFERED: {Status.SCHEDULED, Status.PENDING, Status.AWAITING_ATTENDANCE},
    Status.SCHEDULED: {Status.COMPLETED, Status.AWAITING_ATTENDANCE},
    Status.DECLINED: set(),
    Status.EXPIRED: set(),
    Status.COMPLETED: set(),
}


class InvalidTransition(Exception):
    pass


def apply_transition(req, new_status, actor, action_name, message_override=None, amount_ml=None):
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

    `amount_ml` is COMPLETED-only (StaffConfirmCompletionView is its only
    real caller with a non-None value) -- how many millilitres staff
    recorded for this booking. Moves Facility.stock_level_ml the opposite
    direction for a DONOR vs. a RECIPIENT (see the block below), and
    credits the requester's own running lifetime total on accounts.User --
    total_drawn_ml for a DONOR, total_received_ml for a RECIPIENT. All of
    those are millilitres too, so the figure is applied as given rather
    than converted. The view already validated a RECIPIENT amount against
    available stock before calling this, so stock_level_ml going negative
    here would mean that check was bypassed, not that this function needs
    to re-guard it.
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
            amount_ml=amount_ml,
        )
        # completed_at always gets set here, independent of amount_ml below
        # -- a booking reaching COMPLETED is what "finished" means for the
        # Finished Transactions list, and that must not depend on staff
        # having entered an amount (amount_ml is effectively always given
        # by the real endpoint, but apply_transition's signature allows
        # None, and this field shouldn't silently stay empty if it ever is).
        req.completed_at = timezone.now()
        update_fields = ["completed_at"]
        if amount_ml:
            req.amount_ml = amount_ml
            update_fields.append("amount_ml")
            if req.request_type == MilkBankRequest.RequestType.DONOR:
                Facility.objects.filter(pk=req.allocated_facility_id).update(
                    stock_level_ml=F("stock_level_ml") + amount_ml
                )
                User.objects.filter(pk=req.owner_id).update(total_drawn_ml=F("total_drawn_ml") + amount_ml)
            else:
                Facility.objects.filter(pk=req.allocated_facility_id).update(
                    stock_level_ml=F("stock_level_ml") - amount_ml
                )
                User.objects.filter(pk=req.owner_id).update(total_received_ml=F("total_received_ml") + amount_ml)
        req.save(update_fields=update_fields)


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
