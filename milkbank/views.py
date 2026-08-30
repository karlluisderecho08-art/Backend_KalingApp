from django.db.models import F
from django.http import FileResponse, Http404
from drf_spectacular.utils import OpenApiTypes, extend_schema
from rest_framework import generics, permissions, status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from core.audit import log_action
from notifications.models import NotificationItem
from notifications.services import notify

from .allocation import AllocationError, LocationRequired, NoOperationalFacility, get_ranked_facilities
from .models import DonorQuestionnaire, Facility, MilkBankRequest, TransactionRecord
from .permissions import IsFacilityStaff, IsRequestOwner
from .serializers import (
    AllocationRequestSerializer,
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
from .transitions import InvalidTransition, apply_transition

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
        return MilkBankRequest.objects.filter(owner=self.request.user).order_by("-submitted_at")


class AllMilkBankRequestsView(generics.ListAPIView):
    """
    GET /milkbank/requests/all/?status=pending -- every booking, for the
    facility dashboard's Pending/Confirmed/Declined tabs. `status` is
    optional and matches MilkBankRequest.Status (e.g. "pending",
    "declined"); omit it to get everything. Staff aren't scoped to a
    single facility today (see accounts.models.User -- no facility FK
    on the role), so this intentionally returns requests for every
    facility, same as MilkBankRequestDetailView already allows any
    facility_staff to view any single request by id.
    """

    serializer_class = MilkBankRequestSerializer
    permission_classes = [permissions.IsAuthenticated, IsFacilityStaff]

    def get_queryset(self):
        qs = MilkBankRequest.objects.select_related("owner", "allocated_facility").order_by("-submitted_at")
        status_param = self.request.query_params.get("status")
        if status_param:
            qs = qs.filter(current_sub_status=status_param)
        return qs


class MilkBankRequestDetailView(generics.RetrieveAPIView):
    """GET /milkbank/requests/<id>/ -- viewable by the owner or any facility staff."""

    queryset = MilkBankRequest.objects.all()
    serializer_class = MilkBankRequestSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_object(self):
        obj = super().get_object()
        if obj.owner_id != self.request.user.id and self.request.user.role != self.request.user.Role.FACILITY_STAFF:
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
        req.save(update_fields=["preferred_date", "preferred_time", "counter_offer_date", "counter_offer_time"])
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
        if serializer.validated_data["staff_message"]:
            req.staff_message = serializer.validated_data["staff_message"]
            req.save(update_fields=["staff_message"])
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


class StaffConfirmCompletionView(generics.GenericAPIView):
    """POST /milkbank/requests/<id>/confirm-completion/ -- also creates the TransactionRecord."""

    queryset = MilkBankRequest.objects.all()
    serializer_class = MilkBankRequestSerializer
    permission_classes = [permissions.IsAuthenticated, IsFacilityStaff]

    @extend_schema(request=None)
    def post(self, request, pk):
        req = self.get_object()
        try:
            apply_transition(req, Status.COMPLETED, request.user, "completed")
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
    return req.owner_id == user.id or user.role == user.Role.FACILITY_STAFF


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
