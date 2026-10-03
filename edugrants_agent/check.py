"""`python -m edugrants_agent check`: proves every part works, for real, in a few minutes.

Tests each piece against the live services, then pushes a handful of real listings through the
whole pipeline (triage, organiser page, rules) in a throwaway database, and prints what happened
to each. Nothing is posted and the real database is not touched. Costs a few cents of Claude usage.
"""
from __future__ import annotations

import asyncio
import json
import re
import traceback

from .config import load_yaml, settings
from .db import DB

OK, BAD, WARN = "✅", "❌", "⚠️"


class Report:
    def __init__(self):
        self.fails = 0
        self.warns = 0

    def line(self, mark: str, text: str) -> None:
        if mark == BAD:
            self.fails += 1
        elif mark == WARN:
            self.warns += 1
        print(f"  {mark} {text}")


def _short(e: Exception) -> str:
    return f"{type(e).__name__}: {e}"[:200]


# --------------------------------------------------------------------------- pieces
def settings_dir():
    from .config import ROOT
    return ROOT


def check_settings(r: Report) -> None:
    print("\n1. Settings (.env)")
    for name, value, why in [
        ("ANTHROPIC_API_KEY", settings.anthropic_api_key, "needed for every search"
         + ("" if (settings_dir() / ".env").exists() else " (no .env file found: copy .env.example to .env)")),
        ("BOT_TOKEN", settings.bot_token, "needed to send cards"),
        ("ADMIN_CHAT_ID", settings.admin_chat_id, "your editors' group; send /help to the bot there"),
    ]:
        r.line(OK if value else BAD, f"{name} {'set' if value else 'missing: ' + why}")
    tg = settings.tg_api_id and settings.tg_api_hash and settings.tg_string_session
    r.line(OK if tg else WARN, "Telegram reading account " + ("set" if tg else
           "not set: the 7 channels will be skipped (see README, tg-login)"))


def describe_key(key: str) -> str:
    """Safe description of the key: never prints it, only what helps spot copy/paste mistakes."""
    from .config import ROOT, SHELL_HAD_ANTHROPIC_KEY
    env_file = ROOT / ".env"
    in_file = env_file.exists() and "ANTHROPIC_API_KEY=" in env_file.read_text(encoding="utf-8", errors="ignore")
    source = ".env" if in_file else ("the terminal environment" if SHELL_HAD_ANTHROPIC_KEY else "unknown")
    notes = []
    if key != key.strip():
        notes.append("has spaces around it")
    k = key.strip()
    if k[:1] in "'\"" or k[-1:] in "'\"":
        notes.append("has quotes around it")
    clean = k.strip("'\"")
    if not clean.startswith("sk-ant-"):
        notes.append("doesn't start with sk-ant-")
    return (f"key from {source}, {len(clean)} characters, ends in ...{clean[-4:]}"
            + (f" ({', '.join(notes)})" if notes else ""))


