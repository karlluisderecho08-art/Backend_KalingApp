import secrets

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import URLValidator
from django.db import transaction
from django.db.models import ProtectedError
from django.http import FileResponse, Http404
from django.utils import timezone
from drf_spectacular.utils import OpenApiTypes, extend_schema
from rest_framework import generics, permissions, status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import User
from core.audit import log_action
from core.retention import maybe_purge, purge_expired_data
from core.versioning import RequiresSupportedApp
from notifications.models import NotificationItem
from notifications.services import notify, notify_many

from . import closures
from .allocation import (
    AllocationError,
    DayFullyBooked,
    AmountOutsideLimits,
    FacilityClosedOnDay,
    InsufficientStock,
    LocationRequired,
    NoOperationalFacility,
    TimeSlotFull,
    claim_slot,
    closed_on,
    get_ranked_facilities,
)
from .business_hours import add_business_hours
from .capacity import has_donor_slot_room, has_room
from .models import DonorQuestionnaire, Facility, MilkBankRequest, TransactionRecord
from .permissions import IsFacilityStaff, IsRequestOwner
from .photo_checks import MAX_PHOTO_BYTES, detect_image, read_head
from .removal import remove_facility, removal_summary, restore_facility
from .collection import confirms_by_delivery_link, delivery_link_due, notify_collection_steps
from .results import decline_notification
from .scheduling import (
    NO_FACILITY_OPEN, PAST_SLOT, appointment_day_closed, appointment_label, appointment_still_ahead,
    booking_slot_problem, can_mother_cancel, closed_reason, counter_offer_errors, day_label, held_day,
    held_time, holiday_dates, slot_has_started,
)
from .serializers import (
    AllocationRequestSerializer,
    CollectionMethodSerializer,
    ConfirmCompletionSerializer,
    DeclineSerializer,
    DonorQuestionnaireCreateSerializer,
    DonorQuestionnaireSerializer,
    FacilitySerializer,
    MilkBankRequestCreateSerializer,
    MilkBankRequestSerializer,
    ProposeCounterOfferSerializer,
    RankedFacilitySerializer,
    RebookSerializer,
    StaffMessageSerializer,
    TransactionRecordSerializer,
    DELIVERY_LINK_INVALID,
    DELIVERY_LINK_REQUIRED,
    collection_details_required,
)
from .transitions import (
    FINAL_STATUSES, STAGE_CHANGED, InvalidTransition, advance_stage, apply_transition, refresh_attendance_deadline,
    sweep_expired_requests,
)

Status = MilkBankRequest.Status
Method = MilkBankRequest.CollectionMethod

# Room for the questionnaire's other form fields around the photo itself.
UPLOAD_FORM_OVERHEAD_BYTES = 256 * 1024

# A mother picking a time that has already begun (scheduling.slot_has_started).
PAST_SLOT_DETAIL = PAST_SLOT

# What a mother is told when the facility has closed the day she holds.
DAY_CLOSED_FOR_HER = (
    "{facility} is closed on your appointment date. The facility will propose a new date for you, "
    "or you can cancel your booking."
)
APPOINTMENT_PASSED_FOR_HER = "Your appointment time has passed, so it can no longer be confirmed."
OFFER_DAY_CLOSED = "The facility is now closed on the date it offered. Please choose a new time instead."
CANCEL_AFTER_COLLECTION = (
    "Your milk has already been collected, so this booking can't be cancelled. "
    "Message the facility in Chat if something has changed."
)


def _slot_problem_response(problem, day=None):
    """A 400 for booking_slot_problem()'s (field, message, code)."""
    field, message, code = problem
    body = {field: [message], "detail": message, "code": code}
    if code == "day_closed" and day is not None:
        body["closed_date"] = day.isoformat()
    return Response(body, status=400)


def _staff_at(facility):
    return User.objects.filter(facility=facility, role=User.Role.FACILITY_STAFF, is_active=True)


def _confirmed(request):
    """Whether a closing-days save was sent again to confirm it (closures.py)."""
    value = request.data.get("confirm_closing")
    return value is True or str(value).lower() in ("true", "1")


class FacilityListView(generics.ListCreateAPIView):
    """
    GET  /milkbank/facilities/ -- plain facility list, e.g. for the scheduler screen. Public.
    POST /milkbank/facilities/ -- add a new facility. Platform-admin only
    (is_staff, not the facility_staff role -- this is the "who runs
    KalingApp" panel, not a single facility's own staff).

    Facilities removed from KalingApp (milkbank/removal.py) are left out.
    GET ?removed=1 lists only those instead -- for platform admins (the admin
    portal's "Removed facilities"); for anyone else it lists nothing.
    """

    serializer_class = FacilitySerializer

    def get_queryset(self):
        if self.request.query_params.get("removed") == "1":
            if not (self.request.user and self.request.user.is_staff):
                return Facility.objects.none()
            return Facility.objects.filter(removed_at__isnull=False).order_by("-removed_at")
        return Facility.objects.filter(removed_at__isnull=True)

    def get_permissions(self):
        if self.request.method == "POST":
            return [permissions.IsAdminUser()]
        return [permissions.AllowAny()]


class FacilityDetailView(generics.RetrieveUpdateDestroyAPIView):
    """
    GET is public; PUT/PATCH/DELETE are platform-admin only (see FacilityListView).

    An edit that closes days with bookings on them, or switches off a
    facility that has bookings in progress, is answered 409 with what it
    would affect until it is sent again with `confirm_closing: true`; then
    the mothers and staff concerned are told (milkbank/closures.py).
    """

    queryset = Facility.objects.all()
    serializer_class = FacilitySerializer

    def get_permissions(self):
        if self.request.method == "GET":
            return [permissions.AllowAny()]
        return [permissions.IsAdminUser()]

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)
        with transaction.atomic():
            # Locked, so the closed days compared against are the saved ones
            # (staff may be changing them from the console at the same time).
            facility = Facility.objects.select_for_update().get(pk=self.get_object().pk)
            serializer = self.get_serializer(facility, data=request.data, partial=partial)
            serializer.is_valid(raise_exception=True)
            data = serializer.validated_data
            closing = closures.plan(
                facility,
                donor_dates=data.get("unavailable_donor_dates"),
                recipient_dates=data.get("unavailable_recipient_dates"),
                is_operational=data.get("is_operational"),
            )
            if closing.needs_confirmation and not _confirmed(request):
                return Response(closing.confirmation(), status=409)
            serializer.save()
            closures.carry_out(closing, actor=request.user, by_admin=True)
        return Response(serializer.data)

    def destroy(self, request, *args, **kwargs):
        facility = self.get_object()
        target = f"Facility:{facility.id} ({facility.name})"
        try:
            facility.delete()
        except ProtectedError:
            # MilkBankRequest.allocated_facility is PROTECT on purpose: a
            # booking's history must not lose the facility it happened at.
            # That used to surface as a bare 500, which the dashboard could
            # only report as "could not delete" while the facility stayed put.
            count = facility.requests.count()
            return Response({
                "detail": (
                    f"{facility.name} has {count} booking{'' if count == 1 else 's'} on record, so it "
                    "can't be erased. Remove it from KalingApp instead: its booking history is kept."
                ),
                "code": "has_bookings",
            }, status=400)
        log_action(request.user, "facility.deleted", target)
        return Response(status=204)


