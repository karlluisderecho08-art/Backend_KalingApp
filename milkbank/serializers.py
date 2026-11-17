from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import URLValidator
from rest_framework import serializers

from accounts.models import User
from accounts.passwords import staff_password_problems
from core.audit import log_action

from .allocation import REQUEST_ML_CEILING
from .capacity import day_summary
from .closures import normalise_dates
from .collection import appointment_start, delivery_link_due
from .results import mother_result
from .scheduling import appointment_day_closed, can_mother_cancel, holiday_dates
from .models import BOTTLE_ML, DonorQuestionnaire, Facility, MilkBankRequest, TransactionRecord
from .photo_checks import MAX_PHOTO_BYTES, detect_image, random_photo_name, read_head


# How a mother's amount is checked on the way in: only that it is a plausible
# whole number of mL (or bottles). Whether it is within limits depends on the
# facility, and the facility isn't chosen until allocation runs -- which only
# picks one whose min_request_ml..max_request_ml holds it (allocation.py).
_REQUESTED_ML_ERRORS = {
    "min_value": "Enter an amount greater than 0 mL.",
    "max_value": f"The most any single request can be is {REQUEST_ML_CEILING} mL.",
    "invalid": "Enter a whole number of millilitres.",
}
_MAX_BOTTLES = REQUEST_ML_CEILING // BOTTLE_ML


def _resolve_bottles(attrs):
    """A request in bottles is stored as bottles x BOTTLE_ML. If both are
    sent they must agree; the bottle count wins as what she actually chose."""
    bottles = attrs.get("requested_bottles")
    if bottles:
        ml = attrs.get("requested_ml")
        if ml is not None and ml != bottles * BOTTLE_ML:
            raise serializers.ValidationError(
                {"requested_ml": f"{bottles} bottle(s) is {bottles * BOTTLE_ML} mL, not {ml} mL."}
            )
        attrs["requested_ml"] = bottles * BOTTLE_ML
    return attrs


DELIVERY_LINK_REQUIRED = "Paste the delivery booking link so the facility can track your courier."
DELIVERY_LINK_INVALID = "Enter a full web link that starts with https://"

PICKUP_PIN_REQUIRED = "Set your pickup pin: tap Use my current location where your milk will be picked up."
PICKUP_PIN_OUTSIDE = "That pin is outside the Philippines. Set it again where your milk will be picked up."
PICKUP_ADDRESS_REQUIRED = "Enter the complete address where your milk will be picked up."
PICKUP_LANDMARK_REQUIRED = "Add a landmark so the facility can find you."

# The first app version (versionCode) that asks for the pickup pin, address
# and landmark. Older installs have no such fields, so a Pick up from them is
# accepted without -- until APP_MIN_VERSION_CODE reaches this version, after
# which every Pick up must carry them (see collection_details_required()).
COLLECTION_DETAILS_FROM_VERSION = 6

# A loose box around the Philippines -- a pin outside it is a GPS glitch or a
# wrong place, never a real pickup for these facilities.
_PH_LAT = (4.0, 21.5)
_PH_LNG = (116.0, 127.0)


def collection_details_required(request):
    """Whether a Pick up from this request must carry its pin and address."""
    from django.conf import settings

    from core.versioning import client_version_code

    return (
        settings.APP_MIN_VERSION_CODE >= COLLECTION_DETAILS_FROM_VERSION
        or (request is not None and client_version_code(request) >= COLLECTION_DETAILS_FROM_VERSION)
    )


def _collection_fields():
    """The two Breastmilk Collection fields, shared by the create and update shapes."""
    return (
        serializers.ChoiceField(
            choices=MilkBankRequest.CollectionMethod.choices, required=False, allow_blank=True, default="",
        ),
        serializers.CharField(max_length=500, required=False, allow_blank=True, default="", trim_whitespace=True),
    )


def _pickup_fields():
    """The Pick up location fields, shared by the create and update shapes."""
    return (
        serializers.CharField(max_length=300, required=False, allow_blank=True, default="", trim_whitespace=True),
        serializers.CharField(max_length=200, required=False, allow_blank=True, default="", trim_whitespace=True),
        serializers.FloatField(required=False, allow_null=True, default=None, min_value=-90, max_value=90),
        serializers.FloatField(required=False, allow_null=True, default=None, min_value=-180, max_value=180),
    )


