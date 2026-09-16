"""
Builds the Knowledge Hub context that Kali is required to answer from.

Deliberately NOT a vector store. The usual RAG machinery -- embeddings,
chunking, similarity search -- exists to select a handful of relevant
passages out of a corpus too large to send at once. This library is six
articles, roughly 20k characters, which comfortably fits in a single
prompt. Sending all of it is both simpler and strictly more reliable
than retrieval would be: there is no top-k cutoff to tune and no way
for the right article to be missed because a query phrased it
differently.

If the library grows past what fits comfortably in a prompt (as a rough
rule of thumb, once it's into the dozens of articles), this is the
seam where real retrieval would go: keep the same
build_knowledge_context() contract but select articles by relevance to
the question instead of returning all of them.
"""

# Only this string stands between the model and answering from general
# training data, so it's written as hard constraints rather than polite
# preferences -- "if it is not in the articles, say so" is the whole
# point of the feature.
STRICT_GROUNDING_RULES = (
    "Answer ONLY using the Knowledge Hub articles provided below. They are your "
    "single source of truth.\n"
    "- If the articles cover the question, answer from them, and name the article "
    "you drew on so she can read it in full (for example: see \"Nutrition During "
    "Pregnancy and Breastfeeding\" in the Knowledge Hub).\n"
    "- If the articles do NOT cover the question, say plainly that it isn't in the "
    "Knowledge Hub yet, briefly name what related topics the articles DO cover, and "
    "suggest she ask a healthcare professional or an IBCLC lactation consultant. Do "
    "not answer from general knowledge, and do not guess.\n"
    "- Never contradict the articles, and never invent an article title, statistic, "
    "or storage time that does not appear in them."
)

# The alternative to STRICT_GROUNDING_RULES: prefer the articles but
# still help when they fall short. Swap which one build_system_prompt()
# uses (STRICT_KNOWLEDGE_ONLY in settings) to loosen this -- with only
# six articles, strict mode genuinely refuses common questions, milk
# bank donation among them.
PREFERRED_GROUNDING_RULES = (
    "Answer from the Knowledge Hub articles below wherever they cover the question, "
    "and name the article you drew on so she can read it in full. Where they don't "
    "cover it, say the Knowledge Hub doesn't have an article on it yet, then still "
    "help using established WHO, AAP and IBCLC guidance. Never contradict the "
    "articles, and never invent an article title or a figure that isn't in them."
)


def build_knowledge_context():
    """
    Returns the whole Knowledge Hub as prompt text, or "" if empty.

    Imported lazily inside the function rather than at module scope:
    chat is a sibling app to articles, and a module-level import would
    make merely importing the chat client depend on the articles app
    being loaded, which bites in tests and management commands.
    """
    from articles.models import Article

    articles = Article.objects.all().order_by("id")
    if not articles:
        return ""

    sections = []
    for article in articles:
        sections.append(
            f"--- ARTICLE: {article.title}\n"
            f"Category: {article.category}\n"
            f"{article.content}"
        )
    return "\n\n".join(sections)


def build_system_prompt(base_prompt, strict=True):
    """
    Base persona + grounding rules + the articles themselves.

    Falls back to the base persona alone when the library is empty --
    a Knowledge Hub with no articles in it would otherwise produce a
    Kali that refuses absolutely everything, which is a worse failure
    than answering from general guidance.
    """
    knowledge = build_knowledge_context()
    if not knowledge:
        return base_prompt

    rules = STRICT_GROUNDING_RULES if strict else PREFERRED_GROUNDING_RULES
    return (
        f"{base_prompt}\n\n"
        f"{rules}\n\n"
        f"=== KNOWLEDGE HUB ARTICLES ===\n{knowledge}"
    )
