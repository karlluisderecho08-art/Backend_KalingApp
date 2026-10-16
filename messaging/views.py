"""
Private messaging between each facility and the platform admins.

Who can reach what is decided here, on the server, from the account
making the request -- never from anything the client sends:

  - A facility account (role facility_staff) only ever reaches its OWN
    facility's thread. There is no facility id in the facility-side URLs
    at all, so there is nothing to tamper with; the thread is looked up
    from request.user.facility. An account not yet assigned a facility
    gets nothing (403), the same fail-closed rule as the booking views.
  - An admin (is_staff) can reach every facility's thread.
  - Everyone else -- mothers, anonymous callers -- is refused outright.

"Clear conversation" is a real delete, not a hidden flag: the messages
are gone from the database for both sides. What is kept is a line in the
audit log saying who cleared which thread and how many messages went,
never the messages themselves.
"""

from django.db import transaction
from django.db.models import Count, F, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from accounts.models import User
from core.audit import log_action
from milkbank.models import Facility

from .models import Conversation, Message
from .serializers import MessageSerializer, SendMessageSerializer

# How much history one request returns. Enough for any real thread between
# a facility and the admins; a cap so one request can't pull an unbounded
# table.
HISTORY_LIMIT = 300


class IsAssignedFacilityStaff(permissions.BasePermission):
    def has_permission(self, request, view):
        user = request.user
        return bool(
            user and user.is_authenticated and user.is_active
            and user.role == User.Role.FACILITY_STAFF and user.facility_id is not None
        )


class IsPlatformAdmin(permissions.BasePermission):
    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and user.is_active and user.is_staff)


class _MessagingView(APIView):
    """
    Shared by every endpoint here: sending is rate limited (reading is not,
    since both dashboards poll), and no response may be cached by the
    browser or anything in between -- these are private messages.
    """

    throttle_scope = "messaging_send"

    def get_throttles(self):
        throttles = super().get_throttles()
        if self.request.method == "POST":
            throttles.append(ScopedRateThrottle())
        return throttles

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "no-store"
        response["Pragma"] = "no-cache"
        return response


def _history(conversation, viewer_side):
    if conversation is None:
        return []
    # The newest HISTORY_LIMIT, returned oldest first for display.
    recent = list(conversation.messages.select_related("sender").order_by("-created_at", "-id")[:HISTORY_LIMIT])
    recent.reverse()
    return MessageSerializer(recent, many=True, context={"viewer_side": viewer_side}).data


def _send(conversation, user, side, body):
    with transaction.atomic():
        message = Message.objects.create(conversation=conversation, side=side, sender=user, body=body)
        # The sender has obviously read everything up to their own message.
        fields = {"last_message_at": message.created_at}
        fields["admin_last_read_at" if side == Message.Side.ADMIN else "facility_last_read_at"] = message.created_at
        Conversation.objects.filter(pk=conversation.pk).update(**fields)
    return message


def _clear(conversation, user):
    if conversation is None:
        return 0
    with transaction.atomic():
        deleted, _ = conversation.messages.all().delete()
        Conversation.objects.filter(pk=conversation.pk).update(
            last_message_at=None, admin_last_read_at=None, facility_last_read_at=None,
        )
    log_action(user, "messaging.cleared", f"Facility:{conversation.facility_id} ({deleted} messages)")
    return deleted


def _unread_for_facility(conversation):
    if conversation is None:
        return 0
    qs = conversation.messages.filter(side=Message.Side.ADMIN)
    if conversation.facility_last_read_at:
        qs = qs.filter(created_at__gt=conversation.facility_last_read_at)
    return qs.count()


# --- Facility side ------------------------------------------------------------


class FacilityThreadView(_MessagingView):
    """
    GET    /messaging/facility/messages/ -- this facility's thread (marks it read)
    POST   /messaging/facility/messages/ {body} -- send to the admins
    DELETE /messaging/facility/messages/ -- clear the whole conversation
    """

    permission_classes = [IsAssignedFacilityStaff]

    def _conversation(self, create=False):
        facility_id = self.request.user.facility_id
        if create:
            return Conversation.objects.get_or_create(facility_id=facility_id)[0]
        return Conversation.objects.filter(facility_id=facility_id).first()

    def get(self, request):
        conversation = self._conversation()
        messages = _history(conversation, Message.Side.FACILITY)
        if conversation is not None:
            Conversation.objects.filter(pk=conversation.pk).update(facility_last_read_at=timezone.now())
        return Response({"facility_name": request.user.facility.name, "messages": messages})

    @extend_schema(request=SendMessageSerializer, responses={201: MessageSerializer})
    def post(self, request):
        serializer = SendMessageSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        message = _send(self._conversation(create=True), request.user, Message.Side.FACILITY,
                        serializer.validated_data["body"])
        return Response(MessageSerializer(message, context={"viewer_side": Message.Side.FACILITY}).data,
                        status=status.HTTP_201_CREATED)

    def delete(self, request):
        return Response({"deleted": _clear(self._conversation(), request.user)})