def _check_pickup(attrs, required):
    """
    A Pick up needs to say where: the pin from her phone, the complete address
    and a landmark -- the facility goes there to collect the milk. Any other
    method carries none of it (left over from switching away, it is cleared).
    `required` is False only for an installed app too old to ask for them.
    """
    if attrs.get("collection_method") != MilkBankRequest.CollectionMethod.PICKUP:
        attrs.update(pickup_address="", pickup_landmark="", pickup_latitude=None, pickup_longitude=None)
        return attrs
    lat, lng = attrs.get("pickup_latitude"), attrs.get("pickup_longitude")
    if (lat is None) != (lng is None):
        raise serializers.ValidationError({"pickup_latitude": PICKUP_PIN_REQUIRED})
    if lat is not None and not (_PH_LAT[0] <= lat <= _PH_LAT[1] and _PH_LNG[0] <= lng <= _PH_LNG[1]):
        raise serializers.ValidationError({"pickup_latitude": PICKUP_PIN_OUTSIDE})
    if required:
        if lat is None:
            raise serializers.ValidationError({"pickup_latitude": PICKUP_PIN_REQUIRED})
        if not attrs.get("pickup_address"):
            raise serializers.ValidationError({"pickup_address": PICKUP_ADDRESS_REQUIRED})
        if not attrs.get("pickup_landmark"):
            raise serializers.ValidationError({"pickup_landmark": PICKUP_LANDMARK_REQUIRED})
    return attrs


def _check_collection(attrs, link_due=False, pickup_required=False):
    """
    A Drop Off goes by courier, so it needs the courier's booking link -- but
    only from the appointment's hour onward (`link_due`, milkbank/collection.py):
    a courier can't be booked before the milk is ready to leave. Any other
    method carries no link (one left over from switching away from Drop Off is
    cleared rather than kept). Only http(s) links are accepted -- the facility
    dashboard opens this in a browser. A Pick up needs its location instead
    (_check_pickup).
    """
    _check_pickup(attrs, pickup_required)
    if attrs.get("collection_method") != MilkBankRequest.CollectionMethod.DROP_OFF:
        attrs["delivery_link"] = ""
        return attrs
    link = attrs.get("delivery_link", "")
    if not link:
        if link_due:
            raise serializers.ValidationError({"delivery_link": DELIVERY_LINK_REQUIRED})
        return attrs
    try:
        URLValidator(schemes=["http", "https"])(link)
    except DjangoValidationError:
        raise serializers.ValidationError({"delivery_link": DELIVERY_LINK_INVALID})
    return attrs


class AllocationRequestSerializer(serializers.Serializer):
    request_type = serializers.ChoiceField(choices=MilkBankRequest.RequestType.choices)
    # Optional on the preview so older clients keep working; when present the
    # preview only offers facilities that can actually fill it.
    requested_ml = serializers.IntegerField(
        required=False, allow_null=True, default=None,
        min_value=1, max_value=REQUEST_ML_CEILING, error_messages=_REQUESTED_ML_ERRORS,
    )
    requested_bottles = serializers.IntegerField(
        required=False, allow_null=True, default=None, min_value=1, max_value=_MAX_BOTTLES,
    )

    def validate(self, attrs):
        return _resolve_bottles(attrs)


