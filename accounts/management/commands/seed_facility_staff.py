import os

from django.core.management.base import BaseCommand

from accounts.models import User
from accounts.passwords import staff_password_problems
from milkbank.models import Facility

# Kept pointing at St. Luke's so an existing bookmark/saved login still
# works; every facility also gets its own staff<id>@ account below.
LEGACY_DEMO_EMAIL = "staff@kalingapp.demo"
LEGACY_DEMO_FACILITY_NAME = "St. Luke's Medical Center"

# Each account has its own variable. There is no shared password any more.
PASSWORD_VAR = "FACILITY_STAFF_PASSWORD_{facility_id}"
LEGACY_PASSWORD_VAR = "FACILITY_STAFF_PASSWORD_LEGACY"
# The old single password for every account. No longer read.
RETIRED_SHARED_VAR = "FACILITY_STAFF_DEMO_PASSWORD"


class Command(BaseCommand):
    """
    manage.py seed_facility_staff

    Creates one facility_staff account per facility (staff<id>@kalingapp.demo,
    plus the older staff@kalingapp.demo), each from ITS OWN environment
    variable -- FACILITY_STAFF_PASSWORD_<facility id> and
    FACILITY_STAFF_PASSWORD_LEGACY. The Facility dashboard has no
    self-registration, so this is how those logins come into existence.
    (Platform admins can also create a staff account with its own password
    from the admin dashboard's facility form.)

    Why one variable per account. These accounts read real mothers'
    bookings, donor questionnaires and serology photos -- health information
    under RA 10173 -- and only for their own facility. They used to share
    ONE password (FACILITY_STAFF_DEMO_PASSWORD), so a single leak opened
    every facility at once. That variable is now ignored.

    What a run does, per account:

      * account exists      -> nothing, whatever the variable says. Its
                               password is managed from the admin portal
                               (User Management -> Set new password); a
                               variable left behind on the host must not
                               quietly undo that on the next deploy.
      * variable unset      -> nothing. A missing account is not created,
                               and nothing guessable is ever set.
      * variable too weak   -> nothing, with a warning. The staff password
                               rules (accounts/passwords.py), and never
                               the same value as another account's variable.
      * variable set        -> the missing account is created with it.

    So these variables only bootstrap a login that doesn't exist yet. To
    change a password, use the admin portal; once an account exists its
    variable can be deleted from the host. A deleted account whose variable
    is still set comes back on the next deploy -- delete the variable first.
    """

    help = "Create or update each facility staff account from its own FACILITY_STAFF_PASSWORD_<id> variable"

    def _sync(self, email, facility, var, password, duplicates):
        if User.objects.filter(email__iexact=email).exists():
            self.stdout.write(
                f"{email} exists -- left as it is (its password is set from the admin portal"
                + (f"; {var} is no longer needed and can be deleted)." if password else ").")
            )
            return
        if not password:
            self.stdout.write(f"{var} not set -- {email} not created.")
            return
        if password in duplicates:
            self.stdout.write(self.style.WARNING(
                f"{var} is the same as another staff account's password -- {email} left as it is. "
                "Give every account its own password."
            ))
            return
        problems = staff_password_problems(password, User(email=email))
        if problems:
            self.stdout.write(self.style.WARNING(f"{var} was not used for {email}: {' '.join(problems)}"))
            return
        User.objects.create_user(
            email=email, password=password, role=User.Role.FACILITY_STAFF, facility=facility, is_active=True,
        )
        self.stdout.write(self.style.SUCCESS(f"Created {email} -> {facility.name}"))

    def handle(self, *args, **options):
        if os.environ.get(RETIRED_SHARED_VAR):
            self.stdout.write(self.style.WARNING(
                f"{RETIRED_SHARED_VAR} is no longer used (one password for every facility). "
                f"Set {PASSWORD_VAR.format(facility_id='<id>')} for each facility instead, then remove it."
            ))

        facilities = list(Facility.objects.order_by("id"))
        if not facilities:
            self.stdout.write(self.style.WARNING(
                "No facilities exist -- run seed_facilities first, then re-run this."
            ))
            return

        legacy_facility = Facility.objects.filter(name=LEGACY_DEMO_FACILITY_NAME).first() or facilities[0]
        accounts = [
            # Addressed by id rather than a slug of the name: short enough to
            # type at a login screen, and it can't produce a 60-character
            # address out of a long hospital name.
            (f"staff{facility.id}@kalingapp.demo", facility, PASSWORD_VAR.format(facility_id=facility.id))
            for facility in facilities
        ] + [(LEGACY_DEMO_EMAIL, legacy_facility, LEGACY_PASSWORD_VAR)]

        passwords = [os.environ.get(var, "") for _email, _facility, var in accounts]
        set_values = [password for password in passwords if password]
        duplicates = {password for password in set_values if set_values.count(password) > 1}

        for (email, facility, var), password in zip(accounts, passwords):
            self._sync(email, facility, var, password, duplicates)
