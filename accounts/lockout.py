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
  * Password checks for one account happen one at a time
    (one_attempt_at_a_time), so guesses sent all at once are counted, and
    locked out, exactly as if they had been sent one after another.
"""

import threading
from contextlib import contextmanager
from datetime import timedelta

from django.utils import timezone

LOCK_THRESHOLD = 5
BASE_LOCK = timedelta(minutes=1)
MAX_LOCK = timedelta(hours=24)
ATTEMPT_MEMORY = timedelta(hours=24)

LOCKOUT_FIELDS = ["failed_login_attempts", "last_failed_login_at", "locked_until"]

# The server answers several requests at once (gunicorn.conf.py). Without
# this, ten guesses at one account sent together would all pass "is it
# locked?" before any of them had been counted, and all ten passwords would be
# checked -- where one after another, the sixth onwards is refused unchecked.
# They would also each add one to the same stale count, and most of them would
# vanish. Holding a lock per account from "is it locked?" to recording the
# outcome restores one-after-another for that account, while different
# accounts still sign in side by side.
#
# Striped over a fixed set of locks so memory stays bounded; two accounts that
# share a stripe merely take turns. Covers one process only -- see
# gunicorn.conf.py.
_ACCOUNT_LOCKS = [threading.Lock() for _ in range(64)]


@contextmanager
def one_attempt_at_a_time(user):
    """
    Hold while checking a password for `user`: from the lock check to
    register_failure() or clear(). Re-reads her lockout state once it's her
    turn -- the copy loaded before waiting may be several attempts out of date.
    """
    with _ACCOUNT_LOCKS[user.pk % len(_ACCOUNT_LOCKS)]:
        user.refresh_from_db(fields=LOCKOUT_FIELDS)
        yield


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
    user.save(update_fields=LOCKOUT_FIELDS)
    return user.failed_login_attempts, user.failed_login_attempts == LOCK_THRESHOLD


def clear(user):
    """Forget every failed attempt and lift any lock. Cheap no-op for a clean account."""
    if user.failed_login_attempts or user.locked_until or user.last_failed_login_at:
        user.failed_login_attempts = 0
        user.last_failed_login_at = None
        user.locked_until = None
        user.save(update_fields=LOCKOUT_FIELDS)
