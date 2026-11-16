"""
Removing a facility from KalingApp, and bringing it back.

A facility with no bookings is simply deleted (FacilityDetailView). One with
bookings on record can't be: every booking keeps pointing at the facility it
happened at (MilkBankRequest.allocated_facility is PROTECT), because they are
mothers' history -- what she donated or received, and where. Removing it
instead:

  * takes it out of KalingApp: every facility list, the mother app's
    matching (allocation.eligibility_filters), and the admin's own list
    (it shows under "Removed facilities" there instead);
  * switches it to not operational;
  * cancels every booking still in progress there -- each mother is told
    plainly, and can book again with another facility (results.py,
    FACILITY_REMOVED_REASON);
  * switches off its facility staff logins (they can't sign in, and any
    session they have stops working);
  * keeps everything else: finished bookings, transactions, the audit log.

Restoring undoes the removal and switches its staff logins back on. It stays
not operational until an admin switches that on again -- cancelled bookings
are not brought back.
"""

from django.db import transaction
from django.utils import timezone

from accounts.models import User
from core.audit import log_action

from .models import Facility, MilkBankRequest
from .results import FACILITY_REMOVED_REASON, decline_notification
from .transitions import FINAL_STATUSES, apply_transition

Status = MilkBankRequest.Status
OPEN_STATUSES = [status for status in Status.values if status not in FINAL_STATUSES]


def _staff(facility):
    """Its facility staff logins -- never an admin account, which only happens to share the role."""
    return User.objects.filter(
        facility=facility, role=User.Role.FACILITY_STAFF, is_staff=False, is_superuser=False,
    )


def removal_summary(facility):
    """What removing `facility` would do -- for the admin portal's confirmation."""
    return {
        "bookings_on_record": facility.requests.count(),
        "open_bookings": facility.requests.filter(current_sub_status__in=OPEN_STATUSES).count(),
        "staff_logins": _staff(facility).filter(is_active=True).count(),
        "removed": facility.removed_at is not None,
    }


def remove_facility(facility, actor):
    """
    Removes `facility` from KalingApp (see the module docstring). Returns
    {"cancelled_bookings", "staff_logins_switched_off"}. Removing an already
    removed facility does nothing more.
    """
    with transaction.atomic():
        # Locked so a booking can't be made at it while it is being removed:
        # booking takes the same row lock (allocation.claim_slot).
        facility = Facility.objects.select_for_update().get(pk=facility.pk)
        if facility.removed_at is not None:
            return {"cancelled_bookings": 0, "staff_logins_switched_off": 0}
        facility.removed_at = timezone.now()
        facility.is_operational = False
        facility.save(update_fields=["removed_at", "is_operational"])

        notice = decline_notification(FACILITY_REMOVED_REASON, "")
        cancelled = 0
        for req in MilkBankRequest.objects.filter(allocated_facility=facility, current_sub_status__in=OPEN_STATUSES):
            apply_transition(
                req, Status.DECLINED, actor, "declined_facility_removed",
                message_override=notice, facility_removed=True,
            )
            req.decline_reason = FACILITY_REMOVED_REASON
            req.save(update_fields=["decline_reason"])
            cancelled += 1

        switched_off = _staff(facility).filter(is_active=True).update(is_active=False)

        log_action(
            actor, "facility.removed",
            f"Facility:{facility.id} ({facility.name}) -- {cancelled} open booking(s) cancelled, "
            f"{switched_off} staff login(s) switched off"[:255],
        )
    return {"cancelled_bookings": cancelled, "staff_logins_switched_off": switched_off}


def restore_facility(facility, actor):
    """
    Brings a removed facility back, not operational, with its staff logins
    switched on again. Returns {"staff_logins_switched_on"}.
    """
    with transaction.atomic():
        facility = Facility.objects.select_for_update().get(pk=facility.pk)
        if facility.removed_at is None:
            return {"staff_logins_switched_on": 0}
        facility.removed_at = None
        facility.save(update_fields=["removed_at"])
        # Removing is the only thing that switches a facility staff login off
        # (the admin portal has no other way to), so every switched-off login
        # here is one the removal switched off.
        switched_on = _staff(facility).filter(is_active=False).update(is_active=True)
        log_action(
            actor, "facility.restored",
            f"Facility:{facility.id} ({facility.name}) -- {switched_on} staff login(s) switched back on"[:255],
        )
    return {"staff_logins_switched_on": switched_on}
