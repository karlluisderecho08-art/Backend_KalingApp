from django.utils import timezone
from rest_framework import generics, permissions
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from core.audit import log_action

from .models import User
from .serializers import LocationConsentSerializer, RegisterSerializer, UserSerializer


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


class MeView(generics.RetrieveAPIView):
    """GET /auth/me/ -- the profile of whoever the access token belongs to."""

    serializer_class = UserSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_object(self):
        return self.request.user


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
