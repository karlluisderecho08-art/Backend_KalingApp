from rest_framework import serializers

from accounts.models import User
from core.audit import log_action

from .models import DonorQuestionnaire, Facility, MilkBankRequest, TransactionRecord


class AllocationRequestSerializer(serializers.Serializer):
    request_type = serializers.ChoiceField(choices=MilkBankRequest.RequestType.choices)


class FacilitySerializer(serializers.ModelSerializer):
    """
    staff_user_id / new_staff_email / new_staff_password are write-only
    and not Facility columns -- see create() below, which is the only
    place they do anything. Accepted (and silently ignored, same as any
    unknown attribute Django lets you set on an instance without saving
    it) on an update too, since ModelSerializer.update() isn't
    overridden here -- FacilityDetailView's PATCH/PUT was never meant to
    reassign staff, only FacilityListView's POST (a brand-new facility,
    from the admin dashboard's Add Facility modal).

    A facility is created with no staff account at all by default --
    both fields are optional, and leaving them out is the common case
    for every facility already seeded (see milkbank/management/commands/
    seed_facilities.py, none of which pass either).
    """

    staff_user_id = serializers.IntegerField(write_only=True, required=False, allow_null=True)
    new_staff_email = serializers.EmailField(write_only=True, required=False)
    new_staff_password = serializers.CharField(write_only=True, required=False, min_length=8)

    class Meta:
        model = Facility
        fields = [
            "id", "name", "type", "contact", "address", "operating_hours",
            "donor_requirements", "recipient_requirements",
            "unavailable_donor_dates", "unavailable_recipient_dates",
            "is_operational", "capacity", "booked_count", "stock_level_ml",
            "latitude", "longitude",
            "staff_user_id", "new_staff_email", "new_staff_password",
        ]

    def validate(self, attrs):
        staff_user_id = attrs.get("staff_user_id")
        new_staff_email = attrs.get("new_staff_email")
        new_staff_password = attrs.get("new_staff_password")

        if staff_user_id and new_staff_email:
            raise serializers.ValidationError(
                "Choose either an existing staff account or a new one, not both."
            )
        if bool(new_staff_email) != bool(new_staff_password):
            raise serializers.ValidationError(
                {"new_staff_email": "A new staff account needs both an email and a password."}
            )

        if staff_user_id:
            try:
                user = User.objects.get(pk=staff_user_id, role=User.Role.FACILITY_STAFF)
            except User.DoesNotExist:
                raise serializers.ValidationError(
                    {"staff_user_id": "No facility-staff account with that id."}
                )
            # Reassigning a staff member already running a different
            # facility is a real, disruptive action (that facility loses
            # its only logged-in account) and not what "assign a staff
            # account for the facility being created" asks for -- this
            # flow is for staff with nowhere to log in yet.
            if user.facility_id is not None:
                raise serializers.ValidationError(
                    {"staff_user_id": "That staff account is already assigned to a facility."}
                )

        if new_staff_email and User.objects.filter(email__iexact=new_staff_email).exists():
            raise serializers.ValidationError(
                {"new_staff_email": "An account with this email already exists."}
            )

        return attrs

    def create(self, validated_data):
        staff_user_id = validated_data.pop("staff_user_id", None)
        new_staff_email = validated_data.pop("new_staff_email", None)
        new_staff_password = validated_data.pop("new_staff_password", None)

        facility = super().create(validated_data)

        actor = self.context["request"].user if "request" in self.context else None
        if staff_user_id:
            User.objects.filter(pk=staff_user_id).update(facility=facility)
            log_action(actor, "facility.staff_assigned", f"User:{staff_user_id} -> Facility:{facility.id}")
        elif new_staff_email:
            new_user = User.objects.create_user(
                email=new_staff_email, password=new_staff_password,
                role=User.Role.FACILITY_STAFF, facility=facility, is_active=True,
            )
            log_action(
                actor, "facility.staff_created",
                f"User:{new_user.id} ({new_staff_email}) -> Facility:{facility.id}",
            )

        return facility


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

    stages = serializers.ListField(child=serializers.CharField(), read_only=True)
    allocated_facility_name = serializers.CharField(source="allocated_facility.name", read_only=True)
    # Owner contact, for the facility-staff list/detail views -- harmless
    # to also hand back to the owner themselves, it's their own info.
    owner_email = serializers.EmailField(source="owner.email", read_only=True)
    owner_name = serializers.CharField(source="owner.mom_name", read_only=True)

    class Meta:
        model = MilkBankRequest
        fields = [
            "id", "request_type", "allocated_facility", "allocated_facility_name",
            "stages", "current_stage_index", "current_sub_status", "staff_message", "decline_reason",
            "submitted_at", "preferred_date", "preferred_time", "attendance_confirmed",
            "counter_offer_date", "counter_offer_time", "owner_email", "owner_name",
            "response_deadline", "needs_representative", "representative_name",
            "representative_birthday", "representative_contact_number",
            "neonate_name", "clinic_info", "has_prescription_proof",
            "has_cooler", "has_medical_abstract",
            "amount_ml", "completed_at",
        ]
        read_only_fields = [
            "allocated_facility", "current_stage_index", "current_sub_status",
            "staff_message", "decline_reason", "submitted_at", "attendance_confirmed",
            "counter_offer_date", "counter_offer_time", "response_deadline",
            "needs_representative", "representative_name",
            "representative_birthday", "representative_contact_number",
            "neonate_name", "clinic_info", "has_prescription_proof",
            "has_cooler", "has_medical_abstract",
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

    # Optional pickup representative -- only meaningful when request_type is
    # RECIPIENT, but accepted unconditionally here and just ignored by the
    # view for a DONOR request rather than erroring on an unexpected field.
    needs_representative = serializers.BooleanField(required=False, default=False)
    representative_name = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")
    representative_birthday = serializers.DateField(required=False, allow_null=True, default=None)
    representative_contact_number = serializers.CharField(
        max_length=50, required=False, allow_blank=True, default=""
    )

    # Recipient requirements checklist -- same "accepted unconditionally,
    # ignored for a DONOR request" treatment as the representative fields
    # above. See MilkBankRequest.neonate_name for why these exist at all.
    neonate_name = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")
    clinic_info = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")
    has_prescription_proof = serializers.BooleanField(required=False, default=False)
    has_cooler = serializers.BooleanField(required=False, default=False)
    has_medical_abstract = serializers.BooleanField(required=False, default=False)


class ProposeCounterOfferSerializer(serializers.Serializer):
    """
    What staff send when offering a mother a different appointment slot.

    staff_message is optional at this layer but the facility dashboard
    always sends one, because a proposed date with no explanation reads
    as the facility moving her appointment for no reason. It lands in
    MilkBankRequest.staff_message, which the mobile app already shows in
    its "Message from Facility Team" card right above the accept/decline
    buttons for the counter-offer -- so this is the only thing that tells
    her a doctor was not available on the day she picked.
    """

    counter_offer_date = serializers.DateField()
    counter_offer_time = serializers.CharField(max_length=20)
    staff_message = serializers.CharField(required=False, allow_blank=True, default="")


class RebookSerializer(serializers.Serializer):
    preferred_date = serializers.DateField()
    preferred_time = serializers.CharField(max_length=20)


class StaffMessageSerializer(serializers.Serializer):
    staff_message = serializers.CharField(required=False, allow_blank=True, default="")


class DeclineSerializer(StaffMessageSerializer):
    """
    What staff send to decline a request, from the Booking Request desk or
    from any in-person phase. `reason` is the short label the statistics
    group by; staff_message is what the mother reads (the dashboard sends
    the reason followed by any notes). Optional so a client that only sends
    staff_message keeps working -- those declines count as "Not specified".
    """

    reason = serializers.CharField(required=False, allow_blank=True, max_length=100, default="")


class ConfirmCompletionSerializer(serializers.Serializer):
    """
    What staff records when closing out a Scheduled booking -- how many
    millilitres were actually drawn (DONOR) or dispensed (RECIPIENT). See
    milkbank.transitions.apply_transition for what this drives:
    Facility.stock_level_ml (added for a donor, subtracted for a
    recipient) and the donor's own total_drawn_ml.

    An integer field, not a float: this is the same unit the stock it
    moves is stored in, so there is no conversion to round and no reason
    to accept a fraction of a millilitre.
    """

    amount_ml = serializers.IntegerField(min_value=1)


class TransactionRecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = TransactionRecord
        fields = ["id", "type", "facility_name", "date", "status", "amount_ml"]


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
            "good_general_health", "lactating_with_excess_supply", "free_of_infectious_disease",
            "recent_transfusion_or_transplant", "uses_tobacco_alcohol_or_drugs",
            "on_medication_or_supplements", "medication_details", "has_recent_serology_test",
            "photo_attached", "submitted_at",
        ]

    def get_photo_attached(self, obj) -> bool:
        return bool(obj.serology_photo)

    def validate_serology_photo(self, value):
        if value and not value.content_type.startswith("image/"):
            raise serializers.ValidationError("Serology photo must be an image file.")
        return value


class DonorQuestionnaireCreateSerializer(DonorQuestionnaireSerializer):
    serology_photo = serializers.FileField(required=False, allow_null=True)

    class Meta(DonorQuestionnaireSerializer.Meta):
        fields = [f for f in DonorQuestionnaireSerializer.Meta.fields if f != "photo_attached"] + ["serology_photo"]
