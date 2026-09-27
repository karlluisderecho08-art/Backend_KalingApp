from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from accounts.models import User


class Command(BaseCommand):
    """
    manage.py seed_demo_user

    Creates (or resets) the account that /auth/demo-login/ logs into.

    The profile is deliberately plausible for the study's actual population
    -- a postpartum mother in Metro Manila -- rather than the placeholder
    "Rachel"/"James" it started as, which was carried over from the Kotlin
    app's hardcoded default UserProfile. A demo account gets shown to
    people; Western given names in a study about Filipino mothers read as
    unfinished.

    The fields are also internally CONSISTENT, which the old seed was not:
    baby_age_weeks is derived from baby_birth_date instead of being an
    unrelated constant, so the profile cannot show a 12-week-old with a
    birth date that implies something else. total_drawn_ml is set to a
    volume a real donor might have reached over that period rather than
    left at zero next to a five-day tracking streak.
    """

    help = "Create or reset the seeded Rachel demo account"

    def handle(self, *args, **options):
        baby_age_weeks = 11
        baby_birth_date = (timezone.now() - timedelta(weeks=baby_age_weeks)).date()

        user, created = User.objects.get_or_create(
            email=settings.DEMO_ACCOUNT_EMAIL,
            defaults={
                "mom_name": "Maria Liza Santos",
                "baby_name": "Mateo",
                "baby_age_weeks": baby_age_weeks,
                "baby_birth_date": baby_birth_date,
                "breastfeeding_status": "Exclusively breastfeeding",
                "pediatric_clinic": "St. Luke's Medical Center - Pediatrics",
                "tracking_streaks": 9,
                # ~11 weeks of occasional expressing, in millilitres.
                "total_drawn_ml": 1480,
            },
        )
        user.set_password("demo-only-not-a-real-password")
        user.save()

        verb = "Created" if created else "Reset"
        self.stdout.write(self.style.SUCCESS(f"{verb} demo account: {user.email}"))
