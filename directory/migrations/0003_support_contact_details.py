from django.db import migrations

# The Contact Directory's entries, with details taken from each
# organization's own official page on 2026-10-02:
#
#   Quezon City General Hospital
#     quezoncity.gov.ph/departments/quezon-city-general-hospital/
#     quezoncity.gov.ph/qcitizen-guides/how-to-donate-to-or-avail-from-qc-human-milk-banks/
#   St. Luke's Medical Center - Quezon City
#     stlukes.com.ph/contact-us
#   Dr. Jose Fabella Memorial Hospital
#     fabella.doh.gov.ph/contact-us/
#
# A data migration rather than an edit to seed_support_contacts alone:
# that command is get_or_create by name, so it never touches a row that
# already exists -- and production already has the Fabella row, carrying
# the Lope de Vega address the hospital has since moved out of. A
# migration runs exactly once per database, which also means anything an
# admin corrects through /admin/ afterwards is not overwritten by the
# next deploy.
#
# `phone` holds one plain number and nothing else, because the app dials
# it as-is (tel:<phone>). An extension or a second number goes in the
# description instead -- letters in a dialled string are read as keypad
# digits, so "local 209" would dial as extra numbers.
#
# Arugaan's phone is deliberately not set. The only numbers published for
# it are on personal blogs from 2009-2011 (old 7-digit landlines and
# volunteers' own mobiles), none confirmed by Arugaan itself. Its row is
# only created here if it is somehow missing; an existing one is left
# exactly as it is.
CONTACTS = [
    {
        "name": "Quezon City General Hospital",
        "description": (
            "Quezon City government hospital and home of the Quezon City Human Milk Bank "
            "(2nd floor). For the milk bank, call the hospital and ask for local 209, or "
            "call 0960-363-6492. Milk bank hours: daily 8:00 AM - 5:00 PM, "
            "holidays 8:00 AM - 12:00 NN."
        ),
        "phone": "(02) 8863-0800",
        "address": "Seminary Road, Brgy. Bahay Toro, Project 8, Quezon City",
        "email": "humanmilkbank.qcgh@gmail.com",
    },
    {
        "name": "St. Luke's Medical Center - Quezon City",
        "description": (
            "Private hospital with breastfeeding support services and a human milk bank. "
            "Call the trunk line and ask for lactation support; customer service is on "
            "local 4122."
        ),
        "phone": "(02) 8723-0101",
        "address": "279 E. Rodriguez Sr. Ave., Quezon City 1112",
        "email": "customer.qc@stlukes.com.ph",
    },
    {
        "name": "Dr. Jose Fabella Memorial Hospital Human Milk Bank",
        "description": "Government-accredited human milk bank.",
        "phone": "(02) 8866-7960",
        "address": "San Lazaro Compound, Tayuman St., Santa Cruz, Manila",
        "email": "mcc@fabella.doh.gov.ph",
    },
]

ARUGAAN = {
    "name": "Arugaan",
    "description": "Volunteer-run breastfeeding and milk-banking advocacy organization.",
    "phone": "",
    "address": "2 Starlight Street corner Vista Street, SSS Village, Marikina City, Metro Manila",
    "email": "arugaan.breastfeeding@gmail.com",
}


def set_contact_details(apps, schema_editor):
    SupportContact = apps.get_model("directory", "SupportContact")
    for data in CONTACTS:
        fields = {key: value for key, value in data.items() if key != "name"}
        SupportContact.objects.update_or_create(name=data["name"], defaults=fields)
    SupportContact.objects.get_or_create(
        name=ARUGAAN["name"],
        defaults={key: value for key, value in ARUGAAN.items() if key != "name"},
    )


class Migration(migrations.Migration):

    dependencies = [
        ("directory", "0002_supportcontact_email"),
    ]

    operations = [
        # Nothing to undo on the way back: the previous values are not
        # worth restoring (Fabella's was an address it no longer occupies).
        migrations.RunPython(set_contact_details, migrations.RunPython.noop),
    ]
