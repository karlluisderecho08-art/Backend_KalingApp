"""
Breastmilk Collection for donors: when the delivery link is due, and the
notifications that walk her through the way she chose.

She picks how her milk reaches the facility right after choosing her
appointment date and time (the app no longer asks on the health form). A Drop
off goes by courier, and a courier can only be booked when the milk is ready
to leave -- so the delivery link is asked for, and required, only from the
appointment's own hour onward ("the same day and the same hour"), until her
milk is collected.

Notifications (all Bookings, all with a Tagalog line in core/tagalog_catalog.py):
  * when her appointment is confirmed (Scheduled), and whenever she changes the
    method, the steps for that method, with her date and time;
  * at the appointment hour, for a Drop off with no link yet, a reminder to
    book the courier and paste its link. Sent once per appointment, from the
    same sweep that expires overdue requests (transitions.sweep_expired_requests).
"""

from datetime import date, datetime

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
STEPS = {
    Method.PICKUP: (
        "Pick up: the facility will collect your milk from you on {date} at {time}. "
        "Have your milk ready by then. You can change this in Booking Status until your milk is collected."
    ),
    Method.WALK_IN: (
        "Walk-in: bring your milk to the facility yourself on {date} at {time}. "
        "Keep it cold on the way, in a cooler or insulated bag. "
        "You can change this in Booking Status until your milk is collected."
    ),
    Method.DROP_OFF: (
        "Drop off: on {date}, at {time}, book a courier (for example Lalamove or Grab) to bring your milk "
        "to the facility. Then open Booking Status and paste the courier's delivery link so the facility "
        "can track it. You can change this in Booking Status until your milk is collected."
    ),
}
REMINDER_TITLE = "Time to send your milk"
REMINDER_BODY = (
    "It's time for your {time} drop off. Book your courier now, then open Booking Status "
    "and paste the delivery link so the facility can track your milk."
)


def _appointment_day(req):
    day = req.preferred_date
    return date.fromisoformat(day) if isinstance(day, str) else day


def appointment_start(req):
    """When her appointment begins, as an aware datetime (Manila), or None if the time can't be read."""
    try:
        clock = datetime.strptime((req.preferred_time or "").strip(), "%I:%M %p").time()
        day = _appointment_day(req)
    except (TypeError, ValueError):
        return None
    if day is None:
        return None
    return datetime.combine(day, clock, tzinfo=BUSINESS_TZ)


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


def notify_collection_steps(req):
    """The steps for the way she chose, with her date and time. Nothing when no method is set."""
    if req.request_type != MilkBankRequest.RequestType.DONOR or req.collection_method not in STEPS:
        return
    notify(req.owner, STEPS_TITLE, STEPS[req.collection_method].format(**_when(req)), NotificationItem.Category.BOOKINGS)


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
        notify(req.owner, REMINDER_TITLE, REMINDER_BODY.format(time=req.preferred_time), NotificationItem.Category.BOOKINGS)
        req.delivery_reminder_for = key
        req.save(update_fields=["delivery_reminder_for"])
        reminded.append(req)
    return reminded
