"""
Business-hours arithmetic for the booking response SLA.

The app has always told mothers that a facility responds "within 8 hours
during working hours (Mon-Fri, 8 AM-5 PM)" and that an unconfirmed booking
is "automatically cancelled" -- but nothing enforced either claim. This is
the clock that makes them true.

Counted in Asia/Manila, not UTC: the facilities are Manila hospitals and
"working hours" means their local clock. Everything stored and returned is
still UTC-aware (settings.TIME_ZONE is UTC); the local zone is used only
for deciding which wall-clock hours count.
"""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# Where the facilities actually are. The Philippines has no daylight
# saving time, which is why the hour arithmetic below can safely use
# .replace(hour=...) on an aware datetime without a DST gap to fall into.
BUSINESS_TZ = ZoneInfo("Asia/Manila")

# Matches Facility.operating_hours' default ("8:00 AM - 5:00 PM (Mon-Fri)")
# and the wording shown in the mother app.
WORKDAY_START_HOUR = 8
WORKDAY_END_HOUR = 17

# The SLA itself: 8 *business* hours, so a request submitted late on a
# Friday is not silently killed over a weekend the facility never worked.
SLA_BUSINESS_HOURS = 8

# A deadline can never be more than this far out, so a bug in the holiday
# table (every day marked a holiday, say) surfaces as a loud error instead
# of an infinite loop inside a request.
_MAX_DAYS_SCANNED = 400


def _easter_sunday(year):
    """Anonymous Gregorian algorithm -- Maundy Thursday, Good Friday and
    Black Saturday are all defined relative to Easter, which moves yearly."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    lu = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * lu) // 451
    month, day = divmod(h + lu - 7 * m + 114, 31)
    return date(year, month, day + 1)


# Dates that are neither fixed nor computable -- Eid'l Fitr and Eid'l Adha
# follow the lunar calendar and are fixed by presidential proclamation only
# weeks ahead, as are one-off additions like an election day or a declared
# national day of mourning.
#
# THIS NEEDS A HUMAN ONCE A YEAR. Add the proclaimed dates here when
# Malacanang publishes the following year's holiday list; anything missing
# just means the clock keeps running on a day the facility was closed.
PROCLAIMED_HOLIDAYS = set()


def philippine_holidays(year):
    """
    Regular holidays and special non-working days for `year`.

    The special non-working days (Black Saturday, Ninoy Aquino Day, All
    Saints' Day, Immaculate Conception, Christmas Eve, New Year's Eve) are
    included because the question here is only "was the facility open?",
    and on those days it wasn't -- the pay-rule distinction between a
    regular holiday and a special one doesn't matter for an SLA clock.
    Which special days get proclaimed does shift slightly year to year, so
    this list is worth a glance each January.
    """
    easter = _easter_sunday(year)

    # Last Monday of August, per RA 9492.
    national_heroes_day = date(year, 8, 31)
    while national_heroes_day.weekday() != 0:  # 0 = Monday
        national_heroes_day -= timedelta(days=1)

    holidays = {
        date(year, 1, 1),        # New Year's Day
        easter - timedelta(days=3),   # Maundy Thursday
        easter - timedelta(days=2),   # Good Friday
        easter - timedelta(days=1),   # Black Saturday
        date(year, 4, 9),        # Araw ng Kagitingan
        date(year, 5, 1),        # Labor Day
        date(year, 6, 12),       # Independence Day
        national_heroes_day,
        date(year, 8, 21),       # Ninoy Aquino Day
        date(year, 11, 1),       # All Saints' Day
        date(year, 11, 30),      # Bonifacio Day
        date(year, 12, 8),       # Feast of the Immaculate Conception
        date(year, 12, 24),      # Christmas Eve
        date(year, 12, 25),      # Christmas Day
        date(year, 12, 30),      # Rizal Day
        date(year, 12, 31),      # Last Day of the Year
    }
    holidays |= {d for d in PROCLAIMED_HOLIDAYS if d.year == year}
    return holidays


def is_business_day(day):
    """True if `day` is a weekday the facility would have been open."""
    if day.weekday() >= 5:  # Saturday, Sunday
        return False
    return day not in philippine_holidays(day.year)


def _next_workday_start(cursor):
    """08:00 on the next day the facility is open, in Manila local time."""
    nxt = (cursor + timedelta(days=1)).replace(
        hour=WORKDAY_START_HOUR, minute=0, second=0, microsecond=0
    )
    scanned = 0
    while not is_business_day(nxt.date()):
        nxt += timedelta(days=1)
        scanned += 1
        if scanned > _MAX_DAYS_SCANNED:
            raise RuntimeError("No business day found -- check the holiday table")
    return nxt


def add_business_hours(start, hours=SLA_BUSINESS_HOURS):
    """
    `start` plus `hours` of working time, as a UTC-aware datetime.

    Time outside Mon-Fri 08:00-17:00 Manila doesn't count, so a request
    arriving at 4 PM on a Friday has one hour of its budget left that day
    and picks the rest up on Monday morning -- or Tuesday, if Monday is a
    holiday. A `start` outside working hours is treated as arriving at the
    next opening, so nothing is consumed while the facility is shut.
    """
    remaining = timedelta(hours=hours)
    cursor = start.astimezone(BUSINESS_TZ)

    scanned = 0
    while remaining > timedelta(0):
        scanned += 1
        if scanned > _MAX_DAYS_SCANNED:
            raise RuntimeError("Business-hours clock failed to terminate")

        if not is_business_day(cursor.date()):
            cursor = _next_workday_start(cursor)
            continue

        opens = cursor.replace(hour=WORKDAY_START_HOUR, minute=0, second=0, microsecond=0)
        closes = cursor.replace(hour=WORKDAY_END_HOUR, minute=0, second=0, microsecond=0)

        if cursor < opens:
            cursor = opens
        if cursor >= closes:
            cursor = _next_workday_start(cursor)
            continue

        available = closes - cursor
        if remaining <= available:
            return (cursor + remaining).astimezone(timezone.utc)

        remaining -= available
        cursor = _next_workday_start(cursor)

    return cursor.astimezone(timezone.utc)
