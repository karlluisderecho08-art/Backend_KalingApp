from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import generics, permissions
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from core.audit import log_action

from .models import User
from .serializers import (
    LocationConsentSerializer,
    RegisterSerializer,
    StaffUserListSerializer,
    UpdateProfileSerializer,
    UserSerializer,
)


class IsFacilityStaff(permissions.BasePermission):
    """
    Same check as milkbank.permissions.IsFacilityStaff -- duplicated
    (not imported) so accounts, the lower-level app, never has to
    depend on milkbank.
    """

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.role == User.Role.FACILITY_STAFF)


def _tokens_for(user):
    """Issue a fresh access/refresh token pair for a user."""
    refresh = RefreshToken.for_user(user)
    return {"access": str(refresh.access_token), "refresh": str(refresh)}


class RegisterView(generics.CreateAPIView):
    """
    POST /auth/register/  {email, password, mom_name, baby_name}

    Replaces RegisterScreen's `isLoggedIn = true` boolean flip: on success
    we hand back real JWTs, so the client is logged in the moment the
    account exists -- same UX, real auth underneath.
    """

    queryset = User.objects.all()
    serializer_class = RegisterSerializer
    permission_classes = [permissions.AllowAny]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response({
            "user": UserSerializer(user).data,
            **_tokens_for(user),
        }, status=201)


class DemoLoginView(APIView):
    """
    POST /auth/demo-login/

    Preserves the Kotlin WelcomeScreen's "Bypass / Quick-Access Demo Mode"
    button: no credentials, straight into the seeded "Rachel" account, for
    panel demos. The account itself is created by the seed_demo_user
    management command, not here -- this view only ever logs in.
    """

    permission_classes = [permissions.AllowAny]

    # responses=UserSerializer is an approximation -- the real response
    # also includes access/refresh tokens alongside the user fields, but
    # drf-spectacular needs a concrete serializer to document at all.
    @extend_schema(request=None, responses=UserSerializer)
    def post(self, request):
        try:
            user = User.objects.get(email="rachel@kalingapp.demo")
        except User.DoesNotExist:
            return Response(
                {"detail": "Demo account not seeded. Run: manage.py seed_demo_user"},
                status=500,
            )
        return Response({
            "user": UserSerializer(user).data,
            **_tokens_for(user),
        })


class MeView(generics.RetrieveUpdateAPIView):
    """
    GET /auth/me/ -- the profile of whoever the access token belongs to.
    PATCH/PUT /auth/me/ -- update her own mom/baby profile info (see
    UpdateProfileSerializer for exactly which fields). Responds with
    the full UserSerializer shape either way, so the client doesn't
    need a second GET after saving.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get_object(self):
        return self.request.user

    def get_serializer_class(self):
        return UpdateProfileSerializer if self.request.method in ("PUT", "PATCH") else UserSerializer

    def update(self, request, *args, **kwargs):
        super().update(request, *args, **kwargs)
        # Respond with the full read shape, not UpdateProfileSerializer's
        # narrower write shape -- the client's UserInfo parsing expects
        # email/role/etc. to always be present.
        return Response(UserSerializer(self.get_object()).data)


class StaffUserListView(generics.ListAPIView):
    """GET /auth/users/ -- every mother account, newest first, for the
    facility dashboard's User Management table."""

    serializer_class = StaffUserListSerializer
    permission_classes = [permissions.IsAuthenticated, IsFacilityStaff]

    def get_queryset(self):
        return User.objects.filter(role=User.Role.MOTHER).order_by("-date_joined")


class StaffUserSetActiveView(APIView):
    """
    POST /auth/users/<id>/activate/
    POST /auth/users/<id>/deactivate/

    Toggles Django's own is_active flag -- an inactive user's tokens
    still decode fine (JWTs aren't looked up in the DB per request),
    but simplejwt's default OutstandingToken check isn't enabled here,
    so this is enforced the usual Django way: every DRF view already
    requires IsAuthenticated, and Django's ModelBackend refuses to
    authenticate (and thus issue a new login) for is_active=False.
    Already-issued tokens keep working until they expire -- fine for a
    facility dashboard action, not a "lock this account out instantly"
    control.
    """

    permission_classes = [permissions.IsAuthenticated, IsFacilityStaff]
    # Set per-URL via as_view(active=True/False) -- see urls.py.
    active = None

    @extend_schema(request=None, responses=StaffUserListSerializer)
    def post(self, request, pk):
        user = generics.get_object_or_404(User, pk=pk, role=User.Role.MOTHER)
        user.is_active = self.active
        user.save(update_fields=["is_active"])
        log_action(request.user, "user.activated" if self.active else "user.deactivated", f"User:{user.id}")
        return Response(StaffUserListSerializer(user).data)


class LocationConsentView(APIView):
    """
    POST /auth/location/  {latitude, longitude, consent: true}

    The Phase 1 decision: location comes from device GPS, not a typed
    address (see roadmap gap #3). Chosen deliberately because Phase 3's
    Smart Allocation needs a real distance calculation -- but GPS is
    personal data under RA 10173, so storing it requires an explicit,
    logged consent event, not just a privacy-policy paragraph nobody
    reads. The audit log entry is that evidence trail.
    """

    permission_classes = [permissions.IsAuthenticated]
    serializer_class = LocationConsentSerializer

    def post(self, request):
        serializer = LocationConsentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        user = request.user
        user.latitude = data["latitude"]
        user.longitude = data["longitude"]
        user.location_consent_given = True
        user.location_consent_at = timezone.now()
        user.save(update_fields=[
            "latitude", "longitude", "location_consent_given", "location_consent_at",
        ])

        log_action(user, "location.consent_given", f"User:{user.id}")

        return Response(UserSerializer(user).data)
