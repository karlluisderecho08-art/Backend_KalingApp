"""
The code emails' designed HTML version (accounts.emails.code_email_html): the
code large in its own box, her name escaped, Tagalog when she uses the app in
Tagalog, the plain text still there for mail apps that can't show HTML, and
the Brevo backend passing the HTML on.
"""

import json
from unittest.mock import patch

from django.core import mail
from django.core.mail import EmailMessage, EmailMultiAlternatives
from django.test import TestCase, override_settings

from core.email_backends import BrevoApiEmailBackend
from core.tests import _FakeResponse

from .emails import (
    VERIFICATION_CODE_TTL_MINUTES,
    send_email_change_code,
    send_password_reset_email,
    send_verification_email,
)
from .models import PendingRegistration, User


def html_of(message):
    return next(content for content, mimetype in message.alternatives if mimetype == "text/html")


class CodeEmailHtmlTests(TestCase):
    def pending(self, name="Ana"):
        return PendingRegistration.objects.create(email="ana@example.com", password="x", mom_name=name)

    def test_the_verification_email_has_the_designed_version(self):
        pending = self.pending()
        send_verification_email(pending)
        message = mail.outbox[0]
        html = html_of(message)
        self.assertIn("Verify your KalingApp account", html)
        self.assertIn("Hi Ana,", html)
        self.assertIn("Your 6-digit verification code is:", html)
        self.assertIn(f">{pending.code}</td>", html)
        self.assertIn(f"<strong>{VERIFICATION_CODE_TTL_MINUTES} minutes</strong>", html)
        self.assertIn('lang="en"', html)
        # The plain text is still there, with the same code.
        self.assertIn(pending.code, message.body)
        self.assertNotIn("<", message.body)

    def test_without_a_name_she_is_greeted_plainly(self):
        send_verification_email(self.pending(name=""))
        self.assertIn("Hi there,", html_of(mail.outbox[0]))

    def test_her_name_is_escaped(self):
        send_verification_email(self.pending(name='<b>Ana</b> "& co"'))
        html = html_of(mail.outbox[0])
        self.assertNotIn("<b>Ana</b>", html)
        self.assertIn("&lt;b&gt;Ana&lt;/b&gt; &quot;&amp; co&quot;", html)

    def test_in_tagalog(self):
        pending = self.pending()
        send_verification_email(pending, tagalog=True)
        html = html_of(mail.outbox[0])
        self.assertIn('lang="fil"', html)
        self.assertIn("Beripikahin ang iyong KalingApp account", html)
        self.assertIn("Kumusta, Ana!", html)
        self.assertIn("Ang iyong 6-digit na code para sa pagberipika ay:", html)
        self.assertIn(f"<strong>{VERIFICATION_CODE_TTL_MINUTES} minuto</strong>", html)
        self.assertIn("Kung hindi ikaw ang sumubok na gumawa ng KalingApp account", html)
        self.assertNotIn("Verify your", html)

    def test_a_name_is_never_translated(self):
        # "May" is also a month the Tagalog catalog knows.
        send_verification_email(self.pending(name="May"), tagalog=True)
        self.assertIn("Kumusta, May!", html_of(mail.outbox[0]))
        self.assertIn("Kumusta, May!", mail.outbox[0].body)

    def test_the_password_reset_email_has_the_designed_version(self):
        user = User.objects.create_user(email="mother@example.com", password="x", mom_name="Ana")
        send_password_reset_email(user)
        html = html_of(mail.outbox[0])
        self.assertIn("Reset your KalingApp password", html)
        self.assertIn("Your 6-digit password reset code is:", html)
        self.assertIn(f">{user.password_reset_code}</td>", html)
        self.assertIn("your password hasn&#x27;t been changed", html)
        send_password_reset_email(user, tagalog=True)
        self.assertIn("I-reset ang iyong KalingApp password", html_of(mail.outbox[1]))

    def test_the_email_change_code_has_the_designed_version(self):
        user = User.objects.create_user(email="admin@example.com", password="x")
        user.pending_email, user.pending_email_code = "new@example.com", "246810"
        user.save()
        send_email_change_code(user)
        html = html_of(mail.outbox[0])
        self.assertEqual(mail.outbox[0].to, ["new@example.com"])
        self.assertIn("Confirm your new KalingApp email address", html)
        self.assertIn(">246810</td>", html)
        self.assertIn("Enter it in the KalingApp admin portal", html)


@override_settings(BREVO_API_KEY="test-key")
class BrevoSendsTheHtmlTests(TestCase):
    def send(self, message):
        with patch("core.email_backends.urllib.request.urlopen", return_value=_FakeResponse()) as urlopen:
            BrevoApiEmailBackend().send_messages([message])
        return json.loads(urlopen.call_args[0][0].data.decode())

    def test_the_html_version_goes_to_brevo(self):
        message = EmailMultiAlternatives("Subject", "Plain 123456", "KalingApp <a@example.com>", ["m@example.com"])
        message.attach_alternative("<p>Designed 123456</p>", "text/html")
        payload = self.send(message)
        self.assertEqual(payload["htmlContent"], "<p>Designed 123456</p>")
        self.assertEqual(payload["textContent"], "Plain 123456")

    def test_a_plain_email_sends_no_html(self):
        payload = self.send(EmailMessage("Subject", "Plain", "a@example.com", ["m@example.com"]))
        self.assertNotIn("htmlContent", payload)
