"""Toshmat aka, the manager agent: schedules the team's work, puts it on the task board, checks the
results, reports every evening, and takes orders typed in plain words in the agent group.

Workers: the finder, the writer and GrantBek (community). Every job is a task row (type, agent, status,
result); everything that happens is an event row. Both are what the report and the dashboard read.
Orders typed in the group run through real tools (orders.py): whatever Toshmat aka says he handed to
someone has actually been handed to them.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from html import escape as _escape

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .agents import AGENTS, note, say
from .config import settings

log = logging.getLogger(__name__)


def escape(text) -> str:   # keep Uzbek apostrophes readable (o'z, Xo'p)
    return _escape(str(text), quote=False)

TAKEN = ("accepted", "drafted", "in_review", "published", "declined", "publishing")


def name(agent: str) -> str:
    """Read every time: at startup the names become the bots' names in Telegram."""
    return AGENTS[agent]["name"]


def _db():
    from .bot import db
    return db()


def focus_text(db) -> str:
    """What the boss asked the finder to look for this week (added to the finder's instructions)."""
    f = db.get_meta("focus") or {}
    if f.get("text") and (f.get("until") or "") >= datetime.utcnow().strftime("%Y-%m-%d"):
        return f["text"]
    return ""


def is_paused(db) -> bool:
    from .controls import is_paused as agent_paused
    return agent_paused(db, "finder")


# --------------------------------------------------------------------------- Monday list (Mirzo)
def weekly_markup(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Chop etish", callback_data=f"wk:pub:{task_id}"),
        InlineKeyboardButton(text="✏️ Tahrirlash", callback_data=f"wk:ed:{task_id}"),
        InlineKeyboardButton(text="❌ Bekor", callback_data=f"wk:no:{task_id}"),
    ]])


async def weekly_list(created_by: str = "manager") -> str:
    """Mirzo prepares the Monday post and sends it to the group with publish buttons."""
    from .weekly import build_weekly
    from .controls import is_paused as agent_paused
    db = _db()
    if created_by == "manager" and agent_paused(db, "writer"):
        note("writer", "info", "Haftalik ro'yxat o'tkazib yuborildi: Mirzo to'xtatilgan")
        return "paused"
    task_id = db.add_task("weekly_list", "writer", created_by=created_by, status="working")
    await say("manager", f"{name('writer')}, bu haftaning muddatlar ro'yxatini tayyorla.", kind="task", task_id=task_id)
    text, rows = build_weekly(db, settings.channel_handle)
    if not text:
        db.update_task(task_id, "done", {"items": 0})
        await say("writer", "Bu hafta muddati tugaydigan dastur topilmadi, ro'yxat chiqmaydi.", kind="done", task_id=task_id)
        return "empty"
    db.update_task(task_id, "waiting", {"items": len(rows), "text": text})
    await say("writer", f"Haftalik ro'yxat tayyor ({len(rows)} ta dastur). Chop etaymi?", kind="done", task_id=task_id)
    await say("writer", text, reply_markup=weekly_markup(task_id), log_it=False)
    return "ready"


# --------------------------------------------------------------------------- daily report (Toshmat)
def day_numbers(db) -> dict:
    from .dashboard import _tz_offset, costs, reason_group, reason_uz
    tz = _tz_offset()
    today = "date(%s, ?) = date('now', ?)"
    one = lambda where, *a: db.conn.execute(f"SELECT COUNT(*) FROM items WHERE {where}", a).fetchone()[0]  # noqa: E731
    found = one(today % "discovered_at", tz, tz)
    n = {
        "found": found,
        "duplicates": one(today % "discovered_at" + " AND status='duplicate'", tz, tz),
        "vibe_cut": one(today % "discovered_at" + " AND status='rejected' AND data_json IS NULL", tz, tz),
        "checked": one(today % "discovered_at" + " AND data_json IS NOT NULL", tz, tz),
        "waiting_now": one("status='extracted'"),
        "taken_today": one(today % "updated_at" + f" AND status IN ({','.join('?' * len(TAKEN))})", tz, tz, *TAKEN),
        "published_today": one(today % "updated_at" + " AND status='published'", tz, tz),
        "skipped_today": one(today % "updated_at" + " AND status='skipped'", tz, tz),
        "errors_now": one("status='error'"),
        "searches_today": db.conn.execute("SELECT COUNT(*) FROM tasks WHERE type='search' AND date(created_at, ?) = "
                                          "date('now', ?)", (tz, tz)).fetchone()[0],
    }
    c = costs(db, 7)
    n["cost_today"], n["cost_month"] = c["today"], c["month"]
    ev = lambda kind, like: db.conn.execute(  # noqa: E731
        "SELECT COUNT(*) FROM events WHERE agent='community' AND kind=? AND text LIKE ? AND date(at, ?) = date('now', ?)",
        (kind, like, tz, tz)).fetchone()[0]
    n["answered_dm"], n["answered_comments"] = ev("done", "%shaxsiy%"), ev("done", "%izoh%")
    n["handed_to_owner"] = ev("task", "%")
    n["auto_written"] = one(today % "updated_at" + " AND reason='auto: Toshmat aka'", tz, tz)
    reasons: dict[str, int] = {}
    for r in db.conn.execute("SELECT reason FROM items WHERE status='rejected' AND data_json IS NOT NULL AND "
                             + today % "discovered_at", (tz, tz)):
        k = reason_group(reason_uz(r["reason"]))
        reasons[k] = reasons.get(k, 0) + 1
    n["reject_reasons"] = reasons
    n["sources_down"] = [r["source"] for r in db.health()
                         if r["last_error_at"] and (not r["last_ok"] or r["last_error_at"] > r["last_ok"])]
    return n


