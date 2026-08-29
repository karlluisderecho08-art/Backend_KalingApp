from django.urls import path

from .views import MarkAllNotificationsReadView, MarkNotificationReadView, MyNotificationsView

urlpatterns = [
    path("mine/", MyNotificationsView.as_view(), name="notifications-mine"),
    path("<int:pk>/read/", MarkNotificationReadView.as_view(), name="notification-read"),
    path("read-all/", MarkAllNotificationsReadView.as_view(), name="notifications-read-all"),
]
