from rest_framework import generics, permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import NotificationItem
from .serializers import NotificationItemSerializer


class MyNotificationsView(generics.ListAPIView):
    """GET /notifications/mine/ -- the mother's own notification list."""

    serializer_class = NotificationItemSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return NotificationItem.objects.filter(owner=self.request.user)


class MarkNotificationReadView(generics.GenericAPIView):
    """POST /notifications/<id>/read/ -- owner-only."""

    queryset = NotificationItem.objects.all()
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        # Scoping the queryset to the owner (rather than a separate
        # permission class) means someone else's notification 404s
        # instead of 403ing -- doesn't even confirm it exists.
        return NotificationItem.objects.filter(owner=self.request.user)

    def post(self, request, pk):
        notification = self.get_object()
        notification.is_read = True
        notification.save(update_fields=["is_read"])
        return Response(NotificationItemSerializer(notification).data)


class MarkAllNotificationsReadView(APIView):
    """POST /notifications/read-all/"""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        NotificationItem.objects.filter(owner=request.user, is_read=False).update(is_read=True)
        return Response(status=204)