class MyFacilityClosedDaysView(APIView):
    """
    GET  /milkbank/facilities/mine/closed-days/
         -> {facility_id, facility_name, unavailable_donor_dates,
             unavailable_recipient_dates, holiday_dates, bookings_on_closed_days}
    POST /milkbank/facilities/mine/closed-days/
         {start, end?, close_for: "both" | "donors" | "recipients" | "none", confirm_closing?}

    A facility's own staff closing (or reopening) days at their facility --
    a doctor on leave, a lab out of service -- from the console's Closed Days
    page. Every weekday from `start` to `end` (inclusive, `end` defaulting to
    `start`) is set to `close_for`; "none" opens them again. Weekends and
    holidays are always closed and are skipped.

    Closing days that bookings fall on is answered 409 with those bookings
    until it is sent again with `confirm_closing: true`; then each mother and
    the other staff are told (milkbank/closures.py). The answer lists every
    booking still on a closed day, so the page can show what needs a new date.
    """

    permission_classes = [permissions.IsAuthenticated, IsFacilityStaff]

    def _facility(self, request, lock=False):
        if not request.user.facility_id:
            raise Http404
        queryset = Facility.objects.select_for_update() if lock else Facility.objects
        return generics.get_object_or_404(queryset, pk=request.user.facility_id, removed_at__isnull=True)

    @staticmethod
    def _payload(facility):
        on_closed = [
            req for req in MilkBankRequest.objects.filter(
                allocated_facility=facility, current_sub_status__in=closures.OPEN_STATUSES,
            ).select_related("owner", "allocated_facility")
            if appointment_still_ahead(req) and appointment_day_closed(req)
        ]
        on_closed.sort(key=lambda r: (held_day(r), r.id))
        return {
            "facility_id": facility.id,
            "facility_name": facility.name,
            "unavailable_donor_dates": sorted(facility.unavailable_donor_dates or []),
            "unavailable_recipient_dates": sorted(facility.unavailable_recipient_dates or []),
            "holiday_dates": holiday_dates(),
            "bookings_on_closed_days": [closures.booking_summary(req) for req in on_closed],
        }

    def get(self, request):
        return Response(self._payload(self._facility(request)))

    @extend_schema(request=None)
    def post(self, request):
        from datetime import date as _date, timedelta as _timedelta

        close_for = request.data.get("close_for")
        if close_for not in ("both", "donors", "recipients", "none"):
            return Response({"close_for": ["Choose who the day is closed for."]}, status=400)
        try:
            start = _date.fromisoformat(str(request.data.get("start") or ""))
            end = _date.fromisoformat(str(request.data.get("end") or start.isoformat()))
        except ValueError:
            return Response({"start": ["Choose a date."], "detail": "Choose a date."}, status=400)
        if end < start:
            return Response({"end": ["The last day can't be before the first."],
                             "detail": "The last day can't be before the first."}, status=400)
        if start < closures.today():
            return Response({"start": ["Choose today or a later date."],
                             "detail": "Choose today or a later date."}, status=400)
        if end - start >= _timedelta(days=closures.MAX_RANGE_DAYS):
            message = f"Close at most {closures.MAX_RANGE_DAYS} days at a time."
            return Response({"end": [message], "detail": message}, status=400)

        with transaction.atomic():
            facility = self._facility(request, lock=True)
            donor, recipient = closures.set_days(facility, start, end, close_for)
            closing = closures.plan(facility, donor_dates=donor, recipient_dates=recipient)
            if closing.needs_confirmation and not _confirmed(request):
                return Response(closing.confirmation(), status=409)
            facility.unavailable_donor_dates = donor
            facility.unavailable_recipient_dates = recipient
            facility.save(update_fields=["unavailable_donor_dates", "unavailable_recipient_dates"])
            if close_for == "none":
                log_action(
                    request.user, "facility.days_reopened",
                    f"Facility:{facility.id} -- {start.isoformat()} to {end.isoformat()}"[:255],
                )
            closures.carry_out(closing, actor=request.user, by_admin=False)
        return Response(self._payload(facility))


class FacilityRemovalView(APIView):
    """
    GET  /milkbank/facilities/<id>/remove/ -- what removing it would do
         {bookings_on_record, open_bookings, staff_logins, removed}
    POST /milkbank/facilities/<id>/remove/ -- remove it from KalingApp
         -> {detail, cancelled_bookings, staff_logins_switched_off}

    Platform-admin only. For a facility with bookings on record, which can't
    be deleted outright -- see milkbank/removal.py for exactly what removing
    does. The admin portal asks GET first, then deletes (no bookings) or
    removes (bookings) with one confirmation either way.
    """

    permission_classes = [permissions.IsAdminUser]

    def get(self, request, pk):
        facility = generics.get_object_or_404(Facility, pk=pk)
        return Response(removal_summary(facility))

    @extend_schema(request=None)
    def post(self, request, pk):
        facility = generics.get_object_or_404(Facility, pk=pk)
        result = remove_facility(facility, request.user)
        cancelled = result["cancelled_bookings"]
        return Response({
            "detail": (
                f"{facility.name} was removed from KalingApp. Its booking history is kept"
                + (f"; {cancelled} booking{'' if cancelled == 1 else 's'} in progress "
                   f"{'was' if cancelled == 1 else 'were'} cancelled and the mothers were told." if cancelled else ".")
            ),
            **result,
        })


class FacilityRestoreView(APIView):
    """
    POST /milkbank/facilities/<id>/restore/ -- bring a removed facility back
         -> {detail, staff_logins_switched_on, facility}

    Platform-admin only. It comes back NOT operational: the admin switches
    that on once it is ready to take bookings again.
    """

    permission_classes = [permissions.IsAdminUser]

    @extend_schema(request=None)
    def post(self, request, pk):
        facility = generics.get_object_or_404(Facility, pk=pk)
        result = restore_facility(facility, request.user)
        facility.refresh_from_db()
        return Response({
            "detail": f"{facility.name} is back on KalingApp. Switch on \"operational\" when it can take bookings.",
            **result,
            "facility": FacilitySerializer(facility, context={"request": request}).data,
        })


def _allocation_error_response(exc):
    if isinstance(exc, LocationRequired):
        return Response({"detail": "Location required. Call POST /auth/location/ first."}, status=400)
    if isinstance(exc, NoOperationalFacility):
        # Deliberately one message for every cause (closed, no capacity
        # configured, fully booked, or -- recipients only -- not enough
        # stock on hand). "Fully booked" became reachable when the
        # capacity gate landed in get_ranked_facilities; the wording
        # covers it without naming which internal filter rejected her,
        # which is not something a mother can act on anyway.
        return Response(
            {"detail": "No facility is available right now — they're either closed or fully booked. Please try again later."},
            status=404,
        )
    if isinstance(exc, FacilityClosedOnDay):
        # Every facility that could take her has closed that day.
        return Response({
            "detail": NO_FACILITY_OPEN.format(date=day_label(exc.day)),
            "closed_date": exc.day.isoformat(),
            "code": "day_closed",
        }, status=409)
    if isinstance(exc, DayFullyBooked):
        # Unlike the catch-all above, this one she can act on: pick another day.
        return Response({
            "detail": (
                f"Every milk bank is fully booked on {exc.day:%B} {exc.day.day}, {exc.day.year}. "
                "Please choose another date."
            ),
            "fully_booked_date": exc.day.isoformat(),
        }, status=409)
    if isinstance(exc, TimeSlotFull):
        # She can act on it: another time that day, or another day.
        return Response({
            "detail": (
                f"{exc.time} on {exc.day:%B} {exc.day.day}, {exc.day.year} is fully booked for donors. "
                "Please choose another time."
            ),
            "full_slot": {"date": exc.day.isoformat(), "time": exc.time},
        }, status=409)
    if isinstance(exc, AmountOutsideLimits):
        return Response({
            "detail": (
                f"No milk bank available right now accepts a request of {exc.requested_ml} mL. "
                "Please check the amounts shown for your milk bank and try again."
            ),
        }, status=400)
    if isinstance(exc, InsufficientStock):
        return Response({
            "detail": (
                f"No milk bank currently holds that much milk. The most available right now is "
                f"{exc.max_available_ml} mL — please request that amount or less."
            ),
            "max_available_ml": exc.max_available_ml,
        }, status=400)
    raise exc  # pragma: no cover -- only the subclasses above exist today


def _amount_label(data):
    """How she asked: "3 bottles (300 mL)" or "250 mL"."""
    bottles = data.get("requested_bottles")
    if bottles:
        return f"{bottles} bottle{'s' if bottles != 1 else ''} ({data['requested_ml']} mL)"
    return f"{data['requested_ml']} mL"


class SmartAllocationView(APIView):
    """
    POST /milkbank/allocate/  {"request_type": "DONOR" | "RECIPIENT"}

    A *preview* -- shows what Smart Allocation would pick, without
    creating a booking. Useful for the client to show "you'll likely be
    matched with X" before the mother commits. The real booking is
    created by MilkBankRequestCreateView below, which runs the same
    allocation for real.
    """

    permission_classes = [permissions.IsAuthenticated, RequiresSupportedApp]
    serializer_class = AllocationRequestSerializer

    def post(self, request):
        serializer = AllocationRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        request_type = serializer.validated_data["request_type"]
        is_recipient = request_type == "RECIPIENT"
        requested_ml = serializer.validated_data["requested_ml"] if is_recipient else None
        bottles = is_recipient and bool(serializer.validated_data["requested_bottles"])

        try:
            ranked = get_ranked_facilities(request.user, request_type, requested_ml, bottles=bottles)
        except AllocationError as exc:
            return _allocation_error_response(exc)

        return Response({
            "request_type": request_type,
            "allocated_facility_id": ranked[0].id,
            "ranked_facilities": RankedFacilitySerializer(ranked, many=True).data,
        })


