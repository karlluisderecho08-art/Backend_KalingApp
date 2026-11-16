import os
import re

from django.core.management.base import BaseCommand

from accounts.models import User


def moved_away_from(email):
    """
    When an account last changed its email AWAY from `email` (the admin
    portal's Settings -> Change email, logged as "account.email_changed",
    "User:<id> from <old> to <new>"), or None if none ever did.
    """
    from core.models import AuditLogEntry

    entry = (
        AuditLogEntry.objects.filter(
            action="account.email_changed",
            target__iregex=rf"^User:[0-9]+ from {re.escape(email)}( |$)",
        )
        .order_by("-created_at")
        .first()
    )
    return entry.created_at if entry else None


class Command(BaseCommand):
    """
    manage.py ensure_admin

    The non-interactive version of createsuperuser -- meant to run
    inside build.sh on every deploy, where there's no terminal to answer
    prompts. Reads ADMIN_EMAIL/ADMIN_PASSWORD from the environment
    (set in Render's dashboard, never committed -- same treatment as
    OPENAI_API_KEY).

    Deliberately does NOT touch the password on an already-existing
    admin account: once created, a redeploy should never silently
    overwrite a password someone may have since changed through /admin/.

    Nor does it recreate an admin under an address an account has since
    moved away from (Settings -> Change email in the admin portal): the
    account still exists under its new address, and a fresh admin at the old
    one -- an inbox its owner may have given up -- would be a second way in.
    It warns instead, until ADMIN_EMAIL is set to the new address -- which
    also makes that account the owner admin again (accounts/owner.py).

    Role is a different story: create_superuser doesn't set one, so it
    fell back to the model default (Role.MOTHER) and the admin account
    was showing up in the facility dashboard's User Management table,
    a mother-accounts-only list (see accounts.views.StaffUserListView).
    Self-healed here on every deploy, same free-tier-workaround pattern
    as the rest of this command -- correcting a role is safe to repeat,
    unlike a password.
    """

    help = "Create the initial admin account from ADMIN_EMAIL/ADMIN_PASSWORD, if it doesn't exist yet"

    def handle(self, *args, **options):
        email = os.environ.get("ADMIN_EMAIL")
        password = os.environ.get("ADMIN_PASSWORD")

        if not email or not password:
            self.stdout.write(self.style.WARNING("ADMIN_EMAIL/ADMIN_PASSWORD not set -- skipping admin creation."))
            return

        try:
            admin = User.objects.get(email=email)
        except User.DoesNotExist:
            moved = moved_away_from(email)
            if moved:
                self.stdout.write(self.style.WARNING(
                    f"ADMIN_EMAIL is {email}, but an admin account moved from that address to a new one on "
                    f"{moved:%Y-%m-%d} (Change email), so no admin was created there. In Render, set "
                    "ADMIN_EMAIL to that account's new address (it also marks the owner admin, the one "
                    "that may delete other admins)."
                ))
                return
            User.objects.create_superuser(email=email, password=password, role=User.Role.FACILITY_STAFF)
            self.stdout.write(self.style.SUCCESS(f"Created admin account: {email}"))
            return

        if admin.role != User.Role.FACILITY_STAFF:
            admin.role = User.Role.FACILITY_STAFF
            admin.save(update_fields=["role"])
            self.stdout.write(self.style.SUCCESS(f"Fixed role on existing admin account: {email}"))
        else:
            self.stdout.write(f"Admin account already exists: {email} (password left untouched)")
