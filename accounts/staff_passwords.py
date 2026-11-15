"""
The one set of rules for a facility staff account's password, wherever it is
set: the admin portal's "Set new password" (AdminUserSetPasswordView), a new
staff login added from the facility form (milkbank FacilitySerializer), and
the deploy-time variables (seed_facility_staff).

Stricter than a mother's 8 characters on purpose: a staff login reads real
mothers' bookings, donor questionnaires and serology photos -- health
information under RA 10173 -- for its whole facility.
"""

from django.conf import settings
from django.contrib.auth.password_validation import get_password_validators, validate_password
from django.core.exceptions import ValidationError

MIN_LENGTH = 12

# Published in this public repository's history, so never acceptable.
KNOWN_PUBLIC_PASSWORDS = {"demo-only-not-a-real-password"}


def staff_password_problems(password, user):
    """
    Every reason `password` can't be used for the staff account `user` (an
    unsaved User with just its email is enough), as messages an admin can
    read. Empty when it is fine.
    """
    if password in KNOWN_PUBLIC_PASSWORDS:
        return ["This password is published in KalingApp's public code history. Choose another."]
    problems = []
    if len(password) < MIN_LENGTH:
        problems.append(f"A staff password needs at least {MIN_LENGTH} characters.")
    # Django's own rules -- not too like the email, not a common password, not
    # only numbers -- minus its 8-character minimum, which the one above replaces.
    validators = get_password_validators([
        validator for validator in settings.AUTH_PASSWORD_VALIDATORS
        if not validator["NAME"].endswith(".MinimumLengthValidator")
    ])
    try:
        validate_password(password, user, password_validators=validators)
    except ValidationError as exc:
        problems.extend(exc.messages)
    return problems
