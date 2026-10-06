"""
Which appointment slots staff may offer a mother in a counter-offer.

These are the same rules the facility dashboard greys out in its "Propose
New Date" dialog (Facility_KalingApp/lib/scheduling.ts) and that the
mother's own scheduler applies when she picks a slot. The dashboard only
hides the options; this is what actually refuses them, so a stale tab or a
direct API call cannot offer a slot nobody could attend.

  - The time must be one of SLOTS (the hourly slots, 8 AM to 4 PM -- there
    is no 5 PM slot).
  - Past dates are out, and so are weekends (the facility works Mon-Fri).
  - Dates the request's facility has marked unavailable for her pathway
    (Facility.unavailable_donor_dates / unavailable_recipient_dates) are out.
  - On today's date, a slot that has already started is out.
  - The slot she already asked for is out -- offering it back is not a
    counter-offer.

"Today" and "now" are Manila time, the same clock the SLA uses
(business_hours.BUSINESS_TZ): a facility's day does not roll over at
08:00 Manila just because settings.TIME_ZONE is UTC.
"""

from datetime import datetime

from .business_hours import BUSINESS_TZ
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


def counter_offer_errors(req, offered_date, offered_time, now=None):
    """
    Why `offered_date` / `offered_time` cannot be offered on `req`, as a
    {field: message} dict -- empty when the slot is fine. Keyed by the
    serializer field names so the dashboard can show each next to its input.

    `now` is for tests; it defaults to the current moment.
    """
    errors = {}
    now = (now or datetime.now(BUSINESS_TZ)).astimezone(BUSINESS_TZ)
    today = now.date()

    hour = SLOT_HOURS.get(offered_time)
    if hour is None:
        errors["counter_offer_time"] = (
            "Choose one of the offered times: " + ", ".join(label for label, _ in SLOTS) + "."
        )

    # Date first: a past or closed day makes the time moot, and one message
    # per field is enough to act on.
    if offered_date < today:
        errors["counter_offer_date"] = "This date has already passed."
    elif offered_date.weekday() >= 5:
        errors["counter_offer_date"] = "The facility is closed on weekends."
    elif offered_date.isoformat() in _unavailable_dates(req):
        errors["counter_offer_date"] = "The facility is unavailable on this date."
    elif offered_date == today and now.hour >= SLOTS[-1][1]:
        # The last slot has started, so nothing is left to offer today.
        errors["counter_offer_date"] = "There are no times left today."

    if "counter_offer_time" not in errors and "counter_offer_date" not in errors:
        if offered_date == today and hour <= now.hour:
            # A slot starting exactly now counts as started.
            errors["counter_offer_time"] = "This time has already passed."
        elif offered_date == req.preferred_date and offered_time == req.preferred_time:
            errors["counter_offer_time"] = "This is the time she already asked for."

    return errors


def _unavailable_dates(req):
    facility = req.allocated_facility
    if req.request_type == MilkBankRequest.RequestType.DONOR:
        return facility.unavailable_donor_dates or []
    return facility.unavailable_recipient_dates or []
