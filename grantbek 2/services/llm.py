"""Optional Claude API layer.

This is the *only* place that spends Anthropic tokens. It is used to phrase a
natural answer, strictly grounded in grants/FAQ rows we retrieved from our own
database — so Claude never invents grants. If no API key is configured, callers
skip this module entirely and use keyword-only FAQ matching (zero cost).

Model defaults to Claude Haiku (cheapest current-gen tier) and is configurable
via the LLM_MODEL env var.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from config import settings

logger = logging.getLogger(__name__)

_client = None


def _get_client():
    global _client
    if _client is None:
        from anthropic import AsyncAnthropic

        _client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


_SYSTEM = (
    "You are GrantBek, a friendly assistant that helps students find educational "
    "grants and scholarships. Rules:\n"
    "1. Answer ONLY using the CONTEXT provided. Never invent grants, deadlines, "
    "amounts, or URLs.\n"
    "2. If the context does not contain the answer, say briefly that you don't "
    "have that specific info and suggest /search or /categories.\n"
    "3. Reply in {lang_name}.\n"
    "4. Keep replies short and warm (a few sentences). Use Telegram-friendly "
    "plain text, no tables.\n"
)

_LANG_NAME = {"en": "English", "uz": "Uzbek (o'zbek tili)"}


def _build_context(grants: list[dict[str, Any]], faq: Optional[dict[str, Any]], lang: str) -> str:
    parts: list[str] = []
    if faq:
        q = faq.get(f"question_{lang}") or faq.get("question_en") or ""
        a = faq.get(f"answer_{lang}") or faq.get("answer_en") or ""
        parts.append(f"[FAQ]\nQ: {q}\nA: {a}")
    for g in grants:
        title = g.get(f"title_{lang}") or g.get("title_en") or ""
        desc = g.get(f"description_{lang}") or g.get("description_en") or ""
        parts.append(
            "[GRANT]\n"
            f"Title: {title}\n"
            f"Description: {desc}\n"
            f"Organization: {g.get('organization','')}\n"
            f"Funding: {g.get('amount','')} {g.get('currency','')}\n"
            f"Country: {g.get('country','')}\n"
            f"Deadline: {g.get('deadline','') or 'see source'}\n"
            f"URL: {g.get('url','')}"
        )
    return "\n\n".join(parts) if parts else "(no matching grants or FAQ found)"


async def answer(
    question: str,
    grants: list[dict[str, Any]],
    faq: Optional[dict[str, Any]],
    lang: str = "en",
) -> Optional[str]:
    """Return a grounded natural-language answer, or None on failure."""
    if not settings.llm_enabled:
        return None

    context = _build_context(grants, faq, lang)
    system = _SYSTEM.format(lang_name=_LANG_NAME.get(lang, "English"))
    user = f"CONTEXT:\n{context}\n\nUSER QUESTION:\n{question}"

    try:
        client = _get_client()
        resp = await client.messages.create(
            model=settings.llm_model,
            max_tokens=settings.llm_max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        chunks = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
        text = "".join(chunks).strip()
        return text or None
    except Exception as exc:  # network, auth, rate limit, etc.
        logger.warning("Claude call failed, falling back to keyword answer: %s", exc)
        return None
