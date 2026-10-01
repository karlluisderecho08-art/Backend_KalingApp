from django.core.management.base import BaseCommand

from directory.models import SupportContact


class Command(BaseCommand):
    """
    manage.py seed_support_contacts

    Creates the two organizations the Kotlin app's ContactDirectoryScreen
    originally showed (CODEBASE-1.md section 5/7), if they are missing.

    The directory's verified details -- and its other entries -- now come
    from migration 0003_support_contact_details, which runs before this
    on every deploy. So in practice both rows below already exist by the
    time this runs and it changes nothing; it is kept as a safety net,
    and its values match that migration so it can never put a stale
    address back. Arugaan's phone stays blank because no number for it
    has been confirmed -- the app shows "pending verification" for a
    blank field, which is the honest thing to show.
    """

    help = "Seed the Arugaan and Fabella support contact entries"

    def handle(self, *args, **options):
        contacts = [
            {
                "name": "Arugaan",
                "description": "Volunteer-run breastfeeding and milk-banking advocacy organization.",
                "email": "arugaan.breastfeeding@gmail.com",
                "phone": "",
                "address": "2 Starlight Street corner Vista Street, SSS Village, Marikina City, Metro Manila",
            },
            {
                "name": "Dr. Jose Fabella Memorial Hospital Human Milk Bank",
                "description": "Government-accredited human milk bank.",
                "email": "mcc@fabella.doh.gov.ph",
                "phone": "(02) 8866-7960",
                "address": "San Lazaro Compound, Tayuman St., Santa Cruz, Manila",
            },
        ]
        for data in contacts:
            obj, created = SupportContact.objects.get_or_create(
                name=data["name"],
                defaults={
                    "description": data["description"], "phone": data["phone"],
                    "address": data["address"], "email": data["email"],
                },
            )
            verb = "Created" if created else "Already exists"
            self.stdout.write(self.style.SUCCESS(f"{verb}: {obj.name}"))
