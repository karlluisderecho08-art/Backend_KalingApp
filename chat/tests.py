from datetime import timedelta
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from articles.models import Article

from .chunking import MAX_CHARS, TARGET_CHARS, split_into_passages
from .gemini_client import get_ai_response
from .guardrail import OFF_TOPIC_RESPONSE, is_breastfeeding_topic
from .knowledge import NO_MATCH_NOTICE, build_knowledge_context, build_system_prompt
from .models import ChatMessage, ChatSession, KnowledgeChunk, KnowledgeDocument
from .retrieval import ensure_embeddings, retrieve, sync_index, unembedded_chunks
from .views import FOLLOW_UP_WINDOW


LATCH_CONTENT = """Good breastfeeding starts with the right position and a good latch.

Positioning

Hold the baby so that their ear, shoulder, and hip form a straight line. The baby's nose should line up with the nipple.

Latching

You can tell the latch is good when the baby's mouth is wide open and the chin touches the breast."""

STORAGE_CONTENT = """Expressed breastmilk keeps safely for different lengths of time depending on where it is stored.

Refrigerator

Freshly expressed milk keeps for up to four days in the back of a refrigerator at four degrees Celsius or colder.

Freezer

In a separate-door freezer, milk keeps for up to six months, and up to twelve months is acceptable."""


def make_article(**overrides):
    fields = {
        "title": "How to Breastfeed Correctly: Positioning and Latch",
        "category": "Latching Techniques",
        "read_time": "4 min read",
        "teaser": "Positioning basics.",
        "content": LATCH_CONTENT,
        "author": "KalingApp",
    }
    fields.update(overrides)
    return Article.objects.create(**fields)


def make_literature(**overrides):
    fields = {
        "title": "Donor Human Milk Screening Practices",
        "citation": "Olonan-Jusi et al. (2021). Human milk banking in the Philippines.",
        "content": "Donor screening covers serology and a health history interview before "
                   "any milk is accepted. Pasteurisation follows the Holder method.",
    }
    fields.update(overrides)
    return KnowledgeDocument.objects.create(**fields)


class ChunkingTests(SimpleTestCase):
    """
    Passages are the unit retrieval returns and the unit the model reads,
    so a passage that opens mid-sentence is a real defect, not cosmetic:
    it reads as a non-sequitur and the model grounds an answer in it
    anyway.
    """

    def test_empty_content_produces_no_passages(self):
        # Not one empty passage -- an unfinished document should
        # contribute nothing rather than a chunk that weakly matches
        # everything.
        self.assertEqual(split_into_passages(""), [])
        self.assertEqual(split_into_passages("   \n\n  "), [])

    def test_short_document_stays_one_passage(self):
        passages = split_into_passages("One short paragraph about latching.")

        self.assertEqual(passages, ["One short paragraph about latching."])

    def test_paragraphs_are_packed_up_to_the_target_size(self):
        paragraph = "word " * 60  # ~300 chars
        content = "\n\n".join([paragraph.strip()] * 6)

        passages = split_into_passages(content)

        self.assertGreater(len(passages), 1)
        for passage in passages:
            self.assertLessEqual(len(passage), MAX_CHARS)

    def test_an_oversized_paragraph_is_split_on_sentence_boundaries(self):
        sentence = "This is a sentence about milk storage that carries real content. "
        content = sentence * 40  # one paragraph, far over the ceiling

        passages = split_into_passages(content)

        self.assertGreater(len(passages), 1)
        for passage in passages:
            self.assertLessEqual(len(passage), MAX_CHARS)
            # Passages after the first open with the previous passage's
            # overlap tail, which is mid-sentence by design -- but never
            # mid-word.
            self.assertFalse(passage.startswith("his is"), passage[:40])
            self.assertFalse(passage.startswith("entence"), passage[:40])
        # The sentences themselves survive the split intact.
        self.assertIn(sentence.strip(), passages[0])

    def test_passages_overlap_so_a_boundary_fact_keeps_its_setup(self):
        # Long enough that adding the second paragraph exceeds
        # TARGET_CHARS, but under MAX_CHARS so it stays a single unit.
        first = ("A " * 480).strip()
        second = "The refrigerator keeps milk for four days."
        self.assertLess(len(first), MAX_CHARS)
        self.assertGreater(len(first) + len(second), TARGET_CHARS)

        passages = split_into_passages(f"{first}\n\n{second}")

        self.assertEqual(len(passages), 2)
        # The second passage carries a tail of the first rather than
        # starting cold.
        self.assertIn("A A", passages[1])
        self.assertIn(second, passages[1])

    def test_overlap_does_not_start_mid_word(self):
        content = f"{'suspendisse ' * 80}\n\nStorage times differ by location."

        passages = split_into_passages(content)

        self.assertGreater(len(passages), 1)
        self.assertFalse(passages[1].startswith("uspendisse"))


