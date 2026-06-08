"""Schema definition and initialisation.

Grants and FAQs carry both English and Uzbek fields. A grant's *_uz columns
may be empty, in which case the bot falls back to the English text.

Full-text search uses FTS5 when available; ``HAS_FTS5`` records the result so
the search service can fall back to LIKE queries on builds without it.
"""
from __future__ import annotations

import logging

from db.database import get_connection

logger = logging.getLogger(__name__)

HAS_FTS5: bool = True

SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (
    id          INTEGER PRIMARY KEY,
    slug        TEXT UNIQUE NOT NULL,
    name_en     TEXT NOT NULL,
    name_uz     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS grants (
    id              INTEGER PRIMARY KEY,
    title_en        TEXT NOT NULL,
    title_uz        TEXT DEFAULT '',
    description_en  TEXT NOT NULL,
    description_uz  TEXT DEFAULT '',
    organization    TEXT DEFAULT '',
    amount          TEXT DEFAULT '',
    currency        TEXT DEFAULT '',
    funding_level   TEXT DEFAULT '',          -- 'full' | 'partial' | ''
    format_mode     TEXT DEFAULT '',          -- e.g. 'Online' / 'Offline' / ''
    age_category    TEXT DEFAULT '',          -- e.g. '11-18'
    flag            TEXT DEFAULT '',          -- country flag emoji
    deadline        TEXT DEFAULT '',          -- ISO date YYYY-MM-DD or ''
    eligibility_en  TEXT DEFAULT '',
    eligibility_uz  TEXT DEFAULT '',
    benefits_en     TEXT DEFAULT '[]',        -- JSON array of strings
    benefits_uz     TEXT DEFAULT '[]',        -- JSON array of strings
    extra_links     TEXT DEFAULT '[]',        -- JSON array of {emoji,label_en,label_uz,url}
    country         TEXT DEFAULT '',
    category_slug   TEXT DEFAULT '',
    url             TEXT DEFAULT '',
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS faqs (
    id           INTEGER PRIMARY KEY,
    question_en  TEXT NOT NULL,
    answer_en    TEXT NOT NULL,
    question_uz  TEXT DEFAULT '',
    answer_uz    TEXT DEFAULT '',
    keywords     TEXT DEFAULT '',
    category     TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS user_prefs (
    user_id   INTEGER PRIMARY KEY,
    language  TEXT NOT NULL DEFAULT 'en',
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS user_searches (
    id            INTEGER PRIMARY KEY,
    user_id       INTEGER,
    query         TEXT,
    results_count INTEGER,
    timestamp     TEXT DEFAULT (datetime('now'))
);
"""

# Contentless-linked FTS index over searchable grant text.
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS grants_fts USING fts5(
    title_en, title_uz, description_en, description_uz,
    eligibility_en, eligibility_uz, organization, country, category_slug,
    content='grants', content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS grants_ai AFTER INSERT ON grants BEGIN
    INSERT INTO grants_fts(rowid, title_en, title_uz, description_en, description_uz,
        eligibility_en, eligibility_uz, organization, country, category_slug)
    VALUES (new.id, new.title_en, new.title_uz, new.description_en, new.description_uz,
        new.eligibility_en, new.eligibility_uz, new.organization, new.country, new.category_slug);
END;

CREATE TRIGGER IF NOT EXISTS grants_ad AFTER DELETE ON grants BEGIN
    INSERT INTO grants_fts(grants_fts, rowid, title_en, title_uz, description_en, description_uz,
        eligibility_en, eligibility_uz, organization, country, category_slug)
    VALUES ('delete', old.id, old.title_en, old.title_uz, old.description_en, old.description_uz,
        old.eligibility_en, old.eligibility_uz, old.organization, old.country, old.category_slug);
END;

CREATE TRIGGER IF NOT EXISTS grants_au AFTER UPDATE ON grants BEGIN
    INSERT INTO grants_fts(grants_fts, rowid, title_en, title_uz, description_en, description_uz,
        eligibility_en, eligibility_uz, organization, country, category_slug)
    VALUES ('delete', old.id, old.title_en, old.title_uz, old.description_en, old.description_uz,
        old.eligibility_en, old.eligibility_uz, old.organization, old.country, old.category_slug);
    INSERT INTO grants_fts(rowid, title_en, title_uz, description_en, description_uz,
        eligibility_en, eligibility_uz, organization, country, category_slug)
    VALUES (new.id, new.title_en, new.title_uz, new.description_en, new.description_uz,
        new.eligibility_en, new.eligibility_uz, new.organization, new.country, new.category_slug);
END;
"""


async def init_db() -> None:
    """Create all tables; attempt FTS5 and remember whether it worked."""
    global HAS_FTS5
    conn = await get_connection()
    await conn.executescript(SCHEMA)
    await conn.commit()

    try:
        await conn.executescript(FTS_SCHEMA)
        await conn.commit()
        HAS_FTS5 = True
        logger.info("FTS5 search index ready")
    except Exception as exc:  # pragma: no cover - depends on sqlite build
        HAS_FTS5 = False
        logger.warning("FTS5 unavailable, falling back to LIKE search: %s", exc)


async def rebuild_fts() -> None:
    """Repopulate the FTS index from the grants table (used after seeding)."""
    if not HAS_FTS5:
        return
    conn = await get_connection()
    try:
        await conn.execute(
            "INSERT INTO grants_fts(grants_fts) VALUES ('rebuild')"
        )
        await conn.commit()
    except Exception as exc:  # pragma: no cover
        logger.warning("FTS rebuild failed: %s", exc)