class MilkBankRequestCreateView(APIView):
    """
    POST /milkbank/requests/  {request_type, preferred_date, preferred_time}

    The real version of submitRecipientForm()/submitDonorRequest() +
    finalizeAppointment() combined -- the Kotlin app splits "fill out
    the form" and "pick a date" across two screens, but the backend only
    needs the final result. Runs Smart Allocation for real and creates
    the booking in one step.
    """

    permission_classes = [permissions.IsAuthenticated, RequiresSupportedApp]
    serializer_class = MilkBankRequestCreateSerializer

    def post(self, request):
        serializer = MilkBankRequestCreateSerializer(
            data=request.data, context={"pickup_required": collection_details_required(request)},
        )
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        # The app's calendar only offers real slot times still ahead, on days
        # the milk banks are open; this is what actually refuses anything else
        # (a form left open past its hour, an older app that doesn't grey out
        # holidays, a direct call). A day her matched facility alone has
        # closed is allocation's job: she can still go to another one.
        problem = booking_slot_problem(data["preferred_date"], data["preferred_time"])
        if problem:
            return _slot_problem_response(problem, data["preferred_date"])

        # One open request per user (roadmap Phase 4 concurrency
        # decision: simplest option, matches the Kotlin app's single
        # `currentMilkBankRequest`). Enforced here, not as a DB
        # constraint, so the error message can actually explain why.
        open_statuses = [Status.PENDING, Status.AWAITING_ATTENDANCE, Status.SCHEDULED, Status.COUNTER_OFFERED]
        if MilkBankRequest.objects.filter(owner=request.user, current_sub_status__in=open_statuses).exists():
            return Response({"detail": "You already have an open request."}, status=400)

        bottles = bool(data["requested_bottles"])
        try:
            ranked = get_ranked_facilities(
                request.user, data["request_type"], data["requested_ml"], day=data["preferred_date"],
                bottles=bottles, time=data["preferred_time"],
            )
        except AllocationError as exc:
            return _allocation_error_response(exc)

        # Everything from taking the slot to telling people about it is one
        # transaction: if creating the booking fails after the slot was
        # claimed, the claim is rolled back with it, instead of leaving a
        # facility permanently one slot short for a booking that never
        # existed.
        with transaction.atomic():
            # Her account row is locked until this booking is made, and the
            # one-open-request rule asked again under that lock. A double tap on
            # Book sends two requests that now run side by side
            # (gunicorn.conf.py): both pass the check above before either has
            # booked. With the lock the second waits, then finds the first
            # booking here and is refused -- one booking, not two.
            User.objects.select_for_update().filter(pk=request.user.pk).first()
            if MilkBankRequest.objects.filter(owner=request.user, current_sub_status__in=open_statuses).exists():
                return Response({"detail": "You already have an open request."}, status=400)

            # The ranking above was read a moment ago, and "a moment" is
            # long enough for another mother to take the last slot of her
            # day. So the slot is not assumed -- it is claimed: the
            # facility's row is locked and her day re-counted (see
            # allocation.claim_slot), and the booking created below, still
            # inside this transaction, is what occupies the slot (a pending
            # request already holds its day -- see capacity.py).
            #
            # If her nearest facility filled up in that instant she is not
            # turned away: she goes to the next one in her own ranking,
            # which is exactly the facility Smart Allocation would have
            # chosen had she asked a second later. Only when nothing in
            # the list has room left is the booking refused, with the same
            # answer she would have got if they had all been full to
            # begin with.
            facility = next(
                (
                    candidate for candidate in ranked
                    if claim_slot(
                        candidate, data["request_type"], data["preferred_date"], data["requested_ml"], bottles,
                        time=data["preferred_time"],
                    )
                ),
                None,
            )
            if facility is None:
                # Every one of them had closed the day by now, or filled it.
                if all(closed_on(f, data["request_type"], data["preferred_date"]) for f in Facility.objects.filter(
                    pk__in=[candidate.pk for candidate in ranked],
                )):
                    return _allocation_error_response(FacilityClosedOnDay(data["preferred_date"]))
                return _allocation_error_response(DayFullyBooked(data["preferred_date"]))

            req = MilkBankRequest.objects.create(
                owner=request.user,
                request_type=data["request_type"],
                allocated_facility=facility,
                preferred_date=data["preferred_date"],
                preferred_time=data["preferred_time"],
                # Starts the facility's 8-business-hour response clock right
                # away -- this is a plain .create(), not apply_transition (there's
                # no "from" status on a brand-new request), so it doesn't get this
                # for free the way every later transition does.
                response_deadline=add_business_hours(timezone.now()),
                # Only ever meaningful for a RECIPIENT request, but harmless to
                # store as-is for a DONOR one -- the mobile form simply never
                # collects these outside the Recipient Pathway, so they arrive
                # as the serializer's defaults (False / "").
                needs_representative=data["needs_representative"],
                representative_name=data["representative_name"],
                representative_birthday=data["representative_birthday"],
                representative_contact_number=data["representative_contact_number"],
                # Same "harmless as-is for a DONOR" treatment as the
                # representative fields just above.
                neonate_name=data["neonate_name"],
                clinic_info=data["clinic_info"],
                has_prescription_proof=data["has_prescription_proof"],
                has_cooler=data["has_cooler"],
                has_medical_abstract=data["has_medical_abstract"],
                requested_ml=data["requested_ml"],
                requested_bottles=data["requested_bottles"],
                collection_method=data["collection_method"],
                delivery_link=data["delivery_link"],
                pickup_address=data["pickup_address"],
                pickup_landmark=data["pickup_landmark"],
                pickup_latitude=data["pickup_latitude"],
                pickup_longitude=data["pickup_longitude"],
            )
            log_action(request.user, "booking.created", f"MilkBankRequest:{req.id}")
            notify(
                request.user,
                "Milk Bank Request Submitted",
                f"Your {data['request_type'].lower()} request was submitted to {facility.name}.",
                NotificationItem.Category.BOOKINGS,
            )
            # Staff previously only found out a request was waiting on them by
            # opening the Booking Request tab and looking -- nothing told them
            # one had arrived. Every active staff account assigned to the
            # facility this landed at, not just whoever's logged in right now.
            notify_many(
                User.objects.filter(facility=facility, role=User.Role.FACILITY_STAFF, is_active=True),
                "New Booking Request",
                f"{request.user.mom_name or request.user.email} submitted a "
                f"{data['request_type'].lower()} request"
                + (f" for {_amount_label(data)}." if data["requested_ml"] else "."),
                NotificationItem.Category.BOOKINGS,
            )

        return Response(MilkBankRequestSerializer(req).data, status=201)


class MyMilkBankRequestsView(generics.ListAPIView):
    """GET /milkbank/requests/mine/ -- the mother's own booking history, newest first."""

    serializer_class = MilkBankRequestSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        # Global, not scoped to this user -- see sweep_expired_requests()'s
        # docstring. Cheap at this app's scale, and means she never sees a
        # pending/awaiting_attendance status that's actually already overdue
        # just because nothing else happened to sweep it first.
        sweep_expired_requests()
        maybe_purge()  # data retention, at most hourly -- see core/retention.py
        return (
            MilkBankRequest.objects.filter(owner=self.request.user)
            .select_related("owner", "allocated_facility", "donor_questionnaire")
            .order_by("-submitted_at", "-id")
        )


class AllMilkBankRequestsView(generics.ListAPIView):
    """
    GET /milkbank/requests/all/?status=pending -- every booking AT THIS
    STAFF MEMBER'S OWN FACILITY, for the facility dashboard's
    Pending/Confirmed/Declined tabs. `status` is optional and matches
    MilkBankRequest.Status (e.g. "pending", "declined"); omit it to get
    everything for this facility.

    Used to return every booking for every facility, system-wide --
    fixed once accounts.models.User gained a facility FK. A staff
    account with no facility assigned yet sees an empty list rather
    than an error or everyone else's bookings (fail closed).
    """

    serializer_class = MilkBankRequestSerializer
    permission_classes = [permissions.IsAuthenticated, IsFacilityStaff]

    def get_queryset(self):
        sweep_expired_requests()  # see MyMilkBankRequestsView.get_queryset()
        qs = (
            MilkBankRequest.objects
            .filter(allocated_facility=self.request.user.facility_id)
            .select_related("owner", "allocated_facility", "donor_questionnaire")
            .order_by("-submitted_at", "-id")
        )
        status_param = self.request.query_params.get("status")
        if status_param:
            qs = qs.filter(current_sub_status=status_param)
        return qs


