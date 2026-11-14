from django.contrib.auth.hashers import make_password
from rest_framework import serializers

from . import lockout
from .legacy_signups import email_is_taken
from .models import PendingRegistration, User


class UserSerializer(serializers.ModelSerializer):
    """Read-only shape returned for 'who am I' / after login+register."""

    # A plain string, not a nested FacilitySerializer -- the Facility
    # dashboard just needs to show "you're viewing bookings for X" after
    # login, not the full facility record. None for a mother account, or
    # a facility_staff account not yet assigned one.
    facility_name = serializers.CharField(source="facility.name", read_only=True, default=None)

    class Meta:
        model = User
        fields = [
            "id", "email", "role", "is_staff", "facility", "facility_name",
            "mom_name", "baby_name", "baby_age_weeks", "breastfeeding_status",
            "baby_birth_date", "pediatric_clinic", "tracking_streaks", "total_drawn_ml",
            "total_received_ml",
            "latitude", "longitude", "location_consent_given", "has_seen_walkthrough",
        ]
        # Nothing writes through this serializer today (only ever used in
        # read paths -- MeView is a RetrieveAPIView), but is_staff and
        # facility both control real access (Django admin/moderation,
        # and now which facility's bookings this account can see), so
        # both are marked read-only here in case a write path is ever
        # added later -- facility assignment stays an admin-only action.
        read_only_fields = ["is_staff", "facility"]


class UpdateProfileSerializer(serializers.ModelSerializer):
    """
    Write shape for PATCH /auth/me/ -- the fields the Edit Profile
    screen actually lets a mother change about herself and her baby,
    plus has_seen_walkthrough (the app sends {"has_seen_walkthrough":
    true} alone, via the same PATCH, once she dismisses the onboarding
    tour -- a partial PATCH here only touches whichever fields it's
    given, not the rest). Deliberately narrower than UserSerializer's
    full read shape otherwise: email/role/is_staff aren't
    account-editable here, and tracking_streaks/total_drawn_ml/
    latitude/longitude/location_consent_given are system-computed or
    consent-gated, not something a plain profile edit should overwrite.
    """

    class Meta:
        model = User
        fields = ["mom_name", "baby_name", "baby_age_weeks", "pediatric_clinic", "has_seen_walkthrough"]
        extra_kwargs = {field: {"required": False} for field in fields}


class RegisterSerializer(serializers.Serializer):
    """
    Creates a PendingRegistration, NOT a User.

    Signing up no longer brings an account into existence -- that only
    happens once the emailed code comes back correct (see
    VerifyEmailView). A plain Serializer rather than a ModelSerializer
    for exactly that reason: there is no model instance being built
    from these fields in the usual one-to-one way.
    """

    email = serializers.EmailField()
    # write_only: accepted on the way in, never echoed back in a response.
    password = serializers.CharField(write_only=True, min_length=8)
    mom_name = serializers.CharField(required=False, allow_blank=True, max_length=150)
    baby_name = serializers.CharField(required=False, allow_blank=True, max_length=150)

    def validate_email(self, value):
        value = value.strip().lower()
        # Any existing account blocks the address -- active or deactivated.
        # A deactivated mother signing up again must not get a fresh account
        # (that would undo the deactivation and erase her history). The one
        # exception is an abandoned signup from before verification existed,
        # which nothing can verify any more; VerifyEmailView clears it when
        # the replacement account is created. See accounts/legacy_signups.py.
        if email_is_taken(value):
            raise serializers.ValidationError("An account with this email already exists.")
        return value

    def create(self, validated_data):
        email = validated_data["email"]
        # Same default the Kotlin RegisterScreen applies: an empty baby
        # name becomes "James", not a blank string.
        baby_name = validated_data.get("baby_name") or "James"

        # A previous attempt for this address may still be sitting here
        # unverified (she lost the code, the app closed before she
        # entered it, she typo'd and came back). Replacing it rather
        # than erroring means an abandoned attempt can never lock an
        # email address out permanently -- and the replacement carries a
        # fresh code and attempt count, not leftover state from whatever
        # went wrong the first time.
        PendingRegistration.objects.filter(email__iexact=email).delete()

        # No code or sent_at here on purpose -- send_verification_email()
        # owns those, and generating one in both places meant the row's
        # code could be replaced moments after this returned.
        return PendingRegistration.objects.create(
            email=email,
            # Hashed here, at the boundary -- the plaintext never reaches
            # the database, exactly as if this were a real account.
            password=make_password(validated_data["password"]),
            mom_name=validated_data.get("mom_name", ""),
            baby_name=baby_name,
        )


class StaffUserListSerializer(serializers.ModelSerializer):
    """Read shape for the facility dashboard's User Management table and
    reports -- only what those screens show (data minimisation, RA 10173):
    no baby details, clinic, streaks or location consent.

    total_drawn_ml / total_received_ml are what she donated and received
    at THIS facility only, from the annotations on
    accounts.views._mothers_of_my_facility -- never her lifetime totals,
    which would tell one hospital what she did at another. Only use this
    serializer on that queryset.
    """

    total_drawn_ml = serializers.IntegerField(source="facility_drawn_ml", read_only=True)
    total_received_ml = serializers.IntegerField(source="facility_received_ml", read_only=True)

    class Meta:
        model = User
        fields = [
            "id", "email", "mom_name",
            "total_drawn_ml", "total_received_ml",
            "is_active", "date_joined",
        ]


class AdminUserListSerializer(serializers.ModelSerializer):
    """
    Read shape for the admin dashboard's User Management table -- every
    account in the system, not just mothers like StaffUserListSerializer
    above. A platform admin is the only role meant to see across that
    whole boundary: who the facility-staff accounts actually are, which
    facility each is assigned to, and who else has platform-admin access.

    facility_name mirrors UserSerializer's own field -- None for a
    mother or an unassigned staff account, the facility's name otherwise.
    total_drawn_ml / total_received_ml are 0 for a staff or admin
    account (the fields exist on every row, but only a mother's booking
    history ever credits them) -- shown anyway rather than hidden
    per-role, so the table has one consistent shape to render.
    """

    facility_name = serializers.CharField(source="facility.name", read_only=True, default=None)
    # True only while the lock is still running -- locked_until alone would
    # stay set (in the past) after a lock has expired on its own.
    is_locked = serializers.SerializerMethodField()

    def get_is_locked(self, user) -> bool:
        return lockout.is_locked(user)

    class Meta:
        model = User
        fields = [
            "id", "email", "role", "is_staff", "is_superuser",
            "facility", "facility_name",
            "mom_name", "baby_name", "baby_age_weeks",
            "total_drawn_ml", "total_received_ml", "location_consent_given",
            "is_active", "date_joined", "last_login",
            "failed_login_attempts", "locked_until", "is_locked",
        ]


class LocationConsentSerializer(serializers.Serializer):
    """
    Not a ModelSerializer -- this isn't "edit some User fields," it's
    "record one consent event." `consent` must be sent and be true, or
    we refuse to store coordinates at all (RA 10173: no GPS storage
    without an explicit yes).
    """

    latitude = serializers.FloatField()
    longitude = serializers.FloatField()
    consent = serializers.BooleanField()

    def validate_consent(self, value):
        if not value:
            raise serializers.ValidationError("Location cannot be stored without consent.")
        return value
