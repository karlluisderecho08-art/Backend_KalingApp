"""
A Drop off donor no longer has a Counseling and Testing stage
(MilkBankRequest.DROP_OFF_DONOR_STAGES), so a Drop off already past Booking
Confirmation keeps its place in the new, shorter list:

    old index  old stage                 new index  new stage
        2      Counseling and Testing        2      Breastmilk Collection
        3      Breastmilk Collection         2      Breastmilk Collection
        4      Results                       3      Results

One waiting in Counseling and Testing moves on to Breastmilk Collection -- the
facility approves Drop offs automatically now. Stages 0 and 1 are the same in
both lists.
"""

from django.db import migrations
from django.db.models import F


def forwards(apps, schema_editor):
    MilkBankRequest = apps.get_model("milkbank", "MilkBankRequest")
    MilkBankRequest.objects.filter(
        request_type="DONOR", collection_method="drop_off", current_stage_index__gte=3,
    ).update(current_stage_index=F("current_stage_index") - 1)


def backwards(apps, schema_editor):
    # Best effort: puts Breastmilk Collection and Results back at 3 and 4.
    MilkBankRequest = apps.get_model("milkbank", "MilkBankRequest")
    MilkBankRequest.objects.filter(
        request_type="DONOR", collection_method="drop_off", current_stage_index__gte=2,
    ).update(current_stage_index=F("current_stage_index") + 1)


class Migration(migrations.Migration):
    dependencies = [
        ("milkbank", "0021_pickup_address"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
