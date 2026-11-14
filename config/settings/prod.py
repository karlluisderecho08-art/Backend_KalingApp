from .base import *  # noqa: F401,F403

DEBUG = False

# Both of these must be set via env in prod — no default, so a
# misconfigured deploy fails loudly at startup instead of silently
# running with an insecure key or accepting all hosts. base.py's
# SECRET_KEY has a dev-only default; this re-reads it without one.
SECRET_KEY = env("DJANGO_SECRET_KEY")
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS")

# Render terminates HTTPS at its own edge and forwards plain HTTP to
# this app -- without telling Django that, SECURE_SSL_REDIRECT would
# see every request as "already HTTP" and redirect-loop forever. This
# header is how Render (like Heroku) signals the original protocol.
# Throttle counters (REST_FRAMEWORK's DEFAULT_THROTTLE_RATES) live in
# the cache, so the cache must be shared across processes or the limits
# don't actually hold. Django's default is LocMemCache -- per-process
# memory -- and gunicorn runs several workers here, each keeping its own
# private counter. Requests spread across them, so the effective limit
# became roughly rate x workers.
#
# Not a theoretical concern: rate limiting passed its tests (one
# process) and then did nothing whatsoever in production. Confirmed by
# sending 14 login attempts against the deployed service and getting 14
# rejections for the wrong password, zero for being throttled.
#
# Database-backed rather than Redis purely to avoid adding a paid
# service -- it reuses the Postgres instance already running. The
# write-per-throttled-request costs nothing at this scale, and build.sh
# runs `createcachetable` (idempotent) to make the table.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.db.DatabaseCache",
        "LOCATION": "django_cache_table",
    }
}

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

# HSTS: browsers that have seen this API once refuse to talk to it over
# plain HTTP for a year, so a network attacker can't downgrade the first
# request. Turned on once HTTPS was confirmed on the live URL (2026-11-14:
# https serves 200, http answers 301 to https). includeSubDomains covers
# only names UNDER this API's own host (it has none), never the rest of
# onrender.com. Not preloaded: that is a manual submission to the
# browsers' built-in list which is slow to undo, so Django's reminder
# about it (security.W021) is silenced on purpose.
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = False
SILENCED_SYSTEM_CHECKS = ["security.W021"]

# This project's own deployed dashboards, trusted no matter what the
# environment says. They lived in CORS_ALLOWED_ORIGINS alone at first,
# and the moment they went live the env var here was still listing only
# the two localhost ports -- so the browser blocked every login response
# on both dashboards. Worth knowing how that presents: the frontend
# can't see a blocked response at all, so its fetch throws a bare
# TypeError and the only thing it can honestly show the user is "Could
# not reach the server," which looks exactly like the backend being
# down. Pinning the canonical origins in code means a stale or
# forgotten dashboard edit can't take first-party login offline again.
FIRST_PARTY_DASHBOARD_ORIGINS = [
    "https://kalingapp-admin-portal.vercel.app",
    "https://kalingapp-facility.vercel.app",
]

# The env var still works, for anything not known at build time (a
# custom domain, a move off Vercel, a reviewer's own deploy). Comma
# separated, e.g. CORS_ALLOWED_ORIGINS=https://kalingapp.ph
# dict.fromkeys rather than set(), so the order stays readable in
# /admin/ and in any debugging dump of this setting.
CORS_ALLOWED_ORIGINS = list(
    dict.fromkeys(env.list("CORS_ALLOWED_ORIGINS", default=[]) + FIRST_PARTY_DASHBOARD_ORIGINS)
)

# No pattern-matched origins. This used to also allow any
# kalingapp-(admin|facility)-<anything>.vercel.app, for Vercel's
# per-deployment preview URLs -- but anyone can create a Vercel project
# with a name that fits that pattern, so it admitted sites this project
# doesn't own. A preview that needs the live API can be added by exact
# URL through CORS_ALLOWED_ORIGINS above.
# (kalingapp-admin.vercel.app was also dropped: it is an old, stale admin
# deployment; the admin console lives at kalingapp-admin-portal.)
CORS_ALLOWED_ORIGIN_REGEXES = []
