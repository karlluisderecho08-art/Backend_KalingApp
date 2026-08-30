from django.urls import path

from .views import AdminDashboardStatsView

urlpatterns = [
    path("stats/", AdminDashboardStatsView.as_view(), name="admin-dashboard-stats"),
]
