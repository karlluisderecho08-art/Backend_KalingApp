"""
Closing days at a facility, and what happens to the bookings already on them.

A facility closes a day for donors, for recipients or for both
(Facility.unavailable_donor_dates / unavailable_recipient_dates). Its own
staff do it from the facility console's Closed Days page
(views.MyFacilityClosedDaysView); a platform admin can too, through the
facility itself (views.FacilityDetailView). Mothers' calendars grey the day
out, and nothing new can be booked on it (allocation.claim_slot).

Bookings already on a day being closed are not cancelled -- the mother did
nothing wrong, and a new date is the fix, not a refusal:

  * whoever closes the day is shown them first and has to confirm (the API
    answers 409 until `confirm_closing` is sent);
  * each mother is told that day is closed, and that the facility will
    propose a new date -- or that she can cancel;
  * the facility's staff are told which bookings need a new date, and the
    console marks each one ("Closed day") until they propose one. Staff
    can't accept such a booking as it stands, and she can't confirm it.

A platform admin switching a facility off ("not operational") only stops new
bookings there: bookings already in progress carry on, so they are counted
for the admin's confirmation and the staff are told, but no mother is.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from accounts.models import User
from core.audit import log_action
from notifications.models import NotificationItem
from notifications.services import notify, notify_many

from .business_hours import BUSINESS_TZ
from .models import MilkBankRequest
from .scheduling import appointment_still_ahead, day_label, held_day

Status = MilkBankRequest.Status
RequestType = MilkBankRequest.RequestType

# Not FINAL_STATUSES from transitions (which imports this module's neighbours).
OPEN_STATUSES = [Status.PENDING, Status.AWAITING_ATTENDANCE, Status.SCHEDULED, Status.COUNTER_OFFERED]

# The longest stretch closed in one go from the Closed Days page.
MAX_RANGE_DAYS = 92

DATE_FORMAT_ERROR = "Each closed day must be a date written as yyyy-mm-dd."

MOTHER_TITLE = "Facility closed on your appointment day"
MOTHER_BODY = (
    "{facility} will be closed on {date}, the day of your appointment. The facility will propose "
    "a new date in Booking Status, or you can cancel your booking there."
)


def today():
    return datetime.now(BUSINESS_TZ).date()


def normalise_dates(values):
    """
    A list of closed days as given (yyyy-mm-dd strings), checked and tidied:
    unique, sorted. Raises ValueError(DATE_FORMAT_ERROR) for anything else.
    """
    if values is None:
        return []
    if not isinstance(values, (list, tuple)):
        raise ValueError(DATE_FORMAT_ERROR)
    days = set()
    for value in values:
        if not isinstance(value, str):
            raise ValueError(DATE_FORMAT_ERROR)
        try:
            days.add(date.fromisoformat(value.strip()))
        except ValueError:
            raise ValueError(DATE_FORMAT_ERROR) from None
    return sorted(d.isoformat() for d in days)


def added_days(before, after, from_day=None):
    """Days in `after` but not `before`, from `from_day` (default today) on -- the ones being closed now."""
    from_day = (from_day or today()).isoformat()
    return {d for d in after if d not in set(before or []) and d >= from_day}


def bookings_on(facility, donor_days, recipient_days):
    """
    The open bookings at `facility` whose appointment is still ahead and falls
    on one of these days for its pathway (the offered day, for a counter-offer
    waiting on her answer).
    """
    if not donor_days and not recipient_days:
        return []
    found = []
    queryset = (
        MilkBankRequest.objects.filter(allocated_facility=facility, current_sub_status__in=OPEN_STATUSES)
        .select_related("owner", "allocated_facility")
        .order_by("preferred_date", "id")
    )
    for req in queryset:
        days = donor_days if req.request_type == RequestType.DONOR else recipient_days
        if held_day(req).isoformat() in days and appointment_still_ahead(req):
            found.append(req)
    return sorted(found, key=lambda r: (held_day(r), r.id))


def booking_summary(req):
    """One booking, as a closing-days confirmation lists it."""
    offered = req.current_sub_status == Status.COUNTER_OFFERED and req.counter_offer_date
    owner = req.owner
    return {
        "id": req.id,
        "date": held_day(req).isoformat(),
        "time": req.counter_offer_time if offered else req.preferred_time,
        "request_type": req.request_type,
        "status": req.current_sub_status,
        "mother": (owner.mom_name or owner.email) if owner else "",
    }


@dataclass
class ClosingPlan:
    """What saving a facility's new closed days (and operational switch) would do."""

    facility: object
    closed_donor_days: set = field(default_factory=set)
    closed_recipient_days: set = field(default_factory=set)
    affected: list = field(default_factory=list)
    switching_off: bool = False
    switching_on: bool = False
    open_bookings: int = 0

    @property
    def closed_days(self):
        return sorted(self.closed_donor_days | self.closed_recipient_days)

    @property
    def needs_confirmation(self):
        return bool(self.affected) or (self.switching_off and self.open_bookings > 0)

    def confirmation(self):
        """The 409 body asking to confirm, saying exactly what will happen."""
        parts = []
        if self.affected:
            count = len(self.affected)
            parts.append(
                f"{count} booking{'' if count == 1 else 's'} fall{'s' if count == 1 else ''} on the "
                f"day{'' if len(self.closed_days) == 1 else 's'} being closed. "
                f"{'Its mother' if count == 1 else 'Each mother'} will be told, and the facility "
                "will need to propose a new date."
            )
        if self.switching_off and self.open_bookings:
            count = self.open_bookings
            parts.append(
                f"{self.facility.name} has {count} booking{'' if count == 1 else 's'} in progress. "
                f"{'It stays' if count == 1 else 'They stay'} open: switching it off only stops new bookings."
            )
        return {
            "code": "confirm_closing",
            "detail": " ".join(parts) + " Save again to confirm.",
            "affected_bookings": [booking_summary(req) for req in self.affected],
            "closed_days": self.closed_days,
            "open_bookings_when_switched_off": self.open_bookings if self.switching_off else 0,
        }


