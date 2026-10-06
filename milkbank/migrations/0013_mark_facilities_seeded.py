"""
Records that the demo facilities have already been seeded, on any database
that has facilities.

seed_facilities now seeds once per database and remembers that with a
"seed.facilities" line in the audit log (see that command for why: it used
to recreate, on every deploy, any seeded facility an admin had deleted).
A database seeded BEFORE that change has the facilities but not the line,
so without this the very next deploy would seed one more time -- bringing a
deleted facility back yet again. migrate runs before seed_facilities in
build.sh, so this lands first.

An empty database gets no line, and is seeded normally the first time.
"""

from django.db import migrations

SEEDED_ACTION = "seed.facilities"


def mark_seeded(apps, schema_editor):
    Facility = apps.get_model("milkbank", "Facility")
    AuditLogEntry = apps.get_model("core", "AuditLogEntry")
    if Facility.objects.exists() and not AuditLogEntry.objects.filter(action=SEEDED_ACTION).exists():
        AuditLogEntry.objects.create(
            actor=None, action=SEEDED_ACTION, target="Facility: demo set (recorded by migration)",
        )


class Migration(migrations.Migration):

    dependencies = [
        ("milkbank", "0012_milkbankrequest_decline_reason"),
        ("core", "0001_initial"),
    ]

    operations = [
        # No reverse: the audit log is append-only.
        migrations.RunPython(mark_seeded, migrations.RunPython.noop),
    ]
