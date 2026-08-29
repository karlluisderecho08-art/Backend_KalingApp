from rest_framework import serializers

from .models import NotificationItem


class NotificationItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = NotificationItem
        fields = ["id", "title", "description", "category", "created_at", "is_read"]
