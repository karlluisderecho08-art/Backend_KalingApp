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

from .knowledge import build_system_prompt
from .local_fallback import get_local_clinical_response

SYSTEM_PROMPT = (
    "You are Kali, a breastfeeding and lactation support assistant. Stay strictly "
    "within breastfeeding and lactation topics. For anything resembling a medical "
    "emergency or a mental health crisis, direct the user to a real healthcare "
    "professional instead of attempting to handle it yourself. "
    "Keep replies conversational and concise -- a short paragraph or two, or a "
    "brief bulleted list, the way a real chat message reads, not an exhaustive "
    "article. Cover the most important points fully rather than listing every "
    "possible point briefly; if there's clearly more that could help, end by "
    "offering to go deeper rather than cramming it all in at once. "
    "The conversation so far is provided; treat short replies like \"yes\", "
    "\"sure\" or \"tell me more\" as answers to what you just asked, not as new "
    "questions out of nowhere."
)

# How many past messages to replay to the model. Without any, every turn
# arrived with no idea what came before, so a mother answering "yes" to
# Kali's own "would you like to know more?" was read as a brand-new
# question about nothing. Bounded rather than unbounded because the
# whole Knowledge Hub already sits in the system prompt: history is the
# part that grows without limit over a long session, and a runaway
# prompt costs both latency and the MAX_TOKENS headroom for the answer.
HISTORY_TURNS = 10

# 4000 (carried over from bedrock_client.py, see the module docstring)
# made real generations slow enough to trip gunicorn's worker timeout.
# Dropping to 1024 alone fixed the timeouts but traded them for a new
# problem: confirmed via response.candidates[0].finish_reason ==
# MAX_TOKENS that Gemini was hitting that cap mid-answer and just
# stopping -- an incomplete reply instead of a slow/failed one. The
# SYSTEM_PROMPT addition above is the real fix (asking for an actually
# concise reply instead of relying on a hard cutoff to enforce brevity);
# this bump to 1536 is just extra headroom so a reply that runs slightly
# long still finishes as a complete thought instead of getting cut off.
MAX_TOKENS = 1536
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


def get_ai_response(prompt, model=None, history=None):
    """
    Returns (reply_text, token_count, used_fallback: bool). Never
    raises -- any failure just means used_fallback=True.

    `history` is the earlier turns of this conversation, oldest first,
    as (is_user, text) pairs. Without it every message reached the model
    with no memory of the exchange it belonged to.

    `model` is accepted only to keep this a drop-in replacement for
    bedrock_client.get_ai_response()'s call sites -- unused here, same
    as it was unused there.
    """
    if not settings.GEMINI_API_KEY:
        return get_local_clinical_response(prompt), 0, True

    try:
        from google.genai import types

        contents = []
        for is_user, text in (history or [])[-HISTORY_TURNS:]:
            # Gemini names the assistant role "model", not "assistant".
            contents.append(
                types.Content(role="user" if is_user else "model", parts=[types.Part(text=text)])
            )
        contents.append(types.Content(role="user", parts=[types.Part(text=prompt)]))

        client = _get_client()
        response = client.models.generate_content(
            model=settings.GEMINI_MODEL,
            contents=contents,
            config=types.GenerateContentConfig(
                # Rebuilt per call, not cached at import: it embeds the
                # Knowledge Hub articles, and admins add those through
                # the admin panel while the server is running. Caching
                # would mean a new article silently not existing to Kali
                # until the next deploy.
                system_instruction=build_system_prompt(
                    SYSTEM_PROMPT, strict=settings.CHAT_STRICT_KNOWLEDGE_ONLY
                ),
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
