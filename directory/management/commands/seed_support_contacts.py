from django.core.management.base import BaseCommand

from directory.models import SupportContact


class Command(BaseCommand):
    """
    manage.py seed_support_contacts

    Creates the two organizations the Kotlin app's ContactDirectoryScreen
    already shows (CODEBASE-1.md section 5/7). phone/address are left
    blank on purpose -- same "" the current app ships with, which is why
    it falls back to "pending verification" copy. Filling in the real
    numbers/addresses is an admin data-entry task, not a code change
    (see roadmap Phase 2), so this command doesn't guess at them.
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
                "email": "",
                "phone": "8866-7960",
                "address": "1003 Lope de Vega St, Santa Cruz, Manila, 1003 Metro Manila",
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