@override_settings(CHAT_RETRIEVAL_USE_EMBEDDINGS=False)
class IndexSyncTests(APITestCase):
    """
    The chunk table is a derived cache over two source models in two
    different apps. These cover it staying honest as those sources
    change, which it must do without a reindex step, because admins edit
    articles through the admin panel on a running server.
    """

    def test_both_halves_of_the_knowledge_base_are_indexed(self):
        make_article()
        make_literature()

        sync_index()

        kinds = set(KnowledgeChunk.objects.values_list("source_kind", flat=True))
        self.assertEqual(kinds, {"article", "literature"})

    def test_literature_chunks_carry_their_citation(self):
        document = make_literature()

        sync_index()

        chunk = KnowledgeChunk.objects.get(source_kind="literature")
        self.assertEqual(chunk.source_reference, document.citation)
        # And a citation the model can print verbatim.
        self.assertIn("Olonan-Jusi", chunk.citation)

    def test_article_chunks_cite_the_knowledge_hub_by_article_title(self):
        make_article()

        sync_index()

        chunk = KnowledgeChunk.objects.filter(source_kind="article").first()
        self.assertIn("How to Breastfeed Correctly", chunk.citation)
        self.assertIn("Knowledge Hub", chunk.citation)

    def test_editing_a_document_rechunks_it(self):
        article = make_article()
        sync_index()
        self.assertFalse(KnowledgeChunk.objects.filter(text__icontains="mastitis").exists())

        article.content = "Mastitis often starts as a sore, red, wedge-shaped patch."
        article.save()
        sync_index()

        self.assertTrue(KnowledgeChunk.objects.filter(text__icontains="mastitis").exists())
        # The superseded passages are gone, not left alongside the new ones.
        self.assertFalse(KnowledgeChunk.objects.filter(text__icontains="wide open").exists())

    def test_unchanged_documents_are_not_rechunked(self):
        make_article()
        sync_index()
        before = set(KnowledgeChunk.objects.values_list("id", flat=True))

        summary = sync_index()

        self.assertEqual(summary["reindexed_documents"], 0)
        self.assertEqual(summary["created_chunks"], 0)
        # Same rows, so anything already embedded keeps its vector.
        self.assertEqual(set(KnowledgeChunk.objects.values_list("id", flat=True)), before)

    def test_deleting_a_document_removes_its_passages(self):
        article = make_article()
        sync_index()
        self.assertTrue(KnowledgeChunk.objects.exists())

        article.delete()
        sync_index()

        self.assertFalse(KnowledgeChunk.objects.filter(source_kind="article").exists())

    def test_deactivated_literature_is_withdrawn_from_retrieval(self):
        document = make_literature()
        sync_index()
        self.assertTrue(KnowledgeChunk.objects.filter(source_kind="literature").exists())

        document.is_active = False
        document.save()
        sync_index()

        self.assertFalse(KnowledgeChunk.objects.filter(source_kind="literature").exists())
        # Withdrawn from retrieval, not deleted.
        self.assertTrue(KnowledgeDocument.objects.filter(pk=document.pk).exists())