class MilkBankRequestDetailView(generics.RetrieveAPIView):
    """
    GET /milkbank/requests/<id>/ -- viewable by the owner, or by
    facility_staff at the facility this booking was allocated to (not
    facility_staff generally -- a different hospital's staff must not
    be able to view this just by knowing/guessing its id).
    """

    queryset = MilkBankRequest.objects.all()
    serializer_class = MilkBankRequestSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_object(self):
        sweep_expired_requests()  # see MyMilkBankRequestsView.get_queryset()
        obj = super().get_object()
        user = self.request.user
        is_owner = obj.owner_id == user.id
        is_staff_at_this_facility = (
            user.role == user.Role.FACILITY_STAFF and user.facility_id == obj.allocated_facility_id
        )
        if not is_owner and not is_staff_at_this_facility:
            self.permission_denied(self.request)
        return obj


# --- Mother-side actions ---

COLLECTION_LOCKED = (
    "Your collection method can't be changed after your booking is confirmed. "
    "Message the facility in Chat if something has changed."
)


def _collection_locked(req):
    """Past Booking Confirmation, the way her milk reaches the facility is fixed:
    the facility plans around it (and a Drop off has a shorter list of stages,
    so changing the method then would move her)."""
    return req.current_stage_index > req.stages.index("Booking Confirmation")


def _delivery_link_error(link):
    """Why a pasted courier link can't be used, or None."""
    if not link:
        return DELIVERY_LINK_REQUIRED
    try:
        URLValidator(schemes=["http", "https"])(link)
    except DjangoValidationError:
        return DELIVERY_LINK_INVALID
    return None


def _confirmation_refusal(req):
    """
    Why she can't confirm this booking right now, as a response, or None:
    -- unless it is a Drop off, which sends its courier link up to two hours
    after -- its time has already begun; or the facility has closed the day
    it is on (staff will propose a new one).
    """
    if req.current_sub_status != Status.AWAITING_ATTENDANCE:
        return None  # the transition itself refuses it, with the usual message
    if not confirms_by_delivery_link(req) and slot_has_started(req.preferred_date, req.preferred_time):
        return Response({"detail": APPOINTMENT_PASSED_FOR_HER, "code": "appointment_passed"}, status=400)
    if appointment_day_closed(req):
        return Response({
            "detail": DAY_CLOSED_FOR_HER.format(facility=req.allocated_facility.name),
            "code": "day_closed",
        }, status=409)
    return None


def _confirm_attendance(req, user):
    """
    Confirms her accepted booking: Scheduled, on to the next stage, and her
    facility told. Raises InvalidTransition when it isn't waiting on her.

    The one place both pathways confirm through -- a DONOR reaches
    AWAITING_ATTENDANCE via StaffAcceptView, a RECIPIENT via
    StaffAdvanceStageView's "Booking Confirmation" branch. This is what puts
    her on the facility's next queue: Counseling and Testing, or -- a Drop off,
    which confirms with its courier link and has no Counseling and Testing --
    Breastmilk Collection straight away.
    """
    apply_transition(req, Status.SCHEDULED, user, "attendance_confirmed")
    req.attendance_confirmed = True
    req.current_stage_index += 1
    req.save(update_fields=["attendance_confirmed", "current_stage_index"])
    name = req.owner.mom_name or req.owner.email
    if confirms_by_delivery_link(req):
        title = "Delivery Link Sent"
        body = (
            f"{name} sent her courier's delivery link, so her drop off is confirmed "
            f"and she is now on Breastmilk Collection."
        )
    else:
        title = "Attendance Confirmed"
        body = f"{name} confirmed she's coming for her \"{req.stages[req.current_stage_index]}\" appointment."
    notify_many(
        User.objects.filter(facility=req.allocated_facility, role=User.Role.FACILITY_STAFF, is_active=True),
        title, body, NotificationItem.Category.BOOKINGS,
    )


class CollectionMethodView(generics.GenericAPIView):
    """
    POST /milkbank/requests/<id>/collection-method/
         {collection_method, delivery_link, pickup_address, pickup_landmark,
          pickup_latitude, pickup_longitude}

    Until her booking is confirmed, a donor can change how her milk reaches
    the facility. A Drop off's courier link can be pasted any time once the
    facility has accepted -- and pasting it while the booking waits on her IS
    her confirmation. After confirmation the method is locked; only a Drop
    off's link can still be replaced (a courier rebooked), until her milk is
    collected. The facility sees changes on its next refresh.
    """

    queryset = MilkBankRequest.objects.all()
    serializer_class = CollectionMethodSerializer
    permission_classes = [permissions.IsAuthenticated, IsRequestOwner]

    def post(self, request, pk):
        req = self.get_object()
        if req.request_type != MilkBankRequest.RequestType.DONOR:
            return Response({"detail": "Only a donation has a collection method."}, status=400)
        if req.current_sub_status in FINAL_STATUSES:
            return Response({"detail": "This booking has already ended."}, status=400)
        collection_stage = req.stages.index("Breastmilk Collection")
        if req.current_stage_index > collection_stage:
            return Response({"detail": "Your milk has already been collected."}, status=400)

        wanted = request.data.get("collection_method", "")
        if _collection_locked(req):
            if req.collection_method != Method.DROP_OFF or wanted != Method.DROP_OFF:
                return Response({"detail": COLLECTION_LOCKED}, status=400)
            link = str(request.data.get("delivery_link") or "").strip()
            error = _delivery_link_error(link)
            if error:
                return Response({"delivery_link": [error]}, status=400)
            req.delivery_link = link
            req.save(update_fields=["delivery_link"])
            log_action(request.user, "booking.delivery_link_changed", f"MilkBankRequest:{req.id}")
            return Response(MilkBankRequestSerializer(req).data)

        current_rules = collection_details_required(request)
        # Pasting a Drop off's link while it waits on her confirms it -- not on
        # a day the facility has since closed.
        if (
            current_rules and wanted == Method.DROP_OFF and str(request.data.get("delivery_link") or "").strip()
            and req.request_type == MilkBankRequest.RequestType.DONOR
            and req.current_sub_status == Status.AWAITING_ATTENDANCE and appointment_day_closed(req)
        ):
            return Response({
                "detail": DAY_CLOSED_FOR_HER.format(facility=req.allocated_facility.name),
                "code": "day_closed",
            }, status=409)
        before = req.collection_method
        req.collection_method = wanted
        serializer = self.get_serializer(data=request.data, context={
            # An app this version asks for the link any time after acceptance;
            # an older one only from the appointment hour.
            "link_due": False if current_rules else delivery_link_due(req),
            "pickup_required": current_rules,
        })
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        req.collection_method = data["collection_method"]
        req.delivery_link = data["delivery_link"]
        req.pickup_address = data["pickup_address"]
        req.pickup_landmark = data["pickup_landmark"]
        req.pickup_latitude = data["pickup_latitude"]
        req.pickup_longitude = data["pickup_longitude"]
        req.save(update_fields=[
            "collection_method", "delivery_link",
            "pickup_address", "pickup_landmark", "pickup_latitude", "pickup_longitude",
        ])
        log_action(request.user, "booking.collection_method_changed", f"MilkBankRequest:{req.id}")

        confirm_now = (
            current_rules and req.current_sub_status == Status.AWAITING_ATTENDANCE
            and confirms_by_delivery_link(req) and bool(req.delivery_link)
        )
        if req.collection_method != before:
            # A Drop off's deadline differs from a confirmation's.
            refresh_attendance_deadline(req)
            # A new way: tell her its steps -- unless confirming right now
            # sends them anyway (Scheduled does).
            if not confirm_now:
                notify_collection_steps(req)
        if confirm_now:
            try:
                _confirm_attendance(req, request.user)
            except InvalidTransition as exc:
                return Response({"detail": str(exc)}, status=400)
        return Response(MilkBankRequestSerializer(req).data)


class ConfirmAttendanceView(generics.GenericAPIView):
    """
    POST /milkbank/requests/<id>/confirm-attendance/  {delivery_link for a Drop off}

    The mother confirms her accepted booking. A Drop off confirms with its
    courier's delivery link instead (required from this app version on).
    """

    queryset = MilkBankRequest.objects.all()
    serializer_class = MilkBankRequestSerializer
    permission_classes = [permissions.IsAuthenticated, IsRequestOwner]

    @extend_schema(request=None)
    def post(self, request, pk):
        req = self.get_object()
        refusal = _confirmation_refusal(req)
        if refusal is not None:
            return refusal
        link_required = confirms_by_delivery_link(req) and collection_details_required(request)
        if link_required:
            link = str(request.data.get("delivery_link") or "").strip()
            error = _delivery_link_error(link)
            if error:
                return Response({"delivery_link": [error]}, status=400)
            # Set before confirming, so the steps she is sent know it's in.
            req.delivery_link = link
        try:
            _confirm_attendance(req, request.user)
        except InvalidTransition as exc:
            return Response({"detail": str(exc)}, status=400)
        if link_required:
            req.save(update_fields=["delivery_link"])
        return Response(MilkBankRequestSerializer(req).data)


