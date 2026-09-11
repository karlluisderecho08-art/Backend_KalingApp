from rest_framework import serializers

from .models import User


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
            "baby_birth_date", "pediatric_clinic", "tracking_streaks", "total_drawn_oz",
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
    account-editable here, and tracking_streaks/total_drawn_oz/
    latitude/longitude/location_consent_given are system-computed or
    consent-gated, not something a plain profile edit should overwrite.
    """

    class Meta:
        model = User
        fields = ["mom_name", "baby_name", "baby_age_weeks", "pediatric_clinic", "has_seen_walkthrough"]
        extra_kwargs = {field: {"required": False} for field in fields}


class RegisterSerializer(serializers.ModelSerializer):
    # write_only: accepted on the way in, never echoed back in a response.
    password = serializers.CharField(write_only=True, min_length=8)
    # Plain EmailField, not the model field: ModelSerializer would
    # otherwise auto-attach a UniqueValidator from User.email's
    # unique=True, which blocks re-registering an email that only ever
    # got as far as an abandoned/never-verified attempt (she lost the
    # code, the app crashed before she entered it, etc.) -- that email
    # would be permanently stuck, unable to ever register again, even
    # though nothing about it was ever actually confirmed. validate_email
    # below enforces the uniqueness that actually matters: no second
    # registration against an email that's already genuinely verified.
    email = serializers.EmailField()

    class Meta:
        model = User
        fields = ["email", "password", "mom_name", "baby_name"]

    def validate_email(self, value):
        value = value.strip().lower()
        if User.objects.filter(email__iexact=value, is_active=True).exists():
            raise serializers.ValidationError("An account with this email already exists.")
        return value

    def create(self, validated_data):
        # Same default the Kotlin RegisterScreen applies today: an empty
        # baby name becomes "James", not a blank string in the DB.
        validated_data.setdefault("baby_name", "")
        if not validated_data["baby_name"]:
            validated_data["baby_name"] = "James"

        # Clear out any stale, never-verified row for this email (see the
        # email field comment above) so this attempt can start fresh --
        # a fresh row means a fresh code/attempt-count too, not leftover
        # state from whatever went wrong the first time.
        User.objects.filter(email__iexact=validated_data["email"], is_active=False).delete()

        password = validated_data.pop("password")
        # create_user (not create()) is what actually hashes the password --
        # this is the whole reason a password field needs a manager method
        # instead of just being another column.
        user = User.objects.create_user(password=password, **validated_data)
        return user


class StaffUserListSerializer(serializers.ModelSerializer):
    """Read shape for the facility dashboard's User Management table --
    only the fields that actually exist on a mother's account (no phone,
    no city -- the model never captured either; see UserSerializer)."""

    class Meta:
        model = User
        fields = [
            "id", "email", "mom_name", "baby_name", "baby_age_weeks",
            "breastfeeding_status", "baby_birth_date", "pediatric_clinic",
            "tracking_streaks", "total_drawn_oz", "location_consent_given",
            "is_active", "date_joined",
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
