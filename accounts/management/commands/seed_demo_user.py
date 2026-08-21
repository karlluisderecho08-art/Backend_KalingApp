from django.core.management.base import BaseCommand

from accounts.models import User


class Command(BaseCommand):
    """
    manage.py seed_demo_user

    Creates (or resets) the "Rachel" account that /auth/demo-login/ logs
    into -- the server-side equivalent of the Kotlin app's hardcoded
    default UserProfile (momName="Rachel", babyName="James").
    """

    help = "Create or reset the seeded Rachel demo account"

    def handle(self, *args, **options):
        user, created = User.objects.get_or_create(
            email="rachel@kalingapp.demo",
            defaults={
                "mom_name": "Rachel",
                "baby_name": "James",
                "baby_age_weeks": 12,
                "breastfeeding_status": "Exclusively breastfeeding",
                "pediatric_clinic": "St. Luke's Pediatrics",
                "tracking_streaks": 5,
            },
        )
        user.set_password("demo-only-not-a-real-password")
        user.save()

        verb = "Created" if created else "Reset"
        self.stdout.write(self.style.SUCCESS(f"{verb} demo account: {user.email}"))
