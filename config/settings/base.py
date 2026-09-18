"""
Settings shared by every environment. dev.py and prod.py both import
everything from here with `from .base import *`, then override the
handful of values that actually need to differ.
"""

from pathlib import Path

import environ

# BASE_DIR is the repo root (two levels up from this file: settings/ -> config/ -> root).
BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env()
# Reads a `.env` file at the repo root, if one exists, into os.environ.
# This mirrors the .env -> Secrets Gradle Plugin pattern already used on
# the Android side: secrets live in a git-ignored file, never in source.
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("DJANGO_SECRET_KEY", default="django-insecure-dev-only-change-me")

# The key the Kotlin app currently ships inside the APK (BuildConfig.
# OPENAI_API_KEY) -- this is the whole point of Phase 7's proxy: the key
# lives here instead, server-side, never shipped to a device. Leave this
# unset (or MY_OPENAI_API_KEY) to run on local-fallback-only responses;
# add a real key to .env to start using the real API, no code changes.
OPENAI_API_KEY = env("OPENAI_API_KEY", default="MY_OPENAI_API_KEY")

# --- AWS Bedrock (Kali chat, DeepSeek-R1) ---
# Superseded OPENAI_API_KEY above as the real model behind chat -- see
# chat/bedrock_client.py. Left OPENAI_API_KEY in place rather than
# deleting it: chat/openai_client.py still exists as a fallback path
# nothing currently calls, in case AWS access ever needs to be reverted.
#
# Unlike OPENAI_API_KEY's single bearer token, Bedrock authenticates
# with a real AWS IAM access key pair (SigV4 signing, handled by
# boto3) -- generated for an IAM user with bedrock:InvokeModel-only
# permission, not the AWS account's root credentials. Leave both blank
# to run on local-fallback-only responses, same as an unset
# OPENAI_API_KEY did.
AWS_ACCESS_KEY_ID = env("AWS_ACCESS_KEY_ID", default="")
AWS_SECRET_ACCESS_KEY = env("AWS_SECRET_ACCESS_KEY", default="")
# us-east-1 is where DeepSeek-R1's Bedrock availability was confirmed
# at integration time -- change this only if AWS moves/expands that,
# and only together with AWS_BEDROCK_MODEL_ID below (a model enabled in
# one region isn't automatically enabled in another).
AWS_BEDROCK_REGION = env("AWS_BEDROCK_REGION", default="us-east-1")
# The cross-region inference profile ID, not the bare model ID --
# DeepSeek-R1 (like several newer Bedrock models) is invoked through an
# inference profile rather than a plain on-demand model ID. Confirm the
# exact ID shown in your own Bedrock console's Model catalog after
# requesting access -- AWS has been known to adjust these.
AWS_BEDROCK_MODEL_ID = env("AWS_BEDROCK_MODEL_ID", default="us.deepseek.r1-v1:0")

# --- Google Gemini (Kali chat) ---
# Superseded AWS_* above as the real model behind chat -- see
# chat/gemini_client.py. The Bedrock Marketplace subscription never
# actually went live (a billing/card decline blocked it), so this moves
# to Gemini's free tier instead. Left the Bedrock/OpenAI settings above
# in place rather than deleting them, same reasoning as before: an easy
# revert if either provider is ever picked back up, not because they're
# still used.
#
# Gemini authenticates with a single API key (like OPENAI_API_KEY did),
# not a signed request -- generate one at https://aistudio.google.com/apikey.
# Leave blank to run on local-fallback-only responses, same as an unset
# OPENAI_API_KEY/AWS_ACCESS_KEY_ID did.
GEMINI_API_KEY = env("GEMINI_API_KEY", default="")
# gemini-3.6-flash (the newest flagship model at integration time) has a
# free-tier quota of only 20 requests/DAY -- confirmed by actually
# hitting it: "429 RESOURCE_EXHAUSTED ... limit: 20". gemini-3.5-flash-
# lite is a separate, much less in-demand model with its own quota
# bucket, and also tested noticeably faster (1-2s vs 7-20s) and never
# hit finish_reason=MAX_TOKENS the way the flagship model did -- a
# better choice on every axis for this app's actual needs, not just a
# workaround for the quota.
GEMINI_MODEL = env("GEMINI_MODEL", default="gemini-3.5-flash-lite")

