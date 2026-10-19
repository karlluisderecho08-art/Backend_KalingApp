"""
Serology photo rules and file lifecycle, representative-retention display,
and adding a staff login to an existing facility.
"""

import io
import os
import shutil
import tempfile
import time
from pathlib import Path

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import override_settings
from rest_framework.test import APITestCase

from accounts.models import User

from .models import DonorQuestionnaire, Facility, MilkBankRequest
from .tests import make_facility, make_request

ANSWERS = {
    "good_general_health": "true",
    "lactating_with_excess_supply": "true",
    "free_of_infectious_disease": "true",
    "recent_transfusion_or_transplant": "false",
    "uses_tobacco_alcohol_or_drugs": "false",
    "on_medication_or_supplements": "false",
}


def photo():
    return SimpleUploadedFile("serology.jpg", b"\xff\xd8\xff\xe0fake", content_type="image/jpeg")


class MediaTestCase(APITestCase):
    def setUp(self):
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        override = override_settings(MEDIA_ROOT=self.media)
        override.enable()
        self.addCleanup(override.disable)
        self.mother = User.objects.create_user(email="privacy@example.com", password="x", is_active=True)
        self.facility = make_facility()
        self.req = make_request(self.mother, self.facility)

    def submit(self, **fields):
        self.client.force_authenticate(user=self.mother)
        return self.client.post(
            f"/milkbank/requests/{self.req.id}/donor-questionnaire/", {**ANSWERS, **fields}, format="multipart",
        )


class SerologyPhotoRequiredTests(MediaTestCase):
    def test_yes_without_a_photo_is_refused(self):
        response = self.submit(has_recent_serology_test="true")

        self.assertEqual(response.status_code, 400)
        self.assertIn("serology_photo", response.data)
        self.assertFalse(DonorQuestionnaire.objects.exists())

    def test_yes_with_a_photo_is_accepted(self):
        self.assertEqual(self.submit(has_recent_serology_test="true", serology_photo=photo()).status_code, 201)

    def test_no_needs_no_photo(self):
        self.assertEqual(self.submit(has_recent_serology_test="false").status_code, 201)


class SerologyFileLifecycleTests(MediaTestCase):
    def stored_path(self):
        return Path(self.media) / DonorQuestionnaire.objects.get(request=self.req).serology_photo.name

    def test_deleting_the_request_removes_the_file(self):
        self.submit(has_recent_serology_test="true", serology_photo=photo())
        path = self.stored_path()
        self.assertTrue(path.exists())

        with self.captureOnCommitCallbacks(execute=True):
            MilkBankRequest.objects.filter(pk=self.req.pk).delete()

        self.assertFalse(path.exists())

    def test_an_admin_deleting_the_account_removes_the_file(self):
        self.submit(has_recent_serology_test="true", serology_photo=photo())
        path = self.stored_path()
        admin = User.objects.create_superuser(email="del-admin@example.com", password="password123")
        self.client.force_authenticate(user=admin)

        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.client.delete(f"/auth/admin/users/{self.mother.id}/").status_code, 204)

        self.assertFalse(path.exists())

    def test_the_orphan_sweep_removes_only_old_unreferenced_files(self):
        self.submit(has_recent_serology_test="true", serology_photo=photo())
        kept = self.stored_path()
        folder = kept.parent
        old_orphan = folder / "left_behind.jpg"
        new_orphan = folder / "being_uploaded.jpg"
        old_orphan.write_bytes(b"x")
        new_orphan.write_bytes(b"x")
        two_days_ago = time.time() - 2 * 24 * 3600
        os.utime(old_orphan, (two_days_ago, two_days_ago))
        os.utime(kept, (two_days_ago, two_days_ago))

        out = io.StringIO()
        call_command("purge_orphaned_serology_files", stdout=out)

        self.assertFalse(old_orphan.exists())
        self.assertTrue(new_orphan.exists(), "a file too new to judge must be left alone")
        self.assertTrue(kept.exists(), "a photo still in use must never be removed")
        self.assertIn("1 deleted", out.getvalue())

    def test_the_orphan_sweep_is_harmless_with_no_folder(self):
        out = io.StringIO()
        call_command("purge_orphaned_serology_files", stdout=out)  # must not raise
        self.assertIn("skipped", out.getvalue())


class RepresentativePurgeVisibleTests(APITestCase):
    def test_the_booking_says_when_the_details_were_removed(self):
        mother = User.objects.create_user(email="rep@example.com", password="x", is_active=True)
        req = make_request(mother, make_facility(), request_type=MilkBankRequest.RequestType.RECIPIENT)
        MilkBankRequest.objects.filter(pk=req.pk).update(representative_purged_at="2026-10-20T00:00:00Z")
        self.client.force_authenticate(user=mother)

        data = self.client.get("/milkbank/requests/mine/").data[0]

        self.assertIsNotNone(data["representative_purged_at"])
        self.assertIn("closed_at", data)


class AddStaffToExistingFacilityTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(email="fac-admin@example.com", password="password123")
        self.client.force_authenticate(user=self.admin)
        self.facility = make_facility(name="PGH")
        self.existing = User.objects.create_user(
            email="first-staff@example.com", password="x", is_active=True,
            role=User.Role.FACILITY_STAFF, facility=self.facility,
        )

    def patch(self, **data):
        return self.client.patch(f"/milkbank/facilities/{self.facility.id}/", data, format="json")

    def test_a_new_login_is_added_and_the_old_one_kept(self):
        response = self.patch(new_staff_email="second-staff@example.com", new_staff_password="longpassword")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            response.data["staff_emails"], ["first-staff@example.com", "second-staff@example.com"],
        )
        new = User.objects.get(email="second-staff@example.com")
        self.assertEqual((new.role, new.facility_id), (User.Role.FACILITY_STAFF, self.facility.id))
        self.existing.refresh_from_db()
        self.assertEqual(self.existing.facility_id, self.facility.id)

    def test_an_unassigned_staff_account_can_be_assigned(self):
        spare = User.objects.create_user(
            email="spare-staff@example.com", password="x", is_active=True, role=User.Role.FACILITY_STAFF,
        )
        self.assertEqual(self.patch(staff_user_id=spare.id).status_code, 200)
        spare.refresh_from_db()
        self.assertEqual(spare.facility_id, self.facility.id)

    def test_staff_already_at_another_facility_is_not_moved(self):
        other = make_facility(name="Other")
        busy = User.objects.create_user(
            email="busy-staff@example.com", password="x", is_active=True,
            role=User.Role.FACILITY_STAFF, facility=other,
        )
        self.assertEqual(self.patch(staff_user_id=busy.id).status_code, 400)
        busy.refresh_from_db()
        self.assertEqual(busy.facility_id, other.id)

    def test_an_email_already_in_use_is_refused(self):
        self.assertEqual(
            self.patch(new_staff_email="first-staff@example.com", new_staff_password="longpassword").status_code, 400,
        )

    def test_an_ordinary_edit_changes_no_staff(self):
        self.assertEqual(self.patch(capacity=30).status_code, 200)
        self.assertEqual(list(Facility.objects.get(pk=self.facility.pk).staff.values_list("email", flat=True)),
                         ["first-staff@example.com"])
