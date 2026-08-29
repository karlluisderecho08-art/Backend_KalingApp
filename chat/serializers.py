from rest_framework import serializers

from .models import ChatMessage


class ChatMessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ChatMessage
        fields = ["id", "text", "is_user", "is_system_notice", "created_at"]


class SendMessageSerializer(serializers.Serializer):
    text = serializers.CharField(max_length=2000)
