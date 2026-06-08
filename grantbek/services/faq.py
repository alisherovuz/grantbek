"""FAQ matching via lightweight keyword overlap scoring (no external API).

Scoring: for a user query, count overlapping tokens between the query and each
FAQ's keywords + question text (both languages), weighting keyword hits higher.
Returns the best match above a small threshold, else None.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from db import database
from bot.i18n import t

_TOKEN = re.compile(r"[0-9a-zA-Z\u0400-\u04FF']+")
_STOP = {
    "the", "a", "an", "is", "are", "do", "i", "to", "for", "of", "and", "or",
    "what", "how", "can", "my", "me", "in", "on", "you", "it", "be",
    "men", "men ", "uchun", "va", "yoki", "qanday", "nima", "bu", "ham",
}


def _tokens(text: str) -> set[str]:
    return {w.lower() for w in _TOKEN.findall(text or "") if len(w) > 1 and w.lower() not in _STOP}


async def get_all_faqs() -> list[dict[str, Any]]:
    rows = await database.fetch_all("SELECT * FROM faqs ORDER BY id")
    return [dict(r) for r in rows]


async def get_faq_by_id(faq_id: int) -> Optional[dict[str, Any]]:
    row = await database.fetch_one("SELECT * FROM faqs WHERE id = ?", (faq_id,))
    return dict(row) if row else None


async def find_faq(query: str, threshold: float = 1.5) -> Optional[dict[str, Any]]:
    q_tokens = _tokens(query)
    if not q_tokens:
        return None

    best: Optional[dict[str, Any]] = None
    best_score = 0.0
    for faq in await get_all_faqs():
        kw = _tokens(faq.get("keywords", ""))
        q_text = _tokens(faq.get("question_en", "")) | _tokens(faq.get("question_uz", ""))
        score = 2.0 * len(q_tokens & kw) + 1.0 * len(q_tokens & q_text)
        if score > best_score:
            best_score = score
            best = faq
    return best if best_score >= threshold else None


def _loc(faq: dict[str, Any], field: str, lang: str) -> str:
    if lang == "uz":
        uz = (faq.get(f"{field}_uz") or "").strip()
        if uz:
            return uz
    return faq.get(f"{field}_en") or ""


def format_faq_response(faq: dict[str, Any], lang: str = "en") -> str:
    import html

    q = html.escape(_loc(faq, "question", lang))
    a = html.escape(_loc(faq, "answer", lang))
    return f"❓ <b>{q}</b>\n\n{a}"


def faq_question(faq: dict[str, Any], lang: str = "en") -> str:
    return _loc(faq, "question", lang)
