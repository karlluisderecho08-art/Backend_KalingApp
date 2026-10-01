from importlib import import_module

from django.apps import apps
from django.test import TestCase

from .models import SupportContact

# Migration modules start with a digit, so they can't be imported with a
# plain `import` statement.
contact_details = import_module("directory.migrations.0003_support_contact_details")


class SupportContactDetailsMigrationTests(TestCase):
    """The test database is built by running every migration, so the rows
    0003_support_contact_details creates are already here."""

    def test_directory_lists_the_four_organizations(self):
        response = self.client.get("/directory/")
        self.assertEqual(response.status_code, 200)
        self.assertCountEqual(
            [contact["name"] for contact in response.json()],
            [
                "Quezon City General Hospital",
                "St. Luke's Medical Center - Quezon City",
                "Dr. Jose Fabella Memorial Hospital Human Milk Bank",
                "Arugaan",
            ],
        )

    def test_phone_is_a_single_diallable_number(self):
        # The app dials `phone` as-is, and a dialler reads letters as
        # keypad digits -- so an extension ("local 209") must never be
        # stored in this field.
        for contact in SupportContact.objects.all():
            self.assertRegex(contact.phone, r"^[0-9() +-]*$", contact.name)

    def test_arugaan_has_no_unconfirmed_phone_number(self):
        arugaan = SupportContact.objects.get(name="Arugaan")
        self.assertEqual(arugaan.phone, "")
        self.assertEqual(arugaan.email, "arugaan.breastfeeding@gmail.com")

    def test_existing_fabella_row_is_corrected_not_duplicated(self):
        # Production's situation before this migration: the row already
        # exists, with the address the hospital has moved out of.
        SupportContact.objects.filter(name__startswith="Dr. Jose Fabella").update(
            phone="8866-7960",
            address="1003 Lope de Vega St, Santa Cruz, Manila, 1003 Metro Manila",
            email="",
        )
        contact_details.set_contact_details(apps, None)

        fabella = SupportContact.objects.get(name="Dr. Jose Fabella Memorial Hospital Human Milk Bank")
        self.assertEqual(fabella.address, "San Lazaro Compound, Tayuman St., Santa Cruz, Manila")
        self.assertEqual(fabella.phone, "(02) 8866-7960")
        self.assertEqual(SupportContact.objects.count(), 4)

    def test_existing_arugaan_row_is_left_alone(self):
        SupportContact.objects.filter(name="Arugaan").update(phone="(02) 8000-0000")
        contact_details.set_contact_details(apps, None)
        self.assertEqual(SupportContact.objects.get(name="Arugaan").phone, "(02) 8000-0000")
