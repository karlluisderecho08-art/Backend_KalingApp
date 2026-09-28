from .models import NotificationItem


def notify(owner, title, description, category):
    """
    Write one notification, server-side. Call this from wherever
    something notification-worthy actually happens (see
    milkbank/transitions.py) instead of expecting the client to know to
    show one.
    """
    return NotificationItem.objects.create(owner=owner, title=title, description=description, category=category)


def notify_many(owners, title, description, category):
    """
    Same as notify(), but for an event with more than one recipient --
    a new booking landing at a facility (every staff account assigned to
    it, not just one), a comment getting reported (every admin, not just
    whoever happens to be logged in when it happens). One call site
    instead of a loop of notify() calls at each caller, and one INSERT
    instead of N.

    `owners` is consumed once (list() it yourself first if you need it
    again after calling this) -- a queryset works fine since bulk_create
    only needs to iterate it.
    """
    NotificationItem.objects.bulk_create([
        NotificationItem(owner=owner, title=title, description=description, category=category)
        for owner in owners
    ])
