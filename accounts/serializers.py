from rest_framework import serializers

from .models import User


class UserSerializer(serializers.ModelSerializer):
    """Read-only shape returned for 'who am I' / after login+register."""

    class Meta:
        model = User
        fields = [
            "id", "email", "role", "is_staff",
            "mom_name", "baby_name", "baby_age_weeks", "breastfeeding_status",
            "baby_birth_date", "pediatric_clinic", "tracking_streaks", "total_drawn_oz",
            "latitude", "longitude", "location_consent_given",
        ]
        # Nothing writes through this serializer today (only ever used in
        # read paths -- MeView is a RetrieveAPIView), but is_staff controls
        # Django admin/content-moderation access, so it's marked read-only
        # here too in case a write path is ever added later.
        read_only_fields = ["is_staff"]


class RegisterSerializer(serializers.ModelSerializer):
    # write_only: accepted on the way in, never echoed back in a response.
    password = serializers.CharField(write_only=True, min_length=8)

    class Meta:
        model = User
        fields = ["email", "password", "mom_name", "baby_name"]

    def create(self, validated_data):
        # Same default the Kotlin RegisterScreen applies today: an empty
        # baby name becomes "James", not a blank string in the DB.
        validated_data.setdefault("baby_name", "")
        if not validated_data["baby_name"]:
            validated_data["baby_name"] = "James"

        password = validated_data.pop("password")
        # create_user (not create()) is what actually hashes the password --
        # this is the whole reason a password field needs a manager method
        # instead of just being another column.
        user = User.objects.create_user(password=password, **validated_data)
        return user


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