@override_settings(CHAT_RETRIEVAL_USE_EMBEDDINGS=False)
class LexicalRetrievalTests(APITestCase):
    """
    Retrieval with embeddings off -- the state it runs in with no
    GEMINI_API_KEY, and the floor everything else degrades to. BM25 alone
    has to be good enough to be worth shipping on its own.
    """

    def setUp(self):
        make_article()
        make_article(
            title="Storing Expressed Breastmilk Safely",
            category="Milk Storage & Safety",
            content=STORAGE_CONTENT,
        )

    def test_retrieval_returns_the_relevant_passage_not_the_whole_corpus(self):
        passages = retrieve("How long can I keep milk in the refrigerator?")

        self.assertTrue(passages)
        self.assertIn("four days", passages[0].text)
        self.assertEqual(passages[0].source_title, "Storing Expressed Breastmilk Safely")
        # The point of retrieval: strictly less than everything.
        self.assertLess(len(passages), KnowledgeChunk.objects.count())

    def test_an_exact_clinical_term_finds_its_passage(self):
        make_literature()

        passages = retrieve("What does donor screening involve?")

        self.assertTrue(passages)
        self.assertIn("serology", passages[0].text)

    def test_an_unrelated_question_retrieves_nothing_lexically(self):
        # BM25 abstains outright on a question sharing no term with any
        # passage, rather than returning the least-bad one. Note this is
        # a property of the lexical ranker specifically -- the semantic
        # ranker cannot abstain this way, see
        # HybridRetrievalTests.test_retrieval_does_not_decide_topicality.
        self.assertEqual(retrieve("bicycle maintenance schedules"), [])

    def test_a_blank_question_retrieves_nothing(self):
        self.assertEqual(retrieve("   "), [])

    def test_top_k_bounds_how_much_reaches_the_prompt(self):
        passages = retrieve("milk latch baby breast storage refrigerator freezer", top_k=1)

        self.assertEqual(len(passages), 1)

    def test_retrieval_is_deterministic(self):
        question = "How do I know the latch is right?"

        first = [chunk.pk for chunk in retrieve(question)]
        second = [chunk.pk for chunk in retrieve(question)]

        self.assertEqual(first, second)

    def test_an_empty_knowledge_base_retrieves_nothing(self):
        Article.objects.all().delete()
        KnowledgeChunk.objects.all().delete()

        self.assertEqual(retrieve("how do I get a good latch"), [])


