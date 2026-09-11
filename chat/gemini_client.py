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

NOTE: exercised against a real key now -- it works (confirmed with a
genuine, on-topic reply and a real token count), but the first attempt
against the live Render backend hit exactly the SendGrid-style bug
already documented in accounts/views.py: a long generation (this model
can legitimately take a while for a detailed, MAX_TOKENS=4000 answer)
ran past gunicorn's default 30s worker timeout, which kills the whole
process from *outside* Python -- no try/except here can catch that,
because the process is gone. HTTP_TIMEOUT_MS below bounds the SDK call
itself to something shorter than gunicorn's timeout (see render.yaml,
bumped alongside this), so a slow call fails fast as a normal,
catchable exception and falls back gracefully instead of taking the
whole worker down with it.
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

# 4000 was carried over from bedrock_client.py without re-checking
# whether it still made sense: DeepSeek-R1 (a *reasoning* model) needed
# that headroom for an invisible chain-of-thought before its visible
# answer, which Gemini doesn't do. Left at 4000 here, real generations
# took long enough to trip a 504 DEADLINE_EXCEEDED against a 25s client
# timeout (and would have kept tripping gunicorn's worker timeout before
# that fix) -- 1024 is still a generous, thorough-answer-length budget
# for an actual chat reply, and finishes fast enough that neither
# timeout below should realistically be needed as anything but a safety
# net for a genuinely stuck call.
MAX_TOKENS = 1024
# Milliseconds -- see the module docstring. Must stay comfortably under
# gunicorn's --timeout (render.yaml) so a slow call fails as a catchable
# Python exception well before gunicorn would kill the process instead.
HTTP_TIMEOUT_MS = 25_000

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
                http_options=types.HttpOptions(timeout=HTTP_TIMEOUT_MS),
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
