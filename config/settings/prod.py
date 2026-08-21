from .base import *  # noqa: F401,F403

DEBUG = False

# In prod this must be set via env — no default, so a misconfigured
# deploy fails loudly at startup instead of silently accepting all hosts.
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS")
