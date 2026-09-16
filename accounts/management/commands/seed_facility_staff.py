import os

from django.core.management.base import BaseCommand

from accounts.models import User
from milkbank.models import Facility


class Command(BaseCommand):
    """
    manage.py seed_facility_staff

    Creates (or resets) the demo facility_staff account -- the Facility
    web dashboard has no self-registration flow (staff accounts are
    handed out, not signed up for), so this is how credentials to log in
    with come into existence.

    The password comes from FACILITY_STAFF_DEMO_PASSWORD and there is no
    default. It used to be a literal in this file, which was a genuine
    breach rather than an untidiness: this repository is public, and
    build.sh runs this command on every deploy, so anyone reading the
    source had working facility-staff credentials against production --
    and that role can read real mothers' bookings, accept or decline
    them, and open donor questionnaires and serology photos. Health
    information under RA 10173, reachable by anyone who found the repo.

    With the variable unset this command now does nothing rather than
    falling back to anything guessable. That is the safer failure: a
    dashboard nobody can log into is a much smaller problem than one
    everybody can.
    """

    help = "Create or reset the facility staff demo account from FACILITY_STAFF_DEMO_PASSWORD"

    def handle(self, *args, **options):
        password = os.environ.get("FACILITY_STAFF_DEMO_PASSWORD")
        if not password:
            self.stdout.write(self.style.WARNING(
                "FACILITY_STAFF_DEMO_PASSWORD not set -- skipping facility staff account. "
                "Set it in the host's environment to (re)create staff@kalingapp.demo."
            ))
            return

        facility = Facility.objects.filter(name="St. Luke's Medical Center").first()

        user, created = User.objects.get_or_create(
            email="staff@kalingapp.demo",
            defaults={"role": User.Role.FACILITY_STAFF, "facility": facility},
        )
        user.role = User.Role.FACILITY_STAFF
        user.facility = facility
        user.set_password(password)
        user.save()

        verb = "Created" if created else "Reset"
        facility_note = facility.name if facility else "no facility found -- run seed_facilities first"
        self.stdout.write(self.style.SUCCESS(f"{verb} facility staff demo account: {user.email} ({facility_note})"))
