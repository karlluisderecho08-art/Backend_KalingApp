"""
The owner admin: the one admin account that may delete other admin accounts.

It is the admin account whose email is in ADMIN_EMAIL, set in Render's
dashboard -- the same setting ensure_admin creates the first admin from. So
it is chosen by whoever controls Render (the project's owner), not by anything
an admin can change from the portal.

Why there is one at all: any admin used to be able to delete any other admin.
One stolen admin login could then remove every other admin (and everything
tied to their accounts), leaving nobody able to undo it. Now ordinary admins
can't delete admin accounts; the owner admin can (to retire an old or unused
one), and nobody can delete the owner admin from the portal.

If ADMIN_EMAIL is unset, or names no admin account (for instance after the
owner changed their email and ADMIN_EMAIL wasn't updated yet), there is no
owner admin and no admin account can be deleted until it is set again.
"""

import os


def owner_email():
    """ADMIN_EMAIL, trimmed and lower-cased -- '' when it isn't set."""
    return os.environ.get("ADMIN_EMAIL", "").strip().lower()


def is_admin(user):
    return bool(user.is_staff or user.is_superuser)


def is_owner(user):
    email = owner_email()
    return bool(email) and is_admin(user) and user.email.lower() == email
