"""
Which appointment slots staff may offer a mother in a counter-offer.

These are the same rules the facility dashboard greys out in its "Propose
New Date" dialog (Facility_KalingApp/lib/scheduling.ts) and that the
mother's own scheduler applies when she picks a slot. The dashboard only
hides the options; this is what actually refuses them, so a stale tab or a
direct API call cannot offer a slot nobody could attend.

  - The time must be one of SLOTS (the hourly slots, 8 AM to 4 PM -- there
    is no 5 PM slot).
  - Past dates are out, and so are weekends (the facility works Mon-Fri) and
    Philippine holidays (business_hours.philippine_holidays -- the same days
    the response clock skips).
  - Dates the request's facility has marked unavailable for her pathway
    (Facility.unavailable_donor_dates / unavailable_recipient_dates) are out.
  - On today's date, a slot that has already started is out.
  - The slot she already asked for is out -- offering it back is not a
    counter-offer.
  - A day with no booking slot left at her facility is out (capacity.py).
  - For a DONOR at a facility that limits donor bookings per time slot, a
    time already at that limit is out (capacity.has_donor_slot_room).

"Today" and "now" are Manila time, the same clock the SLA uses
(business_hours.BUSINESS_TZ): a facility's day does not roll over at
08:00 Manila just because settings.TIME_ZONE is UTC.
"""

from datetime import date, datetime, timedelta

from django.utils import timezone

from .business_hours import BUSINESS_TZ, philippine_holidays
from .capacity import has_donor_slot_room, has_room
from .models import MilkBankRequest

# What the mother's scheduler offers, in this order. Labels, not parsed
# times, because that is exactly what is stored in preferred_time and
# counter_offer_time and shown back to her.
SLOTS = (
    ("8:00 AM", 8), ("9:00 AM", 9), ("10:00 AM", 10),
    ("11:00 AM", 11), ("12:00 PM", 12), ("1:00 PM", 13),
    ("2:00 PM", 14), ("3:00 PM", 15), ("4:00 PM", 16),
)
SLOT_HOURS = dict(SLOTS)

# How far ahead holiday_dates() lists holidays for the calendars: a little
# over a year, so any date a mother or staff member is likely to pick is
# greyed out ahead of time. A date beyond it is still refused when booked.
HOLIDAY_LOOKAHEAD_DAYS = 400

# Why a date can't be booked, in the words she reads. All have a Tagalog line
# in core/tagalog_catalog.py.
INVALID_TIME = "Choose one of the offered times: " + ", ".join(label for label, _ in SLOTS) + "."
WEEKEND_CLOSED = "The milk banks are closed on weekends. Please choose a weekday."
HOLIDAY_CLOSED = "{date} is a holiday, and the milk banks are closed. Please choose another date."
FACILITY_CLOSED = "{facility} is closed on {date}. Please choose another date."
# Every facility that could take her has closed the day (allocation.FacilityClosedOnDay).
NO_FACILITY_OPEN = "No milk bank is open on {date}. Please choose another date."
# A mother picking a time that has already begun (slot_has_started).
PAST_SLOT = "This time has already passed. Please choose a later time."


def _as_date(day):
    return date.fromisoformat(day) if isinstance(day, str) else day


def day_label(day):
    """'December 25, 2026' -- how a date is named in messages."""
    day = _as_date(day)
    return f"{day:%B} {day.day}, {day.year}"


def is_holiday(day):
    """A Philippine holiday (business_hours.philippine_holidays): the facilities are closed."""
    day = _as_date(day)
    return day in philippine_holidays(day.year)


def holiday_dates(start=None, days=HOLIDAY_LOOKAHEAD_DAYS):
    """The holidays from `start` (default: today, Manila) for `days` days, as sorted yyyy-mm-dd."""
    start = start or datetime.now(BUSINESS_TZ).date()
    end = start + timedelta(days=days)
    found = set()
    for year in range(start.year, end.year + 1):
        found |= {d for d in philippine_holidays(year) if start <= d < end}
    return sorted(d.isoformat() for d in found)


