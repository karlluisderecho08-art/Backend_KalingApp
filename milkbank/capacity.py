"""
Per-day booking capacity.

Facility.capacity is how many bookings the facility takes ON ONE DAY. A
booking takes a slot on the day it is for -- its preferred_date -- not on
the day it was submitted, and every day has its own count: a mother booking
for Oct 12 uses one of Oct 12's slots and nothing else.

The count is not stored anywhere. It is the number of requests at that
facility for that day that still hold their slot, read from the
MilkBankRequest rows themselves, so it cannot drift from the bookings that
actually exist. (It used to be a single Facility.booked_count that went up
on every submission whatever the date and down when a request closed --
one running total for all days at once, kept in step by hand.)

Which requests hold their day's slot:
  - pending, awaiting attendance, scheduled, counter offered: yes. A
    pending request reserves its day from the moment it is submitted, so
    a day can be shown as full before anyone approves anything, and two
    mothers can never both be given the last slot.
  - completed: yes. The appointment happened on that day and used that
    day's slot; finishing it does not give the day a spare one.
  - declined, expired: no. The slot goes back the moment either happens.

A counter-offered request keeps holding its ORIGINAL day until she accepts
the new one; accepting moves it (and is refused if the new day has filled
up in the meantime -- see AcceptCounterOfferView).

Per-time-slot limit for donors (optional, per facility):

When Facility.limit_donor_slots is on, each appointment time (preferred_time,
one of scheduling.SLOTS) on a day takes at most donor_bookings_per_slot DONOR
bookings -- the lab can only run so many donor screenings at once. It sits
alongside the daily capacity, it does not replace it: a donor booking needs
room in BOTH its day and its hour. The same requests hold an hour as hold a
day (everything but declined / expired), counting donors only.
"""

from datetime import date, datetime, timedelta

from django.db.models import Count

from .business_hours import BUSINESS_TZ
from .models import MilkBankRequest

Status = MilkBankRequest.Status

# The only statuses that give their day's slot back.
RELEASED_STATUSES = (Status.DECLINED, Status.EXPIRED)

# How far ahead fully_booked_dates() looks -- comfortably past anything the
# mother's scheduler lets her pick.
BOOKING_HORIZON_DAYS = 90


def today() -> date:
    """Today in Manila, the facility's own calendar (see business_hours)."""
    return datetime.now(BUSINESS_TZ).date()


def holding_requests():
    """Requests that currently occupy a slot on their preferred_date."""
    return MilkBankRequest.objects.exclude(current_sub_status__in=RELEASED_STATUSES)


def slots_taken(facility_id, day, exclude_request_id=None) -> int:
    """How many of `day`'s slots at the facility are taken."""
    queryset = holding_requests().filter(allocated_facility_id=facility_id, preferred_date=day)
    if exclude_request_id is not None:
        queryset = queryset.exclude(pk=exclude_request_id)
    return queryset.count()


def has_room(facility, day, exclude_request_id=None) -> bool:
    """
    Whether the facility can take one more booking on `day`.

    `exclude_request_id` is for moving an existing request to `day`: it must
    not be counted against itself.
    """
    if facility.capacity <= 0:
        return False
    return slots_taken(facility.pk, day, exclude_request_id) < facility.capacity


def donor_slot_taken(facility_id, day, time, exclude_request_id=None) -> int:
    """How many DONOR bookings hold the `time` slot on `day` at the facility."""
    queryset = holding_requests().filter(
        allocated_facility_id=facility_id, preferred_date=day, preferred_time=time,
        request_type=MilkBankRequest.RequestType.DONOR,
    )
    if exclude_request_id is not None:
        queryset = queryset.exclude(pk=exclude_request_id)
    return queryset.count()


def has_donor_slot_room(facility, day, time, exclude_request_id=None) -> bool:
    """
    Whether one more DONOR booking fits in the `time` slot on `day`. Always
    true when the facility doesn't limit donor slots. Checks the hour only --
    the day's own capacity is has_room()'s job, and a booking needs both.
    """
    if not facility.limit_donor_slots:
        return True
    return donor_slot_taken(facility.pk, day, time, exclude_request_id) < facility.donor_bookings_per_slot


def full_donor_slots(facility, start: date, end: date) -> dict:
    """{"yyyy-mm-dd": [times with no donor room left]} for start <= day < end."""
    if not facility.limit_donor_slots:
        return {}
    rows = (
        holding_requests()
        .filter(
            allocated_facility_id=facility.pk, preferred_date__gte=start, preferred_date__lt=end,
            request_type=MilkBankRequest.RequestType.DONOR,
        )
        .values("preferred_date", "preferred_time")
        .annotate(n=Count("id"))
        .filter(n__gte=facility.donor_bookings_per_slot)
    )
    full = {}
    for row in rows:
        full.setdefault(row["preferred_date"].isoformat(), []).append(row["preferred_time"])
    return {day: sorted(times, key=_slot_order) for day, times in sorted(full.items())}


def _slot_order(time):
    from .scheduling import SLOT_HOURS  # scheduling imports this module
    return SLOT_HOURS.get(time, 99)


def counts_by_day(facility_ids, start: date, end: date) -> dict:
    """{(facility_id, day): slots taken} for start <= day < end, one query."""
    rows = (
        holding_requests()
        .filter(allocated_facility_id__in=list(facility_ids), preferred_date__gte=start, preferred_date__lt=end)
        .values("allocated_facility_id", "preferred_date")
        .annotate(n=Count("id"))
    )
    return {(row["allocated_facility_id"], row["preferred_date"]): row["n"] for row in rows}


def day_summary(facility, start=None, days=BOOKING_HORIZON_DAYS) -> dict:
    """
    Everything the facility payload shows about its calendar, from one query:

      bookings_today      slots taken today
      upcoming_bookings   [{date, count}] for the next 10 weekdays from today
      fully_booked_dates  every day in the horizon with no slot left
    """
    start = start or today()
    counts = counts_by_day([facility.pk], start, start + timedelta(days=days))
    per_day = {day: n for (_facility_id, day), n in counts.items()}

    upcoming = []
    day = start
    while len(upcoming) < 10:
        if day.weekday() < 5:
            upcoming.append({"date": day.isoformat(), "count": per_day.get(day, 0)})
        day += timedelta(days=1)

    if facility.capacity <= 0:
        full = []  # takes no bookings at all; "full" would be misleading, it is closed
    else:
        full = sorted(d.isoformat() for d, n in per_day.items() if n >= facility.capacity)

    # Donor hours that are full, and the days where every hour is -- a donor
    # can't book those days at all, even while the day itself has room left
    # for recipients.
    from .scheduling import SLOTS  # scheduling imports this module

    donor_full_slots = full_donor_slots(facility, start, start + timedelta(days=days))
    every_slot = {label for label, _hour in SLOTS}
    donor_full_days = sorted(day for day, times in donor_full_slots.items() if every_slot <= set(times))

    return {
        "bookings_today": per_day.get(start, 0),
        "upcoming_bookings": upcoming,
        "fully_booked_dates": full,
        "full_donor_slots": donor_full_slots,
        "donor_fully_booked_dates": donor_full_days,
    }
