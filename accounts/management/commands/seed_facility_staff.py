import os

from django.core.management.base import BaseCommand

from accounts.models import User
from milkbank.models import Facility

# Kept pointing at St. Luke's so an existing bookmark/saved login still
# works; every facility also gets its own staff<id>@ account below.
LEGACY_DEMO_EMAIL = "staff@kalingapp.demo"
LEGACY_DEMO_FACILITY_NAME = "St. Luke's Medical Center"


class Command(BaseCommand):
    """
    manage.py seed_facility_staff

    Creates (or resets) one facility_staff account per facility -- the
    Facility web dashboard has no self-registration flow (staff accounts
    are handed out, not signed up for), so this is how credentials to log
    in with come into existence.

    One account per facility, not one overall. Smart Allocation sends a
    booking to whichever facility ranks best for that mother's location
    (milkbank/allocation.py), while the dashboard shows a staff member
    strictly their own facility's bookings -- so a single St. Luke's
    account, which is all this command used to create, simply could not
    see a booking allocated to Fabella or PGH. It looked like bookings
    weren't arriving at all; they were arriving somewhere nobody had a
    login for. The scoping is right (one hospital's staff must not read
    another's mothers' bookings, donor questionnaires or serology
    photos); the gap was that three of the four facilities had no staff
    account at all.

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

    help = "Create or reset one facility staff account per facility from FACILITY_STAFF_DEMO_PASSWORD"

    def _upsert_staff(self, email, facility, password):
        """get_or_create then overwrite, so re-running on every deploy
        re-syncs role/facility/password instead of duplicating anyone."""
        user, created = User.objects.get_or_create(
            email=email,
            defaults={"role": User.Role.FACILITY_STAFF, "facility": facility},
        )
        user.role = User.Role.FACILITY_STAFF
        user.facility = facility
        user.is_active = True
        user.set_password(password)
        user.save()
        return user, created

    def handle(self, *args, **options):
        password = os.environ.get("FACILITY_STAFF_DEMO_PASSWORD")
        if not password:
            self.stdout.write(self.style.WARNING(
                "FACILITY_STAFF_DEMO_PASSWORD not set -- skipping facility staff accounts. "
                "Set it in the host's environment to (re)create them."
            ))
            return

        facilities = list(Facility.objects.order_by("id"))
        if not facilities:
            self.stdout.write(self.style.WARNING(
                "No facilities exist -- run seed_facilities first, then re-run this."
            ))
            return

        # Addressed by id rather than a slug of the name: short enough to
        # actually type at a login screen, and it can't produce a
        # 60-character address out of "Dr. Jose Fabella Memorial Hospital
        # Human Milk Bank". The mapping is printed below precisely
        # because an id on its own isn't self-describing.
        for facility in facilities:
            email = f"staff{facility.id}@kalingapp.demo"
            _user, created = self._upsert_staff(email, facility, password)
            verb = "Created" if created else "Reset  "
            self.stdout.write(self.style.SUCCESS(f"{verb} {email} -> {facility.name}"))

        legacy_facility = Facility.objects.filter(name=LEGACY_DEMO_FACILITY_NAME).first() or facilities[0]
        _user, created = self._upsert_staff(LEGACY_DEMO_EMAIL, legacy_facility, password)
        verb = "Created" if created else "Reset  "
        self.stdout.write(self.style.SUCCESS(
            f"{verb} {LEGACY_DEMO_EMAIL} -> {legacy_facility.name} (kept for existing logins)"
        ))
