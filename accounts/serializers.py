from rest_framework import serializers

from .models import User


class UserSerializer(serializers.ModelSerializer):
    """Read-only shape returned for 'who am I' / after login+register."""

    class Meta:
        model = User
        fields = [
            "id", "email", "role",
            "mom_name", "baby_name", "baby_age_weeks", "breastfeeding_status",
            "baby_birth_date", "pediatric_clinic", "tracking_streaks", "total_drawn_oz",
        ]


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
