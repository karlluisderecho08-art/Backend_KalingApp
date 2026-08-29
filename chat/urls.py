from django.urls import path

from .views import ChatHistoryView, SendMessageView

urlpatterns = [
    path("message/", SendMessageView.as_view(), name="chat-message"),
    path("history/", ChatHistoryView.as_view(), name="chat-history"),
]
