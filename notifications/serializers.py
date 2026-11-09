from rest_framework import serializers

from .models import NotificationItem


class NotificationItemSerializer(serializers.ModelSerializer):
    # title/description go out in the language the app asked for (core/i18n.py
    # translates them on the way out); these two stay English, so the app can
    # keep deciding what to offer from the original wording
    # (NotificationGuidance.kt matches words like "declined").
    title_en = serializers.CharField(source="title", read_only=True)
    description_en = serializers.CharField(source="description", read_only=True)

    class Meta:
        model = NotificationItem
        fields = ["id", "title", "description", "title_en", "description_en", "category", "created_at", "is_read"]
