from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib import admin

from .models import User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    # DjangoUserAdmin's defaults assume a `username` field; we log in
    # with email instead, so the fieldsets/list need to say so.
    ordering = ("email",)
    list_display = ("email", "mom_name", "role", "is_staff")
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Profile", {
            "fields": (
                "role", "mom_name", "baby_name", "baby_age_weeks",
                "breastfeeding_status", "baby_birth_date", "pediatric_clinic",
                "tracking_streaks", "total_drawn_oz",
            ),
        }),
        ("Location", {
            "fields": ("latitude", "longitude", "location_consent_given", "location_consent_at"),
        }),
        ("Permissions", {
            "fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions"),
        }),
    )
    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            "fields": ("email", "password1", "password2"),
        }),
    )
