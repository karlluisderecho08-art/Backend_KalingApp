"""
Deletes a serology photo's stored file when its questionnaire row goes.

Deleting a row never touches the file behind a FileField, so before this a
deleted request or account left the donor's blood-test photo sitting in the
storage bucket indefinitely. The questionnaire is deleted with its request,
and the request with its owner's account (CASCADE), and Django sends
post_delete for every row a cascade removes -- so this one hook covers an
admin deleting an account, a mother deleting her own, and anything else that
removes a request.

The file is removed only after the transaction commits: if the delete is
rolled back, the photo must still be there. A storage failure is logged and
never undoes the account deletion; `manage.py purge_orphaned_serology_files`
(run on every deploy) sweeps up anything a failure left behind.
"""

import logging

from django.db import transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver

from .models import DonorQuestionnaire

logger = logging.getLogger(__name__)


def _delete_stored_file(storage, name):
    try:
        storage.delete(name)
    except Exception:  # noqa: BLE001 -- the orphan sweep retries it
        logger.exception("Could not delete serology photo %s", name)


@receiver(post_delete, sender=DonorQuestionnaire)
def delete_serology_photo_file(sender, instance, **kwargs):
    photo = instance.serology_photo
    if photo and photo.name:
        storage, name = photo.storage, photo.name
        transaction.on_commit(lambda: _delete_stored_file(storage, name))
