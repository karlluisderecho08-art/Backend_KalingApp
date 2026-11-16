"""
Trusted devices: a sign-in lockout a stranger can't turn against the owner.

The lockout (accounts/lockout.py) locks an account after 5 wrong passwords --
for everyone, the real owner included. Anyone who knew a mother's, a staff
member's or the admin's email could therefore keep that account locked out
with a handful of requests, without ever knowing the password.

OWASP's answer ("Slow Down Online Guessing Attacks with Device Cookies"), which
this follows: every successful sign-in hands that phone or browser a signed
device token. A sign-in that brings a valid one for the same account is from a
device the owner has already proved herself on, so

  * the account-wide lock doesn't apply to it -- what strangers did can't keep
    her out of the devices she already uses;
  * its own wrong passwords lock that device only, with the same escalation.

A sign-in without one (a new device, or an attacker) gets the account-wide
lockout exactly as before, so guessing is no easier for anybody.

The token is not a login: it opens nothing by itself, and is only ever looked
at together with a password. It names the account, a random id for the device,
and a fingerprint of the account's current password hash -- so changing or
resetting the password retires every device token at once (the next sign-in
on each device issues a new one). Signed with the server's secret key; a
forged or edited token is simply ignored.
"""

import secrets

from django.core import signing
from django.utils.crypto import salted_hmac

SALT = "kalingapp.trusted-device"
# A device not signed in on for this long has to prove itself again.
MAX_AGE_SECONDS = 90 * 24 * 60 * 60


def _password_fingerprint(user):
    # Keyed with the server's secret, so the token (readable, though signed)
    # says nothing about the password hash itself.
    return salted_hmac("kalingapp.trusted-device.password", user.password).hexdigest()[:20]


def issue(user, previous=None):
    """
    A device token for `user` after a successful sign-in. Keeps the device's
    id when `previous` is that device's still-valid token (so it stays the
    same device), otherwise starts a new one.
    """
    device = device_id(previous, user) or secrets.token_urlsafe(16)
    return signing.dumps({"u": user.pk, "d": device, "p": _password_fingerprint(user)}, salt=SALT, compress=True)


def device_id(token, user):
    """The device id in `token` if it is a valid, unexpired token for `user`'s current password; else None."""
    if not isinstance(token, str) or not token or len(token) > 1024:
        return None
    try:
        data = signing.loads(token, salt=SALT, max_age=MAX_AGE_SECONDS)
    except signing.BadSignature:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("u") != user.pk or data.get("p") != _password_fingerprint(user):
        return None
    device = data.get("d")
    return device if isinstance(device, str) and device else None
