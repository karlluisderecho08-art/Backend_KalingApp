import json
import urllib.error
from unittest.mock import patch

from django.core.mail import EmailMessage
from django.test import TestCase, override_settings

from .email_backends import BrevoApiEmailBackend


class _FakeResponse:
    status = 201

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@override_settings(BREVO_API_KEY="xkeysib-test-key")
class BrevoApiEmailBackendTests(TestCase):
    """
    Covers the HTTP sender that replaced SMTP in production. SMTP itself
    is unreachable from the live host (outbound 25/465/587 blocked --
    see core/email_backends.py), so this path is the one that actually
    has to work.

    Every test patches urlopen: a unit test must never make a real
    network call, and certainly never spend real send quota.
    """

    def _message(self):
        return EmailMessage(
            subject="Your KalingApp verification code",
            body="Your code is 123456.",
            from_email="kalingapp.admin@gmail.com",
            to=["mother@example.com"],
        )

    def test_posts_the_expected_payload_to_brevo(self):
        with patch("core.email_backends.urllib.request.urlopen", return_value=_FakeResponse()) as mock_urlopen:
            sent = BrevoApiEmailBackend().send_messages([self._message()])

        self.assertEqual(sent, 1)
        request = mock_urlopen.call_args[0][0]
        self.assertEqual(request.full_url, "https://api.brevo.com/v3/smtp/email")
        # Header names are normalised to capitalised form by urllib.
        self.assertEqual(request.get_header("Api-key"), "xkeysib-test-key")

        payload = json.loads(request.data.decode())
        self.assertEqual(payload["sender"]["email"], "kalingapp.admin@gmail.com")
        self.assertEqual(payload["to"], [{"email": "mother@example.com"}])
        self.assertEqual(payload["subject"], "Your KalingApp verification code")
        self.assertIn("123456", payload["textContent"])

    def test_surfaces_brevo_error_body_rather_than_a_bare_status_code(self):
        """
        The status code alone ("400") is useless for diagnosing this;
        Brevo puts the real reason (unverified sender, bad key, quota)
        in the response body, and accounts/views.py records whatever
        propagates out of here into the audit log.
        """
        http_error = urllib.error.HTTPError(
            url="https://api.brevo.com/v3/smtp/email",
            code=400,
            msg="Bad Request",
            hdrs=None,
            fp=None,
        )
        http_error.read = lambda: b'{"message":"Sender not valid"}'

        with patch("core.email_backends.urllib.request.urlopen", side_effect=http_error):
            with self.assertRaises(RuntimeError) as caught:
                BrevoApiEmailBackend().send_messages([self._message()])

        self.assertIn("400", str(caught.exception))
        self.assertIn("Sender not valid", str(caught.exception))

    def test_fail_silently_swallows_the_error_and_reports_nothing_sent(self):
        with patch("core.email_backends.urllib.request.urlopen", side_effect=OSError("boom")):
            sent = BrevoApiEmailBackend(fail_silently=True).send_messages([self._message()])

        self.assertEqual(sent, 0)

    @override_settings(BREVO_API_KEY="")
    def test_refuses_to_send_with_no_key_configured(self):
        with patch("core.email_backends.urllib.request.urlopen") as mock_urlopen:
            with self.assertRaises(ValueError):
                BrevoApiEmailBackend().send_messages([self._message()])

        mock_urlopen.assert_not_called()
