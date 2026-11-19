"""
Every donor goes through Counseling and Testing again, a Drop off too (the
owner's rule, 2026-11-19; migration 0022 had taken it out of a Drop off's
stages). A Drop off's stage index moves back onto the full list:

    short list (0022)          full list (now)
    2  Breastmilk Collection   2  Counseling and Testing   -- still open: her
                                                             milk isn't in yet,
                                                             so she is screened
                                                             first
                               3  Breastmilk Collection    -- it ended there
    3  Results                 4  Results

Each mother sent back to Counseling and Testing, and her facility's staff,
are told. Stages 0 and 1 are the same in both lists.
"""

from django.conf import settings
from django.db import migrations
from django.db.models import F

FINAL_STATUSES = ("completed", "declined", "expired", "cancelled")

MOTHER_TITLE = "Milk Bank Request: Counseling and Testing"
MOTHER_BODY = (
    "Every donation now includes Counseling and Testing, drop offs too: a short counseling "
    "session, a physical check and blood tests at the milk bank. Your booking is back on that "
    "step, before Breastmilk Collection. Your facility will tell you when to come, and you can "
    "message them in Chat from Booking Status."
)
STAFF_TITLE = "Drop Off Back on Counseling and Testing"
STAFF_BODY = (
    "{name}'s drop off now goes through Counseling and Testing before Breastmilk Collection, "
    "like every donation. It is on your Counseling and Testing queue: please tell her when to come."
)


def forwards(apps, schema_editor):
    MilkBankRequest = apps.get_model("milkbank", "MilkBankRequest")
    NotificationItem = apps.get_model("notifications", "NotificationItem")
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    drop_offs = MilkBankRequest.objects.filter(request_type="DONOR", collection_method="drop_off")

    # Results, open or not: one step further down the full list.
    drop_offs.filter(current_stage_index__gte=3).update(current_stage_index=F("current_stage_index") + 1)
    # Ended on Breastmilk Collection: keeps that stage.
    drop_offs.filter(current_stage_index=2, current_sub_status__in=FINAL_STATUSES).update(current_stage_index=3)
    # Still open on Breastmilk Collection: stays at 2, which is now Counseling
    # and Testing.
    for req in drop_offs.filter(current_stage_index=2).exclude(current_sub_status__in=FINAL_STATUSES):
        if req.owner_id is None:
            continue
        owner = User.objects.get(pk=req.owner_id)
        NotificationItem.objects.create(
            owner_id=req.owner_id, title=MOTHER_TITLE, description=MOTHER_BODY, category="Bookings",
        )
        if req.allocated_facility_id is None:
            continue
        name = owner.mom_name or owner.email
        for staff in User.objects.filter(facility_id=req.allocated_facility_id, role="facility_staff", is_active=True):
            NotificationItem.objects.create(
                owner_id=staff.pk, title=STAFF_TITLE, description=STAFF_BODY.format(name=name), category="Bookings",
            )


def backwards(apps, schema_editor):
    # Back onto 0022's short list. (Who was told can't be untold.)
    MilkBankRequest = apps.get_model("milkbank", "MilkBankRequest")
    MilkBankRequest.objects.filter(
        request_type="DONOR", collection_method="drop_off", current_stage_index__gte=3,
    ).update(current_stage_index=F("current_stage_index") - 1)


class Migration(migrations.Migration):
    dependencies = [
        ("milkbank", "0024_cancel_deadlines_kept_bookings"),
        ("notifications", "0002_sign_in_lockout"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
