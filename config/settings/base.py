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

# --- Outgoing email (account verification codes -- see accounts/emails.py) ---
# SendGrid's SMTP relay always authenticates with the literal username
# "apikey"; the real secret is the password. Falls back to Django's
# console backend (prints the email to the terminal instead of actually
# sending it) whenever no key is configured, so local dev/tests work
# without needing a real SendGrid account -- but this means production
# MUST have SENDGRID_API_KEY set in Render's env, or verification codes
# will only ever reach the server log, never a mother's inbox.
SENDGRID_API_KEY = env("SENDGRID_API_KEY", default="")
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="noreply@kalingapp.local")

if SENDGRID_API_KEY:
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_HOST = "smtp.sendgrid.net"
    EMAIL_PORT = 587
    EMAIL_USE_TLS = True
    EMAIL_HOST_USER = "apikey"
    EMAIL_HOST_PASSWORD = SENDGRID_API_KEY
    # Without this, a blocked/slow outbound connection to SendGrid hangs
    # with no timeout at all -- found the hard way: a stuck send_mail()
    # call held the request open long enough for gunicorn's own worker
    # timeout to kill the process, which happens *outside* Python's
    # control and can't be caught by a try/except in the view. A short,
    # explicit timeout means a bad connection fails fast, as a normal
    # catchable exception, well within that worker timeout.
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
