from django.urls import path

from .views import SupportContactListView

urlpatterns = [
    path("", SupportContactListView.as_view(), name="support-contact-list"),
]