class FacilitySerializer(serializers.ModelSerializer):
    """
    staff_user_id / new_staff_email / new_staff_password are write-only
    and not Facility columns -- see create() and update() below. Either one
    ADDS a staff login to the facility (an unassigned staff account, or a
    brand-new one); nothing ever removes or moves a login already assigned.
    On create this is the Add Facility modal's staff section; on update it
    is the same section in Edit Facility, for a facility that lost its only
    login or needs another.

    A facility is created with no staff account at all by default --
    both fields are optional, and leaving them out is the common case
    for every facility already seeded (see milkbank/management/commands/
    seed_facilities.py, none of which pass either).
    """

    # capacity is bookings PER DAY. These describe its calendar and are
    # computed from the bookings themselves (capacity.py), never stored:
    #   bookings_today      slots taken today
    #   upcoming_bookings   [{date, count}] for the next 10 weekdays
    #   fully_booked_dates  days with no slot left -- the mother's scheduler
    #                       and the dashboard's "Propose New Date" grey these
    #                       out, the same way they grey out unavailable dates
    #   booked_count        = bookings_today. The old single running total
    #                       this name used to mean is gone; kept so a
    #                       dashboard build that still reads it shows today's
    #                       figure rather than breaking.
    bookings_today = serializers.SerializerMethodField()
    upcoming_bookings = serializers.SerializerMethodField()
    fully_booked_dates = serializers.SerializerMethodField()
    booked_count = serializers.SerializerMethodField()
    # With limit_donor_slots on: {"yyyy-mm-dd": ["10:00 AM", ...]} -- the donor
    # appointment times already at donor_bookings_per_slot -- and the days on
    # which every time is. The donor's scheduler greys these out; recipients
    # are unaffected. Both empty when the facility doesn't limit donor slots.
    full_donor_slots = serializers.SerializerMethodField()
    donor_fully_booked_dates = serializers.SerializerMethodField()
    # Who can sign in to this facility's console, so the admin can see it
    # before adding another account.
    staff_emails = serializers.SerializerMethodField()
    # The Philippine holidays from today for a little over a year -- the
    # facilities are closed on them, so the mother's scheduler and the
    # console's "Propose New Date" grey them out (scheduling.holiday_dates).
    holiday_dates = serializers.SerializerMethodField()

    staff_user_id = serializers.IntegerField(write_only=True, required=False, allow_null=True)
    new_staff_email = serializers.EmailField(write_only=True, required=False)
    new_staff_password = serializers.CharField(write_only=True, required=False)

    class Meta:
        model = Facility
        fields = [
            "id", "name", "type", "contact", "address", "operating_hours",
            "donor_requirements", "recipient_requirements",
            "unavailable_donor_dates", "unavailable_recipient_dates",
            "is_operational", "capacity", "booked_count", "bookings_today",
            "upcoming_bookings", "fully_booked_dates", "stock_level_ml",
            "latitude", "longitude",
            "min_request_ml", "max_request_ml", "dispenses_bottles",
            "limit_donor_slots", "donor_bookings_per_slot",
            "full_donor_slots", "donor_fully_booked_dates", "staff_emails", "holiday_dates",
            "staff_user_id", "new_staff_email", "new_staff_password",
            "removed_at",
        ]
        # Set only by removing/restoring (milkbank/removal.py), never by an edit.
        read_only_fields = ["removed_at"]
        extra_kwargs = {
            "min_request_ml": {"min_value": 1, "max_value": REQUEST_ML_CEILING},
            "max_request_ml": {"min_value": 1, "max_value": REQUEST_ML_CEILING},
            "donor_bookings_per_slot": {"min_value": 1},
        }

    def _calendar(self, obj):
        # One query per facility for all three fields, cached on the instance
        # for the duration of this serialization.
        if not hasattr(obj, "_calendar_summary"):
            obj._calendar_summary = day_summary(obj) if obj.pk else {
                "bookings_today": 0, "upcoming_bookings": [], "fully_booked_dates": [],
                "full_donor_slots": {}, "donor_fully_booked_dates": [],
            }
        return obj._calendar_summary

    def get_bookings_today(self, obj) -> int:
        return self._calendar(obj)["bookings_today"]

    def get_booked_count(self, obj) -> int:
        return self._calendar(obj)["bookings_today"]

    def get_upcoming_bookings(self, obj) -> list:
        return self._calendar(obj)["upcoming_bookings"]

    def get_fully_booked_dates(self, obj) -> list:
        return self._calendar(obj)["fully_booked_dates"]

    def get_full_donor_slots(self, obj) -> dict:
        return self._calendar(obj)["full_donor_slots"]

    def get_donor_fully_booked_dates(self, obj) -> list:
        return self._calendar(obj)["donor_fully_booked_dates"]

    def get_holiday_dates(self, obj) -> list:
        return holiday_dates()

    def _closed_days(self, value):
        try:
            return normalise_dates(value)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc))

    def validate_unavailable_donor_dates(self, value):
        return self._closed_days(value)

    def validate_unavailable_recipient_dates(self, value):
        return self._closed_days(value)

    def _viewer_is_platform_admin(self):
        request = self.context.get("request")
        user = getattr(request, "user", None)
        return bool(user and user.is_authenticated and user.is_staff)

    def get_staff_emails(self, obj) -> list:
        if not obj.pk or not self._viewer_is_platform_admin():
            return []
        return sorted(obj.staff.filter(role=User.Role.FACILITY_STAFF).values_list("email", flat=True))

    def to_representation(self, instance):
        data = super().to_representation(instance)
        # Staff login addresses are for the platform admin's facility form
        # only. The facility list is public (the app's directory and matching
        # read it without signing in), and it used to hand everyone the
        # usernames of the accounts that open mothers' health records. Anyone
        # but an admin -- including a serializer used without a request --
        # gets no such key at all.
        if not self._viewer_is_platform_admin():
            data.pop("staff_emails", None)
        return data

    def _validate_request_limits(self, attrs):
        """The per-request range must make sense on its own, and -- for a
        facility that dispenses bottles -- leave room for at least one whole
        bottle, or a mother choosing bottles there could pick no count at all.
        On an edit, fields not being changed are read from the facility."""
        def current(field, default):
            if field in attrs:
                return attrs[field]
            return getattr(self.instance, field) if self.instance is not None else default

        low = current("min_request_ml", Facility._meta.get_field("min_request_ml").default)
        high = current("max_request_ml", Facility._meta.get_field("max_request_ml").default)
        bottles = current("dispenses_bottles", False)
        if low > high:
            raise serializers.ValidationError(
                {"max_request_ml": "The maximum per request can't be less than the minimum."}
            )
        if bottles and (high // BOTTLE_ML) * BOTTLE_ML < low:
            raise serializers.ValidationError({
                "dispenses_bottles": (
                    f"Bottles hold {BOTTLE_ML} mL, so the range {low}-{high} mL has no whole "
                    f"number of bottles in it. Widen the range to include a multiple of {BOTTLE_ML} mL."
                )
            })

    def _validate_donor_slot_limit(self, attrs):
        """A per-time-slot limit above the day's own capacity could never be
        reached -- the day would fill first -- so it is refused rather than
        saved as a setting that silently does nothing."""
        def current(field, default):
            if field in attrs:
                return attrs[field]
            return getattr(self.instance, field) if self.instance is not None else default

        if not current("limit_donor_slots", False):
            return
        per_slot = current("donor_bookings_per_slot", 3)
        capacity = current("capacity", 0)
        if capacity and per_slot > capacity:
            raise serializers.ValidationError({
                "donor_bookings_per_slot": (
                    f"Donor bookings per time slot ({per_slot}) can't be more than "
                    f"bookings per day ({capacity})."
                )
            })

    def validate(self, attrs):
        self._validate_request_limits(attrs)
        self._validate_donor_slot_limit(attrs)
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

        if new_staff_password:
            # A staff login opens mothers' health records: the staff password
            # rules (accounts/passwords.py), the same as when an admin
            # sets a new one later.
            problems = staff_password_problems(new_staff_password, User(email=new_staff_email or ""))
            if problems:
                raise serializers.ValidationError({"new_staff_password": problems})

        if new_staff_email and User.objects.filter(email__iexact=new_staff_email).exists():
            raise serializers.ValidationError(
                {"new_staff_email": "An account with this email already exists."}
            )

        return attrs

    def create(self, validated_data):
        staff = self._pop_staff(validated_data)
        facility = super().create(validated_data)
        self._assign_staff(facility, *staff)
        return facility

    def update(self, instance, validated_data):
        # Adding a staff account to an existing facility works the same way as
        # at creation (an unassigned staff account, or a brand-new one). It
        # only ever ADDS a login: nobody already assigned is removed or moved.
        staff = self._pop_staff(validated_data)
        facility = super().update(instance, validated_data)
        self._assign_staff(facility, *staff)
        return facility

    @staticmethod
    def _pop_staff(validated_data):
        return (
            validated_data.pop("staff_user_id", None),
            validated_data.pop("new_staff_email", None),
            validated_data.pop("new_staff_password", None),
        )

    def _assign_staff(self, facility, staff_user_id, new_staff_email, new_staff_password):
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
    # to also hand back to the owner themselves, it's their own info. Both ""
    # for a completed booking kept after its mother deleted her account
    # (owner_deleted, with former_owner_ref in place of who she was).
    owner_email = serializers.SerializerMethodField()
    owner_name = serializers.SerializerMethodField()
    owner_deleted = serializers.SerializerMethodField()
    # DONOR only (null for a RECIPIENT): whether her screening answers arrived.
    # They are sent right after the booking, and that can fail -- the app then
    # offers to send them again, and staff can't accept her without them.
    has_questionnaire = serializers.SerializerMethodField()
    # While it is open: whether the day it holds is now closed at her facility
    # (closures.py) -- staff need to propose a new date.
    appointment_day_closed = serializers.SerializerMethodField()
    # Whether she can cancel it herself from the app (scheduling.can_mother_cancel).
    can_cancel = serializers.SerializerMethodField()
    # How the request ended, in plain words for the mother -- null while it is
    # still in progress. See milkbank/results.py for why her app shows this
    # instead of staff_message.
    result = serializers.SerializerMethodField()

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
            "amount_ml", "completed_at", "requested_ml", "requested_bottles", "result",
            "closed_at", "representative_purged_at", "collection_method", "delivery_link",
            "pickup_address", "pickup_landmark", "pickup_latitude", "pickup_longitude",
            "appointment_start_ms", "delivery_link_due",
            "owner_deleted", "former_owner_ref", "has_questionnaire", "appointment_day_closed", "can_cancel",
        ]
        read_only_fields = [
            "allocated_facility", "current_stage_index", "current_sub_status",
            "staff_message", "decline_reason", "submitted_at", "attendance_confirmed",
            "counter_offer_date", "counter_offer_time", "response_deadline",
            "needs_representative", "representative_name",
            "representative_birthday", "representative_contact_number",
            "neonate_name", "clinic_info", "has_prescription_proof",
            "has_cooler", "has_medical_abstract", "requested_ml", "requested_bottles",
            "closed_at", "representative_purged_at", "collection_method", "delivery_link",
            "pickup_address", "pickup_landmark", "pickup_latitude", "pickup_longitude",
            "former_owner_ref",
        ]

    # When her appointment begins (epoch ms; null when its time can't be read),
    # and whether a Drop off's delivery link is wanted now -- the app shows the
    # link field from that hour on (milkbank/collection.py).
    appointment_start_ms = serializers.SerializerMethodField()
    delivery_link_due = serializers.SerializerMethodField()

    def get_appointment_start_ms(self, obj) -> int | None:
        start = appointment_start(obj)
        return int(start.timestamp() * 1000) if start else None

    def get_delivery_link_due(self, obj) -> bool:
        return delivery_link_due(obj)

    def get_result(self, obj):
        return mother_result(obj)

    def get_owner_email(self, obj) -> str:
        return obj.owner.email if obj.owner_id else ""

    def get_owner_name(self, obj) -> str:
        return (obj.owner.mom_name or "") if obj.owner_id else ""

    def get_owner_deleted(self, obj) -> bool:
        return obj.owner_id is None

    def get_has_questionnaire(self, obj) -> bool | None:
        if obj.request_type != MilkBankRequest.RequestType.DONOR:
            return None
        return hasattr(obj, "donor_questionnaire")

    def get_appointment_day_closed(self, obj) -> bool:
        if obj.current_sub_status not in _OPEN_FOR_CLOSING:
            return False
        return appointment_day_closed(obj)

    def get_can_cancel(self, obj) -> bool:
        return can_mother_cancel(obj)


