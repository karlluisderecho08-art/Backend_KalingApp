import secrets

from django.conf import settings
from django.db.models import F
from django.http import FileResponse, Http404
from django.utils import timezone
from drf_spectacular.utils import OpenApiTypes, extend_schema
from rest_framework import generics, permissions, status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from core.audit import log_action
from notifications.models import NotificationItem
from notifications.services import notify

from .allocation import AllocationError, LocationRequired, NoOperationalFacility, get_ranked_facilities
from .business_hours import add_business_hours
from .models import ML_PER_FLUID_OUNCE, DonorQuestionnaire, Facility, MilkBankRequest, TransactionRecord
from .permissions import IsFacilityStaff, IsRequestOwner
from .serializers import (
    AllocationRequestSerializer,
    ConfirmCompletionSerializer,
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
)
from .transitions import InvalidTransition, apply_transition, sweep_expired_requests

Status = MilkBankRequest.Status


class FacilityListView(generics.ListCreateAPIView):
    """
    GET  /milkbank/facilities/ -- plain facility list, e.g. for the scheduler screen. Public.
    POST /milkbank/facilities/ -- add a new facility. Platform-admin only
    (is_staff, not the facility_staff role -- this is the "who runs
    KalingApp" panel, not a single facility's own staff).
    """

    queryset = Facility.objects.all()
    serializer_class = FacilitySerializer

    def get_permissions(self):
        if self.request.method == "POST":
            return [permissions.IsAdminUser()]
        return [permissions.AllowAny()]


class FacilityDetailView(generics.RetrieveUpdateDestroyAPIView):
    """GET is public; PUT/PATCH/DELETE are platform-admin only (see FacilityListView)."""

    queryset = Facility.objects.all()
    serializer_class = FacilitySerializer

    def get_permissions(self):
        if self.request.method == "GET":
            return [permissions.AllowAny()]
        return [permissions.IsAdminUser()]


def _allocation_error_response(exc):
    if isinstance(exc, LocationRequired):
        return Response({"detail": "Location required. Call POST /auth/location/ first."}, status=400)
    if isinstance(exc, NoOperationalFacility):
        return Response({"detail": "No operational facility is currently available."}, status=404)
    raise exc  # pragma: no cover -- only the two subclasses above exist today


