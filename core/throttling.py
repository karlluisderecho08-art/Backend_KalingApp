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

Keying on the leftmost entry instead -- the address the edge first saw
-- gives one bucket per client again.

Known limitation, deliberately accepted: a caller can put anything in
X-Forwarded-For, so someone determined can rotate that header and slip
past this. It still stops the realistic threat, which is an unattended
script hammering one endpoint, and it is not the only thing standing
in the way: verification and reset codes have their own per-account
attempt caps and cooldowns that no amount of header spoofing touches.
Pinning this to a header the edge is trusted to overwrite would be the
stronger fix if that ever becomes worth it.
"""

from rest_framework.throttling import AnonRateThrottle, ScopedRateThrottle


class _ClientIPIdentMixin:
    def get_ident(self, request):
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.META.get("REMOTE_ADDR")


class ClientIPScopedRateThrottle(_ClientIPIdentMixin, ScopedRateThrottle):
    """ScopedRateThrottle that buckets by originating client, not by proxy path."""


class ClientIPAnonRateThrottle(_ClientIPIdentMixin, AnonRateThrottle):
    """AnonRateThrottle with the same correction, for the global default."""
