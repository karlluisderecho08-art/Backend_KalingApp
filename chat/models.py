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
        # id as the tie-break, for the same reason as SendMessageView's
        # history query: two messages with an identical created_at must
        # still come back in the order they were written.
        ordering = ["created_at", "id"]

    def __str__(self):
        who = "Mother" if self.is_user else "Kali"
        return f"{who}: {self.text[:50]}"


class KnowledgeDocument(models.Model):
    """
    A source in the domain-limited knowledge base that is NOT a public
    Knowledge Page article -- the manuscript's "research team's reviewed
    literature" half of it (see the Trusted Access definition), alongside
    the "organization-verified reference materials" that articles.Article
    already covers.

    Deliberately a separate model rather than more Article rows. An
    Article is published content: it appears on the Verified Knowledge
    Page, carries a teaser/read_time/rating, and mothers comment on it.
    Reviewed literature is retrieval-only -- Kali may ground an answer in
    it and cite it, but it is never rendered as a browsable article, and
    it needs a real citation field that Article has no place for.
    """

    title = models.CharField(max_length=255)
    citation = models.CharField(
        max_length=500,
        help_text="Full reference as it should be cited, e.g. "
        "\"WHO (2023). Infant and young child feeding.\"",
    )
    content = models.TextField(help_text="The reviewed text itself. Plain text; blank lines separate paragraphs.")
    is_active = models.BooleanField(
        default=True,
        help_text="Uncheck to withdraw a source from retrieval without deleting it.",
    )
    added_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["title"]

    def __str__(self):
        return self.title


class KnowledgeChunk(models.Model):
    """
    One retrievable passage from the knowledge base, plus its embedding.

    Source identity is denormalized (kind + id + title + reference)
    rather than held as two nullable foreign keys, for two reasons. The
    index spans two apps -- articles.Article and this app's
    KnowledgeDocument -- and chat already avoids importing articles at
    module scope on purpose (see retrieval.py). And the chunk table is
    a derived cache: it is rebuilt from its sources by
    `manage.py reindex_knowledge` at any time, so it carries the few
    fields a citation needs instead of joining back for them.

    Embeddings live in a JSONField of floats rather than a pgvector
    column: this project runs SQLite in dev and Postgres in production
    (see DATABASES), and a vector column would exist in only one of
    them. At this corpus size -- tens of documents, hundreds of chunks --
    cosine similarity in Python is a few milliseconds, so an index type
    that forces the two environments apart buys nothing.
    """

    class SourceKind(models.TextChoices):
        ARTICLE = "article", "Verified article"
        LITERATURE = "literature", "Reviewed literature"

    source_kind = models.CharField(max_length=20, choices=SourceKind.choices)
    source_id = models.PositiveIntegerField()
    source_title = models.CharField(max_length=255)
    source_reference = models.CharField(
        max_length=500,
        blank=True,
        help_text="Article category, or the literature citation.",
    )
    ordinal = models.PositiveIntegerField(help_text="Position of this passage within its source document.")
    text = models.TextField()

    embedding = models.JSONField(
        default=list,
        blank=True,
        help_text="Embedding vector, or [] when not embedded yet -- such a chunk is still "
        "retrievable lexically.",
    )
    embedding_model = models.CharField(
        max_length=100,
        blank=True,
        help_text="Which model produced `embedding`. A change here invalidates the vector, "
        "since similarities are only comparable within one model's space.",
    )
    # Hash of the source document's indexed text. Cheap way to answer
    # "has this document changed since we chunked it" without diffing
    # the content itself on every request.
    source_revision = models.CharField(max_length=64)
    indexed_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["source_kind", "source_id", "ordinal"]
        constraints = [
            models.UniqueConstraint(
                fields=["source_kind", "source_id", "ordinal"],
                name="unique_chunk_per_source_position",
            )
        ]
        indexes = [models.Index(fields=["source_kind", "source_id"])]

    def __str__(self):
        return f"{self.source_title} [{self.ordinal}]"

    @property
    def citation(self):
        """How this passage should be named in an answer."""
        if self.source_kind == self.SourceKind.LITERATURE:
            return self.source_reference or self.source_title
        return f'"{self.source_title}" in the Knowledge Hub'