# True: Kali may answer ONLY from the Knowledge Hub articles, and says
# so plainly when they don't cover something rather than falling back on
# general training data (see chat/knowledge.py). That's deliberate --
# every answer is then traceable to a source the team controls and can
# cite, and the library is expected to grow as admins add articles.
#
# The tradeoff is real and worth knowing before flipping this: with a
# small library Kali genuinely refuses common questions, including milk
# bank donation, which the articles don't currently cover at all. Set
# to False to have her prefer the articles but still help from WHO/AAP/
# IBCLC guidance where they fall short.
CHAT_STRICT_KNOWLEDGE_ONLY = env.bool("CHAT_STRICT_KNOWLEDGE_ONLY", default=True)

# /auth/demo-login/ hands out real tokens to anyone who POSTs to it, no
# credentials at all (it backs the app's "Bypass / Quick-Access Demo
# Mode" button). Off unless explicitly enabled: fine pointed at a
# laptop for a demo, not something to leave reachable on the public
# internet. dev.py turns it back on.
DEMO_LOGIN_ENABLED = env.bool("DEMO_LOGIN_ENABLED", default=False)

# Shared secret for POST /milkbank/sweep-expired/ (see milkbank/views.py's
# StaffSweepExpiredView) -- lets an external scheduler (this host has no
# Celery/cron worker of its own; a free pinger like cron-job.org works)
# expire overdue bookings on a timer instead of only when a mother or
# facility happens to load their requests. Empty means the endpoint refuses
# every request (fails closed), not that the check is skipped -- an unset
# token must never mean "anyone can trigger this."
MILKBANK_SWEEP_TOKEN = env("MILKBANK_SWEEP_TOKEN", default="")

# --- Outgoing email (account verification codes -- see accounts/emails.py) ---
# BREVO_API_KEY is the one that actually works in production, and it's
# checked first below. Everything after it speaks SMTP, which the live
# host blocks outright: a real send attempt from the server recorded
#
#     email.verification_failed -- via smtp.gmail.com
#     -- OSError: [Errno 101] Network is unreachable
#
# i.e. outbound ports 25/465/587 are closed (standard anti-spam posture
# on free hosting tiers). That is provider-independent -- Gmail, Brevo
# and SendGrid all fail the same way before their servers are ever
# reached -- which is why swapping SMTP providers never fixed anything,
# and why identical code sent fine from a laptop and silently nothing
# from the server. The API key path goes over HTTPS/443 instead; see
# core/email_backends.py. This is a *different* credential from
# BREVO_SMTP_KEY: it starts with "xkeysib-" and lives under Brevo's
# SMTP & API -> API keys tab.
BREVO_API_KEY = env("BREVO_API_KEY", default="")

# --- SMTP fallbacks (kept for local dev, where SMTP isn't blocked) ---
# Gmail is tried ahead of Brevo below, which is the opposite of what
# this file originally did. Reason, found the hard way: Brevo's SMTP
# relay rejects this account's credentials outright ("535 5.7.8
# Authentication failed") and has done so consistently across several
# days, two freshly-generated SMTP keys, and both accepted username
# forms -- with the byte-for-byte AUTH payload verified against what
# Brevo's own dashboard displays. Whatever the cause, it's on Brevo's
# side and not something this codebase can fix.
#
# That matters more than it looks: provider selection here is a plain
# if/elif chain resolved once at import, with no runtime failover. So
# while Brevo's SMTP sat first, merely HAVING BREVO_SMTP_* set in the
# server env silently killed every outgoing email -- the send would
# fail auth and there was no fallback to Gmail at send time. Ordering
# Gmail first means a leftover/half-finished Brevo SMTP config can't
# take the whole email system down with it.
#
# Gmail SMTP needs 2-Step Verification turned on for the sending Google
# account, then a 16-character "App Password" generated at
# https://myaccount.google.com/apppasswords -- GMAIL_APP_PASSWORD below
# is that App Password, NOT the account's normal login password (Google
# blocks plain-password SMTP login entirely now).
GMAIL_ADDRESS = env("GMAIL_ADDRESS", default="")
GMAIL_APP_PASSWORD = env("GMAIL_APP_PASSWORD", default="")

