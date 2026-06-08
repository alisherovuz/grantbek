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
    "You are GrantBek, a warm and polite assistant that helps students find "
    "educational grants and scholarships. Rules:\n"
    "1. Answer ONLY using the CONTEXT provided. Never invent grants, deadlines, "
    "amounts, or URLs.\n"
    "2. If the CONTEXT does not contain the answer, say so briefly and kindly, and "
    "suggest /search or /categories.\n"
    "3. Reply in the SAME language the user wrote in (e.g. Uzbek, English, or "
    "Russian). Match their language exactly.\n"
    "4. Do NOT begin with a greeting or thanks (no 'Salom', 'Assalomu alaykum', "
    "'Hello', 'Hi', 'rahmat uchun'). Start directly with the answer.\n"
    "5. Be SHORT: 1-3 short sentences, simple and friendly. No tables, no long "
    "bullet lists, no markdown headers. Mention at most 2 grants, one line each.\n"
)

_WEB_SYSTEM = (
    "You are GrantBek, a warm and polite assistant that helps students find "
    "educational grants and scholarships. You have two sources of information:\n"
    "1. CONTEXT below — grants from EduGrands' own curated database (or the exact "
    "post the user is commenting under). Prefer these and use them first.\n"
    "2. The web_search tool — use it to find real, current grants or scholarships "
    "only when CONTEXT does not cover what the user asked.\n"
    "Rules:\n"
    "- NEVER invent grants, deadlines, amounts, eligibility, or links. State only "
    "what is in CONTEXT or what you actually found via web_search, with the "
    "official link.\n"
    "- If searches find nothing solid, say so honestly rather than guessing.\n"
    "- Reply in the SAME language the user wrote in (Uzbek, English, or Russian). "
    "Match their language exactly.\n"
    "- Do NOT begin with a greeting or thanks (no 'Salom', 'Assalomu alaykum', "
    "'Hello', 'Hi'). Start directly with the answer.\n"
    "- Be SHORT: 1-3 short, polite sentences. No tables, no markdown headers. Keep "
    "to the 1-2 most relevant options, one short line each.\n"
)

_LANG_NAME = {"en": "English", "uz": "Uzbek (o'zbek tili)"}


def _build_context(
    grants: list[dict[str, Any]],
    faq: Optional[dict[str, Any]],
    lang: str,
    post_text: Optional[str] = None,
) -> str:
    parts: list[str] = []
    if post_text:
        parts.append(
            "[GRANT POST — the user is commenting under this exact channel post. "
            "Answer about THIS grant, using this text as the source of truth]\n"
            + post_text.strip()
        )
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
    allow_web: bool = False,
    post_text: Optional[str] = None,
) -> Optional[str]:
    """Return a grounded natural-language answer, or None on failure.

    When allow_web is True (and web search is enabled), Claude may use the
    web_search tool to research grants the curated DB doesn't cover.
    When post_text is given, it's treated as the authoritative context (the
    grant post the user is commenting under).
    """
    if not settings.llm_enabled:
        return None

    use_web = allow_web and settings.enable_web_search
    context = _build_context(grants, faq, lang, post_text)
    system = _WEB_SYSTEM if use_web else _SYSTEM
    user = f"CONTEXT:\n{context}\n\nUSER QUESTION:\n{question}"

    kwargs: dict[str, Any] = dict(
        model=settings.llm_model,
        max_tokens=settings.llm_max_tokens,
        system=system,
    )
    if use_web:
        kwargs["tools"] = [
            {
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": settings.web_search_max_uses,
            }
        ]

    try:
        client = _get_client()
        messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
        resp = None
        for _ in range(4):  # handle server-tool pause_turn continuations
            resp = await client.messages.create(messages=messages, **kwargs)
            if getattr(resp, "stop_reason", None) == "pause_turn":
                messages.append({"role": "assistant", "content": resp.content})
                continue
            break
        chunks = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
        text = "".join(chunks).strip()
        return text or None
    except Exception as exc:  # network, auth, rate limit, etc.
        logger.warning("Claude call failed, falling back to keyword answer: %s", exc)
        return None
