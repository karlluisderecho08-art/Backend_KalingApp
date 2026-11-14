"""
Rate limiting that survives being behind a proxy.

DRF identifies an anonymous client by joining the WHOLE
X-Forwarded-For chain when NUM_PROXIES is unset. Behind this host's
edge that chain is `<client>, <edge>, <internal>`, and the two proxy
hops change from request to request, so every request produced a
different cache key and its own private counter:

    throttle_login_58.69.116.244,104.23.160.251,10.28.132.184
    throttle_login_58.69.0.182,104.23.160.210,10.28.131.3
    throttle_login_58.69.0.182,172.70.222.169,10.28.131.3

The limit therefore never triggered no matter how many attempts were
made -- confirmed against production, where 14 rapid login attempts
produced 14 counters of one. Unit tests passed throughout, because the
test client sends no X-Forwarded-For at all and falls through to a
stable REMOTE_ADDR.

Keying on the leftmost entry fixed that, but the leftmost entry is
whatever the CALLER wrote: the edge appends to an X-Forwarded-For the
client sent, it does not replace it. So rotating a fake first entry gave
every request a fresh bucket again, and the login limit could be skipped
at will.

So the client is now counted from the RIGHT. Each proxy in front of this
app appends the address it received the request from, and nothing a
caller sends can change those entries. With TRUSTED_PROXY_HOPS proxies
(Render's edge, then its internal router -- the two hops seen in the
production chains above), the client is the entry that many places in
from the end:

    <anything the caller made up>, <client>, <edge>, <internal>
                                    ^ -(hops + 1)

Fewer entries than that means there were fewer proxies than expected
(local development, tests), and the leftmost entry is then the best
there is.
"""

from django.conf import settings
from rest_framework.throttling import AnonRateThrottle, ScopedRateThrottle, UserRateThrottle


def client_ip(request):
    """The caller's address, read so that a forged X-Forwarded-For can't change it."""
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        parts = [part.strip() for part in forwarded.split(",") if part.strip()]
        if parts:
            hops = max(int(getattr(settings, "TRUSTED_PROXY_HOPS", 2)), 0)
            return parts[max(len(parts) - (hops + 1), 0)]
    return request.META.get("REMOTE_ADDR")


class _ClientIPIdentMixin:
    def get_ident(self, request):
        return client_ip(request)


class ClientIPScopedRateThrottle(_ClientIPIdentMixin, ScopedRateThrottle):
    """ScopedRateThrottle that buckets by originating client, not by proxy path."""


class ClientIPAnonRateThrottle(_ClientIPIdentMixin, AnonRateThrottle):
    """AnonRateThrottle with the same correction, for the global default."""


class ChatBurstThrottle(UserRateThrottle):
    """Kali messages per minute, per account (settings: chat_burst)."""

    scope = "chat_burst"


class ChatDailyThrottle(UserRateThrottle):
    """Kali messages per day, per account (settings: chat_daily)."""

    scope = "chat_daily"
