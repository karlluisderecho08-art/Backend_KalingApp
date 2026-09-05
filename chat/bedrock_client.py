"""
Replaces openai_client.py as the real model behind Kali. Same contract,
same three "silently fall back" conditions (no credentials configured,
a call that fails, anything unexpected in the response shape) -- only
the provider and the auth mechanism changed.

Uses boto3's Bedrock Converse API rather than a raw HTTP call: unlike
OpenAI's single bearer-token header, AWS authenticates with SigV4
request signing, which boto3 handles internally from the access
key/secret pair -- reimplementing that by hand (as openai_client.py
does for OpenAI's simpler auth) isn't worth it for one HTTP call.
Converse is AWS's model-agnostic chat API (the same request/response
shape works across every Bedrock model family), which is why this
reads far more like a normal chat completion than a DeepSeek-specific
integration.

NOTE: written against AWS's documented Converse API contract, but not
yet exercised against a real, credentialed call (no AWS account was
available while building this) -- if the very first real call fails,
paste the exact boto3 exception back and this can be adjusted quickly
rather than guessed at twice.
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

# DeepSeek-R1 is a *reasoning* model -- it spends a chunk of this budget
# on an internal chain-of-thought before it ever writes the visible
# answer. A small max_tokens (fine for a non-reasoning model like
# gpt-4o) risks the response getting cut off before the reasoning even
# finishes, coming back empty. AWS's own guidance caps this at 8192;
# this sits comfortably under that with room for both the thinking and
# the answer.
MAX_TOKENS = 4000

_client = None


def _get_client():
    # Built lazily, once per process -- not at import time, so that
    # importing this module never requires boto3 to already have
    # credentials available (matters for local dev/tests with none set).
    global _client
    if _client is None:
        import boto3

        _client = boto3.client(
            "bedrock-runtime",
            region_name=settings.AWS_BEDROCK_REGION,
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        )
    return _client


def _extract_reply_text(content_blocks):
    """
    A reasoning model's response can come back as more than one content
    block -- e.g. a reasoningContent block (the chain-of-thought) ahead
    of the actual text block -- rather than always a single plain-text
    block the way a non-reasoning model replies. Scan for the block
    that actually has text instead of assuming content[0] is it.
    """
    for block in content_blocks:
        text = block.get("text")
        if text:
            return text
    return ""


def get_ai_response(prompt, model=None):
    """
    Returns (reply_text, token_count, used_fallback: bool). Never
    raises -- any failure just means used_fallback=True.

    `model` is accepted only to keep this a drop-in replacement for
    openai_client.get_ai_response()'s call sites -- there is currently
    only one real model behind this (see chat/views.py; the old
    two-tier gpt-4o/gpt-4o-mini cost-saving switch was intentionally
    not carried over when the model changed), so it's unused here.
    """
    if not settings.AWS_ACCESS_KEY_ID or not settings.AWS_SECRET_ACCESS_KEY:
        return get_local_clinical_response(prompt), 0, True

    from botocore.exceptions import BotoCoreError, ClientError

    try:
        client = _get_client()
        response = client.converse(
            modelId=settings.AWS_BEDROCK_MODEL_ID,
            system=[{"text": SYSTEM_PROMPT}],
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": MAX_TOKENS, "temperature": 0.4},
        )
        reply = _extract_reply_text(response["output"]["message"]["content"])
        if not reply:
            raise ValueError("Bedrock response had no text content")
        tokens = response.get("usage", {}).get("totalTokens", 0)
        return reply, tokens, False
    except (BotoCoreError, ClientError, KeyError, ValueError) as exc:
        # Same discipline as openai_client.py: record that a real call
        # was attempted and failed, without ever logging credentials or
        # the raw response body -- so a real problem (model access not
        # yet approved in the console, bad credentials, quota) doesn't
        # look identical to "no credentials configured" forever.
        log_action(None, "chat.bedrock_call_failed", type(exc).__name__)
        return get_local_clinical_response(prompt), 0, True
