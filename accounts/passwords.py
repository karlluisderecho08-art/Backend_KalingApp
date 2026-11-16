"""
The password rules, in one place.

A facility staff (or admin) account's password, wherever it is set: the admin
portal's "Set new password" (AdminUserSetPasswordView), a new staff login
added from the facility form (milkbank FacilitySerializer), a staff or admin
password reset (ResetPasswordView) and the deploy-time variables
(seed_facility_staff). Stricter than a mother's on purpose: a staff login reads
real mothers' bookings, donor questionnaires and serology photos -- health
information under RA 10173 -- for its whole facility.

A mother's password, at sign-up (RegisterSerializer) and reset: at least 8
characters, and not one of the passwords people guess first. That second part
matters as much as the length. The sign-in lockout stops anyone guessing one
account many times, but not someone trying the few most common passwords
("12345678", "password") on many accounts once each -- which an account that
uses one of them can't survive.
"""

from django.conf import settings
from django.contrib.auth.password_validation import get_password_validators, validate_password
from django.core.exceptions import ValidationError

MIN_LENGTH = 12
MOTHER_MIN_LENGTH = 8

# Published in this public repository's history, so never acceptable.
KNOWN_PUBLIC_PASSWORDS = {"demo-only-not-a-real-password"}


def _django_checks(password, user):
    """
    Django's own rules -- not too like the email, not a common password, not
    only numbers -- without its 8-character minimum: each caller states its own
    length rule, with its own wording.
    """
    validators = get_password_validators([
        validator for validator in settings.AUTH_PASSWORD_VALIDATORS
        if not validator["NAME"].endswith(".MinimumLengthValidator")
    ])
    try:
        validate_password(password, user, password_validators=validators)
    except ValidationError as exc:
        return list(exc.messages)
    return []


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
    return problems + _django_checks(password, user)


def mother_password_problems(password, user):
    """
    Every reason `password` can't be used for the mother's account `user` (an
    unsaved User with just its email is enough). Each message has a Tagalog
    line in core/tagalog_catalog.py -- she may be using the app in Tagalog.
    """
    if len(password) < MOTHER_MIN_LENGTH:
        return ["Password must be at least 8 characters."]
    return _django_checks(password, user)
