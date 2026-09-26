from django.contrib import admin

from .models import ChatMessage, ChatSession, KnowledgeChunk, KnowledgeDocument


@admin.register(ChatSession)
class ChatSessionAdmin(admin.ModelAdmin):
    list_display = ("owner", "prompt_count", "token_count")


@admin.register(ChatMessage)
class ChatMessageAdmin(admin.ModelAdmin):
    list_display = ("session", "is_user", "is_system_notice", "created_at")
    list_filter = ("is_user", "is_system_notice")


@admin.register(KnowledgeDocument)
class KnowledgeDocumentAdmin(admin.ModelAdmin):
    list_display = ("title", "citation", "is_active", "updated_at")
    list_filter = ("is_active",)
    search_fields = ("title", "citation", "content")
    readonly_fields = ("added_at", "updated_at")


@admin.register(KnowledgeChunk)
class KnowledgeChunkAdmin(admin.ModelAdmin):
    """
    Read-only on purpose. Chunks are derived from their source documents
    and rebuilt from them automatically, so a hand-edited passage would
    be silently discarded the next time its source changed -- edit the
    article or the KnowledgeDocument instead. Visible because "which
    passages does Kali actually have, and are they embedded" is the first
    question to ask when an answer looks wrong.
    """

    list_display = ("source_title", "source_kind", "ordinal", "is_embedded", "indexed_at")
    list_filter = ("source_kind", "embedding_model")
    search_fields = ("source_title", "text")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    @admin.display(boolean=True, description="Embedded")
    def is_embedded(self, obj):
        return bool(obj.embedding)
