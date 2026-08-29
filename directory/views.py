from rest_framework import generics, permissions

from .models import SupportContact
from .serializers import SupportContactSerializer


class SupportContactListView(generics.ListAPIView):
    """GET /directory/ -- replaces the hardcoded supportContacts list."""

    queryset = SupportContact.objects.all()
    serializer_class = SupportContactSerializer
    permission_classes = [permissions.AllowAny]