class SmartAllocationView(APIView):
    """
    POST /milkbank/allocate/  {"request_type": "DONOR" | "RECIPIENT"}

    A *preview* -- shows what Smart Allocation would pick, without
    creating a booking. Useful for the client to show "you'll likely be
    matched with X" before the mother commits. The real booking is
    created by MilkBankRequestCreateView below, which runs the same
    allocation for real.
    """

    permission_classes = [permissions.IsAuthenticated]
    serializer_class = AllocationRequestSerializer

    def post(self, request):
        serializer = AllocationRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        request_type = serializer.validated_data["request_type"]

        try:
            ranked = get_ranked_facilities(request.user, request_type)
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

    permission_classes = [permissions.IsAuthenticated]
    serializer_class = MilkBankRequestCreateSerializer

    def post(self, request):
        serializer = MilkBankRequestCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # One open request per user (roadmap Phase 4 concurrency
        # decision: simplest option, matches the Kotlin app's single
        # `currentMilkBankRequest`). Enforced here, not as a DB
        # constraint, so the error message can actually explain why.
        open_statuses = [Status.PENDING, Status.AWAITING_ATTENDANCE, Status.SCHEDULED, Status.COUNTER_OFFERED]
        if MilkBankRequest.objects.filter(owner=request.user, current_sub_status__in=open_statuses).exists():
            return Response({"detail": "You already have an open request."}, status=400)

        try:
            ranked = get_ranked_facilities(request.user, data["request_type"])
        except AllocationError as exc:
            return _allocation_error_response(exc)

        req = MilkBankRequest.objects.create(
            owner=request.user,
            request_type=data["request_type"],
            allocated_facility=ranked[0],
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
        )
        # Occupies a slot the moment it's created (status=pending already
        # counts as "open") -- see transitions.py for where it's released.
        Facility.objects.filter(pk=ranked[0].id).update(booked_count=F("booked_count") + 1)
        log_action(request.user, "booking.created", f"MilkBankRequest:{req.id}")
        notify(
            request.user,
            "Milk Bank Request Submitted",
            f"Your {data['request_type'].lower()} request was submitted to {ranked[0].name}.",
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
        return MilkBankRequest.objects.filter(owner=self.request.user).order_by("-submitted_at")


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
            .select_related("owner", "allocated_facility")
            .order_by("-submitted_at")
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

class ConfirmAttendanceView(generics.GenericAPIView):
    """POST /milkbank/requests/<id>/confirm-attendance/ -- mother confirms an accepted slot."""

    queryset = MilkBankRequest.objects.all()
    serializer_class = MilkBankRequestSerializer
    permission_classes = [permissions.IsAuthenticated, IsRequestOwner]

    @extend_schema(request=None)
    def post(self, request, pk):
        req = self.get_object()
        try:
            apply_transition(req, Status.SCHEDULED, request.user, "attendance_confirmed")
        except InvalidTransition as exc:
            return Response({"detail": str(exc)}, status=400)
        req.attendance_confirmed = True
        req.current_stage_index += 1
        req.save(update_fields=["attendance_confirmed", "current_stage_index"])
        return Response(MilkBankRequestSerializer(req).data)


class AcceptCounterOfferView(generics.GenericAPIView):
    """POST /milkbank/requests/<id>/accept-counter-offer/ -- mother accepts the facility's proposed slot."""

    queryset = MilkBankRequest.objects.all()
    serializer_class = MilkBankRequestSerializer
    permission_classes = [permissions.IsAuthenticated, IsRequestOwner]

    @extend_schema(request=None)
    def post(self, request, pk):
        req = self.get_object()
        try:
            apply_transition(req, Status.SCHEDULED, request.user, "counter_offer_accepted")
        except InvalidTransition as exc:
            return Response({"detail": str(exc)}, status=400)
        req.preferred_date = req.counter_offer_date
        req.preferred_time = req.counter_offer_time
        req.current_stage_index += 1
        req.save(update_fields=["preferred_date", "preferred_time", "current_stage_index"])
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

        try:
            apply_transition(req, Status.PENDING, request.user, "counter_offer_rejected")
        except InvalidTransition as exc:
            return Response({"detail": str(exc)}, status=400)
        req.preferred_date = serializer.validated_data["preferred_date"]
        req.preferred_time = serializer.validated_data["preferred_time"]
        req.counter_offer_date = None
        req.counter_offer_time = ""
        # Back to square one for the facility to review, not still parked on
        # "Booking Confirmation" -- see StaffAcceptView's comment for why the
        # stage tracker (not just current_sub_status) has to move here too.
        req.current_stage_index = req.stages.index("Status")
        req.save(update_fields=[
            "preferred_date", "preferred_time", "counter_offer_date", "counter_offer_time", "current_stage_index",
        ])
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
        try:
            apply_transition(req, Status.AWAITING_ATTENDANCE, request.user, "accepted")
        except InvalidTransition as exc:
            return Response({"detail": str(exc)}, status=400)
        # apply_transition only moves current_sub_status. The mobile app's
        # Booking Status tracker (and its "Confirm My Attendance" button,
        # gated on stages[current_stage_index] == "Booking Confirmation")
        # reads current_stage_index instead, so without this she'd see her
        # status flip to "Awaiting Attendance" with no way to act on it --
        # every later stage-advancing view (ConfirmAttendanceView,
        # AcceptCounterOfferView) already assumes accepting landed her here.
        req.current_stage_index = req.stages.index("Booking Confirmation")
        update_fields = ["current_stage_index"]
        if serializer.validated_data["staff_message"]:
            req.staff_message = serializer.validated_data["staff_message"]
            update_fields.append("staff_message")
        req.save(update_fields=update_fields)
        return Response(MilkBankRequestSerializer(req).data)


class StaffDeclineView(generics.GenericAPIView):
    """POST /milkbank/requests/<id>/decline/  {staff_message?}"""

    queryset = MilkBankRequest.objects.all()
    serializer_class = StaffMessageSerializer
    permission_classes = [permissions.IsAuthenticated, IsFacilityStaff]

    def post(self, request, pk):
        req = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            apply_transition(req, Status.DECLINED, request.user, "declined")
        except InvalidTransition as exc:
            return Response({"detail": str(exc)}, status=400)
        if serializer.validated_data["staff_message"]:
            req.staff_message = serializer.validated_data["staff_message"]
            req.save(update_fields=["staff_message"])
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
        return Response({"expired_count": len(expired), "expired_ids": [req.id for req in expired]})


class StaffProposeCounterOfferView(generics.GenericAPIView):
    """POST /milkbank/requests/<id>/propose-counter-offer/  {counter_offer_date, counter_offer_time}"""

    queryset = MilkBankRequest.objects.all()
    serializer_class = ProposeCounterOfferSerializer
    permission_classes = [permissions.IsAuthenticated, IsFacilityStaff]

    def post(self, request, pk):
        req = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            apply_transition(req, Status.COUNTER_OFFERED, request.user, "counter_offer_proposed")
        except InvalidTransition as exc:
            return Response({"detail": str(exc)}, status=400)
        req.counter_offer_date = serializer.validated_data["counter_offer_date"]
        req.counter_offer_time = serializer.validated_data["counter_offer_time"]
        req.save(update_fields=["counter_offer_date", "counter_offer_time"])
        return Response(MilkBankRequestSerializer(req).data)


class StaffAdvanceStageView(generics.GenericAPIView):
    """
    POST /milkbank/requests/<id>/advance-stage/

    Moves current_stage_index one step forward WITHOUT touching
    current_sub_status -- for the offline-only phases between "Scheduled"
    and the final stage (DONOR: Counseling and Testing -> Breastmilk
    Analysis -> Results; RECIPIENT has no such gap, see RECIPIENT_STAGES).
    Nothing about these phases happens in this app -- staff just ticks
    each one off here once it's actually done in person, so the mother's
    tracker reflects reality. The last stage itself is a no-op through
    this endpoint on purpose: reaching it doesn't close the booking out,
    only StaffConfirmCompletionView does that (creates the TransactionRecord).
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
        req.current_stage_index += 1
        req.save(update_fields=["current_stage_index"])
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
    POST /milkbank/requests/<id>/confirm-completion/  {amount_oz}

    Closes out a Scheduled booking: creates the TransactionRecord, and
    moves Facility.stock_level_ml by the ounces staff recorded -- up for
    a DONOR (milk actually drawn), down for a RECIPIENT (milk actually
    dispensed). See milkbank.transitions.apply_transition for exactly
    what that update does; the stock-sufficiency check below has to
    happen here, before the transition commits, since apply_transition
    itself has no way to reject the request once it's already moved.
    """

    queryset = MilkBankRequest.objects.all()
    serializer_class = ConfirmCompletionSerializer
    permission_classes = [permissions.IsAuthenticated, IsFacilityStaff]

    def post(self, request, pk):
        req = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        amount_oz = serializer.validated_data["amount_oz"]

        if req.request_type == MilkBankRequest.RequestType.RECIPIENT:
            ml_amount = round(amount_oz * ML_PER_FLUID_OUNCE)
            if req.allocated_facility.stock_level_ml < ml_amount:
                return Response(
                    {"detail": f"{req.allocated_facility.name} only has "
                               f"{req.allocated_facility.stock_level_ml} mL in stock -- not enough to dispense "
                               f"{amount_oz} oz."},
                    status=400,
                )

        try:
            apply_transition(req, Status.COMPLETED, request.user, "completed", amount_oz=amount_oz)
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

    permission_classes = [permissions.IsAuthenticated]
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
        log_action(request.user, "serology_photo.viewed", f"MilkBankRequest:{req.id}")
        return FileResponse(questionnaire.serology_photo.open("rb"))