class AcceptCounterOfferView(generics.GenericAPIView):
    """POST /milkbank/requests/<id>/accept-counter-offer/ -- mother accepts the facility's proposed slot."""

    queryset = MilkBankRequest.objects.all()
    serializer_class = MilkBankRequestSerializer
    permission_classes = [permissions.IsAuthenticated, IsRequestOwner]

    @extend_schema(request=None)
    def post(self, request, pk):
        req = self.get_object()
        # Agreeing to the proposed date IS accepting the booking, so this
        # has to land her in exactly the state StaffAcceptView would --
        # not straight to SCHEDULED. SCHEDULED means "attendance already
        # settled", which would skip the one thing she still owes the
        # facility: saying she's coming for the new date. The mobile app
        # gates "Confirm My Attendance" on the Booking Confirmation stage,
        # so jumping past it left her unable to confirm at all.
        #
        # The target depends on where she is NOW as well as her pathway,
        # because a counter-offer can be proposed from two points:
        #
        #   Already AWAITING_ATTENDANCE (a booking that had to move).
        #     She is already on "Booking Confirmation" and already waiting
        #     on herself. A new date does not undo the review that got her
        #     here, so she stays put and simply re-confirms against the
        #     new date.
        #
        #     Keyed off her current STAGE, not her current_sub_status --
        #     current_sub_status is already COUNTER_OFFERED by the time she
        #     gets here (StaffProposeCounterOfferView set it the moment
        #     staff proposed, and nothing between then and now changes it
        #     back), so a check against Status.AWAITING_ATTENDANCE here can
        #     never be true. current_stage_index survives that same
        #     transition untouched -- propose-counter-offer never writes
        #     it -- so "Booking Confirmation" is the only signal left that
        #     actually still says where she was. This matters for a
        #     RECIPIENT specifically: her stages run Requirements -> Status
        #     -> Booking Confirmation -> Results, so sending her back to
        #     "Status" here would hand already-reviewed paperwork to staff
        #     a second time.
        #
        #   From PENDING (the "no doctor available" desk).
        #     Nothing has been decided yet, so this is an ordinary first
        #     acceptance and mirrors StaffAcceptView exactly:
        #       DONOR      -> AWAITING_ATTENDANCE, "Booking Confirmation".
        #         The new date still needs confirming, so the
        #         8-business-hour clock starts against her now rather than
        #         at propose -- from here the wait really is hers.
        #       RECIPIENT  -> SCHEDULED, "Status". Staff review her
        #         serology test and questionnaire first, and she must not
        #         sit in a status with a running clock for something she
        #         has already done.
        #
        #   From SCHEDULED (a confirmed appointment that had to move -- its day
        #     was closed, say). She is past Booking Confirmation, and goes back
        #     to it: the new date is one she hasn't said she can make yet.
        confirmation = req.stages.index("Booking Confirmation")
        if req.current_stage_index >= confirmation:
            target_status = Status.AWAITING_ATTENDANCE
            target_stage = "Booking Confirmation"
        elif req.request_type == MilkBankRequest.RequestType.DONOR:
            target_status = Status.AWAITING_ATTENDANCE
            target_stage = "Booking Confirmation"
        else:
            target_status = Status.SCHEDULED
            target_stage = "Status"
        was_confirmed = req.current_stage_index > confirmation
        # An offer she opens after its time has passed can't be taken up: the
        # same answer as a slot that filled up meanwhile, so the app offers
        # her "Choose New Time" in the same way.
        if req.current_sub_status == Status.COUNTER_OFFERED and slot_has_started(
            req.counter_offer_date, req.counter_offer_time,
        ):
            return Response({
                "detail": "The time the facility offered has passed. Please choose a new time instead.",
                "code": "offer_passed",
            }, status=409)
        with transaction.atomic():
            # Accepting moves her booking onto the offered day, so that day
            # needs a free slot NOW -- it had one when staff offered it, but
            # other mothers may have filled it since. Locked so two
            # acceptances can't both take the last one (see claim_slot).
            facility = Facility.objects.select_for_update().get(pk=req.allocated_facility_id)
            # ...and it must still be open: the facility may have closed it since.
            if req.counter_offer_date and closed_reason(req.counter_offer_date, facility, req.request_type):
                return Response({"detail": OFFER_DAY_CLOSED, "code": "offer_unavailable"}, status=409)
            if req.counter_offer_date and not has_room(facility, req.counter_offer_date, exclude_request_id=req.pk):
                return Response({
                    "detail": "The date the facility offered has filled up since. "
                              "Please choose a new time instead.",
                }, status=409)
            # A donor also needs room in the offered hour, if the facility
            # limits donor slots -- it may have filled since it was offered.
            if (
                req.counter_offer_date
                and req.request_type == MilkBankRequest.RequestType.DONOR
                and not has_donor_slot_room(
                    facility, req.counter_offer_date, req.counter_offer_time, exclude_request_id=req.pk,
                )
            ):
                return Response({
                    "detail": "The time the facility offered has filled up since. "
                              "Please choose a new time instead.",
                }, status=409)
            # The new appointment is set on the request before the transition,
            # so the deadline it starts (and tells her) is reckoned from it.
            if req.counter_offer_date:
                req.preferred_date = req.counter_offer_date
                req.preferred_time = req.counter_offer_time
            req.current_stage_index = req.stages.index(target_stage)
            if was_confirmed:
                # She confirms the new date afresh -- a Drop off with a courier
                # booked for it, not the one booked for the old date.
                req.attendance_confirmed = False
                if confirms_by_delivery_link(req):
                    req.delivery_link = ""
            try:
                apply_transition(req, target_status, request.user, "counter_offer_accepted")
            except InvalidTransition as exc:
                return Response({"detail": str(exc)}, status=400)
            req.save(update_fields=[
                "preferred_date", "preferred_time", "current_stage_index", "attendance_confirmed", "delivery_link",
            ])
        return Response(MilkBankRequestSerializer(req).data)


class RejectCounterOfferView(generics.GenericAPIView):
    """
    POST /milkbank/requests/<id>/reject-counter-offer/  {preferred_date, preferred_time}

    "Reject and rebook" as one step: back to pending with a new
    preferred slot, same as the Kotlin app routing back to the scheduler.
    """

    queryset = MilkBankRequest.objects.all()
    serializer_class = RebookSerializer
    permission_classes = [permissions.IsAuthenticated, IsRequestOwner]

    def post(self, request, pk):
        req = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        new_date = serializer.validated_data["preferred_date"]
        # The app's calendar only offers real times still ahead on days her
        # facility is open; this is what actually refuses anything else (a
        # stale screen, a direct call).
        problem = booking_slot_problem(
            new_date, serializer.validated_data["preferred_time"], req.allocated_facility, req.request_type,
        )
        if problem:
            return _slot_problem_response(problem, new_date)

        with transaction.atomic():
            # Her request moves to the day she picked, so that day needs a
            # free slot at her facility -- same lock as claim_slot.
            facility = Facility.objects.select_for_update().get(pk=req.allocated_facility_id)
            if not has_room(facility, new_date, exclude_request_id=req.pk):
                return Response({
                    "detail": f"{facility.name} is fully booked on {new_date:%B} {new_date.day}, "
                              f"{new_date.year}. Please choose another date.",
                    "fully_booked_date": new_date.isoformat(),
                }, status=409)
            new_time = serializer.validated_data["preferred_time"]
            if req.request_type == MilkBankRequest.RequestType.DONOR and not has_donor_slot_room(
                facility, new_date, new_time, exclude_request_id=req.pk,
            ):
                return Response({
                    "detail": f"{new_time} on {new_date:%B} {new_date.day}, {new_date.year} is fully "
                              "booked for donors. Please choose another time.",
                    "full_slot": {"date": new_date.isoformat(), "time": new_time},
                }, status=409)
            try:
                apply_transition(req, Status.PENDING, request.user, "counter_offer_rejected")
            except InvalidTransition as exc:
                return Response({"detail": str(exc)}, status=400)
            req.preferred_date = new_date
            req.preferred_time = serializer.validated_data["preferred_time"]
            req.counter_offer_date = None
            req.counter_offer_time = ""
            # Back to square one for the facility to review, not still parked on
            # "Booking Confirmation" -- see StaffAcceptView's comment for why the
            # stage tracker (not just current_sub_status) has to move here too.
            # Anything confirmed for the old date (a confirmed appointment that
            # had to move) is for that date only.
            req.current_stage_index = req.stages.index("Status")
            req.attendance_confirmed = False
            if confirms_by_delivery_link(req):
                req.delivery_link = ""
            req.save(update_fields=[
                "preferred_date", "preferred_time", "counter_offer_date", "counter_offer_time", "current_stage_index",
                "attendance_confirmed", "delivery_link",
            ])
        return Response(MilkBankRequestSerializer(req).data)


