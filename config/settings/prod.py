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
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

# HSTS deliberately NOT enabled yet -- Django's own check warns it can
# cause serious, hard-to-undo problems if turned on before HTTPS is
# confirmed solid (a browser will refuse plain HTTP for the HSTS window
# even if something's misconfigured). Add SECURE_HSTS_SECONDS once
# you've verified HTTPS works cleanly on the real deployed URL.

# Comma-separated list of real web frontend URLs, e.g.
#   CORS_ALLOWED_ORIGINS=https://kalingapp-admin.vercel.app,https://kalingapp-facility.vercel.app
# Defaults to empty -- until this is set, no web frontend can call this
# API from a browser (the Android app is unaffected either way, CORS is
# a browser-only rule). Set this in Render's dashboard once the web
# dashboards have real hosting URLs.
CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS", default=[])
