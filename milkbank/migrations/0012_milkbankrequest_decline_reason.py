from django.db import migrations, models

# Reasons the Booking Request desk offered before decline_reason existed,
# so a decline recorded under them can be counted by reason. Before this,
# the reason was only the opening words of the free-text staff_message
# ("Outdated Serological Test -- notes..."), which can't be grouped.
HISTORIC_REASONS = {"Outdated Serological Test", "No Available Doctor", "Others"}


def backfill_decline_reason(apps, schema_editor):
    MilkBankRequest = apps.get_model("milkbank", "MilkBankRequest")
    for req in MilkBankRequest.objects.filter(current_sub_status="declined", decline_reason=""):
        message = (req.staff_message or "").strip()
        # The dashboard joined reason and notes with an em dash.
        leading = message.split("—")[0].strip()
        reason = leading if leading in HISTORIC_REASONS else "Others"
        req.decline_reason = reason
        req.save(update_fields=["decline_reason"])


class Migration(migrations.Migration):

    dependencies = [
        ("milkbank", "0011_fabella_current_address"),
    ]

    operations = [
        migrations.AddField(
            model_name="milkbankrequest",
            name="decline_reason",
            field=models.CharField(blank=True, max_length=100),
        ),
        # Nothing to undo going back: the column is simply dropped.
        migrations.RunPython(backfill_decline_reason, migrations.RunPython.noop),
    ]