class HybridRetrievalTests(APITestCase):
    """
    The two rankers exist to cover each other's blind spot, so these
    assert the semantic half earns its keep and, more importantly, that
    losing it degrades rather than breaks.

    Embeddings are patched, never called: a real key is often present in
    a developer's .env, and a test suite that quietly makes paid API
    calls depending on local configuration is not a test suite.
    """

    def setUp(self):
        make_article()
        make_article(
            title="Storing Expressed Breastmilk Safely",
            category="Milk Storage & Safety",
            content=STORAGE_CONTENT,
        )
        sync_index()

    def _fake_vectors(self, target_text):
        """
        Embed so that only passages containing `target_text` look similar
        to the query -- a stand-in for semantic similarity that does not
        need a real model to be a meaningful test of the fusion.
        """
        def embed_passages(texts):
            return [[1.0, 0.0] if target_text in text else [0.0, 1.0] for text in texts]

        def embed_query(text):
            return [1.0, 0.0]

        return embed_passages, embed_query

    # Deliberately shares no content word with the storage passage: no
    # "refrigerator", no "days", no "milk", no "store".
    PARAPHRASE = "how long does pumped supply stay good when chilled"

    def test_lexical_search_alone_cannot_reach_the_paraphrase(self):
        """
        The blind spot the semantic ranker exists to cover. Asserted on
        its own so the test below is measuring something real, rather
        than a passage BM25 would have found anyway.
        """
        with override_settings(CHAT_RETRIEVAL_USE_EMBEDDINGS=False):
            passages = retrieve(self.PARAPHRASE)

        self.assertNotIn("four days", " ".join(chunk.text for chunk in passages))

    def test_a_paraphrase_with_no_shared_words_is_found_semantically(self):
        embed_passages, embed_query = self._fake_vectors("four days")

        with patch("chat.retrieval.embed_passages", embed_passages), \
             patch("chat.retrieval.embed_query", embed_query), \
             patch("chat.retrieval.embeddings_available", return_value=True):
            passages = retrieve(self.PARAPHRASE)

        self.assertIn("four days", " ".join(chunk.text for chunk in passages))

    def test_embeddings_are_stored_so_they_are_computed_once(self):
        embed_passages, embed_query = self._fake_vectors("four days")

        with patch("chat.retrieval.embed_passages", embed_passages), \
             patch("chat.retrieval.embeddings_available", return_value=True):
            embedded = ensure_embeddings()

        self.assertGreater(embedded, 0)
        self.assertFalse(unembedded_chunks().exists())

        # A second pass has nothing left to do, so no call is made at all.
        with patch("chat.retrieval.embed_passages", side_effect=AssertionError("re-embedded")), \
             patch("chat.retrieval.embeddings_available", return_value=True):
            self.assertEqual(ensure_embeddings(), 0)

    def test_an_embedding_outage_falls_back_to_lexical_retrieval(self):
        # embed_passages returning Nones is exactly what chat.embeddings
        # does when the API call raises.
        with patch("chat.retrieval.embed_passages", lambda texts: [None] * len(texts)), \
             patch("chat.retrieval.embed_query", lambda text: None), \
             patch("chat.retrieval.embeddings_available", return_value=True):
            passages = retrieve("How long can I keep milk in the refrigerator?")

        self.assertTrue(passages)
        self.assertIn("four days", passages[0].text)

    def test_a_chunk_that_failed_to_embed_is_still_retrievable(self):
        with patch("chat.retrieval.embed_passages", lambda texts: [None] * len(texts)), \
             patch("chat.retrieval.embeddings_available", return_value=True):
            ensure_embeddings()

        self.assertTrue(unembedded_chunks().exists())
        with patch("chat.retrieval.embeddings_available", return_value=False):
            self.assertTrue(retrieve("refrigerator storage time"))

    def test_vectors_from_another_embedding_model_are_not_trusted(self):
        """
        Cosine similarity is only meaningful within one model's space, so
        a stored vector from a different model has to be treated as
        absent and re-embedded, not scored against the new one.
        """
        KnowledgeChunk.objects.update(embedding=[0.5, 0.5], embedding_model="some-older-model")

        self.assertEqual(unembedded_chunks().count(), KnowledgeChunk.objects.count())

    def test_orthogonal_passages_are_not_semantic_matches(self):
        def embed_passages(texts):
            # Orthogonal to the query: similarity 0, which is dropped
            # outright rather than being merely the weakest match.
            return [[0.0, 1.0] for _ in texts]

        with patch("chat.retrieval.embed_passages", embed_passages), \
             patch("chat.retrieval.embed_query", lambda text: [1.0, 0.0]), \
             patch("chat.retrieval.embeddings_available", return_value=True):
            # No lexical overlap either, so nothing should survive.
            self.assertEqual(retrieve("bicycle maintenance schedules"), [])

    def test_the_margin_is_relative_to_the_best_match(self):
        """
        Measured against the real corpus, every passage scores above 0.47
        even for an off-topic question, and the relevant and irrelevant
        ranges overlap -- so the cutoff has to be distance from this
        question's best match, not an absolute cosine value. A passage
        that would clear any sane absolute floor on its own is still
        dropped when a much better passage exists.
        """
        storage_marker = "four days"

        def embed_passages(texts):
            # 0.98 vs 0.80 similarity to the query below: both high in
            # absolute terms, far apart relative to each other.
            return [
                [1.0, 0.2] if storage_marker in text else [1.0, 0.75]
                for text in texts
            ]

        with patch("chat.retrieval.embed_passages", embed_passages), \
             patch("chat.retrieval.embed_query", lambda text: [1.0, 0.0]), \
             patch("chat.retrieval.embeddings_available", return_value=True), \
             override_settings(CHAT_RETRIEVAL_SIMILARITY_MARGIN=0.05):
            passages = retrieve("chilled storage duration")

        self.assertTrue(passages)
        # The 0.80 passages are well above any absolute floor, and still
        # excluded because a 0.98 passage exists for this question.
        for chunk in passages:
            self.assertIn(storage_marker, chunk.text)

    def test_retrieval_does_not_decide_topicality(self):
        """
        Documents a real limitation rather than papering over it: an
        off-topic question produces a flat similarity distribution whose
        top still clears a relative margin, so semantic retrieval cannot
        reject it. Two other mechanisms do, and this is why they matter
        -- the keyword guardrail stops such a message before the model is
        called (see SendMessageViewTests), and strict grounding makes the
        model say a question is not covered.
        """
        def embed_passages(texts):
            # Uniformly mediocre: exactly what an off-topic question
            # looks like against a real corpus.
            return [[1.0, 0.6] for _ in texts]

        with patch("chat.retrieval.embed_passages", embed_passages), \
             patch("chat.retrieval.embed_query", lambda text: [1.0, 0.0]), \
             patch("chat.retrieval.embeddings_available", return_value=True):
            passages = retrieve("bicycle maintenance schedules")

        self.assertTrue(passages, "semantic retrieval is expected to return its best guess here")


