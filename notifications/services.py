from .models import NotificationItem


def notify(owner, title, description, category):
    """
    Write one notification, server-side. Call this from wherever
    something notification-worthy actually happens (see
    milkbank/transitions.py) instead of expecting the client to know to
    show one.
    """
    return NotificationItem.objects.create(owner=owner, title=title, description=description, category=category)
