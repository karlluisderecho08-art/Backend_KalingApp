from django.conf import settings
from django.db import models


class Conversation(models.Model):
    """
    The one private thread between a facility and the platform admins.

    Keyed by facility, not by staff account: a hospital with three staff
    logins shares one thread with the admins, the same way it shares one
    booking queue -- whoever is on shift sees what the others said. Every
    admin (is_staff) sees every facility's thread.

    The last-read times drive the unread badges; one per side, since "the
    admins have read it" and "the facility has read it" are separate facts.
    """

    facility = models.OneToOneField("milkbank.Facility", on_delete=models.CASCADE, related_name="conversation")
    created_at = models.DateTimeField(auto_now_add=True)
    # Bumped on every new message so the admin's inbox can sort by it
    # without aggregating over Message.
    last_message_at = models.DateTimeField(null=True, blank=True)
    admin_last_read_at = models.DateTimeField(null=True, blank=True)
    facility_last_read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-last_message_at"]

    def __str__(self):
        return f"Conversation with {self.facility}"


class Message(models.Model):
    class Side(models.TextChoices):
        FACILITY = "facility", "Facility"
        ADMIN = "admin", "Admin"

    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="messages")
    # Which side wrote it is stored on its own, not inferred from the
    # sender's current role: an account can be reassigned or deleted later
    # and the message must still read as having come from the side it did.
    side = models.CharField(max_length=10, choices=Side.choices)
    sender = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    body = models.TextField(max_length=2000)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"{self.side} message in {self.conversation_id}"


class BookingChat(models.Model):
    """
    The chat between a mother and the facility her booking is allocated to.

    One per booking, and it only exists while the booking is open. The
    moment the booking reaches a final status (completed, declined or
    expired) the whole chat is deleted for both sides --
    milkbank.transitions.apply_transition does that -- so finished
    bookings carry no chat history at all.

    Like Conversation, the facility side is the facility, not one staff
    account: whoever is on shift sees what the others said.
    """

    request = models.OneToOneField("milkbank.MilkBankRequest", on_delete=models.CASCADE, related_name="chat")
    created_at = models.DateTimeField(auto_now_add=True)
    last_message_at = models.DateTimeField(null=True, blank=True)
    mother_last_read_at = models.DateTimeField(null=True, blank=True)
    facility_last_read_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Chat for booking #{self.request_id}"


class BookingMessage(models.Model):
    class Side(models.TextChoices):
        MOTHER = "mother", "Mother"
        FACILITY = "facility", "Facility"

    chat = models.ForeignKey(BookingChat, on_delete=models.CASCADE, related_name="messages")
    side = models.CharField(max_length=10, choices=Side.choices)
    sender = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    body = models.TextField(max_length=2000)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"{self.side} message in booking chat {self.chat_id}"
