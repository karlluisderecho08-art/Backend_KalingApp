"""
Gemini embedding calls, with the same never-raise contract as
gemini_client.get_ai_response().

Every function here returns None (or a list of Nones) rather than
propagating an exception, because retrieval has a working lexical ranker
to fall back on: an embedding outage should cost answer *quality*, never
availability. chat/retrieval.py is written to score whatever vectors it
actually has and ignore the rest.

Document and query embeddings are requested with different task types.
Gemini's retrieval embeddings are asymmetric on purpose -- a question
and the passage answering it are not the same kind of text, and telling
the model which side it is looking at measurably improves the match.
Mixing them up is silent: you get vectors, they are just worse.
"""

from django.conf import settings

from core.audit import log_action

# Gemini rejects oversized batches; chunk the batch rather than the
# corpus. Comfortably under the documented request limit.
BATCH_SIZE = 32

# How long one embedding call may block, in milliseconds, when it is made
# while a mother is waiting for her reply.
#
# This has to be bounded, and the bound has to leave room for what runs
# after it. Embedding happens on the chat request path *before* the
# generation call, whose own HTTP timeout is 25 s
# (gemini_client.HTTP_TIMEOUT_MS) precisely so that it fails as a
# catchable exception before gunicorn's 40 s worker timeout kills the
# process (render.yaml). One request can make two embedding calls -- a
# batch of not-yet-embedded passages (retrieval.ensure_embeddings) and
# the question itself -- so the worst case is 5 + 5 + 25 = 35 s, inside
# the 40 s limit. test_embedding_timeouts.py asserts that arithmetic, so
# raising any of the three numbers fails a test instead of reopening this.
#
# Without it the call has no timeout at all: measured against a server
# that accepts the connection and never answers, embed_content was still
# blocked after 30 s. That is a request that dies to gunicorn rather than
# degrading to keyword retrieval, which is what a failed embedding is
# supposed to cost.
EMBED_REQUEST_TIMEOUT_MS = 5_000

# For `manage.py reindex_knowledge`, which embeds the whole corpus up
# front and has no mother waiting on it -- and so no 40 s ceiling either.
EMBED_BULK_TIMEOUT_MS = 60_000

_client = None


def _get_client():
    # Lazily built and cached per process, same as gemini_client's -- so
    # importing this module never needs a key present.
    global _client
    if _client is None:
        from google import genai

        _client = genai.Client(api_key=settings.GEMINI_API_KEY)
    return _client


def embeddings_available():
    return bool(settings.GEMINI_API_KEY)


def _embed(texts, task_type, timeout_ms=None):
    """
    Returns a list of vectors (or Nones) the same length as `texts`.

    `timeout_ms` applies to each batch's call separately, and a call that
    exceeds it is handled exactly like any other failed call below: its
    texts get None, and retrieval carries on without them. None means the
    request-path default, looked up here at call time rather than bound
    as a default argument, so the module constant is the one place it is
    set.
    """
    if not texts:
        return []
    if not embeddings_available():
        return [None] * len(texts)
    if timeout_ms is None:
        timeout_ms = EMBED_REQUEST_TIMEOUT_MS

    from google.genai import types

    vectors = []
    client = _get_client()
    for start in range(0, len(texts), BATCH_SIZE):
        batch = texts[start:start + BATCH_SIZE]
        try:
            response = client.models.embed_content(
                model=settings.GEMINI_EMBEDDING_MODEL,
                contents=batch,
                config=types.EmbedContentConfig(
                    task_type=task_type,
                    http_options=types.HttpOptions(timeout=timeout_ms),
                ),
            )
            returned = list(response.embeddings or [])
            if len(returned) != len(batch):
                raise ValueError(
                    f"embed_content returned {len(returned)} vectors for {len(batch)} inputs"
                )
            for item in returned:
                values = list(getattr(item, "values", None) or [])
                vectors.append(values or None)
        except Exception as exc:
            # One failed batch must not discard the batches that worked.
            log_action(None, "chat.embedding_call_failed", type(exc).__name__)
            vectors.extend([None] * len(batch))

    return vectors


def embed_passages(texts, timeout_ms=None):
    """Embed knowledge-base passages for storage."""
    return _embed(texts, "RETRIEVAL_DOCUMENT", timeout_ms)


def embed_query(text, timeout_ms=None):
    """Embed one mother's question. Returns a vector or None."""
    return _embed([text], "RETRIEVAL_QUERY", timeout_ms)[0]


def cosine_similarity(left, right):
    """
    Cosine similarity of two equal-length vectors, or None if they can't
    be compared.

    Guards length mismatch rather than truncating: vectors from two
    different embedding models are not comparable at all, and silently
    scoring the overlapping prefix would produce a plausible-looking
    number with no meaning.
    """
    if not left or not right or len(left) != len(right):
        return None

    dot = 0.0
    left_norm = 0.0
    right_norm = 0.0
    for a, b in zip(left, right):
        dot += a * b
        left_norm += a * a
        right_norm += b * b
    if left_norm <= 0 or right_norm <= 0:
        return None
    return dot / ((left_norm ** 0.5) * (right_norm ** 0.5))