class MotherCancelView(generics.GenericAPIView):
    """
    POST /milkbank/requests/<id>/cancel/

    She cancels her own booking from Booking Status, at any point while it is
    open -- except, as a donor, once her milk has been collected
    (scheduling.can_mother_cancel). The slot is free again at once, the chat
    closes, and her facility's staff are told. She can book again straight
    away (one open booking at a time).
    """

    queryset = MilkBankRequest.objects.all()
    serializer_class = MilkBankRequestSerializer
    permission_classes = [permissions.IsAuthenticated, IsRequestOwner]

    @extend_schema(request=None)
    def post(self, request, pk):
        req = self.get_object()
        if req.current_sub_status in FINAL_STATUSES:
            return Response({"detail": "This booking has already ended."}, status=400)
        if not can_mother_cancel(req):
            return Response({"detail": CANCEL_AFTER_COLLECTION, "code": "milk_collected"}, status=400)
        # The slot it frees: the offered one, for a counter-offer she hadn't answered.
        when = appointment_label(held_day(req), held_time(req))
        try:
            apply_transition(req, Status.CANCELLED, request.user, "cancelled_by_mother")
        except InvalidTransition:
            return Response({"detail": "This booking has just changed. Refresh it and try again."}, status=409)
        kind = "donation" if req.request_type == MilkBankRequest.RequestType.DONOR else "milk request"
        name = req.owner.mom_name or req.owner.email
        notify_many(
            _staff_at(req.allocated_facility),
            "Booking Cancelled",
            f"{name} cancelled her {kind} booking for {when}. The slot is free again.",
            NotificationItem.Category.BOOKINGS,
        )
        return Response(MilkBankRequestSerializer(req).data)


# --- Staff-side actions ---

class StaffAcceptView(generics.GenericAPIView):
    """POST /milkbank/requests/<id>/accept/  {staff_message?}"""

    queryset = MilkBankRequest.objects.all()
    serializer_class = StaffMessageSerializer
    permission_classes = [permissions.IsAuthenticated, IsFacilityStaff]

    def post(self, request, pk):
        req = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # Her appointment time has come and gone while the request waited for
        # an answer: accepting it now would confirm a visit that can't happen
        # (she'd be asked to confirm attendance for a past hour, or a Drop off
        # given two hours to book a courier). Staff propose a new date --
        # she keeps her questionnaire -- or decline. Left alone, it still
        # expires on its 8-business-hour clock.
        if req.current_sub_status == Status.PENDING and slot_has_started(req.preferred_date, req.preferred_time):
            return Response({
                "detail": (
                    f"Her appointment time ({appointment_label(req.preferred_date, req.preferred_time)}) has "
                    "passed, so this request can't be accepted as it is. Propose a new date, or decline it."
                ),
                "code": "appointment_passed",
            }, status=400)
        # A day the facility has since closed (or a holiday) can't be accepted
        # either: propose a new date instead.
        if req.current_sub_status == Status.PENDING and appointment_day_closed(req):
            return Response({
                "detail": (
                    f"The facility is closed on her appointment date ({day_label(req.preferred_date)}). "
                    "Propose a new date, or decline it."
                ),
                "code": "day_closed",
            }, status=400)
        # A donor's health screening answers are what Counseling and Testing
        # starts from. They are sent right after booking, and that can fail:
        # she can send them again from Booking Status.
        if (
            req.current_sub_status == Status.PENDING
            and req.request_type == MilkBankRequest.RequestType.DONOR
            and not DonorQuestionnaire.objects.filter(request=req).exists()
        ):
            return Response({
                "detail": (
                    "Her health screening answers haven't arrived, so this request can't be accepted yet. "
                    "She can send them again from Booking Status in her app, or you can decline it "
                    "(\u201cScreening answers missing\u201d)."
                ),
                "code": "screening_missing",
            }, status=400)
        # Where accepting lands depends on the pathway, because the two
        # pathways order their steps differently.
        #
        # DONOR      -> "Booking Confirmation", AWAITING_ATTENDANCE.
        #   Nothing is asked of the facility until she turns up, so the
        #   next move is hers. Confirming attendance advances her again,
        #   into "Counseling and Testing" (see ConfirmAttendanceView),
        #   which is where the Donor Process queue picks her up.
        #
        # RECIPIENT  -> "Status", SCHEDULED.
        #   Her serology test and questionnaire are already submitted and
        #   need a human to read them before anything else happens. That
        #   is facility work, so she must NOT sit in AWAITING_ATTENDANCE
        #   here -- that status starts an 8-business-hour clock against
        #   the mother, and she has nothing left to do. She moves on to
        #   "Booking Confirmation" only once staff pass that review, via
        #   StaffAdvanceStageView.
        #
        # The mobile app's "Confirm My Attendance" button is gated on
        # stages[current_stage_index] == "Booking Confirmation", so this
        # is also what decides whether she is shown that button yet.
        if req.request_type == MilkBankRequest.RequestType.DONOR:
            target_status = Status.AWAITING_ATTENDANCE
            target_stage = "Booking Confirmation"
        else:
            target_status = Status.SCHEDULED
            target_stage = "Status"
        try:
            apply_transition(req, target_status, request.user, "accepted")
        except InvalidTransition as exc:
            return Response({"detail": str(exc)}, status=400)
        req.current_stage_index = req.stages.index(target_stage)
        update_fields = ["current_stage_index"]
        if serializer.validated_data["staff_message"]:
            req.staff_message = serializer.validated_data["staff_message"]
            update_fields.append("staff_message")
        req.save(update_fields=update_fields)
        return Response(MilkBankRequestSerializer(req).data)


class StaffDeclineView(generics.GenericAPIView):
    """
    POST /milkbank/requests/<id>/decline/  {reason?, staff_message?}

    Declines a request from the Booking Request desk (pending, or a
    counter-offer she hasn't answered) or from any in-person phase
    (scheduled) -- a failed blood test, a failed breastmilk collection,
    documents that don't check out, a missed appointment. Either way the
    request ends here: DECLINED is terminal, so it can't be advanced or
    completed, and apply_transition releases the facility's slot. Declining
    while it waits on her to confirm (awaiting attendance), or once it is
    closed, is refused by the transition table.

    `reason` is the short label the admin statistics group by. The mother is
    shown a plain explanation of that reason (milkbank/results.py);
    `staff_message` reaches her only where the reason is not health-related.
    """

    queryset = MilkBankRequest.objects.all()
    serializer_class = DeclineSerializer
    permission_classes = [permissions.IsAuthenticated, IsFacilityStaff]

    def post(self, request, pk):
        req = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            # Her notification says what the app shows her: a plain explanation
            # of the reason, never the staff's technical notes for a
            # health-related decline (see milkbank/results.py).
            apply_transition(
                req, Status.DECLINED, request.user, "declined",
                message_override=decline_notification(
                    serializer.validated_data["reason"], serializer.validated_data["staff_message"],
                ),
            )
        except InvalidTransition as exc:
            return Response({"detail": str(exc)}, status=400)
        update_fields = ["decline_reason"]
        req.decline_reason = serializer.validated_data["reason"]
        if serializer.validated_data["staff_message"]:
            req.staff_message = serializer.validated_data["staff_message"]
            update_fields.append("staff_message")
        req.save(update_fields=update_fields)
        return Response(MilkBankRequestSerializer(req).data)


class StaffExpireView(generics.GenericAPIView):
    """POST /milkbank/requests/<id>/expire/"""

    queryset = MilkBankRequest.objects.all()
    serializer_class = MilkBankRequestSerializer
    permission_classes = [permissions.IsAuthenticated, IsFacilityStaff]

    @extend_schema(request=None)
    def post(self, request, pk):
        req = self.get_object()
        try:
            apply_transition(req, Status.EXPIRED, request.user, "expired")
        except InvalidTransition as exc:
            return Response({"detail": str(exc)}, status=400)
        return Response(MilkBankRequestSerializer(req).data)


