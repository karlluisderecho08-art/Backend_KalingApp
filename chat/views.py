from rest_framework import generics, permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from .guardrail import OFF_TOPIC_RESPONSE, is_breastfeeding_topic
from .models import ChatMessage, ChatSession
from .openai_client import get_ai_response
from .serializers import ChatMessageSerializer, SendMessageSerializer

# The exact thresholds from CODEBASE-1.md section 5: cross either one,
# once, and the session permanently downgrades to the cheaper model.
PROMPT_THRESHOLD = 15
TOKEN_THRESHOLD = 4000

MODEL_SWITCH_NOTICE = (
    "This conversation has grown long enough that Kali has switched to a lighter "
    "model to keep things running smoothly. For in-depth guidance, browse the "
    "Verified Knowledge page."
)


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
        ChatMessage.objects.create(session=session, text=text, is_user=True)

        if not is_breastfeeding_topic(text):
            reply = ChatMessage.objects.create(session=session, text=OFF_TOPIC_RESPONSE, is_user=False)
            return Response({
                "reply": ChatMessageSerializer(reply).data,
                "session": _session_state(session),
            })

        session.prompt_count += 1

        already_switched = session.model_switched
        should_switch = session.prompt_count >= PROMPT_THRESHOLD or session.token_count >= TOKEN_THRESHOLD
        if should_switch and not already_switched:
            session.current_model = ChatSession.Model.FALLBACK_MODEL
            session.model_switched = True
            ChatMessage.objects.create(session=session, text=MODEL_SWITCH_NOTICE, is_user=False, is_system_notice=True)

        reply_text, tokens, used_fallback = get_ai_response(text, session.current_model)
        session.token_count += tokens
        session.save(update_fields=["prompt_count", "token_count", "current_model", "model_switched"])

        reply = ChatMessage.objects.create(session=session, text=reply_text, is_user=False)

        return Response({
            "reply": ChatMessageSerializer(reply).data,
            "used_fallback": used_fallback,
            "session": _session_state(session),
        })


def _session_state(session):
    return {
        "prompt_count": session.prompt_count,
        "token_count": session.token_count,
        "current_model": session.current_model,
        "model_switched": session.model_switched,
    }
