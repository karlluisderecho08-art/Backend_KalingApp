"""
WSGI config for config project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/6.1/howto/deployment/wsgi/
"""

from django.core.wsgi import get_wsgi_application

# No setdefault() here on purpose, unlike manage.py (which stays
# dev-convenient for local commands). This is what the real production
# server (gunicorn) imports -- if DJANGO_SETTINGS_MODULE isn't
# explicitly set to config.settings.prod when it starts, this should
# fail loudly instead of silently serving real traffic with DEBUG=True.
application = get_wsgi_application()
