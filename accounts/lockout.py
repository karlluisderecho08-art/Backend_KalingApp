"""
Sign-in lockout: repeated wrong passwords lock the account for longer and
longer, so a guessed password can't simply be retried forever.

The rule (kept here, in one place, so the app's wording and the tests agree):

  * The 5th wrong password in a row locks the account for 1 minute.
  * Every further wrong password after that doubles the lock:
    2, 4, 8, 16 ... minutes, up to a ceiling of 24 hours.
  * While locked, even the CORRECT password is refused -- otherwise a
    guesser would learn the moment they hit it.
  * A correct sign-in, a password reset (she has proved she owns the
    email), or an admin pressing Unlock clears everything.
  * Wrong passwords more than 24 hours apart don't add up: a count left
    behind by a slip last month must not lock her out on today's first typo.
"""

from datetime import timedelta

from django.utils import timezone

LOCK_THRESHOLD = 5
BASE_LOCK = timedelta(minutes=1)
MAX_LOCK = timedelta(hours=24)
ATTEMPT_MEMORY = timedelta(hours=24)


def lock_duration(failed_attempts):
    """How long the account stays locked after this many wrong passwords in a row."""
    if failed_attempts < LOCK_THRESHOLD:
        return timedelta(0)
    # Capped before shifting so a huge counter can't build an enormous number.
    doublings = min(failed_attempts - LOCK_THRESHOLD, 20)
    return min(BASE_LOCK * (2 ** doublings), MAX_LOCK)


def is_locked(user, now=None):
    now = now or timezone.now()
    return bool(user.locked_until and user.locked_until > now)


def seconds_remaining(user, now=None):
    now = now or timezone.now()
    if not is_locked(user, now):
        return 0
    # Rounded up, so "0 seconds left" never shows while still locked.
    return int((user.locked_until - now).total_seconds()) + 1


def register_failure(user, now=None):
    """
    Record one wrong password. Returns (attempts, newly_locked): `newly_locked`
    is true only for the attempt that FIRST locks the account in this run of
    failures, which is the one admins are told about.
    """
    now = now or timezone.now()
    if user.last_failed_login_at and now - user.last_failed_login_at > ATTEMPT_MEMORY:
        user.failed_login_attempts = 0

    user.failed_login_attempts += 1
    user.last_failed_login_at = now
    duration = lock_duration(user.failed_login_attempts)
    user.locked_until = now + duration if duration else None
    user.save(update_fields=["failed_login_attempts", "last_failed_login_at", "locked_until"])
    return user.failed_login_attempts, user.failed_login_attempts == LOCK_THRESHOLD


def clear(user):
    """Forget every failed attempt and lift any lock. Cheap no-op for a clean account."""
    if user.failed_login_attempts or user.locked_until or user.last_failed_login_at:
        user.failed_login_attempts = 0
        user.last_failed_login_at = None
        user.locked_until = None
        user.save(update_fields=["failed_login_attempts", "last_failed_login_at", "locked_until"])
