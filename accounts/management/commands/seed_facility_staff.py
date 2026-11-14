import os

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand

from accounts.models import User
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

# Published in this public repository's history, so never acceptable.
KNOWN_PUBLIC_PASSWORDS = {"demo-only-not-a-real-password"}
MIN_LENGTH = 12


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

      * variable unset      -> nothing. An existing account keeps working
                               with whatever password it has; a missing one
                               is not created. Nobody is locked out by a
                               deploy, and nothing guessable is ever set.
      * variable too weak   -> nothing, with a warning. At least 12
                               characters, Django's password validators,
                               never a password published in this repo's
                               history, and never the same value as another
                               account's variable.
      * variable set        -> the account is created, or brought back to
                               the facility_staff role at its facility; its
                               password is set only when it differs. (This
                               runs on every deploy, and setting a password
                               signs the account out everywhere --
                               settings.SIMPLE_JWT -- so re-setting an
                               unchanged one would sign staff out on every
                               deploy.)

    To rotate a password: change the variable on the host and redeploy.
    """

    help = "Create or update each facility staff account from its own FACILITY_STAFF_PASSWORD_<id> variable"

    def _password_problem(self, password, email):
        if password in KNOWN_PUBLIC_PASSWORDS:
            return "it is published in this repository's history"
        if len(password) < MIN_LENGTH:
            return f"it is shorter than {MIN_LENGTH} characters"
        try:
            validate_password(password, User(email=email))
        except ValidationError as exc:
            return " ".join(exc.messages)
        return None

    def _upsert_staff(self, email, facility, password):
        user = User.objects.filter(email__iexact=email).first()
        created = user is None
        if created:
            user = User(email=email)
        user.role = User.Role.FACILITY_STAFF
        user.facility = facility
        user.is_active = True
        password_changed = created or not user.check_password(password)
        if password_changed:
            user.set_password(password)
        user.save()
        return created, password_changed

    def _sync(self, email, facility, var, password, duplicates):
        if not password:
            self.stdout.write(f"{var} not set -- {email} left as it is.")
            return
        if password in duplicates:
            self.stdout.write(self.style.WARNING(
                f"{var} is the same as another staff account's password -- {email} left as it is. "
                "Give every account its own password."
            ))
            return
        problem = self._password_problem(password, email)
        if problem:
            self.stdout.write(self.style.WARNING(f"{var} was not used for {email}: {problem}."))
            return
        created, password_changed = self._upsert_staff(email, facility, password)
        if created:
            verb = "Created"
        elif password_changed:
            verb = "Password updated for"
        else:
            verb = "Up to date:"
        self.stdout.write(self.style.SUCCESS(f"{verb} {email} -> {facility.name}"))

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