class FacilityUnreadView(_MessagingView):
    """GET /messaging/facility/unread/ -- for the sidebar badge."""

    permission_classes = [IsAssignedFacilityStaff]

    def get(self, request):
        conversation = Conversation.objects.filter(facility_id=request.user.facility_id).first()
        return Response({"unread": _unread_for_facility(conversation)})


# --- Admin side ---------------------------------------------------------------


def _admin_unread_filter():
    # Facility-side messages newer than the admins' last read of that thread.
    return Q(messages__side=Message.Side.FACILITY) & (
        Q(admin_last_read_at__isnull=True) | Q(messages__created_at__gt=F("admin_last_read_at"))
    )


class AdminConversationListView(_MessagingView):
    """
    GET    /messaging/admin/conversations/ -- every facility, with its thread summary
    DELETE /messaging/admin/conversations/ -- clear EVERY conversation

    Lists facilities rather than existing threads, so an admin can open a
    conversation with a facility that has never written in.
    """

    permission_classes = [IsPlatformAdmin]

    def get(self, request):
        conversations = {
            c.facility_id: c
            for c in Conversation.objects.annotate(unread=Count("messages", filter=_admin_unread_filter()))
        }
        rows = []
        for facility in Facility.objects.order_by("name"):
            c = conversations.get(facility.id)
            last = c.messages.order_by("-created_at", "-id").first() if c and c.last_message_at else None
            rows.append({
                "facility_id": facility.id,
                "facility_name": facility.name,
                "last_message_at": c.last_message_at if c else None,
                "last_message_preview": (last.body[:120] if last else ""),
                "last_message_side": (last.side if last else None),
                "unread": c.unread if c else 0,
            })
        # Most recent conversation first; facilities never messaged after, by
        # name (sort is stable, and rows are already in name order).
        rows.sort(key=lambda r: r["last_message_at"].timestamp() if r["last_message_at"] else float("-inf"),
                  reverse=True)
        return Response(rows)

    def delete(self, request):
        total = 0
        for conversation in Conversation.objects.all():
            total += _clear(conversation, request.user)
        log_action(request.user, "messaging.cleared_all", f"{total} messages")
        return Response({"deleted": total})


class AdminThreadView(_MessagingView):
    """
    GET    /messaging/admin/conversations/<facility_id>/messages/ (marks it read)
    POST   /messaging/admin/conversations/<facility_id>/messages/ {body}
    DELETE /messaging/admin/conversations/<facility_id>/messages/ -- clear it
    """

    permission_classes = [IsPlatformAdmin]

    def get(self, request, facility_id):
        facility = get_object_or_404(Facility, pk=facility_id)
        conversation = Conversation.objects.filter(facility=facility).first()
        messages = _history(conversation, Message.Side.ADMIN)
        if conversation is not None:
            Conversation.objects.filter(pk=conversation.pk).update(admin_last_read_at=timezone.now())
        return Response({"facility_id": facility.id, "facility_name": facility.name, "messages": messages})

    @extend_schema(request=SendMessageSerializer, responses={201: MessageSerializer})
    def post(self, request, facility_id):
        facility = get_object_or_404(Facility, pk=facility_id)
        serializer = SendMessageSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        conversation = Conversation.objects.get_or_create(facility=facility)[0]
        message = _send(conversation, request.user, Message.Side.ADMIN, serializer.validated_data["body"])
        return Response(MessageSerializer(message, context={"viewer_side": Message.Side.ADMIN}).data,
                        status=status.HTTP_201_CREATED)

    def delete(self, request, facility_id):
        facility = get_object_or_404(Facility, pk=facility_id)
        conversation = Conversation.objects.filter(facility=facility).first()
        return Response({"deleted": _clear(conversation, request.user)})


class AdminUnreadView(_MessagingView):
    """GET /messaging/admin/unread/ -- total unread across facilities, for the sidebar badge."""

    permission_classes = [IsPlatformAdmin]

    def get(self, request):
        total = Conversation.objects.aggregate(unread=Count("messages", filter=_admin_unread_filter()))["unread"]
        return Response({"unread": total or 0})
