"""
Admins can't delete one another: only the owner admin (the account Render's
ADMIN_EMAIL names -- accounts/owner.py) may delete another admin account, and
nobody may delete the owner admin.
"""

import os
from unittest.mock import patch

from rest_framework.test import APITestCase

from core.models import AuditLogEntry

from .models import User

OWNER = "owner-admin@example.com"


class AdminsCannotDeleteEachOtherTests(APITestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"ADMIN_EMAIL": "  Owner-Admin@Example.com "})
        env.start()
        self.addCleanup(env.stop)

        self.owner = User.objects.create_superuser(email=OWNER, password="x", role=User.Role.FACILITY_STAFF)
        self.admin = User.objects.create_superuser(email="admin@example.com", password="x", role=User.Role.FACILITY_STAFF)
        self.other_admin = User.objects.create_superuser(
            email="other-admin@example.com", password="x", role=User.Role.FACILITY_STAFF,
        )
        self.mother = User.objects.create_user(email="mother@example.com", password="x", is_active=True)
        self.staff = User.objects.create_user(
            email="staff@example.com", password="x", is_active=True, role=User.Role.FACILITY_STAFF,
        )

    def delete(self, actor, target):
        self.client.force_authenticate(user=actor)
        return self.client.delete(f"/auth/admin/users/{target.id}/")

    def exists(self, account):
        return User.objects.filter(pk=account.pk).exists()

    def test_an_admin_cannot_delete_another_admin(self):
        response = self.delete(self.admin, self.other_admin)

        self.assertEqual(response.status_code, 403)
        self.assertIn("owner admin", response.data["detail"])
        self.assertTrue(self.exists(self.other_admin))
        self.assertTrue(AuditLogEntry.objects.filter(
            action="user.delete_refused", actor=self.admin, target__startswith=f"User:{self.other_admin.id} ",
        ).exists())
        self.assertFalse(AuditLogEntry.objects.filter(action="user.deleted").exists())

    def test_nobody_can_delete_the_owner_admin(self):
        response = self.delete(self.admin, self.owner)

        self.assertEqual(response.status_code, 403)
        self.assertIn("can't be deleted", response.data["detail"])
        self.assertTrue(self.exists(self.owner))

    def test_the_owner_cannot_delete_itself_either(self):
        self.assertEqual(self.delete(self.owner, self.owner).status_code, 400)
        self.assertTrue(self.exists(self.owner))

    def test_the_owner_admin_can_retire_another_admin(self):
        response = self.delete(self.owner, self.other_admin)

        self.assertEqual(response.status_code, 204)
        self.assertFalse(self.exists(self.other_admin))
        self.assertTrue(AuditLogEntry.objects.filter(action="user.deleted", actor=self.owner).exists())

    def test_any_admin_can_still_delete_mothers_and_facility_staff(self):
        self.assertEqual(self.delete(self.admin, self.mother).status_code, 204)
        self.assertEqual(self.delete(self.admin, self.staff).status_code, 204)
        self.assertFalse(self.exists(self.mother) or self.exists(self.staff))

    def test_without_admin_email_no_admin_can_delete_an_admin(self):
        with patch.dict(os.environ, {"ADMIN_EMAIL": ""}):
            self.assertEqual(self.delete(self.owner, self.other_admin).status_code, 403)
            self.assertEqual(self.delete(self.admin, self.owner).status_code, 403)
        self.assertTrue(self.exists(self.other_admin) and self.exists(self.owner))

    def test_a_staff_flag_without_superuser_still_counts_as_an_admin(self):
        half_admin = User.objects.create_user(email="half@example.com", password="x", is_active=True, is_staff=True)
        self.assertEqual(self.delete(self.admin, half_admin).status_code, 403)
        self.assertTrue(self.exists(half_admin))

    def test_the_list_marks_only_the_owner(self):
        self.client.force_authenticate(user=self.admin)
        rows = {row["email"]: row for row in self.client.get("/auth/admin/users/").data}

        self.assertIs(rows[OWNER]["is_owner"], True)
        for email in ("admin@example.com", "other-admin@example.com", "mother@example.com", "staff@example.com"):
            self.assertIs(rows[email]["is_owner"], False)

    def test_a_mother_named_in_admin_email_is_not_an_owner(self):
        with patch.dict(os.environ, {"ADMIN_EMAIL": "mother@example.com"}):
            self.assertEqual(self.delete(self.mother, self.admin).status_code, 403)  # not an admin at all
            self.client.force_authenticate(user=self.admin)
            rows = {row["email"]: row for row in self.client.get("/auth/admin/users/").data}
        self.assertIs(rows["mother@example.com"]["is_owner"], False)
