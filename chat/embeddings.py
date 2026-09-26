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


def _embed(texts, task_type):
    """Returns a list of vectors (or Nones) the same length as `texts`."""
    if not texts:
        return []
    if not embeddings_available():
        return [None] * len(texts)

    from google.genai import types

    vectors = []
    client = _get_client()
    for start in range(0, len(texts), BATCH_SIZE):
        batch = texts[start:start + BATCH_SIZE]
        try:
            response = client.models.embed_content(
                model=settings.GEMINI_EMBEDDING_MODEL,
                contents=batch,
                config=types.EmbedContentConfig(task_type=task_type),
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


def embed_passages(texts):
    """Embed knowledge-base passages for storage."""
    return _embed(texts, "RETRIEVAL_DOCUMENT")


def embed_query(text):
    """Embed one mother's question. Returns a vector or None."""
    return _embed([text], "RETRIEVAL_QUERY")[0]


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