class StaffSweepExpiredView(APIView):
    """
    POST /milkbank/sweep-expired/ -- expires every PENDING/AWAITING_ATTENDANCE
    request whose 8-business-hour deadline has passed.

    Not user-authenticated -- there's no logged-in person on the other end,
    this is meant to be hit by an external scheduler (this host has no
    cron/Celery worker of its own; point a free service like cron-job.org
    at it, e.g. every 15-30 minutes) since the read paths already sweep
    lazily on their own (see MyMilkBankRequestsView.get_queryset()) and this
    just makes the notification arrive sooner than her next page load.

    Authenticated instead by a shared secret in a header, not a query
    string or the URL path, so it doesn't end up logged in plaintext by
    Render's or the scheduler's own request logs. Refuses every request
    (not a permissive 200) if MILKBANK_SWEEP_TOKEN isn't configured, so a
    forgotten env var fails closed rather than quietly leaving this open
    to anyone who finds the URL.
    """

    permission_classes = [permissions.AllowAny]

    @extend_schema(request=None)
    def post(self, request):
        configured_token = settings.MILKBANK_SWEEP_TOKEN
        provided_token = request.headers.get("X-Sweep-Token", "")
        if not configured_token or not secrets.compare_digest(configured_token, provided_token):
            return Response({"detail": "Not found."}, status=404)

        expired = sweep_expired_requests()
        # The same pinger keeps data retention on schedule (core/retention.py).
        purged = purge_expired_data()
        return Response({
            "expired_count": len(expired), "expired_ids": [req.id for req in expired], "purged": purged,
        })


class StaffProposeCounterOfferView(generics.GenericAPIView):
    """
    POST /milkbank/requests/<id>/propose-counter-offer/
         {counter_offer_date, counter_offer_time, staff_message?}

    Reachable from two points in the workflow, for the same underlying
    reason -- the facility cannot host her on the day she asked for:

      PENDING              the Booking Request desk's "Propose New Date"
                           button, used instead of declining when no
                           doctor is available on her date. This is the
                           one that matters most: declining here used to
                           force her to submit the entire request again
                           (questionnaire, serology photo and all) just
                           to move a date she never chose badly.
      AWAITING_ATTENDANCE  an already-accepted booking that has to move.
      SCHEDULED            a confirmed appointment that has to move -- its
                           day was closed, say -- while it is still ahead
                           (scheduling.appointment_still_ahead).
      COUNTER_OFFERED      replacing an offer she hasn't answered yet.

    Either way nothing is refused and nothing is re-submitted: she gets
    Accept / Choose New Time in the app, and choosing a new time returns
    the request to PENDING with her new slot, questionnaire intact (see
    RejectCounterOfferView). She has until 8 business hours later, or the
    offered time if that comes first, to answer
    (transitions.counter_offer_deadline).
    """

    queryset = MilkBankRequest.objects.all()
    serializer_class = ProposeCounterOfferSerializer
    permission_classes = [permissions.IsAuthenticated, IsFacilityStaff]

    def post(self, request, pk):
        req = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # A confirmed booking can be moved only while its appointment is
        # still ahead -- not once she has been seen, or her milk collected.
        if req.current_sub_status == Status.SCHEDULED and not appointment_still_ahead(req):
            return Response({
                "detail": "Her appointment has already taken place, so its date can't be changed now.",
                "code": "appointment_done",
            }, status=400)
        # Refuse a slot nobody could attend (past, weekend, holiday, facility
        # closed, her own slot) before anything moves -- the dashboard greys
        # these out, this is what makes that binding.
        slot_errors = counter_offer_errors(
            req,
            serializer.validated_data["counter_offer_date"],
            serializer.validated_data["counter_offer_time"],
        )
        if slot_errors:
            # Per-field messages for any client that wants them, plus the
            # `detail` the dashboard already shows in its dialog.
            return Response({**slot_errors, "detail": " ".join(slot_errors.values())}, status=400)
        # Set before the transition: the offer's deadline (and what she is
        # told) is reckoned from the offered slot.
        before = (req.counter_offer_date, req.counter_offer_time)
        req.counter_offer_date = serializer.validated_data["counter_offer_date"]
        req.counter_offer_time = serializer.validated_data["counter_offer_time"]
        try:
            apply_transition(req, Status.COUNTER_OFFERED, request.user, "counter_offer_proposed")
        except InvalidTransition as exc:
            req.counter_offer_date, req.counter_offer_time = before
            return Response({"detail": str(exc)}, status=400)
        update_fields = ["counter_offer_date", "counter_offer_time"]
        # Only overwrite an existing message when a new one was actually
        # given -- same treatment StaffAcceptView gives it, so a blank
        # field can't silently wipe what staff said on an earlier action.
        if serializer.validated_data["staff_message"]:
            req.staff_message = serializer.validated_data["staff_message"]
            update_fields.append("staff_message")
        req.save(update_fields=update_fields)
        return Response(MilkBankRequestSerializer(req).data)


class StaffAdvanceStageView(generics.GenericAPIView):
    """
    POST /milkbank/requests/<id>/advance-stage/

    Moves current_stage_index one step forward for the offline-only
    phases staff tick off as they finish them in person:

        DONOR      Counseling and Testing -> Breastmilk Collection -> Results
        RECIPIENT  Status -> Booking Confirmation

    Nothing about these phases happens in this app -- staff just record
    that each one is done, so the mother's tracker reflects reality.

    It usually leaves current_sub_status alone. The one exception is
    arriving at "Booking Confirmation" on the RECIPIENT pathway, which
    also moves her to AWAITING_ATTENDANCE -- see the comment at that
    branch below for why that cannot be skipped.

    The last stage itself is a no-op through this endpoint on purpose:
    reaching it doesn't close the booking out, only
    StaffConfirmCompletionView does that (creates the TransactionRecord).
    """

    queryset = MilkBankRequest.objects.all()
    serializer_class = MilkBankRequestSerializer
    permission_classes = [permissions.IsAuthenticated, IsFacilityStaff]

    @extend_schema(request=None)
    def post(self, request, pk):
        req = self.get_object()
        if req.current_sub_status != Status.SCHEDULED:
            return Response({"detail": "Only a scheduled request can move between phases."}, status=400)
        if req.current_stage_index >= len(req.stages) - 1:
            return Response({"detail": "Already at the final phase."}, status=400)
        # A recipient moving on to Booking Confirmation is asked to confirm her
        # appointment -- not one already begun, or on a day now closed.
        next_stage = req.stages[req.current_stage_index + 1]
        if next_stage == "Booking Confirmation":
            if slot_has_started(req.preferred_date, req.preferred_time):
                return Response({
                    "detail": "Her appointment time has passed. Propose a new date first.",
                    "code": "appointment_passed",
                }, status=400)
            if appointment_day_closed(req):
                return Response({
                    "detail": "The facility is closed on her appointment date. Propose a new date first.",
                    "code": "day_closed",
                }, status=400)
        if not advance_stage(req):
            return Response({"detail": STAGE_CHANGED}, status=409)

        # "Booking Confirmation" is by definition the stage where the ball
        # is in the mother's court. Landing on it while still SCHEDULED
        # would park her on a stage whose entire purpose is her confirming
        # attendance, with no prompt, no notification telling her to act,
        # and no SLA clock -- so she would wait forever on a screen that
        # never asked her for anything.
        #
        # Only the RECIPIENT pathway reaches this branch. A DONOR is put on
        # Booking Confirmation by StaffAcceptView and has already advanced
        # past it (via ConfirmAttendanceView) before this endpoint applies
        # to her at all.
        #
        # apply_transition does its own log_action and notify, with the
        # wording for AWAITING_ATTENDANCE that actually tells her to
        # confirm -- so this returns here rather than falling through to
        # the generic "moved to the X phase" message below and sending two.
        if req.stages[req.current_stage_index] == "Booking Confirmation":
            try:
                apply_transition(req, Status.AWAITING_ATTENDANCE, request.user, "stage_advanced")
            except InvalidTransition as exc:
                return Response({"detail": str(exc)}, status=400)
            return Response(MilkBankRequestSerializer(req).data)

        log_action(request.user, "booking.stage_advanced", f"MilkBankRequest:{req.id}")
        notify(
            req.owner,
            "Milk Bank Request Update",
            f"Your request has moved to the \"{req.stages[req.current_stage_index]}\" phase.",
            NotificationItem.Category.BOOKINGS,
        )
        return Response(MilkBankRequestSerializer(req).data)


