"""
ASGI config for config project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/6.1/howto/deployment/asgi/
"""

from django.core.asgi import get_asgi_application

# Same reasoning as wsgi.py: no silent dev fallback for the entry point
# a real server actually imports.
application = get_asgi_application()
