"""
Tagalog for the server's own mother-facing text.

The Android app sends ``Accept-Language: tl`` while it is set to Tagalog
(KalingApiClient.kt). For those requests the server's messages -- error
``detail``s and field errors, notification titles and descriptions, the
verification/reset emails, Kali's fixed replies -- go out in Tagalog. The
staff/admin web consoles never send it, so they keep getting English.

How it works
------------
Everything is still *written* in English (notifications are stored in
English, facility staff read them that way). ``translate()`` maps an English
message to its Tagalog at the moment it leaves the server, using the reviewed
catalog in ``core/tagalog_catalog.py``:

* exact messages ("Invalid email or password."), and
* templates with ``{name}`` slots ('Your request has moved to the "{stage}"
  phase.'), where the slot's value is itself translated if it is a known
  label (stage names, statuses, request types) and kept as-is otherwise
  (facility names, article titles, numbers).

Text that is not in the catalog is returned unchanged, so a message added
later shows in English rather than wrong -- core/tests_i18n.py fails until it
gets its Tagalog line.

``TagalogResponseMiddleware`` applies this to API responses: every string of
an error response (4xx/5xx bodies are all messages), and on success only the
fields listed in ``_SUCCESS_KEYS`` -- never data the app compares, such as
``current_sub_status`` or ``stages``.
"""

import json
import re
from functools import lru_cache

from .tagalog_catalog import LABELS, MESSAGES

_PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


def is_tagalog(accept_language):
    """True when the first language asked for is Tagalog/Filipino."""
    first = (accept_language or "").split(",")[0].split(";")[0].strip().lower()
    return first in ("tl", "fil") or first.startswith(("tl-", "fil-"))


def wants_tagalog(request):
    if request is None:
        return False
    return is_tagalog(request.META.get("HTTP_ACCEPT_LANGUAGE", ""))


@lru_cache(maxsize=1)
def _compiled():
    exact = {}
    templates = []
    for english, tagalog in MESSAGES.items():
        names = _PLACEHOLDER.findall(english)
        if not names:
            exact[english] = tagalog
            continue
        pattern, seen, pos = "", set(), 0
        for m in _PLACEHOLDER.finditer(english):
            pattern += re.escape(english[pos:m.start()])
            name = m.group(1)
            pattern += f"(?P={name})" if name in seen else f"(?P<{name}>.+?)"
            seen.add(name)
            pos = m.end()
        pattern += re.escape(english[pos:])
        literal = _PLACEHOLDER.sub("", english)
        # A template whose fixed text is only punctuation ("{title}. {message}")
        # would otherwise match any two sentences; it applies only when its
        # parts are themselves known messages.
        join_only = not any(c.isalpha() for c in literal)
        templates.append((len(literal), re.compile(r"\A" + pattern + r"\Z", re.DOTALL), tagalog, join_only))
    # The most specific template (most fixed text) wins when two could match.
    templates.sort(key=lambda t: -t[0])
    return exact, templates


_MONTHS = {
    "January": "Enero", "February": "Pebrero", "March": "Marso", "April": "Abril",
    "May": "Mayo", "June": "Hunyo", "July": "Hulyo", "August": "Agosto",
    "September": "Setyembre", "October": "Oktubre", "November": "Nobyembre", "December": "Disyembre",
}
_DATE = re.compile(r"\A(" + "|".join(_MONTHS) + r") (\d{1,2}), (\d{4})\Z")




def _translate_value(value):
    """A slot's value: a known label, message or date in Tagalog, else unchanged."""
    if value in LABELS:
        return LABELS[value]
    date = _DATE.match(value)
    if date:
        return f"{_MONTHS[date.group(1)]} {date.group(2)}, {date.group(3)}"
    exact, _ = _compiled()
    return exact.get(value, value)


def translate(text):
    """``text`` in Tagalog when the catalog knows it; otherwise ``text`` itself."""
    if not isinstance(text, str) or not text:
        return text
    exact, templates = _compiled()
    if text in exact:
        return exact[text]
    # LABELS are deliberately not looked up on their own here: a mother's own
    # words ("Results") must never be rewritten. They apply inside a slot.
    for _, regex, tagalog, join_only in templates:
        m = regex.match(text)
        if not m:
            continue
        raw = m.groupdict()
        values = {k: _translate_value(v) for k, v in raw.items()}
        # A join applies once one of its parts is a known message; a part she
        # or the staff wrote themselves (an unknown sentence) is kept as is.
        if join_only and all(values[k] == raw[k] for k in raw):
            continue
        return _PLACEHOLDER.sub(lambda p: values[p.group(1)], tagalog)
    return text


def tr(text, request=None, tagalog=None):
    """``text`` in the language of ``request`` (or of ``tagalog`` when given)."""
    if tagalog is None:
        tagalog = wants_tagalog(request)
    return translate(text) if tagalog else text


def _translate_all(data):
    if isinstance(data, str):
        return translate(data)
    if isinstance(data, list):
        return [_translate_all(v) for v in data]
    if isinstance(data, dict):
        return {k: _translate_all(v) for k, v in data.items()}
    return data


# On a successful response, only these fields are messages. Everything else
# is data -- statuses, stage lists, names -- that the app matches on in English.
_SUCCESS_KEYS = {"detail", "message"}


def _translate_keys(data, keys):
    if isinstance(data, list):
        return [_translate_keys(v, keys) for v in data]
    if isinstance(data, dict):
        if data.get("is_user") is True:
            return data  # her own chat message, exactly as she wrote it
        return {
            k: (_translate_all(v) if k in keys else _translate_keys(v, keys))
            for k, v in data.items()
        }
    return data


def _success_keys_for(path):
    keys = set(_SUCCESS_KEYS)
    for prefix, extra in SUCCESS_KEYS_BY_PATH.items():
        if path.startswith(prefix):
            keys |= extra
    return keys


# Endpoint-specific message fields on success responses (filled in from the
# backend inventory; see core/tagalog_catalog.py for the texts themselves).
SUCCESS_KEYS_BY_PATH = {
    # Notifications are stored in English (staff read the same rows, and most
    # are written during someone else's request); translated here, when she
    # reads them. title_en/description_en stay English for the app's own
    # matching (NotificationGuidance.kt).
    "/notifications/": {"title", "description"},
    # Why a request ended / how it went: MilkBankRequestSerializer.result.
    "/milkbank/": {"result"},
    # Kali's fixed replies (off-topic, offline fallbacks) are stored in English
    # and so keep matching guardrail checks; her own messages are skipped above.
    "/chat/": {"text"},
    "/directory/": {"description"},
}


class TagalogResponseMiddleware:
    """Serves the catalog's Tagalog to requests that ask for it (see module doc)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if not wants_tagalog(request):
            return response
        if getattr(response, "streaming", False):
            return response
        if not response.get("Content-Type", "").startswith("application/json"):
            return response
        try:
            data = json.loads(response.content.decode(response.charset or "utf-8"))
        except (ValueError, UnicodeDecodeError):
            return response
        if response.status_code >= 400:
            translated = _translate_all(data)
        else:
            translated = _translate_keys(data, _success_keys_for(request.path))
        if translated != data:
            response.content = json.dumps(translated, ensure_ascii=False).encode("utf-8")
            response["Content-Type"] = "application/json; charset=utf-8"
            if response.has_header("Content-Length"):
                response["Content-Length"] = str(len(response.content))
        response.headers.setdefault("Content-Language", "tl")
        patch_vary(response)
        return response


def patch_vary(response):
    from django.utils.cache import patch_vary_headers

    patch_vary_headers(response, ("Accept-Language",))
