"""
Embedding calls must be time-boxed.

They run on the chat request path before the generation call, and with no
timeout the SDK blocks indefinitely: measured against a server that
accepts a connection and never answers, embed_content was still blocked
after 30 s. gunicorn kills the worker at 40 s, so a stalled embedding
endpoint would have cost a mother her reply instead of costing retrieval
a little ranking quality.

Nothing here makes a real network call. The tests that need a stalled
server use a local socket that accepts and says nothing.
"""

import socket
import threading
import time
from io import StringIO
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APITestCase

from core.models import AuditLogEntry

from . import embeddings
from .embeddings import (
    EMBED_BULK_TIMEOUT_MS,
    EMBED_REQUEST_TIMEOUT_MS,
    embed_passages,
    embed_query,
)
from .gemini_client import HTTP_TIMEOUT_MS
from .retrieval import ensure_embeddings, retrieve, sync_index
from .tests import make_article

# gunicorn's --timeout in render.yaml. If that changes, change this too.
GUNICORN_TIMEOUT_MS = 40_000


def fake_embedding_client():
    """A stand-in client whose embed_content returns one vector per input."""
    client = MagicMock()

    def embed_content(model, contents, config):
        response = MagicMock()
        response.embeddings = [MagicMock(values=[1.0, 0.0]) for _ in contents]
        return response

    client.models.embed_content.side_effect = embed_content
    return client


def timeout_of(client):
    config = client.models.embed_content.call_args.kwargs["config"]
    return config.http_options.timeout


@override_settings(GEMINI_API_KEY="test-key")
class TimeoutIsPassedToEveryCallTests(APITestCase):
    def test_a_query_embedding_carries_the_request_path_timeout(self):
        client = fake_embedding_client()
        with patch.object(embeddings, "_get_client", return_value=client):
            embed_query("how long does milk keep")

        self.assertEqual(timeout_of(client), EMBED_REQUEST_TIMEOUT_MS)

    def test_passage_embeddings_carry_the_request_path_timeout_by_default(self):
        client = fake_embedding_client()
        with patch.object(embeddings, "_get_client", return_value=client):
            embed_passages(["one passage", "another"])

        self.assertEqual(timeout_of(client), EMBED_REQUEST_TIMEOUT_MS)

    def test_a_caller_with_no_mother_waiting_can_ask_for_longer(self):
        client = fake_embedding_client()
        with patch.object(embeddings, "_get_client", return_value=client):
            embed_passages(["one passage"], timeout_ms=EMBED_BULK_TIMEOUT_MS)

        self.assertEqual(timeout_of(client), EMBED_BULK_TIMEOUT_MS)

    def test_every_batch_of_a_large_job_gets_the_timeout(self):
        client = fake_embedding_client()
        texts = [f"passage {n}" for n in range(embeddings.BATCH_SIZE * 2 + 1)]
        with patch.object(embeddings, "_get_client", return_value=client):
            embed_passages(texts)

        self.assertEqual(client.models.embed_content.call_count, 3)
        for call in client.models.embed_content.call_args_list:
            self.assertEqual(call.kwargs["config"].http_options.timeout, EMBED_REQUEST_TIMEOUT_MS)


class TimeBudgetTests(SimpleTestCase):
    def test_the_worst_case_request_fits_inside_the_worker_timeout(self):
        """
        One chat request can make two embedding calls (a batch of
        not-yet-embedded passages, and the question) and then the
        generation call. All three at their limit must still finish
        before gunicorn kills the worker, or the "fails as a catchable
        exception" property that HTTP_TIMEOUT_MS exists for is lost.
        Raising any of the three numbers has to fail here, not in
        production.
        """
        worst_case = 2 * EMBED_REQUEST_TIMEOUT_MS + HTTP_TIMEOUT_MS

        self.assertLess(worst_case, GUNICORN_TIMEOUT_MS)

    def test_the_bulk_timeout_is_longer_than_the_request_one(self):
        self.assertGreater(EMBED_BULK_TIMEOUT_MS, EMBED_REQUEST_TIMEOUT_MS)


