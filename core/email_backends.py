"""
Sends email through Brevo's HTTP API instead of SMTP.

This exists because of a concrete, measured failure: the live backend
cannot open an SMTP connection at all. A real send attempt from the
server recorded

    email.verification_failed -- via smtp.gmail.com
    -- OSError: [Errno 101] Network is unreachable

which is the host blocking outbound SMTP (ports 25/465/587), the usual
anti-spam posture on free hosting tiers. That's not specific to one
provider, so swapping Gmail for Brevo/SendGrid *over SMTP* could never
have worked -- every one of them would fail identically, before the
provider was ever reached. It also explains why the same code sent
fine from a laptop and silently nothing from the server, and why no
bounce ever appeared: the mail never left the building.

HTTPS on 443 is obviously not blocked (the chat feature already calls
Gemini over it), so this goes out that way instead.

Written as a Django email backend rather than a helper function so
nothing else has to change: accounts/emails.py keeps calling plain
send_mail(), and the test suite keeps using Django's in-memory backend.
Only EMAIL_BACKEND in settings points here.

Uses urllib from the standard library rather than adding requests/httpx
as a dependency for one POST.
"""

import json
import urllib.error
import urllib.request
from email.utils import parseaddr

from django.core.mail.backends.base import BaseEmailBackend

BREVO_SEND_URL = "https://api.brevo.com/v3/smtp/email"

# Bounded so a hung API call can't hold a worker open indefinitely --
# same reasoning as EMAIL_TIMEOUT for the SMTP backends, and as
# chat/gemini_client.py's HTTP_TIMEOUT_MS.
TIMEOUT_SECONDS = 15


class BrevoApiEmailBackend(BaseEmailBackend):
    """Delivers each message via one POST to Brevo's transactional endpoint."""

    def __init__(self, fail_silently=False, **kwargs):
        super().__init__(fail_silently=fail_silently, **kwargs)
        # Imported here rather than at module scope so that merely
        # importing this module never requires the setting to exist --
        # matters for tests and for local dev with no key configured.
        from django.conf import settings

        self.api_key = getattr(settings, "BREVO_API_KEY", "")

    def send_messages(self, email_messages):
        if not email_messages:
            return 0

        sent = 0
        for message in email_messages:
            try:
                self._send(message)
            except Exception:
                if not self.fail_silently:
                    raise
            else:
                sent += 1
        return sent

    def _send(self, message):
        if not self.api_key:
            raise ValueError("BREVO_API_KEY is not configured")

        sender_name, sender_email = parseaddr(message.from_email)
        sender = {"email": sender_email}
        if sender_name:
            sender["name"] = sender_name

        payload = {
            "sender": sender,
            "to": [{"email": parseaddr(address)[1]} for address in message.recipients()],
            "subject": message.subject,
            "textContent": message.body,
        }

        request = urllib.request.Request(
            BREVO_SEND_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "api-key": self.api_key,
                "content-type": "application/json",
                "accept": "application/json",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            # Brevo puts the actual reason in the body ("sender not
            # verified", "unrecognised key", quota, ...), which is far
            # more useful than the bare status code alone. Surfaced as a
            # normal exception so accounts/views.py's audit logging
            # records it against the account it belongs to. The api-key
            # is a request *header* and never appears in this body.
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"Brevo API returned {exc.code}: {detail}") from exc
