"""Command line.

  python -m edugrants_agent bot            # run the admin bot + scheduled pipeline (production)
  python -m edugrants_agent run            # one full cycle, print drafts to the terminal (testing)
  python -m edugrants_agent collect        # only gather candidates
  python -m edugrants_agent check          # test every part for real (run this first)
  python -m edugrants_agent health         # which sources work
  python -m edugrants_agent import-history messages.html   # learn from the channel's own export
  python -m edugrants_agent seed FILE.csv  # load already-published grants so they're never reposted
  python -m edugrants_agent show ID        # print one item's post and platform fields
  python -m edugrants_agent reset          # forget every find (start fresh); keeps the channel history
  python -m edugrants_agent railway-env    # print .env cleaned up for Railway's Variables > Raw Editor
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import sys

from .config import settings
from .db import DB
from .dedupe import canonical_url, normalize_title


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(prog="edugrants_agent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("bot")
    sub.add_parser("run")
    sub.add_parser("collect")
    sub.add_parser("health")
    sub.add_parser("reset-exclusions")
    sub.add_parser("tg-login")
    sub.add_parser("recheck")
    sub.add_parser("reset")
    sub.add_parser("railway-env")
    ck = sub.add_parser("check")
    ck.add_argument("-n", type=int, default=5, help="real listings to push through end to end")
    ih = sub.add_parser("import-history")
    ih.add_argument("export_file", help="Telegram Desktop export: messages.html or result.json")
    s = sub.add_parser("seed")
    s.add_argument("csv_file", help="CSV with columns: title,url (url optional)")
    sh = sub.add_parser("show")
    sh.add_argument("item_id", type=int)
    args = ap.parse_args()

    if args.cmd in ("check", "run", "collect"):
        from .bot import auto_import_history
        auto_import_history(DB(settings.db_path))

    if args.cmd == "railway-env":   # secrets go to your terminal only; paste them into Railway yourself
        from dotenv import dotenv_values
        from .config import ROOT
        skip = {"DB_PATH", "HISTORY_FILE"}   # the container uses its own paths (/app/data on the volume)
        for k, v in dotenv_values(ROOT / ".env").items():
            if k not in skip and v not in (None, ""):
                print(f"{k}={v}")
        return

    if args.cmd == "check":
        from .check import run_check
        raise SystemExit(run_check(args.n))

    if args.cmd == "tg-login":  # one time, on your own computer
        from telethon.sessions import StringSession
        from telethon.sync import TelegramClient
        with TelegramClient(StringSession(), int(settings.tg_api_id), settings.tg_api_hash) as client:
            me = client.get_me()
            print(f"\nLogged in as {me.first_name} (@{me.username}). Put this line in .env / Railway variables:\n")
            print("TG_STRING_SESSION=" + client.session.save())
            print("\nKeep it secret: it gives access to this Telegram account.")
        return

    if args.cmd == "bot":
        from .bot import main as bot_main
        asyncio.run(bot_main())
        return

    db = DB(settings.db_path)
    if args.cmd == "import-history":
        from .history import build_profile, import_history
        print(import_history(db, args.export_file))
        profile = build_profile(db)
        settings.profile_file.parent.mkdir(parents=True, exist_ok=True)
        settings.profile_file.write_text(profile, encoding="utf-8")
        print(f"\nprofile written to {settings.profile_file}:\n\n{profile}")
        return

    if args.cmd == "recheck":  # after changing the rules in .env: give rejected items a second chance
        import json as _json
        from .pipeline import Pipeline as _P, format_gaps
        passed = retriage = 0
        for r in db.conn.execute("SELECT * FROM items WHERE status='rejected'").fetchall():
            if r["data_json"]:                       # read already: just re-apply the rules, no AI needed
                _d = _json.loads(r["data_json"])
                if _P.reject_reason(_d) is None and not format_gaps(_d, r["official_url"]):
                    db.update(r["id"], status="extracted", reason=None)
                    passed += 1
            elif (r["reason"] or "").startswith("triage"):   # cut at the vibe step: judge again
                db.update(r["id"], status="new", reason=None)
                retriage += 1
        db.conn.execute("UPDATE history SET excluded=NULL WHERE excluded IS NOT NULL")
        db.conn.commit()
        print(f"{passed} items now pass the rules, {retriage} go back to the vibe check. "
              "Press the search button to send them.")
        return

    if args.cmd == "reset":   # forget all finds, so everything is new again; channel history stays
        with db.tx() as c:
            n = c.execute("SELECT COUNT(*) FROM items").fetchone()[0]
            for table in ("items", "page_watch", "competitor_posts", "source_health"):
                c.execute(f"DELETE FROM {table}")
        print(f"Forgot {n} finds. The channel history ({len(db.history_rows())} programmes) is kept.")
        return

    if args.cmd == "reset-exclusions":  # e.g. after changing ALLOW_FULL_AID or the age range
        n = db.conn.execute("UPDATE history SET excluded=NULL WHERE excluded IS NOT NULL").rowcount
        db.conn.commit()
        print(f"{n} programmes will be watched again")
        return

    if args.cmd == "seed":
        n = 0
        with open(args.csv_file, encoding="utf-8-sig") as f:
            for i, row in enumerate(csv.DictReader(f)):
                title = (row.get("title") or "").strip()
                if not title:
                    continue
                url = (row.get("url") or "").strip() or f"https://edugrants.uz/seed/{i}"
                if db.insert_item(source="seed", url=url, canonical_url=canonical_url(url), title=title,
                                  norm_title=normalize_title(title), summary=None, published_at=None):
                    n += 1
        db.conn.execute("UPDATE items SET status='published', reason='seeded' WHERE source='seed'")
        db.conn.commit()
        print(f"seeded {n} existing listings")
        return

    if args.cmd == "show":
        item = db.get(args.item_id)
        if not item:
            sys.exit("not found")
        print(f"#{item['id']} [{item['status']}] {item['title']}\n{item['url']}\nreason: {item['reason']}\n")
        print(item["post_text"] or "(no post yet)")
        if item["platform_json"]:
            print("\n" + json.dumps(json.loads(item["platform_json"]), ensure_ascii=False, indent=2))
        return

    if args.cmd == "health":
        for r in db.health():
            print(f"{r['source']:<30} ok={r['last_ok']}  items={r['items_last_run']}  err={r['last_error'] or ''}")
        return

    from .pipeline import Pipeline
    p = Pipeline(db)
    if args.cmd == "collect":
        print(f"{p.collect()} new candidates")
    elif args.cmd == "run":
        print(p.run())
        if settings.mode == "finder":
            import re
            from .render import finder_card
            for item in db.by_status("extracted", limit=50):
                card, _ = finder_card(db, item)
                print("\n" + "=" * 60 + f"\n#{item['id']}\n" + re.sub(r"<[^>]+>", "", card))
            return
        for item in db.by_status("drafted", limit=50):
            print("\n" + "=" * 60 + f"\n#{item['id']}  ({item['url']})\n" + "=" * 60)
            print(item["post_text"])


if __name__ == "__main__":
    main()
