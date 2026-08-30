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
    """

    help = "Create the initial admin account from ADMIN_EMAIL/ADMIN_PASSWORD, if it doesn't exist yet"

    def handle(self, *args, **options):
        email = os.environ.get("ADMIN_EMAIL")
        password = os.environ.get("ADMIN_PASSWORD")

        if not email or not password:
            self.stdout.write(self.style.WARNING("ADMIN_EMAIL/ADMIN_PASSWORD not set -- skipping admin creation."))
            return

        if User.objects.filter(email=email).exists():
            self.stdout.write(f"Admin account already exists: {email} (password left untouched)")
            return

        User.objects.create_superuser(email=email, password=password)
        self.stdout.write(self.style.SUCCESS(f"Created admin account: {email}"))
