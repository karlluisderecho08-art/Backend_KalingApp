"""
Data retention: deletes personal data once it has been kept as long as the
policy allows (RA 10173). The periods are settings -- see the "Data
retention" block in config/settings/base.py.

There is no background worker on this host, so purge_expired_data() runs:
  - on every deploy (`manage.py purge_expired_data` in build.sh),
  - whenever the external expiry pinger calls /milkbank/sweep-expired/,
  - and from ordinary traffic through maybe_purge(), at most once an hour.
Each run is idempotent: it only touches what is already past its period, so
running it often costs a few cheap queries and changes nothing extra.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)

# One shared marker in the cache: whoever adds it first in an hour runs the
# purge, every other request that hour skips it.
_RUN_MARKER = "core.retention.last_run"
_RUN_EVERY_SECONDS = 60 * 60


def purge_expired_data(now=None):
    """Deletes everything past its retention period. Returns the counts."""
    from accounts.models import PendingRegistration
    from chat.models import ChatMessage
    from milkbank.models import MilkBankRequest

    from .audit import log_action
    from .models import AuditLogEntry

    now = now or timezone.now()
    counts = {}

    # Sign-ups never verified. Counted from the later of when it was made and
    # when its last code went out, so someone still resending codes isn't cut
    # off mid-way.
    signup_cutoff = now - timedelta(hours=settings.RETENTION_PENDING_SIGNUP_HOURS)
    counts["pending_signups"] = PendingRegistration.objects.filter(
        created_at__lt=signup_cutoff,
    ).filter(Q(sent_at__isnull=True) | Q(sent_at__lt=signup_cutoff)).delete()[0]

    # A pickup representative's details, once the request has been closed for
    # the retention period. The request itself stays (it is her booking
    # history and the facility's record); only the third person's data goes.
    rep_cutoff = now - timedelta(days=settings.RETENTION_REPRESENTATIVE_DAYS)
    counts["representative_details"] = (
        MilkBankRequest.objects.filter(closed_at__isnull=False, closed_at__lt=rep_cutoff)
        .filter(
            Q(representative_purged_at__isnull=True)
            & (~Q(representative_name="") | ~Q(representative_contact_number="") | Q(representative_birthday__isnull=False))
        )
        .update(
            representative_name="",
            representative_birthday=None,
            representative_contact_number="",
            representative_purged_at=now,
        )
    )

    chat_cutoff = now - timedelta(days=settings.RETENTION_CHAT_DAYS)
    counts["chat_messages"] = ChatMessage.objects.filter(created_at__lt=chat_cutoff).delete()[0]

    audit_cutoff = now - timedelta(days=settings.RETENTION_AUDIT_DAYS)
    counts["audit_entries"] = AuditLogEntry.objects.filter(created_at__lt=audit_cutoff).delete()[0]

    if any(counts.values()):
        # Recorded after the audit purge, so this entry is never its own victim.
        log_action(None, "retention.purged", ", ".join(f"{k}={v}" for k, v in counts.items())[:255])
    cache.set(_RUN_MARKER, now.isoformat(), timeout=None)
    return counts


def last_run():
    """ISO timestamp of the last completed purge, or None if none is recorded."""
    return cache.get(_RUN_MARKER)


def maybe_purge():
    """
    Runs the purge if it hasn't run in the last hour. Called from ordinary
    requests, so it must never break one: any failure is logged and swallowed.
    """
    try:
        if cache.add(f"{_RUN_MARKER}.lock", "1", timeout=_RUN_EVERY_SECONDS):
            purge_expired_data()
    except Exception:  # noqa: BLE001 -- retention must never take a request down with it
        logger.exception("Data-retention purge failed")
