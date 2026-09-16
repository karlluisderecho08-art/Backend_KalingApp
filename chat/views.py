from datetime import timedelta

from django.utils import timezone
from rest_framework import generics, permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from .gemini_client import HISTORY_TURNS, get_ai_response
from .guardrail import OFF_TOPIC_RESPONSE, is_breastfeeding_topic
from .models import ChatMessage, ChatSession
from .serializers import ChatMessageSerializer, SendMessageSerializer


class ChatHistoryView(generics.ListAPIView):
    """GET /chat/history/ -- this user's message history, server-side (survives app restarts)."""

    serializer_class = ChatMessageSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        session, _ = ChatSession.objects.get_or_create(owner=self.request.user)
        return session.messages.all()


class SendMessageView(APIView):
    """
    POST /chat/message/  {text}

    The server-side version of sendChatMessage() (CODEBASE-1.md section
    5): save the user's message, check it's on-topic *server-side* (not
    trusting the client), only call the model if it is, track the
    running prompt/token counts, and downgrade the model once,
    permanently, if either threshold is crossed.
    """

    permission_classes = [permissions.IsAuthenticated]
    serializer_class = SendMessageSerializer

    def post(self, request):
        serializer = SendMessageSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        text = serializer.validated_data["text"]

        session, _ = ChatSession.objects.get_or_create(owner=request.user)

        # Read the prior turns before storing this one, so the history
        # handed to the model is what came *before* the current message.
        # Newest-first + reversed so the LIMIT keeps the most recent
        # turns rather than the oldest ones.
        prior = list(
            session.messages.filter(is_system_notice=False).order_by("-created_at")[:HISTORY_TURNS]
        )[::-1]

        ChatMessage.objects.create(session=session, text=text, is_user=True)

        if not is_breastfeeding_topic(text) and not _continues_conversation(prior):
            reply = ChatMessage.objects.create(session=session, text=OFF_TOPIC_RESPONSE, is_user=False)
            return Response({
                "reply": ChatMessageSerializer(reply).data,
                "session": _session_state(session),
            })

        session.prompt_count += 1

        history = [(message.is_user, message.text) for message in prior]
        reply_text, tokens, used_fallback = get_ai_response(text, history=history)
        session.token_count += tokens
        session.save(update_fields=["prompt_count", "token_count"])

        reply = ChatMessage.objects.create(session=session, text=reply_text, is_user=False)

        return Response({
            "reply": ChatMessageSerializer(reply).data,
            "used_fallback": used_fallback,
            "session": _session_state(session),
        })


# How long after Kali's last reply a message still counts as part of
# that exchange. Long enough that a mother can put the phone down to
# feed the baby and come back to answer; short enough that a question
# typed days later is judged on its own words again.
FOLLOW_UP_WINDOW = timedelta(minutes=30)


def _continues_conversation(prior):
    """
    True if this message is a reply to something Kali just asked.

    The keyword guardrail judges each message in isolation, which breaks
    the most natural exchange in the app: Kali ends a reply with "would
    you like to know more about that?", the mother answers "yes", and
    "yes" contains no breastfeeding keyword, so she's told her own
    answer is off topic. Anything short and conversational -- "yes",
    "sure", "what about at night?" -- fails the same way.

    Rather than trying to enumerate affirmations, this lets a message
    through whenever it lands in an open exchange, and leaves judging it
    to the model, which sees the conversation and is confined to the
    Knowledge Hub by its system prompt. A genuinely off-topic question
    asked mid-conversation gets refused there instead -- in context, and
    in Kali's own words, rather than by a canned line.
    """
    if not prior:
        return False

    last = prior[-1]
    if last.is_user:
        return False
    return timezone.now() - last.created_at < FOLLOW_UP_WINDOW


def _session_state(session):
    return {
        "prompt_count": session.prompt_count,
        "token_count": session.token_count,
    }
