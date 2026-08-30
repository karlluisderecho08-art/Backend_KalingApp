from django.core.management.base import BaseCommand

from accounts.models import User


class Command(BaseCommand):
    """
    manage.py seed_facility_staff

    Creates (or resets) a demo facility_staff account -- the Facility web
    dashboard has no self-registration flow (staff accounts are handed
    out, not signed up for), so this is the only way to get real
    credentials to log in and test against. Mirrors seed_demo_user.py's
    pattern for the mother-side demo account.
    """

    help = "Create or reset the seeded facility staff demo account"

    def handle(self, *args, **options):
        user, created = User.objects.get_or_create(
            email="staff@kalingapp.demo",
            defaults={"role": User.Role.FACILITY_STAFF},
        )
        user.role = User.Role.FACILITY_STAFF
        user.set_password("demo-only-not-a-real-password")
        user.save()

        verb = "Created" if created else "Reset"
        self.stdout.write(self.style.SUCCESS(f"{verb} facility staff demo account: {user.email}"))
