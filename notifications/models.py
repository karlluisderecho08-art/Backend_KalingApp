from django.conf import settings
from django.db import models


class NotificationItem(models.Model):
    """
    An in-app notification, ported from the Kotlin NotificationItem
    (CODEBASE-1.md section 3). The Kotlin app writes these by hand from
    inside screen code (e.g. finalizeAppointment() calling
    addNotification() directly) -- here they're written server-side,
    from the one place a booking's status actually changes
    (milkbank/transitions.py), so nothing can change status without a
    notification also firing.
    """

    class Category(models.TextChoices):
        REMINDERS = "Reminders", "Reminders"
        ARTICLES = "Articles", "Articles"
        BOOKINGS = "Bookings", "Bookings"

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    title = models.CharField(max_length=255)
    description = models.TextField()
    category = models.CharField(max_length=20, choices=Category.choices)
    created_at = models.DateTimeField(auto_now_add=True)
    is_read = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.owner} - {self.title}"
