"""Seed the SQLite database from db/seed_data.json. Idempotent: safe to re-run.

Run:  python scripts/seed_db.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

# Allow running as a standalone script: add project root to sys.path.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import settings  # noqa: E402
from db import database, models  # noqa: E402

logging.basicConfig(level=settings.log_level)
logger = logging.getLogger("seed")

SEED_FILE = ROOT / "db" / "seed_data.json"


async def seed() -> None:
    await models.init_db()
    data = json.loads(SEED_FILE.read_text(encoding="utf-8"))

    cats = data.get("categories", [])
    for c in cats:
        await database.execute(
            "INSERT INTO categories (slug, name_en, name_uz) VALUES (?, ?, ?) "
            "ON CONFLICT(slug) DO UPDATE SET name_en=excluded.name_en, name_uz=excluded.name_uz",
            (c["slug"], c["name_en"], c["name_uz"]),
        )

    # Grants: replace existing set so re-running reflects edits to seed_data.json.
    grants = data.get("grants", [])
    await database.execute("DELETE FROM grants")
    for g in grants:
        await database.execute(
            """INSERT INTO grants
               (title_en, title_uz, description_en, description_uz, organization,
                funding_level, format_mode, age_category, flag, deadline,
                benefits_en, benefits_uz, extra_links, country, category_slug, url)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                g.get("title_en", ""), g.get("title_uz", ""),
                g.get("description_en", ""), g.get("description_uz", ""),
                g.get("organization", ""),
                g.get("funding_level", ""), g.get("format_mode", ""),
                g.get("age_category", ""), g.get("flag", ""), g.get("deadline", ""),
                json.dumps(g.get("benefits_en", []), ensure_ascii=False),
                json.dumps(g.get("benefits_uz", []), ensure_ascii=False),
                json.dumps(g.get("extra_links", []), ensure_ascii=False),
                g.get("country", ""), g.get("category_slug", ""), g.get("url", ""),
            ),
        )

    faqs = data.get("faqs", [])
    await database.execute("DELETE FROM faqs")
    for f in faqs:
        await database.execute(
            """INSERT INTO faqs
               (question_en, answer_en, question_uz, answer_uz, keywords, category)
               VALUES (?,?,?,?,?,?)""",
            (
                f.get("question_en", ""), f.get("answer_en", ""),
                f.get("question_uz", ""), f.get("answer_uz", ""),
                f.get("keywords", ""), f.get("category", ""),
            ),
        )

    await models.rebuild_fts()
    await database.close_connection()
    logger.info(
        "Seeded %d categories, %d grants, %d FAQs into %s",
        len(cats), len(grants), len(faqs), settings.database_path,
    )


if __name__ == "__main__":
    asyncio.run(seed())
