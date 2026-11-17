"""
Breastmilk Collection for donors: when the delivery link is due, and the
notifications that walk her through the way she chose.

She picks how her milk reaches the facility right after choosing her
appointment date and time (the app no longer asks on the health form), and can
change it until her booking is confirmed -- after that it is fixed, so the
facility can plan around it (views.CollectionMethodView).

A Drop off goes by courier, so a Drop off donor confirms her booking by pasting
the courier's delivery link instead of confirming attendance. She can paste it
any time once the facility has accepted -- usually on the day, once the courier
is booked -- so her booking doesn't expire 8 business hours after acceptance:
it waits until DROP_OFF_LINK_GRACE after her appointment time
(transitions.attendance_deadline). She can still replace the link (a courier
rebooked) until her milk is collected.

Notifications (all Bookings, all with a Tagalog line in core/tagalog_catalog.py):
  * when her appointment is confirmed (Scheduled), and whenever she changes the
    method, the steps for that method, with her date and time. Sent while she
    can still change it, they say so; sent at confirmation, they say to message
    the facility instead -- the method is fixed from then on;
  * at the appointment hour, for a Drop off with no link yet, a reminder to
    book the courier and paste its link. Sent once per appointment, from the
    same sweep that expires overdue requests (transitions.sweep_expired_requests).
"""

from datetime import date, datetime, timedelta

from django.utils import timezone

from notifications.models import NotificationItem
from notifications.services import notify

from .business_hours import BUSINESS_TZ
from .models import MilkBankRequest

Method = MilkBankRequest.CollectionMethod
Status = MilkBankRequest.Status

# While the booking is still heading to her appointment.
OPEN_STATUSES = (Status.AWAITING_ATTENDANCE, Status.SCHEDULED)

STEPS_TITLE = "How your milk will reach the facility"
# Sent while she can still change the method -- until her booking is confirmed.
STEPS = {
    Method.PICKUP: (
        "Pick up: the facility will collect your milk from you on {date} at {time}. "
        "Have your milk ready by then. You can change this in Booking Status until your booking is confirmed."
    ),
    Method.WALK_IN: (
        "Walk-in: bring your milk to the facility yourself on {date} at {time}. "
        "Keep it cold on the way, in a cooler or insulated bag. "
        "You can change this in Booking Status until your booking is confirmed."
    ),
    Method.DROP_OFF: (
        "Drop off: book a courier (for example Lalamove or Grab) to bring your milk to the facility on "
        "{date} at {time}, then paste its delivery link in Booking Status. That confirms your booking "
        "and lets the facility track your milk."
    ),
}
# Sent once her booking is confirmed: the method is fixed from then on
# (views.CollectionMethodView), so these say what to do if something changes.
STEPS_CONFIRMED = {
    Method.PICKUP: (
        "Pick up: the facility will collect your milk from you on {date} at {time}. "
        "Have your milk ready by then. If something changes, message the facility in Chat from Booking Status."
    ),
    Method.WALK_IN: (
        "Walk-in: bring your milk to the facility yourself on {date} at {time}. "
        "Keep it cold on the way, in a cooler or insulated bag. "
        "If something changes, message the facility in Chat from Booking Status."
    ),
}
# A Drop off whose link is already in (pasting it is what confirmed her booking).
STEPS_DROP_OFF_LINKED = (
    "Drop off: your courier brings your milk to the facility on {date} at {time}, and the facility "
    "can track it with the delivery link you sent. If you book a different courier, paste its new "
    "link in Booking Status."
)
# How long after her appointment time a Drop off waits for its delivery link
# before the booking expires.
DROP_OFF_LINK_GRACE = timedelta(hours=2)
REMINDER_TITLE = "Time to send your milk"
REMINDER_BODY = (
    "It's time for your {time} drop off. Book your courier now, then open Booking Status "
    "and paste the delivery link so the facility can track your milk."
)


def _appointment_day(req):
    day = req.preferred_date
    return date.fromisoformat(day) if isinstance(day, str) else day


