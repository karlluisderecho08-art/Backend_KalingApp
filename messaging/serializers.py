import re

from rest_framework import serializers

from .models import Message

MAX_BODY = 2000

# Control characters other than tab and newline. Nothing legitimate types
# them, and some (e.g. U+202E, right-to-left override) can make a message
# display differently from what was actually sent.
_UNSAFE_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\u200e\u200f\u202a-\u202e\u2066-\u2069]")


class MessageSerializer(serializers.ModelSerializer):
    """
    `sender_name` depends on who is reading: admins see which facility
    staff member wrote a message, but a facility only ever sees "KalingApp
    Admin" for the admin side -- an admin's personal email address is not
    something a facility needs, and it is one less account to target.
    """

    sender_name = serializers.SerializerMethodField()

    class Meta:
        model = Message
        fields = ["id", "side", "body", "created_at", "sender_name"]

    def get_sender_name(self, obj):
        if obj.side == Message.Side.ADMIN and self.context.get("viewer_side") != Message.Side.ADMIN:
            return "KalingApp Admin"
        if obj.sender is None:
            return "Former staff member" if obj.side == Message.Side.FACILITY else "KalingApp Admin"
        return obj.sender.email


class SendMessageSerializer(serializers.Serializer):
    body = serializers.CharField(max_length=MAX_BODY, trim_whitespace=True)

    def validate_body(self, value):
        cleaned = _UNSAFE_CHARS.sub("", value).strip()
        if not cleaned:
            raise serializers.ValidationError("A message can't be empty.")
        return cleaned
