"""Startup bootstrap: guarantee the schema exists and seed if the DB is empty.

This runs inside the app's own process/connection on every boot, so it works
regardless of Railway's ephemeral filesystem (where a pre-deploy seeder's file
is discarded before the app starts). It does NOT close the shared connection.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from db import database, models

logger = logging.getLogger(__name__)

SEED_FILE = Path(__file__).resolve().parent / "seed_data.json"


async def ensure_seeded() -> None:
    """Create tables (idempotent) and load seed data only if grants is empty."""
    await models.init_db()

    row = await database.fetch_one("SELECT COUNT(*) AS n FROM grants")
    if row and row["n"] > 0:
        logger.info("Database already has %d grants; skipping seed", row["n"])
        return

    if not SEED_FILE.exists():
        logger.warning("Seed file not found at %s; starting empty", SEED_FILE)
        return

    data = json.loads(SEED_FILE.read_text(encoding="utf-8"))

    for c in data.get("categories", []):
        await database.execute(
            "INSERT INTO categories (slug, name_en, name_uz) VALUES (?, ?, ?) "
            "ON CONFLICT(slug) DO UPDATE SET name_en=excluded.name_en, name_uz=excluded.name_uz",
            (c["slug"], c["name_en"], c["name_uz"]),
        )

    grants = data.get("grants", [])
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
    logger.info("Seeded %d grants, %d FAQs on startup", len(grants), len(faqs))