def slot_start(day, time_label):
    """When a slot ("10:00 AM" on `day`) begins, as an aware datetime (Manila), or None if it can't be read."""
    try:
        clock = datetime.strptime((time_label or "").strip(), "%I:%M %p").time()
        day = date.fromisoformat(day) if isinstance(day, str) else day
    except (TypeError, ValueError):
        return None
    if not isinstance(day, date):
        return None
    return datetime.combine(day, clock, tzinfo=BUSINESS_TZ)


def appointment_start(req):
    """When her appointment begins, as an aware datetime (Manila), or None if the time can't be read."""
    return slot_start(req.preferred_date, req.preferred_time)


def _collection_stage(req):
    stages = req.stages
    return stages.index("Breastmilk Collection") if "Breastmilk Collection" in stages else None


def delivery_link_due(req, now=None):
    """True from the appointment's hour onward, while her milk is not collected yet."""
    start = appointment_start(req)
    if start is None or req.collection_method != Method.DROP_OFF:
        return False
    stage = _collection_stage(req)
    if stage is not None and req.current_stage_index > stage:
        return False
    return (now or timezone.now()) >= start


def _when(req):
    day = _appointment_day(req)
    return {"date": f"{day:%B} {day.day}, {day.year}", "time": req.preferred_time}


def confirms_by_delivery_link(req):
    """A Drop off donor confirms her booking by pasting the courier's link, not by confirming attendance."""
    return req.request_type == MilkBankRequest.RequestType.DONOR and req.collection_method == Method.DROP_OFF


def drop_off_link_deadline(req, now=None):
    """When a Drop off still waiting for its link expires, or None if her appointment time can't be read.
    At least DROP_OFF_LINK_GRACE from now, so a late acceptance never expires on the spot."""
    start = appointment_start(req)
    if start is None:
        return None
    now = now or timezone.now()
    return max(start + DROP_OFF_LINK_GRACE, now + DROP_OFF_LINK_GRACE)


def notify_collection_steps(req):
    """
    The steps for the way she chose, with her date and time. Nothing when no
    method is set. Once her booking is confirmed (Scheduled) the method is
    fixed, so the steps then say to message the facility if something changes.
    """
    if req.request_type != MilkBankRequest.RequestType.DONOR or req.collection_method not in STEPS:
        return
    template = STEPS[req.collection_method]
    if req.current_sub_status == Status.SCHEDULED:
        template = STEPS_CONFIRMED.get(req.collection_method, template)
    if req.collection_method == Method.DROP_OFF and req.delivery_link:
        template = STEPS_DROP_OFF_LINKED
    notify(req.owner, STEPS_TITLE, template.format(**_when(req)), NotificationItem.Category.BOOKINGS)


def send_delivery_link_reminders(now=None):
    """
    Reminds each Drop off donor at her appointment hour to book the courier and
    paste its link -- once per appointment (a new date or time gets its own),
    and only on the appointment's day. Returns the requests reminded.
    """
    now = now or timezone.now()
    today = now.astimezone(BUSINESS_TZ).date()
    candidates = MilkBankRequest.objects.filter(
        request_type=MilkBankRequest.RequestType.DONOR,
        collection_method=Method.DROP_OFF,
        delivery_link="",
        current_sub_status__in=OPEN_STATUSES,
        preferred_date=today,
    ).select_related("owner")
    reminded = []
    for req in candidates:
        key = f"{_appointment_day(req).isoformat()} {req.preferred_time}"
        if req.delivery_reminder_for == key or not delivery_link_due(req, now):
            continue
        # Claimed before it is sent: every page load runs this sweep, and the
        # server answers several at once (gunicorn.conf.py), so two sweeps can
        # both find her unreminded. Only the one whose UPDATE marks her gets
        # to send; the other finds her already marked and sends nothing.
        claimed = (
            MilkBankRequest.objects.filter(pk=req.pk).exclude(delivery_reminder_for=key)
            .update(delivery_reminder_for=key)
        )
        if not claimed:
            continue
        req.delivery_reminder_for = key
        notify(req.owner, REMINDER_TITLE, REMINDER_BODY.format(time=req.preferred_time), NotificationItem.Category.BOOKINGS)
        reminded.append(req)
    return reminded
