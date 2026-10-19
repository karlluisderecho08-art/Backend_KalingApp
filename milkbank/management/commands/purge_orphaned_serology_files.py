"""
Deletes stored serology photos that no questionnaire refers to any more.

Before milkbank/signals.py existed, deleting a request or an account left its
photo in storage; and if storage is unreachable at the moment of a delete,
the signal's attempt fails. This finds every such file and removes it.

Only files at least a day old are touched, so a photo that is being uploaded
right now -- stored a moment before its questionnaire row commits -- is never
mistaken for an orphan. Runs from build.sh on every deploy and never fails
the deploy: problems are reported and skipped.
"""

from datetime import timedelta

from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand
from django.utils import timezone

from milkbank.models import DonorQuestionnaire

ROOT = "serology_photos"
MIN_AGE = timedelta(days=1)


def _walk(storage, path):
    dirs, files = storage.listdir(path)
    for name in files:
        yield f"{path}/{name}"
    for name in dirs:
        yield from _walk(storage, f"{path}/{name}")


class Command(BaseCommand):
    help = "Deletes stored serology photos that no donor questionnaire refers to."

    def handle(self, *args, **options):
        try:
            referenced = set(
                DonorQuestionnaire.objects.exclude(serology_photo="")
                .exclude(serology_photo__isnull=True)
                .values_list("serology_photo", flat=True)
            )
            stored = list(_walk(default_storage, ROOT))
        except Exception as exc:  # noqa: BLE001 -- no folder yet, or storage unreachable
            self.stdout.write(f"Serology orphan sweep skipped: {exc}")
            return

        cutoff = timezone.now() - MIN_AGE
        deleted = skipped = 0
        for name in stored:
            if name in referenced:
                continue
            try:
                if default_storage.get_modified_time(name) > cutoff:
                    skipped += 1  # too new to be sure it's an orphan
                    continue
                default_storage.delete(name)
                deleted += 1
            except Exception as exc:  # noqa: BLE001
                self.stdout.write(f"Could not remove {name}: {exc}")
        self.stdout.write(f"Serology orphan sweep: {deleted} deleted, {skipped} too recent to judge.")