_OPEN_FOR_CLOSING = (
    MilkBankRequest.Status.PENDING, MilkBankRequest.Status.AWAITING_ATTENDANCE,
    MilkBankRequest.Status.SCHEDULED, MilkBankRequest.Status.COUNTER_OFFERED,
)


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

    # RECIPIENT only, ignored for a DONOR (see validate()). Whole mL, like every
    # other volume in the system. Only a plausibility bound is checked here:
    # the real limits are the facility's own (min_request_ml / max_request_ml)
    # and "not more than it holds", and the facility isn't chosen yet --
    # allocation only picks one that satisfies all three.
    #
    # Optional rather than required, same as the requirements fields above: the
    # mobile app always sends it now, but an installed copy that predates it
    # must still be able to request milk. The facility dashboard shows such a
    # request as "No amount was specified".
    requested_ml = serializers.IntegerField(
        required=False, allow_null=True, default=None,
        min_value=1, max_value=REQUEST_ML_CEILING, error_messages=_REQUESTED_ML_ERRORS,
    )
    # Instead of requested_ml, when she asked in bottles -- only a facility
    # that dispenses_bottles will be chosen for such a request.
    requested_bottles = serializers.IntegerField(
        required=False, allow_null=True, default=None, min_value=1, max_value=_MAX_BOTTLES,
    )

    # DONOR only, ignored for a RECIPIENT. Optional here so an installed app
    # that predates the question can still book (the app itself requires a
    # choice); a Drop Off always needs its delivery link -- see _check_collection.
    collection_method, delivery_link = _collection_fields()
    # Pick up only: where (see _check_pickup).
    pickup_address, pickup_landmark, pickup_latitude, pickup_longitude = _pickup_fields()

    def validate(self, attrs):
        if attrs["request_type"] != MilkBankRequest.RequestType.RECIPIENT:
            attrs["requested_ml"] = None
            attrs["requested_bottles"] = None
        if attrs["request_type"] != MilkBankRequest.RequestType.DONOR:
            attrs["collection_method"] = ""
        _check_collection(attrs, pickup_required=self.context.get("pickup_required", False))
        return _resolve_bottles(attrs)


