from rest_framework import serializers

from .models import DonorQuestionnaire, Facility, MilkBankRequest, TransactionRecord


class FacilitySerializer(serializers.ModelSerializer):
    class Meta:
        model = Facility
        fields = [
            "id", "name", "type", "contact", "address", "operating_hours",
            "donor_requirements", "recipient_requirements",
            "unavailable_donor_dates", "unavailable_recipient_dates",
            "is_operational", "capacity", "booked_count", "stock_level_ml",
            "latitude", "longitude",
        ]


class RankedFacilitySerializer(FacilitySerializer):
    """
    Same shape as FacilitySerializer, plus the two numbers Smart
    Allocation computed for this facility -- lets a caller (or a curious
    developer) see *why* a facility ranked where it did, instead of just
    trusting a single "allocated_facility_id".
    """

    booked_ratio = serializers.FloatField(read_only=True)
    distance_km = serializers.FloatField(read_only=True)

    class Meta(FacilitySerializer.Meta):
        fields = FacilitySerializer.Meta.fields + ["booked_ratio", "distance_km"]


class MilkBankRequestSerializer(serializers.ModelSerializer):
    """Read shape for a booking -- includes the derived `stages` list and
    the facility's name, so the client doesn't need a second lookup."""

    stages = serializers.ReadOnlyField()
    allocated_facility_name = serializers.CharField(source="allocated_facility.name", read_only=True)

    class Meta:
        model = MilkBankRequest
        fields = [
            "id", "request_type", "allocated_facility", "allocated_facility_name",
            "stages", "current_stage_index", "current_sub_status", "staff_message",
            "submitted_at", "preferred_date", "preferred_time", "attendance_confirmed",
            "counter_offer_date", "counter_offer_time",
        ]
        read_only_fields = [
            "allocated_facility", "current_stage_index", "current_sub_status",
            "staff_message", "submitted_at", "attendance_confirmed",
            "counter_offer_date", "counter_offer_time",
        ]


class MilkBankRequestCreateSerializer(serializers.Serializer):
    """
    Write shape for submitting a new booking. Not a ModelSerializer --
    `allocated_facility` isn't client input (Smart Allocation picks it,
    see views.MilkBankRequestCreateView), and `request_type` is validated
    against the same choices the model uses.
    """

    request_type = serializers.ChoiceField(choices=MilkBankRequest.RequestType.choices)
    preferred_date = serializers.DateField()
    preferred_time = serializers.CharField(max_length=20)


class ProposeCounterOfferSerializer(serializers.Serializer):
    counter_offer_date = serializers.DateField()
    counter_offer_time = serializers.CharField(max_length=20)


class RebookSerializer(serializers.Serializer):
    preferred_date = serializers.DateField()
    preferred_time = serializers.CharField(max_length=20)


class StaffMessageSerializer(serializers.Serializer):
    staff_message = serializers.CharField(required=False, allow_blank=True, default="")


class TransactionRecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = TransactionRecord
        fields = ["id", "type", "facility_name", "date", "status"]


class DonorQuestionnaireSerializer(serializers.ModelSerializer):
    """
    photo_attached tells the client whether a photo was uploaded without
    handing back any kind of path or URL to it -- fetching the actual
    bytes only ever happens through SerologyPhotoView, which re-checks
    permission on every request instead of trusting a link.
    """

    photo_attached = serializers.SerializerMethodField()

    class Meta:
        model = DonorQuestionnaire
        fields = [
            "id",
            "currently_lactating_excess", "infant_age_months", "consents_to_screening",
            "good_general_health", "being_treated_for_illness", "recent_fever_or_infection",
            "tested_positive_infectious_disease", "partner_tested_positive_or_at_risk",
            "recent_blood_transfusion", "recent_tattoo_piercing_needle_exposure", "travel_to_risk_area",
            "smokes_or_tobacco", "drinks_alcohol", "alcohol_frequency_details", "uses_illicit_drugs",
            "on_prescription_medications", "medication_list", "uses_herbal_supplements",
            "uses_radioactive_or_radiologic",
            "vegan_without_b12", "recent_live_virus_vaccine",
            "photo_attached", "submitted_at",
        ]

    def get_photo_attached(self, obj):
        return bool(obj.serology_photo)

    def validate_serology_photo(self, value):
        if value and not value.content_type.startswith("image/"):
            raise serializers.ValidationError("Serology photo must be an image file.")
        return value


class DonorQuestionnaireCreateSerializer(DonorQuestionnaireSerializer):
    serology_photo = serializers.FileField(required=False, allow_null=True)

    class Meta(DonorQuestionnaireSerializer.Meta):
        fields = [f for f in DonorQuestionnaireSerializer.Meta.fields if f != "photo_attached"] + ["serology_photo"]