@override_settings(CHAT_RETRIEVAL_USE_EMBEDDINGS=False)
class GroundingPromptTests(APITestCase):
    """
    What actually reaches the model. Retrieval is only useful if the
    prompt carries the retrieved passages and the rules that bind Kali to
    them.
    """

    def setUp(self):
        make_article()
        make_article(
            title="Storing Expressed Breastmilk Safely",
            category="Milk Storage & Safety",
            content=STORAGE_CONTENT,
        )

    def test_the_prompt_carries_the_retrieved_passage_and_its_source(self):
        prompt = build_system_prompt("BASE", question="How long does milk keep in the fridge?")

        self.assertIn("BASE", prompt)
        self.assertIn("four days", prompt)
        self.assertIn("Storing Expressed Breastmilk Safely", prompt)
        self.assertIn("PASSAGE 1", prompt)

    def test_the_prompt_no_longer_carries_the_whole_library(self):
        """
        The regression this change exists to prevent coming back: every
        article in full, in every prompt, regardless of the question.
        """
        prompt = build_system_prompt("BASE", question="How long does milk keep in the fridge?")

        # The latch article is irrelevant to a storage question and
        # should not be along for the ride.
        self.assertNotIn("ear, shoulder, and hip", prompt)

    def test_strict_mode_forbids_answering_beyond_the_passages(self):
        strict = build_system_prompt("BASE", question="fridge storage", strict=True)
        preferred = build_system_prompt("BASE", question="fridge storage", strict=False)

        self.assertIn("ONLY", strict)
        self.assertIn("do not guess", strict)
        self.assertNotIn("Answer ONLY using", preferred)

    def test_strict_mode_is_told_when_retrieval_found_nothing(self):
        """
        Grounding rules pointing at a passage section that isn't there
        read as "no rules apply" -- the exact failure the rules exist to
        prevent. The empty result is stated instead.
        """
        prompt = build_system_prompt("BASE", question="bicycle maintenance schedules", strict=True)

        self.assertIn(NO_MATCH_NOTICE, prompt)
        self.assertIn("ONLY", prompt)

    def test_a_newly_added_article_is_retrievable_without_a_restart(self):
        """
        Admins add articles through the admin panel on a running server.
        The index is reconciled per request so a new article doesn't
        silently fail to exist for Kali until someone runs a command.
        """
        self.assertNotIn("Mastitis", build_system_prompt("BASE", question="mastitis"))

        make_article(
            title="Recognising Mastitis Early",
            content="Mastitis often starts as a sore, red patch on the breast.",
        )

        prompt = build_system_prompt("BASE", question="what does mastitis feel like")
        self.assertIn("Recognising Mastitis Early", prompt)
        self.assertIn("sore, red patch", prompt)

    def test_reviewed_literature_can_ground_an_answer(self):
        make_literature()

        context = build_knowledge_context("what does donor screening involve")

        self.assertIn("serology", context)
        self.assertIn("Olonan-Jusi", context)

    def test_a_prompt_built_without_a_question_is_the_persona_alone(self):
        # No question means nothing to retrieve against. Returning the
        # persona beats returning rules about passages that were never
        # selected.
        self.assertEqual(build_system_prompt("BASE"), "BASE")