def unavailable_dates(facility, request_type):
    """The days `facility` has closed for this pathway (DONOR or RECIPIENT), as yyyy-mm-dd strings."""
    if request_type == MilkBankRequest.RequestType.DONOR:
        return facility.unavailable_donor_dates or []
    return facility.unavailable_recipient_dates or []


def closed_reason(day, facility=None, request_type=None):
    """
    Why nobody can be booked on `day` -- a weekend, a holiday, or (given a
    facility) a day that facility closed for this pathway -- as the message
    she reads, or None when the day is open.
    """
    day = _as_date(day)
    if day.weekday() >= 5:
        return WEEKEND_CLOSED
    if is_holiday(day):
        return HOLIDAY_CLOSED.format(date=day_label(day))
    if facility is not None and day.isoformat() in unavailable_dates(facility, request_type):
        return FACILITY_CLOSED.format(facility=facility.name, date=day_label(day))
    return None


def booking_slot_problem(day, time_label, facility=None, request_type=None, now=None):
    """
    Why a mother can't book `time_label` on `day`, as (field, message, code),
    or None. What every booking a mother makes is checked against -- a new
    one, or the new time she picks after a counter-offer: a real slot time,
    not already begun, and a day the milk banks (and, given one, her facility)
    are open. Her app greys all of these out; this is what refuses them.
    """
    if time_label not in SLOT_HOURS:
        return "preferred_time", INVALID_TIME, "invalid_time"
    if slot_has_started(day, time_label, now):
        return "preferred_time", PAST_SLOT, "slot_passed"
    reason = closed_reason(day, facility, request_type)
    if reason:
        return "preferred_date", reason, "day_closed"
    return None


def held_day(req):
    """
    The day a booking holds at its facility: the offered one while a
    counter-offer waits for her answer, otherwise the one she booked
    (capacity.holding_requests counts it the same way).
    """
    if req.current_sub_status == MilkBankRequest.Status.COUNTER_OFFERED and req.counter_offer_date:
        return _as_date(req.counter_offer_date)
    return _as_date(req.preferred_date)


def held_time(req):
    """The time that goes with held_day()."""
    if req.current_sub_status == MilkBankRequest.Status.COUNTER_OFFERED and req.counter_offer_date:
        return req.counter_offer_time
    return req.preferred_time


def appointment_still_ahead(req):
    """
    Whether her appointment hasn't taken place yet, so its date can still be
    moved (or a day closed under it): any booking still waiting on the desk or
    on her, or a confirmed one on its appointment step -- a donor's first step
    after confirming (Counseling and Testing), or a recipient's document
    review or pickup.
    """
    status = req.current_sub_status
    if status in (MilkBankRequest.Status.PENDING, MilkBankRequest.Status.AWAITING_ATTENDANCE,
                  MilkBankRequest.Status.COUNTER_OFFERED):
        return True
    if status != MilkBankRequest.Status.SCHEDULED:
        return False
    stages = req.stages
    if req.request_type == MilkBankRequest.RequestType.RECIPIENT:
        return 0 <= req.current_stage_index < len(stages) and stages[req.current_stage_index] in ("Status", "Results")
    return req.current_stage_index == stages.index("Booking Confirmation") + 1


def can_mother_cancel(req):
    """
    Whether she can cancel this booking herself: any time it is still open --
    except, as a donor, once her milk has been collected (the Results step):
    by then the facility has it.
    """
    status = req.current_sub_status
    open_statuses = (
        MilkBankRequest.Status.PENDING, MilkBankRequest.Status.AWAITING_ATTENDANCE,
        MilkBankRequest.Status.SCHEDULED, MilkBankRequest.Status.COUNTER_OFFERED,
    )
    if status not in open_statuses:
        return False
    if req.request_type == MilkBankRequest.RequestType.DONOR:
        stages = req.stages
        return not (0 <= req.current_stage_index < len(stages) and stages[req.current_stage_index] == "Results")
    return True


