"""Grant search & formatting.

Search prefers SQLite FTS5; if the build lacks FTS5 (see models.HAS_FTS5),
it falls back to case-insensitive LIKE matching across the key columns.
"""
from __future__ import annotations

import html
import json
import re
from datetime import date, datetime, timedelta
from typing import Any, Optional

from config import settings
from db import database, models
from bot.i18n import t

_FTS_SANITIZE = re.compile(r'[^0-9a-zA-Z\u0400-\u04FF\'\- ]+')


def _localize(grant: dict[str, Any], field: str, lang: str) -> str:
    """Pick the *_uz value when lang=uz and it's non-empty, else *_en."""
    if lang == "uz":
        uz = (grant.get(f"{field}_uz") or "").strip()
        if uz:
            return uz
    return grant.get(f"{field}_en") or grant.get(field) or ""


def _fts_query(raw: str) -> str:
    """Turn a free-text query into a safe FTS5 prefix query (OR of terms)."""
    cleaned = _FTS_SANITIZE.sub(" ", raw).strip()
    terms = [w for w in cleaned.split() if len(w) >= 2]
    if not terms:
        return ""
    return " OR ".join(f'"{w}"*' for w in terms)


async def search_grants(query: str, limit: int = 8) -> list[dict[str, Any]]:
    query = (query or "").strip()
    if not query:
        return []

    if models.HAS_FTS5:
        fts = _fts_query(query)
        if fts:
            try:
                rows = await database.fetch_all(
                    """
                    SELECT g.* FROM grants_fts f
                    JOIN grants g ON g.id = f.rowid
                    WHERE grants_fts MATCH ?
                    ORDER BY bm25(grants_fts) LIMIT ?
                    """,
                    (fts, limit),
                )
                if rows:
                    return [dict(r) for r in rows]
            except Exception:
                pass  # fall through to LIKE

    like = f"%{query}%"
    rows = await database.fetch_all(
        """
        SELECT * FROM grants
        WHERE title_en LIKE ? OR title_uz LIKE ?
           OR description_en LIKE ? OR description_uz LIKE ?
           OR organization LIKE ? OR country LIKE ? OR category_slug LIKE ?
        ORDER BY deadline = '' , deadline
        LIMIT ?
        """,
        (like, like, like, like, like, like, like, limit),
    )
    return [dict(r) for r in rows]


async def get_grant_by_id(grant_id: int) -> Optional[dict[str, Any]]:
    row = await database.fetch_one("SELECT * FROM grants WHERE id = ?", (grant_id,))
    return dict(row) if row else None


async def get_grants_by_category(slug: str, limit: int = 10) -> list[dict[str, Any]]:
    rows = await database.fetch_all(
        "SELECT * FROM grants WHERE category_slug = ? "
        "ORDER BY deadline = '', deadline LIMIT ?",
        (slug, limit),
    )
    return [dict(r) for r in rows]


async def get_upcoming_deadlines(days: int = 30, limit: int = 10) -> list[dict[str, Any]]:
    today = date.today().isoformat()
    horizon = (date.today() + timedelta(days=days)).isoformat()
    rows = await database.fetch_all(
        "SELECT * FROM grants WHERE deadline != '' AND deadline >= ? AND deadline <= ? "
        "ORDER BY deadline LIMIT ?",
        (today, horizon, limit),
    )
    return [dict(r) for r in rows]


async def list_categories() -> list[dict[str, Any]]:
    rows = await database.fetch_all("SELECT * FROM categories ORDER BY id")
    return [dict(r) for r in rows]


_UZ_MONTHS = [
    "", "yanvar", "fevral", "mart", "aprel", "may", "iyun",
    "iyul", "avgust", "sentyabr", "oktyabr", "noyabr", "dekabr",
]

# Labels are tightly coupled to the EduGrands post layout, kept here with the
# formatter rather than in the general i18n table.
_L = {
    "country": {"uz": "Davlat", "en": "Country"},
    "funding": {"uz": "Moliyaviy ta'minot", "en": "Funding"},
    "format": {"uz": "Dastur shakli", "en": "Program format"},
    "age": {"uz": "Yosh toifasi", "en": "Age group"},
    "benefits": {"uz": "➡️Imtiyozlari:", "en": "➡️Benefits:"},
    "register": {"uz": "🔗Ro'yxatdan o'tish uchun:", "en": "🔗Register here:"},
    "deadline": {"uz": "📌Ro'yxatdan o'tishning so'nggi muddati:", "en": "📌Deadline:"},
    "link_word": {"uz": "Havola", "en": "Link"},
}
_FUNDING_WORD = {
    "full": {"uz": "To'liq", "en": "Full"},
    "partial": {"uz": "Qisman", "en": "Partial"},
}


