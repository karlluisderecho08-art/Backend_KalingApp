"""
Replaces bedrock_client.py as the real model behind Kali. Same contract,
same three "silently fall back" conditions (no key configured, a call
that fails, anything unexpected in the response shape) -- only the
provider and auth mechanism changed again (AWS Bedrock's billing/
Marketplace subscription never went live, so this moves to Gemini's
free tier instead).

Gemini authenticates with a single API key, closer to the original
openai_client.py's bearer-token style than Bedrock's SigV4 request
signing -- so this reads simpler than bedrock_client.py, not because
less care went into it, but because Gemini's auth genuinely needs less
code around it.

NOTE: like bedrock_client.py before it, written against Google's
documented google-genai SDK contract but not yet exercised against a
real key at the time this was written -- if the first real call fails,
paste the exact exception back and this can be adjusted quickly rather
than guessed at twice.
"""

from django.conf import settings

from core.audit import log_action

from .local_fallback import get_local_clinical_response

SYSTEM_PROMPT = (
    "You are Kali, a breastfeeding and lactation support assistant. Ground your "
    "answers in WHO, AAP, and IBCLC guidance. Stay strictly within breastfeeding "
    "and lactation topics. For anything resembling a medical emergency or a "
    "mental health crisis, direct the user to a real healthcare professional "
    "instead of attempting to handle it yourself."
)

MAX_TOKENS = 4000

_client = None


def _get_client():
    # Built lazily, once per process -- not at import time, so that
    # importing this module never requires google-genai to already have
    # a key available (matters for local dev/tests with none set).
    global _client
    if _client is None:
        from google import genai

        _client = genai.Client(api_key=settings.GEMINI_API_KEY)
    return _client


def get_ai_response(prompt, model=None):
    """
    Returns (reply_text, token_count, used_fallback: bool). Never
    raises -- any failure just means used_fallback=True.

    `model` is accepted only to keep this a drop-in replacement for
    bedrock_client.get_ai_response()'s call sites -- unused here, same
    as it was unused there.
    """
    if not settings.GEMINI_API_KEY:
        return get_local_clinical_response(prompt), 0, True

    try:
        from google.genai import types

        client = _get_client()
        response = client.models.generate_content(
            model=settings.GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                max_output_tokens=MAX_TOKENS,
                temperature=0.4,
            ),
        )
        reply = response.text
        if not reply:
            raise ValueError("Gemini response had no text content")
        tokens = getattr(response.usage_metadata, "total_token_count", None) or 0
        return reply, tokens, False
    except Exception as exc:
        # Broad on purpose, unlike bedrock_client.py's curated
        # (BotoCoreError, ClientError, KeyError, ValueError) list: the
        # never-raises guarantee here is load-bearing (a chat message
        # must never 500 the request), and google-genai's exact failure
        # surface hasn't been exercised against a real key yet the way
        # boto3's was already well-documented by the time that was written.
        log_action(None, "chat.gemini_call_failed", type(exc).__name__)
        return get_local_clinical_response(prompt), 0, True