# --- Brevo (kept configured, but deliberately NOT first -- see above) ---
# Brevo's SMTP relay login is shown on its dashboard under SMTP & API ->
# SMTP (an opaque alias like b8f6a7001@smtp-relay.brevo.com, not the
# account email); the password is a dedicated "SMTP key" generated on
# that same page, not the account login password. Like every real ESP,
# Brevo also requires verifying the sending address under Senders & IP
# -> Senders before it'll relay mail from it. All of that was done and
# it still refuses to authenticate, so this stays second until Brevo
# support explains why.
BREVO_SMTP_LOGIN = env("BREVO_SMTP_LOGIN", default="")
BREVO_SMTP_KEY = env("BREVO_SMTP_KEY", default="")

# --- SendGrid (superseded by Brevo above; see chat/bedrock_client.py's
# NOTE-style comments for why unused settings are left rather than
# deleted) ---
# SendGrid's SMTP relay always authenticates with the literal username
# "apikey"; the real secret is the password.
SENDGRID_API_KEY = env("SENDGRID_API_KEY", default="")

# Falls back to Django's console backend (prints the email to the
# terminal instead of actually sending it) whenever none of the above
# is configured, so local dev/tests work without needing a real
# account -- but this means production MUST have GMAIL_ADDRESS/
# GMAIL_APP_PASSWORD (or one of the fallbacks below) set in Render's
# env, or verification codes will only ever reach the server log,
# never a mother's inbox.
#
# Deliberately does NOT fall back to BREVO_SMTP_LOGIN: that's an
# auth-only credential (an opaque auto-generated alias like
# b8f6a7001@smtp-relay.brevo.com), not a real address -- it was never
# verified as a sender in Brevo and Brevo would reject it as the From.
# GMAIL_ADDRESS is a reasonable fallback because it's an actual address
# a human owns, and in this project's case is also the one verified as
# a Brevo sender.
DEFAULT_FROM_EMAIL = env(
    "DEFAULT_FROM_EMAIL", default=GMAIL_ADDRESS or "noreply@kalingapp.local",
)

if BREVO_API_KEY:
    # HTTPS, not SMTP -- the only one of these that can actually reach
    # the outside world from the live host. See the note above.
    EMAIL_BACKEND = "core.email_backends.BrevoApiEmailBackend"
    EMAIL_HOST = "api.brevo.com"  # informational: names the transport in audit rows
elif GMAIL_ADDRESS and GMAIL_APP_PASSWORD:
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_HOST = "smtp.gmail.com"
    EMAIL_PORT = 587
    EMAIL_USE_TLS = True
    EMAIL_HOST_USER = GMAIL_ADDRESS
    EMAIL_HOST_PASSWORD = GMAIL_APP_PASSWORD
    # Without this, a blocked/slow outbound connection hangs with no
    # timeout at all -- found the hard way against SendGrid: a stuck
    # send_mail() call held the request open long enough for gunicorn's
    # own worker timeout to kill the process, which happens *outside*
    # Python's control and can't be caught by a try/except in the view.
    # A short, explicit timeout means a bad connection fails fast, as a
    # normal catchable exception, well within that worker timeout.
    EMAIL_TIMEOUT = 10
elif BREVO_SMTP_LOGIN and BREVO_SMTP_KEY:
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_HOST = "smtp-relay.brevo.com"
    EMAIL_PORT = 587
    EMAIL_USE_TLS = True
    EMAIL_HOST_USER = BREVO_SMTP_LOGIN
    EMAIL_HOST_PASSWORD = BREVO_SMTP_KEY
    EMAIL_TIMEOUT = 10
elif SENDGRID_API_KEY:
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_HOST = "smtp.sendgrid.net"
    EMAIL_PORT = 587
    EMAIL_USE_TLS = True
    EMAIL_HOST_USER = "apikey"
    EMAIL_HOST_PASSWORD = SENDGRID_API_KEY
    EMAIL_TIMEOUT = 10
else:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "drf_spectacular",
    "corsheaders",
    "core",
    "accounts",
    "articles",
    "directory",
    "milkbank",
    "notifications",
    "chat",
    "dashboard",
]

# Must be set before the first migration that touches auth tables --
# swapping it later means resetting the database, which is exactly what
# we're about to do since this is still a fresh dev DB.
AUTH_USER_MODEL = "accounts.User"

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Must sit immediately after SecurityMiddleware -- lets gunicorn
    # (which has no built-in static file serving, unlike `runserver`)
    # serve CSS/JS/admin assets directly, without needing a separate
    # nginx/CDN step just to get a testing deploy running.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    # Must sit above CommonMiddleware (django-cors-headers' own
    # requirement) -- this is what lets the admin/facility web dashboards
    # call this API from a different origin (e.g. localhost:5173) at all.
    # The Android app doesn't need this: CORS is a browser-only rule.
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# DATABASE_URL drives which DB engine we use, e.g.:
#   sqlite:///db.sqlite3                                (default, zero setup)
#   postgres://user:pass@localhost:5432/kalingapp        (later, Phase 3)
DATABASES = {
    "default": env.db("DATABASE_URL", default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}"),
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
# collectstatic (run during the Render build step) gathers every app's
# static files into this one folder, which WhiteNoise then serves from.
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

# Where uploaded files (e.g. serology photos) get saved on disk.
# Deliberately no static()/serve() route wired up for this in urls.py --
# see milkbank.models.DonorQuestionnaire for why: files here are only
# ever readable through a permission-checked view, never a bare URL.
MEDIA_ROOT = BASE_DIR / "media"
MEDIA_URL = "/media/"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# No CACHES here on purpose: Django's default LocMemCache is correct
# for dev and tests, which run in a single process. Production is the
# case that needs a shared cache -- see prod.py, and the note there on
# why throttling silently did nothing without it.

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
    # Endpoints are private by default; each view opts INTO being public
    # (e.g. register/login/demo-login use permission_classes = [AllowAny]).
    # Safer default than the other way around.
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),
    # Tells drf-spectacular (the auto-doc page) to read every view and
    # build the endpoint list from them, instead of us hand-writing docs
    # that inevitably drift out of sync with the actual code.
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    # Nothing was throttled at all before this, which left the
    # unauthenticated endpoints open to being hammered: passwords could
    # be guessed against /auth/login/ without limit, and the 5-attempt
    # cap on a verification code could simply be reset by registering
    # the same address again.
    #
    # The scoped rates below are what the auth views actually opt into;
    # "anon"/"user" are the catch-alls for everything else. They're set
    # generously enough that no real mother meets them -- these are
    # blunt abuse limits, not usage quotas.
    "DEFAULT_THROTTLE_CLASSES": (
        "core.throttling.ClientIPAnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ),
    "DEFAULT_THROTTLE_RATES": {
        "anon": "60/min",
        "user": "240/min",
        # Password guessing.
        "login": "10/min",
        # Code guessing, on top of the per-signup attempt cap.
        "verify": "10/min",
        # Each of these sends a real email. Unthrottled they were also a
        # way to burn the provider's daily send quota, which would take
        # signup down for genuine users as collateral.
        "register": "5/min",
        "resend": "3/min",
    },
}

SPECTACULAR_SETTINGS = {
    "TITLE": "KalingApp API",
    "DESCRIPTION": "Backend for the KalingApp mobile app -- accounts, articles, and the support directory.",
    "VERSION": "1.0.0",
    # Article.category and NotificationItem.category are unrelated
    # choice sets that both happen to be named "category" -- without
    # this, drf-spectacular auto-names the second one something opaque
    # like "CategoryFe6Enum" in the generated docs.
    "ENUM_NAME_OVERRIDES": {
        "ArticleCategoryEnum": "articles.models.Article.Category",
        "NotificationCategoryEnum": "notifications.models.NotificationItem.Category",
    },
}
