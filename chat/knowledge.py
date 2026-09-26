"""
Builds the knowledge-base context that Kali is required to answer from.

Retrieval-augmented: chat/retrieval.py selects the passages relevant to
the question actually asked, and only those reach the prompt. This is
the "restricted, read-only retrieval of content exclusively from the
system's domain-limited knowledge base" the assistant is specified to
do, over both halves of that knowledge base -- the organization-verified
articles (articles.Article) and the research team's reviewed literature
(chat.KnowledgeDocument).

This module previously sent the entire library with every message, and
argued that was better than retrieval while the library was six
articles: no top-k cutoff to tune, no way for the right article to be
missed. That reasoning was sound at that size and stops holding once the
corpus includes literature -- it cannot grow past one prompt, it spends
the same ~20k characters whether the answer needs two paragraphs or
twenty, and it gives the model an entire library to pick from when the
point is to ground the answer in the part that actually applies.

What it did buy is worth keeping deliberately rather than by accident,
so retrieval keeps both properties: a lexical ranker means an exact
clinical term can never be missed by a semantic near-miss, and an empty
retrieval is passed through honestly instead of being papered over.
"""

from django.conf import settings

from .retrieval import retrieve

# Only this string stands between the model and answering from general
# training data, so it is written as hard constraints rather than polite
# preferences -- "if it is not in the passages, say so" is the whole
# point of the feature.
STRICT_GROUNDING_RULES = (
    "Answer ONLY using the knowledge base passages provided below. They are your "
    "single source of truth.\n"
    "- If the passages cover the question, answer from them, and cite the source "
    "named in the passage header so she can read it in full (for example: see "
    "\"Nutrition During Pregnancy and Breastfeeding\" in the Knowledge Hub).\n"
    "- If the passages do NOT cover the question, say plainly that it is not in the "
    "knowledge base yet, briefly name what the passages DO cover, and suggest she "
    "ask a healthcare professional or an IBCLC lactation consultant. Do not answer "
    "from general knowledge, and do not guess.\n"
    "- The passages are excerpts, not whole documents. Do not conclude that "
    "something is untrue merely because these excerpts do not mention it -- say it "
    "is not covered here.\n"
    "- Never contradict the passages, and never invent a source title, statistic, "
    "or storage time that does not appear in them."
)

# The alternative to STRICT_GROUNDING_RULES: prefer the passages but
# still help when they fall short. Swap which one build_system_prompt()
# uses (CHAT_STRICT_KNOWLEDGE_ONLY in settings) to loosen this -- with a
# small corpus, strict mode genuinely refuses common questions, milk
# bank donation among them.
PREFERRED_GROUNDING_RULES = (
    "Answer from the knowledge base passages below wherever they cover the question, "
    "and cite the source named in the passage header so she can read it in full. "
    "Where they do not cover it, say the knowledge base does not have anything on it "
    "yet, then still help using established WHO, AAP and IBCLC guidance. Never "
    "contradict the passages, and never invent a source title or a figure that is "
    "not in them."
)

# Shown in place of the passages when retrieval comes back empty. Without
# it the prompt would carry grounding rules referring to a section that
# is not there, and the likeliest reading of that is "there are no rules
# to follow" -- the exact failure the rules exist to prevent.
NO_MATCH_NOTICE = (
    "No passage in the knowledge base matched this question. Tell her plainly that "
    "the knowledge base does not cover it yet and suggest she ask a healthcare "
    "professional or an IBCLC lactation consultant. Do not answer it from general "
    "knowledge."
)


def build_knowledge_context(question, top_k=None):
    """
    The retrieved passages as prompt text, or "" if nothing matched.

    Each passage is labelled with the source it came from, because the
    grounding rules require Kali to cite one and the model can only cite
    what the prompt tells it. Passages are numbered as well, so a
    follow-up question that lands on the same sources has a stable way
    to refer to them.
    """
    passages = retrieve(question, top_k=top_k)
    if not passages:
        return ""

    sections = []
    for position, chunk in enumerate(passages, start=1):
        sections.append(
            f"--- PASSAGE {position} | SOURCE: {chunk.citation}\n{chunk.text}"
        )
    return "\n\n".join(sections)


def build_system_prompt(base_prompt, question="", strict=True):
    """
    Base persona + grounding rules + the passages retrieved for this
    question.

    `question` defaults to "" only so that a caller which has not been
    updated still produces a usable prompt rather than an exception; with
    no question there is nothing to retrieve against, so that path
    returns the persona alone -- the same thing this did when the library
    was empty. Real callers pass the mother's message.
    """
    if not question:
        return base_prompt

    rules = STRICT_GROUNDING_RULES if strict else PREFERRED_GROUNDING_RULES
    context = build_knowledge_context(question)

    if not context:
        # Strict mode has a defined answer for "not covered", so it keeps
        # its rules and is told the retrieval was empty. The looser mode
        # is allowed to help from general guidance anyway, and handing it
        # rules about passages that do not exist only confuses that.
        if not strict:
            return f"{base_prompt}\n\n{PREFERRED_GROUNDING_RULES}\n\n{NO_MATCH_NOTICE}"
        return f"{base_prompt}\n\n{rules}\n\n=== KNOWLEDGE BASE ===\n{NO_MATCH_NOTICE}"

    return (
        f"{base_prompt}\n\n"
        f"{rules}\n\n"
        f"=== KNOWLEDGE BASE PASSAGES (retrieved for this question) ===\n{context}"
    )
