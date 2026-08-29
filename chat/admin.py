from django.contrib import admin

from .models import ChatMessage, ChatSession


@admin.register(ChatSession)
class ChatSessionAdmin(admin.ModelAdmin):
    list_display = ("owner", "prompt_count", "token_count", "current_model", "model_switched")


@admin.register(ChatMessage)
class ChatMessageAdmin(admin.ModelAdmin):
    list_display = ("session", "is_user", "is_system_notice", "created_at")
    list_filter = ("is_user", "is_system_notice")