SUGGEST_TOOL = {
    "name": "suggestions",
    "description": "2-3 short, concrete suggestions in Uzbek for the channel owner.",
    "input_schema": {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "string"}, "maxItems": 3}},
                     "required": ["items"]},
}


def suggestions(n: dict) -> list[str]:
    """Toshmat aka reads the day's numbers and says what to do (one cheap Claude call)."""
    try:
        from .bot import STATE
        llm = STATE["pipeline"].llm
        out = llm._call("report", settings.model_fast,
                        "You are the manager of an AI team that finds grants for the EduGrants Telegram channel "
                        "(Uzbekistan, audience 10-20). Read today's numbers and give 2-3 short, concrete, actionable "
                        "suggestions in Uzbek (Latin script) for the owner: e.g. a source that keeps failing, a rule "
                        "that cuts too much, the queue piling up, cost rising. No generic advice. If all is fine, say "
                        "so in one item.",
                        json.dumps(n, ensure_ascii=False), SUGGEST_TOOL, max_tokens=600)
        return [s for s in out.get("items", []) if s][:3]
    except Exception:
        log.exception("suggestions failed")
        return []


def report_text(n: dict, tips: list[str]) -> str:
    lines = [f"📋 <b>Kunlik hisobot</b> · {datetime.now().strftime('%d.%m')}", "",
             f"{name('finder')}: {n['searches_today']} marta qidirdi, {n['found']} ta e'lon ko'rdi",
             f"  · takror {n['duplicates']} · vibe filtri {n['vibe_cut']} · rasmiy sahifa tekshirildi {n['checked']}",
             f"  · navbatda kutmoqda: {n['waiting_now']}",
             f"{name('writer')}: bugun {n['taken_today']} ta olindi" + (f" (shundan {n['auto_written']} tasini o'zim berdim)"
                                                           if n.get("auto_written") else "") +
             f", {n['published_today']} ta chop etildi, {n['skipped_today']} ta o'tkazib yuborildi",
             f"{AGENTS['community']['name']}: {n['answered_dm']} ta shaxsiy xabar, {n['answered_comments']} ta izohga "
             f"javob berdi" + (f", {n['handed_to_owner']} tasini sizga yubordi" if n['handed_to_owner'] else ""),
             f"💵 AI xarajati: bugun ${n['cost_today']:.2f}, shu oy ${n['cost_month']:.2f}"]
    if n["reject_reasons"]:
        lines.append("Tashlanganlar: " + ", ".join(f"{k} {v}" for k, v in
                                                     sorted(n["reject_reasons"].items(), key=lambda kv: -kv[1])[:4]))
    if n["errors_now"] or n["sources_down"]:
        lines.append(f"⚠️ Xatolar: {n['errors_now']}" + (f" · ishlamayotgan manbalar: {', '.join(n['sources_down'])}"
                                                         if n["sources_down"] else ""))
    if tips:
        lines += ["", "💡 <b>Maslahatlar:</b>"] + [f"- {escape(t)}" for t in tips]
    return "\n".join(lines)


async def daily_report(created_by: str = "manager") -> str:
    db = _db()
    from .controls import is_paused as agent_paused, over_budget
    if created_by == "manager" and agent_paused(db, "manager"):
        note("manager", "info", "Hisobot o'tkazib yuborildi: Toshmat aka to'xtatilgan")
        return ""
    task_id = db.add_task("daily_report", "manager", created_by=created_by, status="working")
    n = await asyncio.to_thread(day_numbers, db)
    tips = [] if over_budget(db, "manager") else await asyncio.to_thread(suggestions, n)
    text = report_text(n, tips)
    db.update_task(task_id, "done", n)
    from .bot import panel_markup
    await say("manager", text, kind="done", task_id=task_id, reply_markup=panel_markup())
    return text


# --------------------------------------------------------------------------- orders in plain words
def status_text(db) -> str:
    from .bot import next_search_at, ordered_finds
    from .controls import paused_agents
    last = db.get_meta("last_search") or {}
    f = focus_text(db)
    paused = [name(a) for a in sorted(paused_agents(db)) if a in AGENTS]
    return "\n".join([
        f"Navbatda: {len(ordered_finds())} ta topilma",
        f"Oxirgi qidiruv: {last.get('at', '—')[:16]}" + (f" · {last.get('seen')} e'lon" if last else ""),
        "Kunlik qidiruv: " + ("⏸ to'xtatilgan" if is_paused(db) else f"keyingisi {next_search_at() or '—'}"),
        f"Fokus: {f}" if f else "Fokus: yo'q",
        "To'xtatilganlar: " + (", ".join(paused) if paused else "yo'q"),
    ])


async def handle_order(text: str, bot, replied: str | None = None, reply_to: int | None = None) -> None:
    """The owner wrote something to Toshmat aka in the group: understand it, do it with real tools, answer."""
    from .orders import run_order
    await run_order(text, bot, replied=replied, reply_to=reply_to)
