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

    return {
        "bookings_today": per_day.get(start, 0),
        "upcoming_bookings": upcoming,
        "fully_booked_dates": full,
    }
