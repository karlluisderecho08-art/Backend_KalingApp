from django.urls import path
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from .views import (
    DemoLoginView,
    LocationConsentView,
    MeView,
    RegisterView,
    StaffUserListView,
    StaffUserSetActiveView,
)

urlpatterns = [
    path("register/", RegisterView.as_view(), name="register"),
    # TokenObtainPairView is simplejwt's built-in "login": it checks
    # email+password (USERNAME_FIELD="email" on our model) and returns
    # {access, refresh}. We don't need to write login logic ourselves.
    path("login/", TokenObtainPairView.as_view(), name="login"),
    path("refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("demo-login/", DemoLoginView.as_view(), name="demo_login"),
    path("me/", MeView.as_view(), name="me"),
    path("location/", LocationConsentView.as_view(), name="location-consent"),
    path("users/", StaffUserListView.as_view(), name="staff-user-list"),
    path("users/<int:pk>/activate/", StaffUserSetActiveView.as_view(active=True), name="staff-user-activate"),
    path("users/<int:pk>/deactivate/", StaffUserSetActiveView.as_view(active=False), name="staff-user-deactivate"),
]
