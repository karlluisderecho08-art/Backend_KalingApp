import os

from django.core.management.base import BaseCommand

from accounts.models import User


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
            User.objects.create_superuser(email=email, password=password, role=User.Role.FACILITY_STAFF)
            self.stdout.write(self.style.SUCCESS(f"Created admin account: {email}"))
            return

        if admin.role != User.Role.FACILITY_STAFF:
            admin.role = User.Role.FACILITY_STAFF
            admin.save(update_fields=["role"])
            self.stdout.write(self.style.SUCCESS(f"Fixed role on existing admin account: {email}"))
        else:
            self.stdout.write(f"Admin account already exists: {email} (password left untouched)")