def _lbl(key: str, lang: str) -> str:
    return _L[key].get(lang, _L[key]["en"])


def _fmt_deadline(value: str, lang: str) -> str:
    if not value:
        return ""
    try:
        d = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return value
    if lang == "uz":
        return f"{d.day}-{_UZ_MONTHS[d.month]}"
    return d.strftime("%-d %B") if hasattr(d, "strftime") else value


def _json_list(raw: Any) -> list:
    if isinstance(raw, list):
        return raw
    if not raw:
        return []
    try:
        val = json.loads(raw)
        return val if isinstance(val, list) else []
    except (ValueError, TypeError):
        return []


def _benefits(grant: dict[str, Any], lang: str) -> list[str]:
    items = _json_list(grant.get(f"benefits_{lang}"))
    if not items and lang == "uz":
        items = _json_list(grant.get("benefits_en"))
    if not items and lang == "en":
        items = _json_list(grant.get("benefits_en"))
    return [str(i) for i in items]


def format_grant_card(grant: dict[str, Any], lang: str = "en") -> str:
    """Render a grant in the EduGrands channel post format (HTML parse mode)."""
    e = html.escape
    title = e(_localize(grant, "title", lang))
    lines = [f"<b>{title}</b>", ""]

    # Meta block.
    country = e(grant.get("country", "") or "")
    flag = grant.get("flag", "") or ""
    if country or flag:
        lines.append(f"{_lbl('country', lang)}: {country}{flag}")

    funding_word = _FUNDING_WORD.get(grant.get("funding_level", ""), {}).get(lang, "")
    if funding_word:
        lines.append(f"{_lbl('funding', lang)}: {funding_word}")

    fmt = (grant.get("format_mode") or "").strip()
    if fmt:
        lines.append(f"{_lbl('format', lang)}: {e(fmt)}")

    age = (grant.get("age_category") or "").strip()
    if age:
        lines.append(f"{_lbl('age', lang)}: {e(age)}")

    # Description.
    desc = _localize(grant, "description", lang)
    if desc:
        lines += ["", e(desc)]

    # Benefits.
    benefits = _benefits(grant, lang)
    if benefits:
        lines += ["", _lbl("benefits", lang)]
        lines += [f"- {e(b)}" for b in benefits]

    # Registration link.
    url = (grant.get("url") or "").strip()
    if url:
        link = f'<a href="{e(url)}">{_lbl("link_word", lang)}</a>'
        lines += ["", f"{_lbl('register', lang)} {link}"]

    # Deadline.
    dl = _fmt_deadline(grant.get("deadline", ""), lang)
    if dl:
        lines += ["", f"{_lbl('deadline', lang)} {dl}"]

    # Optional extra links (e.g. podcast).
    for ex in _json_list(grant.get("extra_links")):
        label = ex.get(f"label_{lang}") or ex.get("label_en") or ""
        ex_url = ex.get("url") or ""
        emoji = ex.get("emoji") or ""
        if label and ex_url:
            link = f'<a href="{e(ex_url)}">{_lbl("link_word", lang)}</a>'
            lines += ["", f"{emoji}{e(label)}: {link}"]

    # Footer.
    lines += ["", f"⚡️{e(settings.channel_handle)}"]
    return "\n".join(lines)


def grant_title(grant: dict[str, Any], lang: str = "en") -> str:
    return _localize(grant, "title", lang)


def grant_snippet(grant: dict[str, Any], lang: str = "en", length: int = 120) -> str:
    desc = _localize(grant, "description", lang)
    return desc if len(desc) <= length else desc[: length - 1].rstrip() + "…"


async def log_search(user_id: int, query: str, results_count: int) -> None:
    try:
        await database.execute(
            "INSERT INTO user_searches (user_id, query, results_count) VALUES (?, ?, ?)",
            (user_id, query, results_count),
        )
    except Exception:
        pass