class StaffConfirmCompletionView(generics.GenericAPIView):
    """
    POST /milkbank/requests/<id>/confirm-completion/  {amount_ml}

    Closes out a Scheduled booking: creates the TransactionRecord, and
    moves Facility.stock_level_ml by the millilitres staff recorded -- up
    for a DONOR (milk actually drawn), down for a RECIPIENT (milk
    actually dispensed). See milkbank.transitions.apply_transition for
    exactly what that update does; the stock-sufficiency check below has
    to happen here, before the transition commits, since apply_transition
    itself has no way to reject the request once it's already moved.
    """

    queryset = MilkBankRequest.objects.all()
    serializer_class = ConfirmCompletionSerializer
    permission_classes = [permissions.IsAuthenticated, IsFacilityStaff]

    def post(self, request, pk):
        req = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        amount_ml = serializer.validated_data["amount_ml"]
        # Only from the Results step, where the console offers it -- never
        # past a phase nobody ticked off (a donor still at Counseling and
        # Testing has given no milk to record).
        if req.current_sub_status == Status.SCHEDULED and req.current_stage_index != len(req.stages) - 1:
            return Response({
                "detail": "Only a booking on its Results phase can be completed. Move it through its phases first.",
                "code": "not_at_results",
            }, status=400)

        with transaction.atomic():
            # The facility's row is locked and its stock read fresh, until this
            # completion is saved. Requests run side by side (gunicorn.conf.py),
            # so two dispensings from one facility could otherwise both pass
            # the stock check on the same figure and take it below zero.
            facility = Facility.objects.select_for_update().get(pk=req.allocated_facility_id)
            if req.request_type == MilkBankRequest.RequestType.RECIPIENT and facility.stock_level_ml < amount_ml:
                return Response(
                    {"detail": f"{facility.name} only has "
                               f"{facility.stock_level_ml} mL in stock — not enough to dispense "
                               f"{amount_ml} mL."},
                    status=400,
                )

            try:
                apply_transition(req, Status.COMPLETED, request.user, "completed", amount_ml=amount_ml)
            except InvalidTransition as exc:
                return Response({"detail": str(exc)}, status=400)
            req.current_stage_index = len(req.stages) - 1
            req.save(update_fields=["current_stage_index"])
        return Response(MilkBankRequestSerializer(req).data)


class MyTransactionsView(generics.ListAPIView):
    """GET /milkbank/transactions/mine/ -- Transaction History screen."""

    serializer_class = TransactionRecordSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return TransactionRecord.objects.filter(owner=self.request.user)


class MyLatestDonorQuestionnaireView(APIView):
    """
    GET /milkbank/donor-questionnaire/mine/latest/ -- the most recent
    questionnaire this mother has ever submitted, from ANY of her past
    DONOR requests, regardless of what that request's current status is.

    Exists so the app can pre-fill a fresh questionnaire with her last
    answers when she starts a new DONOR request. DonorQuestionnaire.request
    is a strict OneToOneField (see that model's docstring) -- the old row
    can never be reattached to a new request, so a decline followed by a
    resubmit always meant a blank questionnaire, even though her answers
    were sitting right there, genuinely saved, just never surfaced back
    to her. This is a read-only lookup, not a mutation: it hands the
    client her last answers to pre-fill with, nothing more.

    204, not 404, when she's never submitted one -- the client calls this
    unconditionally on every fresh questionnaire open ("is there anything
    to pre-fill?"), not asking after one specific record, so a 404 would
    just be something to special-case away at every call site.
    """

    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(responses=DonorQuestionnaireSerializer)
    def get(self, request):
        questionnaire = (
            DonorQuestionnaire.objects.filter(request__owner=request.user)
            # id breaks a submitted_at tie: two questionnaires saved in the
            # same clock tick otherwise come back in arbitrary order, and
            # .first() would hand her an older submission's answers as her
            # "latest". Ids are assigned in insert order.
            .order_by("-submitted_at", "-id")
            .first()
        )
        if questionnaire is None:
            return Response(status=204)
        return Response(DonorQuestionnaireSerializer(questionnaire).data)


def _can_view_questionnaire(user, req):
    # Same facility-scoping as MilkBankRequestDetailView: this is a
    # donor's health screening data (and possibly a serology photo) --
    # arguably more sensitive than the booking record itself, so a
    # different hospital's staff having blanket access here would be
    # worse than the MilkBankRequestDetailView gap, not just as bad.
    is_staff_at_this_facility = user.role == user.Role.FACILITY_STAFF and user.facility_id == req.allocated_facility_id
    return req.owner_id == user.id or is_staff_at_this_facility


class DonorQuestionnaireView(APIView):
    """
    GET  /milkbank/requests/<id>/donor-questionnaire/ -- metadata only, never the raw file.
    POST /milkbank/requests/<id>/donor-questionnaire/ -- submit it (multipart/form-data), owner-only.

    Only for a DONOR-type request that doesn't already have one -- the
    standing clinical-verification TODO on these 7 questions is
    documented on the model, not solved here.
    """

    permission_classes = [permissions.IsAuthenticated, RequiresSupportedApp]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(request=None, responses=DonorQuestionnaireSerializer)
    def get(self, request, pk):
        req = generics.get_object_or_404(MilkBankRequest, pk=pk)
        if not _can_view_questionnaire(request.user, req):
            self.permission_denied(request)
        if not hasattr(req, "donor_questionnaire"):
            raise Http404
        return Response(DonorQuestionnaireSerializer(req.donor_questionnaire).data)

    @extend_schema(request=DonorQuestionnaireCreateSerializer, responses=DonorQuestionnaireSerializer)
    def post(self, request, pk):
        # Refused before the body is read, so an oversized upload never lands
        # on the server's disk. (The photo itself is limited to the same size
        # again once parsed -- DonorQuestionnaireSerializer.)
        try:
            declared_length = int(request.META.get("CONTENT_LENGTH") or 0)
        except ValueError:
            declared_length = 0
        if declared_length > MAX_PHOTO_BYTES + UPLOAD_FORM_OVERHEAD_BYTES:
            return Response({"detail": "Serology photo must be 10 MB or smaller."}, status=413)

        req = generics.get_object_or_404(MilkBankRequest, pk=pk)
        if req.owner_id != request.user.id:
            self.permission_denied(request)
        if req.request_type != MilkBankRequest.RequestType.DONOR:
            return Response({"detail": "Only donor requests have a questionnaire."}, status=400)
        if hasattr(req, "donor_questionnaire"):
            return Response({"detail": "A questionnaire was already submitted for this request."}, status=400)

        serializer = DonorQuestionnaireCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(request=req)
        log_action(request.user, "donor_questionnaire.submitted", f"MilkBankRequest:{req.id}")
        return Response(DonorQuestionnaireSerializer(serializer.instance).data, status=201)


class SerologyPhotoView(APIView):
    """
    GET /milkbank/requests/<id>/serology-photo/

    The only path that ever reads the actual photo bytes. Same
    permission check as the questionnaire above, re-run on every single
    request -- there's no signed link or public path that could leak
    and bypass it.
    """

    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(request=None, responses={200: OpenApiTypes.BINARY})
    def get(self, request, pk):
        req = generics.get_object_or_404(MilkBankRequest, pk=pk)
        if not _can_view_questionnaire(request.user, req):
            self.permission_denied(request)
        questionnaire = getattr(req, "donor_questionnaire", None)
        if not questionnaire or not questionnaire.serology_photo:
            raise Http404
        try:
            photo = questionnaire.serology_photo.open("rb")
        except FileNotFoundError:
            # The record names a photo the storage no longer has -- e.g. one
            # uploaded before photos moved to Supabase Storage, back when they
            # sat on the server's own disk and were wiped on every deploy.
            # "Not available" (404), never a crash with a traceback.
            log_action(request.user, "serology_photo.missing", f"MilkBankRequest:{req.id}")
            raise Http404
        # Checked again here, from the bytes: photos stored before uploads
        # were checked this way are served only if they really are images,
        # and always under their real type -- never under the type the name
        # suggests, so no stored file can be rendered as a web page.
        kind = detect_image(read_head(photo))
        if kind is None:
            photo.close()
            log_action(request.user, "serology_photo.refused_not_an_image", f"MilkBankRequest:{req.id}")
            raise Http404
        content_type, extension = kind
        log_action(request.user, "serology_photo.viewed", f"MilkBankRequest:{req.id}")
        response = FileResponse(photo, content_type=content_type, filename=f"serology-photo.{extension}")
        # Health information: never kept in a browser or proxy cache, and
        # nothing in it may run even if the file is opened on its own.
        response["Cache-Control"] = "no-store"
        response["X-Content-Type-Options"] = "nosniff"
        response["Content-Security-Policy"] = "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox"
        return response