class GuardrailTests(APITestCase):
    """
    Reproduces a real bug found by actually testing chat against the
    live backend: genuinely on-topic questions ("is my baby getting
    enough milk", "is cluster feeding normal") were being rejected as
    off-topic because the keyword list only had compound phrases like
    "milk supply"/"breast milk", nothing matching plain "milk" or
    "feed"/"feeding" alone.
    """

    def test_recognizes_questions_that_were_previously_missed(self):
        self.assertTrue(is_breastfeeding_topic("How do I know if my baby is getting enough milk?"))
        self.assertTrue(is_breastfeeding_topic("Is it normal for my baby to cluster feed in the evening?"))

    def test_still_recognizes_the_original_compound_phrases(self):
        self.assertTrue(is_breastfeeding_topic("What's a good way to increase my milk supply?"))
        self.assertTrue(is_breastfeeding_topic("How long can I store breast milk in the fridge?"))

    def test_still_rejects_clearly_unrelated_questions(self):
        self.assertFalse(is_breastfeeding_topic("what's the weather like today"))
        self.assertFalse(is_breastfeeding_topic("can you recommend a good movie"))


class GeminiClientFallbackTests(APITestCase):
    """
    get_ai_response() must never raise and must never actually attempt
    a real Gemini call in the test environment (no key is ever
    configured for tests, and none should be needed to run them) --
    every path here should land on the local fallback.
    """

    @patch("chat.gemini_client.settings")
    def test_falls_back_locally_with_no_key_configured(self, mock_settings):
        mock_settings.GEMINI_API_KEY = ""

        reply, tokens, used_fallback = get_ai_response("What is a good latch?")

        self.assertTrue(used_fallback)
        self.assertEqual(tokens, 0)
        self.assertTrue(reply)

    @patch("chat.gemini_client._get_client")
    @patch("chat.gemini_client.settings")
    def test_falls_back_locally_on_a_malformed_response(self, mock_settings, mock_get_client):
        mock_settings.GEMINI_API_KEY = "fake-key"
        mock_settings.GEMINI_MODEL = "gemini-2.5-flash"
        # A response with no text content at all should be treated as a
        # failure (raises internally, caught below), not returned to
        # the mother as an empty reply.
        mock_get_client.return_value.models.generate_content.return_value.text = ""

        reply, tokens, used_fallback = get_ai_response("What is a good latch?")

        self.assertTrue(used_fallback)
        self.assertEqual(tokens, 0)
        self.assertTrue(reply)


class SendMessageViewTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="mother@example.com", password="x", is_active=True)
        self.client.force_authenticate(user=self.user)

    def test_off_topic_message_never_reaches_the_model(self):
        with patch("chat.views.get_ai_response") as mock_get_ai_response:
            response = self.client.post("/chat/message/", {"text": "what's the weather like today"})

        self.assertEqual(response.status_code, 200)
        mock_get_ai_response.assert_not_called()

    @patch("chat.views.get_ai_response", return_value=("A good latch covers most of the areola.", 42, False))
    def test_on_topic_message_calls_the_model_and_tracks_counts(self, mock_get_ai_response):
        response = self.client.post("/chat/message/", {"text": "How do I get a good latch?"})

        self.assertEqual(response.status_code, 200)
        mock_get_ai_response.assert_called_once()
        session = ChatSession.objects.get(owner=self.user)
        self.assertEqual(session.prompt_count, 1)
        self.assertEqual(session.token_count, 42)

    def test_message_history_persists_across_requests(self):
        with patch("chat.views.get_ai_response", return_value=("Some reply.", 10, False)):
            self.client.post("/chat/message/", {"text": "How do I get a good latch?"})

        response = self.client.get("/chat/history/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 2)  # her message + the reply


class ConversationContinuityTests(APITestCase):
    """
    Reproduces a real complaint: Kali ends a reply with "would you like
    to know more about that?", the mother answers "yes", and she's told
    her own answer is off topic. Two separate defects caused it -- the
    keyword guardrail judging each message alone, and the model being
    sent one message with no memory of the exchange it belonged to --
    and both had to be fixed for the conversation to hold together.
    """

    def setUp(self):
        self.user = User.objects.create_user(email="mother@example.com", password="x", is_active=True)
        self.client.force_authenticate(user=self.user)

    def _kali_just_asked(self, question="Would you like to know more about that?"):
        session, _ = ChatSession.objects.get_or_create(owner=self.user)
        ChatMessage.objects.create(session=session, text="How do I get a good latch?", is_user=True)
        ChatMessage.objects.create(session=session, text=question, is_user=False)
        return session

    @patch("chat.views.get_ai_response", return_value=("Here's more detail.", 20, False))
    def test_yes_after_a_question_from_kali_is_not_rejected_as_off_topic(self, mock_get_ai_response):
        self._kali_just_asked()

        response = self.client.post("/chat/message/", {"text": "yes"})

        self.assertEqual(response.status_code, 200)
        mock_get_ai_response.assert_called_once()
        self.assertNotEqual(response.data["reply"]["text"], OFF_TOPIC_RESPONSE)

    @patch("chat.views.get_ai_response", return_value=("Here's more detail.", 20, False))
    def test_the_model_receives_the_conversation_so_far(self, mock_get_ai_response):
        self._kali_just_asked()

        self.client.post("/chat/message/", {"text": "yes"})

        history = mock_get_ai_response.call_args.kwargs["history"]
        # Oldest first, and the current message is NOT duplicated in it --
        # the client appends that itself.
        self.assertEqual(
            history,
            [(True, "How do I get a good latch?"), (False, "Would you like to know more about that?")],
        )

    @patch("chat.views.get_ai_response", return_value=("Here's more detail.", 20, False))
    def test_a_stale_conversation_is_judged_on_its_own_words_again(self, mock_get_ai_response):
        """
        The follow-up allowance is time-boxed: "yes" typed days later
        isn't answering anything, so the keyword guardrail applies as
        normal rather than the exchange staying open forever.
        """
        session = self._kali_just_asked()
        stale = timezone.now() - FOLLOW_UP_WINDOW - timedelta(minutes=1)
        session.messages.update(created_at=stale)

        response = self.client.post("/chat/message/", {"text": "yes"})

        self.assertEqual(response.status_code, 200)
        mock_get_ai_response.assert_not_called()
        self.assertEqual(response.data["reply"]["text"], OFF_TOPIC_RESPONSE)

    def test_a_first_message_with_no_conversation_still_needs_to_be_on_topic(self):
        with patch("chat.views.get_ai_response") as mock_get_ai_response:
            response = self.client.post("/chat/message/", {"text": "yes"})

        self.assertEqual(response.status_code, 200)
        mock_get_ai_response.assert_not_called()
        self.assertEqual(response.data["reply"]["text"], OFF_TOPIC_RESPONSE)

    def test_a_refusal_does_not_open_a_window_for_more_off_topic_questions(self):
        """
        Found live, not by reading: the guardrail was disabling itself.

        The off-topic refusal is stored like any other message from Kali,
        so it became "the last thing Kali said" and satisfied the
        follow-up exception -- which meant the FIRST off-topic question was
        refused and every one after it, for the next 30 minutes, skipped
        the guardrail and reached the model. Reproduced against the live
        deployment: "how do I fix a car engine" was refused, then a
        stock-market question immediately after it got a real generated
        reply that cited a Knowledge Hub article.

        The manuscript claims out-of-scope messages "never reach Gemini",
        which was true exactly once per session and false afterwards.
        """
        with patch("chat.views.get_ai_response") as mock_get_ai_response:
            first = self.client.post("/chat/message/", {"text": "how do I fix a car engine"})
            second = self.client.post(
                "/chat/message/", {"text": "what should I invest in the stock market"}
            )

        self.assertEqual(first.data["reply"]["text"], OFF_TOPIC_RESPONSE)
        # The one that regressed: this used to reach the model.
        self.assertEqual(second.data["reply"]["text"], OFF_TOPIC_RESPONSE)
        mock_get_ai_response.assert_not_called()

    @patch("chat.views.get_ai_response", return_value=("Here's more detail.", 20, False))
    def test_a_real_reply_still_opens_the_follow_up_window(self, mock_get_ai_response):
        """
        The fix must not close the window it was built for: a genuine reply
        from Kali still lets a bare "yes" through.
        """
        self._kali_just_asked()

        response = self.client.post("/chat/message/", {"text": "yes"})

        mock_get_ai_response.assert_called_once()
        self.assertNotEqual(response.data["reply"]["text"], OFF_TOPIC_RESPONSE)