def check_claude(r: Report) -> None:
    print("\n2. Claude")
    if not settings.anthropic_api_key:
        r.line(BAD, "skipped: no API key")
        return
    print(f"  ({describe_key(settings.anthropic_api_key)})")
    from .config import anthropic_client
    client = anthropic_client()
    models = [settings.model_fast] + ([settings.model_writer] if settings.write_on_accept else [])
    from .llm import LLM
    llm = LLM(DB(":memory:"), client=client)
    probe = {"name": "say", "description": "Say ok.", "input_schema": {
        "type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}
    for model in models:
        try:   # the same kind of call the agent makes (structured answer through a tool)
            llm._call("check", model, "Call the tool with text 'ok'.", "Go.", probe, max_tokens=50)
            r.line(OK, f"{model} answers")
        except Exception as e:
            hint = ""
            if "workspace" in str(e):
                hint = ("\n      This key belongs to the whole account. Either create a key inside a workspace "
                        "(Console > Workspaces > Default > API keys), or add ANTHROPIC_WORKSPACE_ID=wrkspc_... to .env.")
            elif "401" in str(e):
                hint = ("\n      The key is wrong or was deleted. Compare the last 4 characters above with the key "
                        "on console.anthropic.com > API Keys; if they differ, paste the right one into .env.")
            r.line(BAD, f"{model}: {_short(e)}{hint}")


def check_bot(r: Report) -> None:
    print("\n3. Telegram bot")
    if not settings.bot_token:
        r.line(BAD, "skipped: no BOT_TOKEN")
        return
    from aiogram import Bot

    async def run():
        bot = Bot(settings.bot_token)
        try:
            me = await bot.get_me()
            r.line(OK, f"bot @{me.username} is alive")
            if settings.admin_chat_id:
                chat = await bot.get_chat(settings.admin_chat_id)
                member = await bot.get_chat_member(settings.admin_chat_id, me.id)
                admin = member.status in ("administrator", "creator")
                r.line(OK if admin else WARN, f"in group '{chat.title}'" +
                       ("" if admin else ": not an admin, so /panel can't pin and the bottom buttons may not work"))
        except Exception as e:
            r.line(BAD, _short(e))
        finally:
            await bot.session.close()

    asyncio.run(run())


def check_feeds(r: Report, config: dict) -> list:
    print("\n4. Websites (RSS feeds)")
    from .collectors import collect_rss
    fresh = []
    for src in config.get("sources", []):
        if src.get("type") != "rss" or not src.get("enabled", True):
            continue
        try:
            items = collect_rss({**src, "role": "source"}, config.get("skip_title_keywords", []))
            mark = OK if items else WARN
            r.line(mark, f"{src['name']}: {len(items)} listings in the last {src.get('max_age_days', 14)} days")
            if src.get("role") != "competitor":
                fresh.extend(items)
        except Exception as e:
            r.line(BAD, f"{src['name']}: {_short(e)}")
    return fresh


def check_telegram(r: Report, config: dict) -> None:
    print("\n5. Telegram channels")
    if not (settings.tg_api_id and settings.tg_api_hash and settings.tg_string_session):
        r.line(WARN, "skipped: reading account not set up")
        return
    from .collectors import tg_client

    async def run():
        async with tg_client() as client:
            me = await client.get_me()
            r.line(OK, f"reading as {me.first_name}")
            for src in config.get("sources", []):
                if src.get("type") != "telegram" or not src.get("enabled", True):
                    continue
                for ch in src.get("channels", []):
                    try:
                        msgs = [m async for m in client.iter_messages(ch, limit=5)]
                        last = msgs[0].date.strftime("%d %b") if msgs else "never"
                        r.line(OK if msgs else WARN, f"{ch} ({src.get('role', 'source')}): readable, last post {last}")
                    except Exception as e:
                        r.line(BAD, f"{ch}: {_short(e)}")

    try:
        asyncio.run(run())
    except Exception as e:
        r.line(BAD, _short(e))


def check_history(r: Report, db: DB) -> list:
    print("\n6. Your channel history")
    rows = db.history_rows()
    r.line(OK if rows else BAD, f"{len(rows)} past programmes loaded" if rows else
           "empty: run `python -m edugrants_agent import-history messages.html`")
    r.line(OK if settings.profile_file.exists() else BAD,
           "vibe profile " + ("present" if settings.profile_file.exists() else "missing (made by import-history)"))
    return rows


def check_end_to_end(r: Report, config: dict, history_rows: list, fresh: list, n: int) -> None:
    print(f"\n7. End to end: {n} real listings through the whole pipeline (nothing is posted)")
    if not settings.anthropic_api_key or not fresh:
        r.line(BAD, "skipped: needs the API key and at least one working feed")
        return
    from .llm import LLM
    from .pipeline import Pipeline
    from .render import finder_card

    test = DB(":memory:")
    test.replace_history([dict(h) for h in history_rows])
    fresh = sorted(fresh, key=lambda c: c.published_at or "", reverse=True)[:n]
    p = Pipeline(test, llm=LLM(test), config=config, options=load_yaml(settings.options_file))
    p.ingest(fresh)
    try:
        p.triage()
        for item in test.by_status("triaged"):
            p.research(item)
    except Exception:
        r.line(BAD, "pipeline crashed:\n" + traceback.format_exc()[-800:])
        return
    for item in test.conn.execute("SELECT * FROM items ORDER BY id"):
        title = item["title"][:70]
        if item["status"] in ("new", "triaged"):
            r.line(BAD, f"not processed: {title}\n      (the Claude call failed, see section 2)")
        elif item["status"] == "extracted":
            r.line(OK, f"CARD: {title}")
            card, _ = finder_card(test, item)
            print("      " + re.sub(r"<[^>]+>", "", card).replace("\n", "\n      "))
        elif item["status"] == "error":
            r.line(BAD, f"{title}\n      error: {item['reason']}")
        else:
            r.line(OK, f"{item['status']}: {title}\n      why: {item['reason'] or item['fit_reason'] or ''}")
    cost = test.stats(days=1)["cost_usd"]
    print(f"\n  Claude cost of this test: ~${cost}")


# --------------------------------------------------------------------------- entry
VERSION = "2026-10-03r"


def run_check(n: int = 5) -> int:
    import logging
    for noisy in ("httpx", "httpx2", "httpcore", "telethon", "aiogram", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    print(f"EduGrants Finder self-check (version {VERSION})")
    r = Report()
    config = load_yaml(settings.sources_file)
    db = DB(settings.db_path)
    check_settings(r)
    check_claude(r)
    check_bot(r)
    fresh = check_feeds(r, config)
    check_telegram(r, config)
    rows = check_history(r, db)
    check_end_to_end(r, config, rows, fresh, n)
    print("\n" + ("=" * 50))
    if r.fails:
        print(f"{BAD} {r.fails} problem(s) above. Fix those, then run check again.")
    elif r.warns:
        print(f"{OK} Works. {r.warns} warning(s) above are optional.")
    else:
        print(f"{OK} Everything works. Start it with: python -m edugrants_agent bot")
    return 1 if r.fails else 0
