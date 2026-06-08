"""Bulk-import grants from a CSV file.

Expected columns (header row, UTF-8). Only title_en and description_en are
required; the rest may be blank:

    title_en, title_uz, description_en, description_uz, organization,
    amount, currency, deadline, eligibility_en, eligibility_uz,
    country, category_slug, url

deadline format: YYYY-MM-DD (or leave blank).

Run:  python scripts/import_csv.py path/to/grants.csv
      python scripts/import_csv.py path/to/grants.csv --replace   # wipe first
"""
from __future__ import annotations

import asyncio
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from db import database, models  # noqa: E402

COLUMNS = [
    "title_en", "title_uz", "description_en", "description_uz", "organization",
    "funding_level", "format_mode", "age_category", "flag", "deadline",
    "benefits_en", "benefits_uz", "country", "category_slug", "url",
]
# benefits_* in the CSV are "|"-separated strings, stored as JSON arrays.
_LIST_COLS = {"benefits_en", "benefits_uz"}


async def import_csv(path: Path, replace: bool) -> int:
    await models.init_db()
    if replace:
        await database.execute("DELETE FROM grants")

    count = 0
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        missing = {"title_en", "description_en"} - set(reader.fieldnames or [])
        if missing:
            raise SystemExit(f"CSV is missing required columns: {', '.join(missing)}")
        for raw in reader:
            row = {c: (raw.get(c) or "").strip() for c in COLUMNS}
            if not row["title_en"]:
                continue
            values = []
            for c in COLUMNS:
                if c in _LIST_COLS:
                    items = [s.strip() for s in row[c].split("|") if s.strip()]
                    values.append(json.dumps(items, ensure_ascii=False))
                else:
                    values.append(row[c])
            await database.execute(
                f"INSERT INTO grants ({', '.join(COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in COLUMNS)})",
                tuple(values),
            )
            count += 1

    await models.rebuild_fts()
    await database.close_connection()
    return count


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        raise SystemExit("Usage: python scripts/import_csv.py grants.csv [--replace]")
    csv_path = Path(args[0])
    if not csv_path.exists():
        raise SystemExit(f"File not found: {csv_path}")
    n = asyncio.run(import_csv(csv_path, replace="--replace" in sys.argv))
    print(f"Imported {n} grants from {csv_path}")