class CollectionMethodSerializer(serializers.Serializer):
    """What a donor sends to change how her milk reaches the facility while
    her booking is still open. Unlike on the form, a method is required."""

    collection_method, delivery_link = _collection_fields()
    pickup_address, pickup_landmark, pickup_latitude, pickup_longitude = _pickup_fields()

    def validate(self, attrs):
        if not attrs.get("collection_method"):
            raise serializers.ValidationError({"collection_method": "Choose how your milk will reach the facility."})
        return _check_collection(
            attrs,
            link_due=self.context.get("link_due", False),
            pickup_required=self.context.get("pickup_required", False),
        )


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
        # Judged by the file's own bytes, never by the type or name the phone
        # declared (see milkbank/photo_checks.py), then renamed at random so
        # what is stored -- and later served to staff -- is exactly that image.
        if not value:
            return value
        if value.size > MAX_PHOTO_BYTES:
            raise serializers.ValidationError("Serology photo must be 10 MB or smaller.")
        kind = detect_image(read_head(value))
        if kind is None:
            raise serializers.ValidationError("Serology photo must be an image file.")
        value.content_type, extension = kind
        value.name = random_photo_name(extension)
        return value


class DonorQuestionnaireCreateSerializer(DonorQuestionnaireSerializer):
    serology_photo = serializers.FileField(required=False, allow_null=True)

    class Meta(DonorQuestionnaireSerializer.Meta):
        fields = [f for f in DonorQuestionnaireSerializer.Meta.fields if f != "photo_attached"] + ["serology_photo"]

    def validate(self, attrs):
        # A donor who says she has a recent serology test attaches its photo:
        # the reminder before the form tells her she will, and staff need it
        # to review her. Answering No is still allowed -- the facility handles
        # testing in person. The app enforces the same rule before sending.
        if attrs.get("has_recent_serology_test") and not attrs.get("serology_photo"):
            raise serializers.ValidationError(
                {"serology_photo": "Please attach a photo of your serology test result."}
            )
        return attrs
