from django.core.management.base import BaseCommand

from accounts.models import User
from milkbank.models import Facility


class Command(BaseCommand):
    """
    manage.py seed_facility_staff

    Creates (or resets) a demo facility_staff account -- the Facility web
    dashboard has no self-registration flow (staff accounts are handed
    out, not signed up for), so this is the only way to get real
    credentials to log in and test against. Mirrors seed_demo_user.py's
    pattern for the mother-side demo account.

    Also assigns it to a real Facility row: a facility_staff account
    with no facility set sees zero bookings under the new per-facility
    scoping (see milkbank/permissions.py) -- a demo account nobody can
    see anything with wouldn't demonstrate much. Run after
    seed_facilities (build.sh already does this in that order); if
    St. Luke's doesn't exist yet, this still creates the account, just
    without a facility assigned.
    """

    help = "Create or reset the seeded facility staff demo account"

    def handle(self, *args, **options):
        facility = Facility.objects.filter(name="St. Luke's Medical Center").first()

        user, created = User.objects.get_or_create(
            email="staff@kalingapp.demo",
            defaults={"role": User.Role.FACILITY_STAFF, "facility": facility},
        )
        user.role = User.Role.FACILITY_STAFF
        user.facility = facility
        user.set_password("demo-only-not-a-real-password")
        user.save()

        verb = "Created" if created else "Reset"
        facility_note = facility.name if facility else "no facility found -- run seed_facilities first"
        self.stdout.write(self.style.SUCCESS(f"{verb} facility staff demo account: {user.email} ({facility_note})"))
