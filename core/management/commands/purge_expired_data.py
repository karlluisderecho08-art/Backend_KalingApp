from django.core.management.base import BaseCommand

from core.retention import purge_expired_data


class Command(BaseCommand):
    help = "Deletes personal data past its retention period (see the Data retention settings)."

    def handle(self, *args, **options):
        # Runs from build.sh on every deploy; a failure here must not block the
        # deploy, since the purge also runs from ordinary traffic afterwards.
        try:
            counts = purge_expired_data()
        except Exception as exc:  # noqa: BLE001
            self.stderr.write(f"Retention purge failed (will retry from traffic): {exc}")
            return
        self.stdout.write("Retention purge: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
