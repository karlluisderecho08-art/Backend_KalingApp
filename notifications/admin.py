from django.contrib import admin

from .models import NotificationItem


@admin.register(NotificationItem)
class NotificationItemAdmin(admin.ModelAdmin):
    list_display = ("owner", "title", "category", "is_read", "created_at")
    list_filter = ("category", "is_read")
