from django.conf import settings
from django.core.management.base import BaseCommand

from chat.models import KnowledgeChunk
from chat.retrieval import ensure_embeddings, sync_index, unembedded_chunks


class Command(BaseCommand):
    """
    manage.py reindex_knowledge

    Rebuilds the retrieval index over the domain-limited knowledge base
    (verified articles + reviewed literature) and embeds every passage.

    Retrieval does not depend on this having been run: chat/retrieval.py
    reconciles chunk text on every query and fills embeddings lazily, a
    few per message. This command exists so that work happens up front --
    after seeding, after a bulk literature import, or after changing
    GEMINI_EMBEDDING_MODEL -- instead of being paid for by whichever
    mother sends the next message.

    Safe to run repeatedly: documents whose content has not changed are
    left alone, and passages already embedded by the current model are
    not re-embedded.
    """

    help = "Rebuild the Kali retrieval index and embed any unembedded passages"

    def add_arguments(self, parser):
        parser.add_argument(
            "--reembed",
            action="store_true",
            help="Discard existing vectors and embed every passage again. Use after a "
            "change that invalidates them but leaves the text identical.",
        )
        parser.add_argument(
            "--no-embeddings",
            action="store_true",
            help="Only rebuild passage text; skip embedding entirely. Retrieval stays "
            "lexical for anything left unembedded.",
        )

    def handle(self, *args, **options):
        summary = sync_index()
        self.stdout.write(
            "Indexed {documents} document(s): {reindexed_documents} rebuilt, "
            "{created_chunks} passage(s) written, {removed_chunks} stale passage(s) "
            "removed.".format(**summary)
        )

        if options["no_embeddings"]:
            self.stdout.write(self.style.WARNING("Skipped embedding (--no-embeddings)."))
            return

        if not settings.CHAT_RETRIEVAL_USE_EMBEDDINGS:
            self.stdout.write(self.style.WARNING(
                "CHAT_RETRIEVAL_USE_EMBEDDINGS is off -- retrieval will be lexical only."
            ))
            return

        if not settings.GEMINI_API_KEY:
            self.stdout.write(self.style.WARNING(
                "No GEMINI_API_KEY set -- cannot embed. Retrieval will be lexical only, "
                "which works; semantic matching will not."
            ))
            return

        if options["reembed"]:
            cleared = KnowledgeChunk.objects.exclude(embedding=[]).update(
                embedding=[], embedding_model=""
            )
            self.stdout.write(f"Cleared {cleared} existing vector(s).")

        # No limit here, unlike the request path: the point of running
        # this is to absorb the whole cost now.
        embedded = ensure_embeddings()
        total = KnowledgeChunk.objects.count()
        remaining = unembedded_chunks().count()

        self.stdout.write(self.style.SUCCESS(
            f"Embedded {embedded} passage(s) with {settings.GEMINI_EMBEDDING_MODEL}. "
            f"{total - remaining}/{total} passage(s) now searchable semantically."
        ))
        if remaining:
            self.stdout.write(self.style.WARNING(
                f"{remaining} passage(s) still unembedded -- the embedding calls for those "
                "failed. They remain retrievable lexically. Re-run to retry."
            ))
