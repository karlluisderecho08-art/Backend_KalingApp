from django.db import migrations, models

# The conversion factor this project used while milk volumes were split
# across two units. Inlined here rather than imported from
# milkbank.models, which no longer defines it: a migration has to keep
# working against the code as it is today, and the whole point of this
# migration is that nothing after it needs to convert anything.
ML_PER_FLUID_OUNCE = 29.5735


def ounces_to_millilitres(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    for user_id, ounces in User.objects.values_list("id", "total_drawn_oz"):
        if ounces:
            User.objects.filter(pk=user_id).update(
                total_drawn_ml=round(ounces * ML_PER_FLUID_OUNCE)
            )


def millilitres_to_ounces(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    for user_id, millilitres in User.objects.values_list("id", "total_drawn_ml"):
        if millilitres:
            User.objects.filter(pk=user_id).update(
                total_drawn_oz=millilitres / ML_PER_FLUID_OUNCE
            )


class Migration(migrations.Migration):
    """
    Moves User.total_drawn_oz to total_drawn_ml, converting the values
    rather than dropping them.

    Add-convert-remove in three steps instead of a RenameField, because a
    rename would keep the old float ounce figures under a column now
    labelled millilitres -- every existing mother's lifetime total would
    silently read ~30x too low.
    """

    dependencies = [
        ("accounts", "0008_alter_pendingregistration_code_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="total_drawn_ml",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.RunPython(ounces_to_millilitres, millilitres_to_ounces),
        migrations.RemoveField(
            model_name="user",
            name="total_drawn_oz",
        ),
    ]
