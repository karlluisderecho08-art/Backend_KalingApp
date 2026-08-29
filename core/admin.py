from django.contrib import admin

from .models import AuditLogEntry


@admin.register(AuditLogEntry)
class AuditLogEntryAdmin(admin.ModelAdmin):
    # Read-only on purpose -- an editable audit log defeats the point of
    # having one. No add/change/delete permission for anyone, including
    # superusers, through this screen.
    list_display = ("created_at", "actor", "action", "target")
    list_filter = ("action",)
    search_fields = ("target",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
