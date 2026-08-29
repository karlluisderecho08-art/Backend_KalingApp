"""
The server-side replacement for the Kotlin app's OpenAiChatClient
(data/Network.kt). Same three "silently fall back" conditions as the
original: no key, still the placeholder, or anything goes wrong.

Uses Python's stdlib (urllib), not a new package -- the Kotlin client
itself is described as "plain OkHttp, no special SDK," so this mirrors
that rather than adding a dependency for a single HTTP call this repo
can't even test yet (no real key configured -- see the "fallback-only
for now" decision).
"""

import json
import urllib.error
import urllib.request

from django.conf import settings

from core.audit import log_action

from .local_fallback import get_local_clinical_response

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
PLACEHOLDER_KEY = "MY_OPENAI_API_KEY"

SYSTEM_PROMPT = (
    "You are Kali, a breastfeeding and lactation support assistant. Ground your "
    "answers in WHO, AAP, and IBCLC guidance. Stay strictly within breastfeeding "
    "and lactation topics. For anything resembling a medical emergency or a "
    "mental health crisis, direct the user to a real healthcare professional "
    "instead of attempting to handle it yourself."
)


def get_ai_response(prompt, model):
    """
    Returns (reply_text, token_count, used_fallback: bool).
    Never raises -- any failure just means used_fallback=True.
    """
    api_key = settings.OPENAI_API_KEY
    if not api_key or api_key == PLACEHOLDER_KEY:
        return get_local_clinical_response(prompt), 0, True

    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    }).encode()

    req = urllib.request.Request(
        OPENAI_URL,
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        reply = data["choices"][0]["message"]["content"]
        tokens = data.get("usage", {}).get("total_tokens", 0)
        return reply, tokens, False
    except (urllib.error.URLError, KeyError, json.JSONDecodeError, TimeoutError) as exc:
        # A real key is configured but the call still failed -- record
        # *that it happened*, without ever writing the key or the raw
        # response body anywhere. Otherwise a real problem (quota
        # exhausted, key revoked, network down) looks identical to
        # "no key configured" forever, with nothing to notice it by.
        detail = f"HTTP {exc.code}" if isinstance(exc, urllib.error.HTTPError) else type(exc).__name__
        log_action(None, "chat.openai_call_failed", detail)
        return get_local_clinical_response(prompt), 0, True