@override_settings(GEMINI_API_KEY="test-key", CHAT_RETRIEVAL_USE_EMBEDDINGS=True)
class EnsureEmbeddingsTimeoutTests(APITestCase):
    def setUp(self):
        make_article()
        sync_index()

    def test_the_request_path_calls_embed_passages_exactly_as_before(self):
        """
        No timeout argument is forwarded unless one was asked for, so the
        request path keeps embed_passages' own short default -- and the
        existing tests, which stand in for embed_passages with a
        one-argument function, keep working.
        """
        with patch("chat.retrieval.embed_passages", return_value=[None]) as embed:
            ensure_embeddings(limit=1)

        self.assertEqual(embed.call_args.kwargs, {})

    def test_a_bulk_caller_has_its_longer_timeout_forwarded(self):
        with patch("chat.retrieval.embed_passages", return_value=[None]) as embed:
            ensure_embeddings(limit=1, timeout_ms=EMBED_BULK_TIMEOUT_MS)

        self.assertEqual(embed.call_args.kwargs, {"timeout_ms": EMBED_BULK_TIMEOUT_MS})

    def test_the_reindex_command_uses_the_bulk_timeout(self):
        with patch(
            "chat.management.commands.reindex_knowledge.ensure_embeddings", return_value=0
        ) as ensure:
            call_command("reindex_knowledge", stdout=StringIO())

        ensure.assert_called_once_with(timeout_ms=EMBED_BULK_TIMEOUT_MS)


class StalledServer:
    """A local server that accepts connections and never answers them."""

    def __enter__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.held = []
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()
        return self

    def _accept(self):
        while True:
            try:
                connection, _ = self.sock.accept()
            except OSError:
                return
            self.held.append(connection)

    def __exit__(self, *exc):
        self.sock.close()
        for connection in self.held:
            connection.close()

    def client(self):
        from google import genai
        from google.genai import types

        return genai.Client(
            api_key="not-a-real-key",
            http_options=types.HttpOptions(base_url=f"http://127.0.0.1:{self.port}"),
        )


@override_settings(GEMINI_API_KEY="test-key", CHAT_RETRIEVAL_USE_EMBEDDINGS=True)
class AStalledEmbeddingServiceTests(APITestCase):
    """
    The real SDK against a server that never answers -- the failure the
    timeout exists for -- with the limit shrunk so the test is quick.
    """

    SHORT_TIMEOUT_MS = 400

    def test_a_stalled_query_embedding_gives_up_instead_of_hanging(self):
        with StalledServer() as server, \
             patch.object(embeddings, "_get_client", return_value=server.client()), \
             patch.object(embeddings, "EMBED_REQUEST_TIMEOUT_MS", self.SHORT_TIMEOUT_MS):
            started = time.monotonic()
            vector = embed_query("how long does milk keep")
            elapsed = time.monotonic() - started

        self.assertIsNone(vector)
        self.assertLess(elapsed, 5, "a stalled call must be cut off at the timeout, not run on")

    def test_a_stalled_call_is_logged_like_any_other_failed_one(self):
        with StalledServer() as server, \
             patch.object(embeddings, "_get_client", return_value=server.client()), \
             patch.object(embeddings, "EMBED_REQUEST_TIMEOUT_MS", self.SHORT_TIMEOUT_MS):
            embed_query("how long does milk keep")

        self.assertTrue(
            AuditLogEntry.objects.filter(action="chat.embedding_call_failed").exists()
        )

    def test_retrieval_still_answers_from_keywords_when_embeddings_stall(self):
        """
        The point of the whole change: a stalled embedding service costs
        retrieval its semantic half, not the mother her answer. Both calls
        a request makes (passages and query) stall here.
        """
        make_article()
        sync_index()

        with StalledServer() as server, \
             patch.object(embeddings, "_get_client", return_value=server.client()), \
             patch.object(embeddings, "EMBED_REQUEST_TIMEOUT_MS", self.SHORT_TIMEOUT_MS):
            started = time.monotonic()
            passages = retrieve("What are the signs of a good latch?")
            elapsed = time.monotonic() - started

        self.assertLess(elapsed, 8)
        self.assertTrue(passages, "keyword retrieval must still work")
        self.assertIn("chin touches", " ".join(chunk.text for chunk in passages))