def plan(facility, donor_dates=None, recipient_dates=None, is_operational=None):
    """
    What changing `facility` to these closed days (None: unchanged) and this
    operational switch (None: unchanged) would affect. Read with the
    facility row locked, so the days compared against are the saved ones.
    """
    result = ClosingPlan(facility=facility)
    if donor_dates is not None:
        result.closed_donor_days = added_days(facility.unavailable_donor_dates, donor_dates)
    if recipient_dates is not None:
        result.closed_recipient_days = added_days(facility.unavailable_recipient_dates, recipient_dates)
    result.affected = bookings_on(facility, result.closed_donor_days, result.closed_recipient_days)
    if is_operational is not None and is_operational != facility.is_operational:
        result.switching_off = not is_operational
        result.switching_on = is_operational
        if result.switching_off:
            result.open_bookings = facility.requests.filter(current_sub_status__in=OPEN_STATUSES).count()
    return result


def _staff(facility, actor):
    staff = User.objects.filter(facility=facility, role=User.Role.FACILITY_STAFF, is_active=True)
    return staff.exclude(pk=actor.pk) if actor is not None else staff


def carry_out(closing, actor, by_admin):
    """
    After the new closed days (and switch) are saved: tell each affected
    mother, tell the facility's staff, and record it in the audit log.
    """
    facility = closing.facility
    days = closing.closed_days
    if days:
        log_action(actor, "facility.days_closed", f"Facility:{facility.id} -- {', '.join(days)}"[:255])
    for req in closing.affected:
        if req.owner is None:
            continue
        notify(
            req.owner, MOTHER_TITLE,
            MOTHER_BODY.format(facility=facility.name, date=day_label(held_day(req))),
            NotificationItem.Category.BOOKINGS,
        )
        log_action(actor, "booking.day_closed", f"MilkBankRequest:{req.id}")

    if closing.affected:
        count = len(closing.affected)
        labels = ", ".join(day_label(d) for d in days)
        who = (
            f"KalingApp admin closed {facility.name} on {labels}. " if by_admin
            else f"{facility.name} was closed on {labels}. "
        )
        notify_many(
            _staff(facility, None if by_admin else actor),
            "Bookings on a closed day",
            who + f"{count} booking{'' if count == 1 else 's'} on "
            f"{'that day' if len(days) == 1 else 'those days'} need{'s' if count == 1 else ''} a new date. "
            "Propose one to each mother from Booking Requests.",
            NotificationItem.Category.BOOKINGS,
        )
    elif days and by_admin:
        labels = ", ".join(day_label(d) for d in days)
        notify_many(
            _staff(facility, None), "Closed days updated",
            f"KalingApp admin closed {facility.name} on {labels}. No bookings were on those days.",
            NotificationItem.Category.BOOKINGS,
        )

    if closing.switching_off:
        log_action(actor, "facility.switched_off", f"Facility:{facility.id} ({facility.name})"[:255])
        count = closing.open_bookings
        notify_many(
            _staff(facility, None), "Facility switched off",
            f"KalingApp admin switched {facility.name} to not operational: no new bookings will be made here "
            "until it is switched back on."
            + (f" Your {count} booking{'' if count == 1 else 's'} in progress stay{'s' if count == 1 else ''} open: "
               "finish them, or propose new dates." if count else ""),
            NotificationItem.Category.BOOKINGS,
        )
    elif closing.switching_on:
        log_action(actor, "facility.switched_on", f"Facility:{facility.id} ({facility.name})"[:255])
        notify_many(
            _staff(facility, None), "Facility switched on",
            f"KalingApp admin switched {facility.name} back on: mothers can be matched here again.",
            NotificationItem.Category.BOOKINGS,
        )


def set_days(facility, start, end, close_for):
    """
    The facility's closed days after setting every day from `start` to `end`
    (inclusive) to `close_for`: "both", "donors", "recipients" or "none"
    (open again). Weekends and holidays are skipped -- they are always closed.
    Days already past are dropped from the lists. Returns (donor, recipient).
    """
    from .scheduling import is_holiday

    donor = set(facility.unavailable_donor_dates or [])
    recipient = set(facility.unavailable_recipient_dates or [])
    day = start
    while day <= end:
        if day.weekday() < 5 and not is_holiday(day):
            iso = day.isoformat()
            (donor.add if close_for in ("both", "donors") else donor.discard)(iso)
            (recipient.add if close_for in ("both", "recipients") else recipient.discard)(iso)
        day += timedelta(days=1)
    first = today().isoformat()
    return sorted(d for d in donor if d >= first), sorted(d for d in recipient if d >= first)
