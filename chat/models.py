from django.conf import settings
from django.db import models


class ChatSession(models.Model):
    """
    One per user -- the server-side home for the running counters the
    Kotlin app currently keeps in ViewModel state (chatSessionPromptCount,
    chatSessionTokenCount), which resets to zero the moment the app
    process dies. Here it survives, and it's the same counter no matter
    which device she's on.

    Used to also track current_model/model_switched, for a two-tier
    gpt-4o -> gpt-4o-mini cost-saving downgrade once a session grew
    long (see git history if that behavior needs reviving) -- dropped
    when the real model behind chat moved to AWS Bedrock/DeepSeek-R1
    (chat/bedrock_client.py), since there's only one model now and
    those fields had nothing left to track.
    """

    owner = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="chat_session")
    prompt_count = models.PositiveIntegerField(default=0)
    token_count = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f"Chat session for {self.owner}"


class ChatMessage(models.Model):
    """A single message in the Kali chat, ported from CODEBASE-1.md section 3."""

    session = models.ForeignKey(ChatSession, on_delete=models.CASCADE, related_name="messages")
    text = models.TextField()
    is_user = models.BooleanField(help_text="True if from the mother, False if from Kali.")
    is_system_notice = models.BooleanField(default=False, help_text="True for the model-switch notice card.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        who = "Mother" if self.is_user else "Kali"
        return f"{who}: {self.text[:50]}"
