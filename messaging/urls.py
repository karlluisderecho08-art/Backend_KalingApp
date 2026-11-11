from django.urls import path

from .views import (
    AdminConversationListView,
    AdminThreadView,
    AdminUnreadView,
    BookingChatUnreadView,
    BookingChatView,
    FacilityThreadView,
    FacilityUnreadView,
)

urlpatterns = [
    path("facility/messages/", FacilityThreadView.as_view(), name="messaging-facility-thread"),
    path("facility/unread/", FacilityUnreadView.as_view(), name="messaging-facility-unread"),
    path("admin/conversations/", AdminConversationListView.as_view(), name="messaging-admin-conversations"),
    path("admin/conversations/<int:facility_id>/messages/", AdminThreadView.as_view(), name="messaging-admin-thread"),
    path("bookings/unread/", BookingChatUnreadView.as_view(), name="messaging-booking-unread"),
    path("bookings/<int:request_id>/messages/", BookingChatView.as_view(), name="messaging-booking-chat"),
    path("admin/unread/", AdminUnreadView.as_view(), name="messaging-admin-unread"),
]