def appointment_day_closed(req):
    """Whether the day her booking holds is now closed at her facility (or a weekend/holiday)."""
    day = held_day(req)
    if day is None:
        return False
    return closed_reason(day, req.allocated_facility, req.request_type) is not None


def slot_has_started(day, time_label, now=None):
    """
    Whether an appointment at `time_label` ("10:00 AM") on `day` has begun,
    on the facility's clock (Manila). A slot starting exactly now counts as
    started, as in counter_offer_errors.

    What it guards: nothing may be booked, accepted or agreed to for a time
    that is already gone -- a request still waiting for the facility when its
    hour passes is no longer acceptable as it stands (staff propose a new date
    or decline it instead), and a mother can't pick or agree to a past slot.

    False when the date or time can't be read: nothing is refused on a guess.
    """
    try:
        day = date.fromisoformat(day) if isinstance(day, str) else day
        clock = datetime.strptime((time_label or "").strip(), "%I:%M %p").time()
    except (TypeError, ValueError):
        return False
    if not isinstance(day, date):
        return False
    now = (now or timezone.now()).astimezone(BUSINESS_TZ)
    return datetime.combine(day, clock, tzinfo=BUSINESS_TZ) <= now


def appointment_label(day, time_label):
    """'November 17, 2026 at 12:00 PM' -- how a slot is named in messages."""
    return f"{day_label(day)} at {time_label}"


def clock_label(moment):
    """('November 20, 2026', '3:00 PM') -- a moment on the Manila clock, as messages name it."""
    local = moment.astimezone(BUSINESS_TZ)
    hour = local.hour % 12 or 12
    return day_label(local.date()), f"{hour}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"


def counter_offer_errors(req, offered_date, offered_time, now=None):
    """
    Why `offered_date` / `offered_time` cannot be offered on `req`, as a
    {field: message} dict -- empty when the slot is fine. Keyed by the
    serializer field names so the dashboard can show each next to its input.

    `now` is for tests; it defaults to the current moment.
    """
    errors = {}
    now = (now or timezone.now()).astimezone(BUSINESS_TZ)
    today = now.date()

    hour = SLOT_HOURS.get(offered_time)
    if hour is None:
        errors["counter_offer_time"] = INVALID_TIME

    # Date first: a past or closed day makes the time moot, and one message
    # per field is enough to act on.
    if offered_date < today:
        errors["counter_offer_date"] = "This date has already passed."
    elif offered_date.weekday() >= 5:
        errors["counter_offer_date"] = "The facility is closed on weekends."
    elif is_holiday(offered_date):
        errors["counter_offer_date"] = "The facility is closed on this holiday."
    elif offered_date.isoformat() in unavailable_dates(req.allocated_facility, req.request_type):
        errors["counter_offer_date"] = "The facility is unavailable on this date."
    elif offered_date == today and now.hour >= SLOTS[-1][1]:
        # The last slot has started, so nothing is left to offer today.
        errors["counter_offer_date"] = "There are no times left today."
    elif not has_room(req.allocated_facility, offered_date, exclude_request_id=req.pk):
        errors["counter_offer_date"] = "This date is fully booked."

    if "counter_offer_time" not in errors and "counter_offer_date" not in errors:
        if offered_date == today and hour <= now.hour:
            # A slot starting exactly now counts as started.
            errors["counter_offer_time"] = "This time has already passed."
        elif offered_date == req.preferred_date and offered_time == req.preferred_time:
            errors["counter_offer_time"] = "This is the time she already asked for."
        elif (
            req.current_sub_status == MilkBankRequest.Status.COUNTER_OFFERED
            and offered_date == req.counter_offer_date and offered_time == req.counter_offer_time
        ):
            errors["counter_offer_time"] = "This is the time already offered to her."
        elif req.request_type == MilkBankRequest.RequestType.DONOR and not has_donor_slot_room(
            req.allocated_facility, offered_date, offered_time, exclude_request_id=req.pk,
        ):
            errors["counter_offer_time"] = "This time is fully booked for donors."

    return errors
