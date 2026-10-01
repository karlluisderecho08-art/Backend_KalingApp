from django.db import migrations

# Dr. Jose Fabella Memorial Hospital no longer operates from Lope de Vega
# St. -- its own site (fabella.doh.gov.ph/contact-us/, checked 2026-10-02)
# gives San Lazaro Compound, Tayuman St. The bookable Facility row still
# carried the old address, which is what a mother sees as "Your Facility"
# on her booking tracker, i.e. where the app was telling her to go.
#
# The coordinates move with it, to OpenStreetMap's pin for the hospital
# on Tayuman Street. Smart Allocation ranks facilities by distance from
# the mother (milkbank/allocation.py), so these are not cosmetic -- though
# the old pin was only about 350 m from the real site, so no mother's
# allocation changes by more than that.
#
# A migration because seed_facilities is get_or_create by name and never
# updates a row that already exists (same reason as
# directory/0003_support_contact_details, which fixed the Contact
# Directory's copy of this address).
#
# Only touches the row while it still holds the old address: facilities
# are editable from the admin dashboard, and an address someone has
# already corrected by hand must not be overwritten by this.
FACILITY_NAME = "Dr. Jose Fabella Memorial Hospital Human Milk Bank"
OLD_ADDRESS = "1003 Lope de Vega St, Santa Cruz, Manila, 1003 Metro Manila"
NEW_ADDRESS = "San Lazaro Compound, Tayuman St., Santa Cruz, Manila"
NEW_LATITUDE = 14.6153
NEW_LONGITUDE = 120.9804


def move_fabella(apps, schema_editor):
    Facility = apps.get_model("milkbank", "Facility")
    Facility.objects.filter(name=FACILITY_NAME, address=OLD_ADDRESS).update(
        address=NEW_ADDRESS, latitude=NEW_LATITUDE, longitude=NEW_LONGITUDE,
    )


class Migration(migrations.Migration):

    dependencies = [
        ("milkbank", "0010_milkbankrequest_clinic_info_and_more"),
    ]

    operations = [
        # No reverse: putting back an address the hospital has left is
        # not something a rollback should do.
        migrations.RunPython(move_fabella, migrations.RunPython.noop),
    ]
