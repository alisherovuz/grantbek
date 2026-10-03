"""Toshmat aka's orders: the owner writes in plain words, Toshmat aka decides what to do and does it
with real tools, then answers.

Before, he picked one action from a short list and wrote his reply at the same moment, so he could say
"Mirzoga aytaman" when there was no way to give Mirzo anything. Now every hand-off is a tool call:
the finder really checks the page, the writer really writes the post and the listing, the channel
really gets the post. His answer is written last, from the tools' results, so it only reports what
happened.

Telegram doesn't let bots read each other, so nothing here goes through the group: the tools call the
other agents' code directly, and the group shows you what each of them did.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from datetime import datetime, timedelta
from html import escape as _escape

from .agents import AGENTS, PROBLEMS, note, plain, say
from .config import settings
from .manager import _db, daily_report, name, status_text, weekly_list

log = logging.getLogger(__name__)

MAX_STEPS = 8          # tool rounds per order; a normal order needs one to three
HISTORY_EVENTS = 12    # recent group lines Toshmat aka sees, so "buni" and "u" make sense
RESULT_CHARS = 6000    # per tool result sent back to the model


def escape(text) -> str:
    return _escape(str(text), quote=False)


def manager_model() -> str:
    return settings.model_manager or settings.model_writer


# --------------------------------------------------------------------------- the tools he can use
TOOLS = [
    {"name": "check_opportunity",
     "description": "The finder checks ONE opportunity the owner gave (a link, a pasted post, or both): reads the "
                    "organiser's page, extracts the facts, checks the channel's rules (Uzbeks eligible, ages, fees, "
                    "funding, deadline) and posts the card in the group. Returns the facts, the item number and "
                    "whether it breaks a rule. Use for 'tekshir', 'qarab chiq', 'bu bizga mosmi'.",
     "input_schema": {"type": "object", "properties": {
         "url": {"type": "string", "description": "The opportunity's link, if there is one."},
         "text": {"type": "string", "description": "The pasted post or description, exactly as the owner gave it, "
                                                   "links included."},
         "title": {"type": "string", "description": "Short name of the opportunity, if known."}}}},
    {"name": "write_post",
     "description": "The writer writes the @EduGrandsUz Telegram post AND the edugrants.uz listing (Create "
                    "Extracurriculars form) for an opportunity and sends the draft to the group with publish "
                    "buttons. Give item_id if it was already checked; otherwise give url and/or text and it is "
                    "checked first. To rewrite an existing draft, give its item_id and the changes in notes. "
                    "Returns the post and the listing.",
     "input_schema": {"type": "object", "properties": {
         "item_id": {"type": "integer"},
         "url": {"type": "string"},
         "text": {"type": "string"},
         "notes": {"type": "string", "description": "The owner's corrections or extra facts, e.g. 'Yosh toifasi: "
                                                    "16-35', 'funding is partial, only 10 seats fully funded'. "
                                                    "They override the extracted facts."}}}},
    {"name": "show_platform_listing",
     "description": "The writer posts the edugrants.uz listing fields (step 1 Basic, step 2 Body) of a written "
                    "item in the group, ready to copy into the admin form. Use when the owner wants the website "
                    "data ('vebsaytga data', 'platforma uchun').",
     "input_schema": {"type": "object", "properties": {"item_id": {"type": "integer"}}, "required": ["item_id"]}},
    {"name": "publish_post",
     "description": "Publishes a ready post (status in_review) to the @EduGrandsUz channel and sends its listing "
                    "to edugrants.uz. Public and cannot be undone: ONLY when the owner clearly asks to publish "
                    "this post in this message ('chop et', 'kanalga joyla').",
     "input_schema": {"type": "object", "properties": {"item_id": {"type": "integer"}}, "required": ["item_id"]}},
    {"name": "search_now",
     "description": "The finder starts a full search of all sources now (2-5 minutes, runs in the background, "
                    "results arrive in the group). Only for 'yangi grant qidir', not for checking one given item.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "list_finds",
     "description": "Finds waiting in the queue for the owner's decision, best first, with item numbers.",
     "input_schema": {"type": "object", "properties": {"limit": {"type": "integer"}}}},
    {"name": "find_items",
     "description": "Looks up items by words of their title (any status: waiting, written, published, "
                    "rejected...). Use to get the item number of something the owner names.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "skip_find",
     "description": "Marks a find as not wanted, with a short reason, so it is never suggested again.",
     "input_schema": {"type": "object", "properties": {"item_id": {"type": "integer"}, "reason": {"type": "string"}},
                      "required": ["item_id"]}},
    {"name": "weekly_list",
     "description": "The writer prepares the Monday list of programmes whose deadline ends this week and sends "
                    "it to the group with publish buttons.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "daily_report",
     "description": "Your daily report (numbers, costs, problems, suggestions), posted now.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "team_status",
     "description": "Queue size, last search, next search, focus, paused agents, today's AI spend per agent, "
                    "broken bots.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "set_focus",
     "description": "Tells the finder what to look for more of, for some days (it scores those higher).",
     "input_schema": {"type": "object", "properties": {
         "text": {"type": "string", "description": "In English, one sentence."},
         "days": {"type": "integer", "description": "Default 7."}}, "required": ["text"]}},
    {"name": "clear_focus", "description": "Removes the finder's focus.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "pause_agent",
     "description": "Pauses or resumes one agent's scheduled work: finder (daily search), writer (Monday list, "
                    "auto posts), community (GrantBek), manager (daily report).",
     "input_schema": {"type": "object", "properties": {
         "agent": {"type": "string", "enum": ["finder", "writer", "community", "manager"]},
         "paused": {"type": "boolean"}}, "required": ["agent", "paused"]}},
    {"name": "change_setting",
     "description": "Changes a team setting (the dashboard's Boshqaruv tab). Keys: run_at (HH:MM), check_ages, "
                    "age_min, age_max, min_days_left, min_show_fit, min_fit_score, research_min_per_run, "
                    "auto_write_per_day, auto_write_min_fit, weekly_at ('mon 08:30'), community_refresh_at, "
                    "report_at, budget_finder, budget_writer, budget_community, budget_manager (USD a day, 0 = none).",
     "input_schema": {"type": "object", "properties": {"key": {"type": "string"}, "value": {"type": "string"}},
                      "required": ["key", "value"]}},
    {"name": "retry_errors",
     "description": "Gives items that failed (page not readable, post not written) another try.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "read_page",
     "description": "You read a web page yourself, to answer a question about it. Doesn't add anything to the "
                    "queue (use check_opportunity for that).",
     "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
]


def system_prompt(db) -> str:
    return f"""You are {name('manager')}, the boss of the EduGrants AI team, in a private Telegram group with the \
channel's owner. EduGrants (@EduGrandsUz, edugrants.uz) posts scholarships, competitions, olympiads, forums and \
exchange programmes for young people in Uzbekistan (most subscribers are 12-20). The owner writes to you in \
plain words, usually Uzbek. Today is {datetime.now().strftime('%Y-%m-%d')}.

Your team, by the names the group shows:
- {name('finder')} (finder): checks one given opportunity (check_opportunity) and searches all sources \
(search_now, every day by himself).
- {name('writer')} (writer): writes the channel post and the edugrants.uz listing (write_post), posts the \
listing fields (show_platform_listing), prepares the Monday deadline list (weekly_list).
- {name('community')} (GrantBek): answers subscribers' DMs and comments by himself; he takes no orders.
- You: publish_post, daily_report, team_status, set_focus, pause_agent, change_setting, retry_errors, read_page, \
list_finds, find_items, skip_find.

How you work:
- The team does something ONLY when you call its tool. Never write that you passed something to someone, asked \
someone, or that something is done or ready, unless a tool result in this conversation shows it. If no tool \
can do what the owner wants, say so plainly.
- A pasted post, a link, or the message the owner replied to is the opportunity he means. Pass its text with \
the links to the tools.
- "tekshir" -> check_opportunity. "post yoz" -> write_post. "vebsaytga / platformaga data yoz" -> write_post, \
then show_platform_listing. "chop et" -> publish_post, only if he clearly asks for it now.
- Facts the owner states or corrects (ages, funding, deadline) go to write_post as notes.
- Use several tools in a row when the order needs it (check, then write, then show the listing).
- The search runs in the background: say it has started and the results will come to the group.

Your final answer: short, in Uzbek (Latin script), warm and a little playful, like a good boss. Say what was \
actually done, with item numbers (#142), and anything that failed or breaks the channel's rules (application \
fee, ages, funding, Uzbek eligibility). Plain text, no markdown, no long lists.

Team status now: {status_text(db).replace(chr(10), '; ')}"""


def conversation_text(db, text: str, replied: str | None) -> str:
    rows = list(reversed(db.events(limit=HISTORY_EVENTS)))
    lines = [f"- {AGENTS.get(r['agent'], {}).get('name', r['agent'])}: {r['text'][:300]}" for r in rows]
    parts = []
    if lines:
        parts.append("Recent lines in the group (oldest first):\n" + "\n".join(lines))
    if replied:
        parts.append("The owner is replying to this message:\n" + replied[:6000])
    parts.append("The owner's message:\n" + text[:6000])
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- helpers
LINK_RE = re.compile(r"https?://[^\s\"'<>)\]]+")


def links_in(text: str | None) -> list[str]:
    seen = []
    for u in LINK_RE.findall(text or ""):
        u = u.rstrip(".,;")
        if u not in seen:
            seen.append(u)
    return seen


def first_opportunity_link(text: str | None) -> str | None:
    """The link a pasted post points to ("Havola"), skipping our own channel and other t.me links."""
    for u in links_in(text):
        if "t.me/" not in u and "edugrants.uz" not in u:
            return u
    return None


def guess_title(text: str | None, url: str | None) -> str:
    for line in plain(text or "").splitlines():
        line = line.strip(" -•⚡️🔗📌➡️")
        if len(line) >= 4:
            return line[:200]
    return url or "Egasi bergan imkoniyat"


def owner_item(db, url: str | None, text: str | None, title: str | None) -> tuple[int, bool]:
    """Puts what the owner handed in on the board. Returns (item id, still to research). An item already
    written or published is returned as it is, so a new check never undoes that work."""
    from .dedupe import canonical_url, normalize_title
    title = (title or "").strip() or guess_title(text, url)
    key = canonical_url(url) if url else "owner:" + hashlib.sha1(plain(text or "").encode()).hexdigest()[:16]
    row = db.conn.execute("SELECT * FROM items WHERE canonical_url=?", (key,)).fetchone()
    if row is not None:
        if row["status"] in ("drafted", "in_review", "publishing", "published"):
            return row["id"], False
        db.update(row["id"], status="triaged", reason=None, fit_score=5, fit_reason="Egasi so'radi")
        return row["id"], True
    item_id = db.insert_item(source="owner", url=url or "", canonical_url=key, title=title,
                             norm_title=normalize_title(title), summary=plain(text or "")[:2000], published_at=None)
    db.update(item_id, status="triaged", fit_score=5, fit_reason="Egasi so'radi")
    return item_id, True


def facts(data: dict) -> dict:
    keys = ("title", "organizer", "host_country", "opportunity_type", "level", "funding", "format", "duration",
            "application_fee", "program_fee", "age_min", "age_max", "deadline_type", "deadline", "status",
            "eligible_countries", "uzbekistan_eligible", "benefits", "registration_url", "verified_from_official",
            "conflicts", "confidence")
    return {k: data.get(k) for k in keys if data.get(k) not in (None, "", [])}


def platform_text(item_id: int, p: dict) -> str:
    """The listing laid out like the admin form, ready to copy field by field."""
    e = escape
    basic = [("Title", p.get("title")), ("Country", p.get("country")), ("Official link", p.get("official_link")),
             ("Registration link", p.get("registration_link")), ("Deadline type", p.get("deadline_type")),
             ("Deadline", p.get("deadline")), ("Opening date", p.get("opening_date")),
             ("Imkoniyat turi", p.get("imkoniyat_turi")), ("Daraja", p.get("daraja")),
             ("Moliyalashtirish", p.get("moliyalashtirish")), ("Format", p.get("format")),
             ("Davomiylik", p.get("davomiylik")), ("Ariza to'lovi", p.get("ariza_tolovi"))]
    body = [("Description", p.get("description")), ("Eligibility", p.get("eligibility")),
            ("Benefits", p.get("benefits")), ("Application Process", p.get("application_process")),
            ("Additional Information", p.get("additional_information"))]
    lines = [f"📋 <b>edugrants.uz uchun ma'lumot</b> · #{item_id}", "", "<b>1-qadam: Basic</b>"]
    lines += [f"<b>{k}</b>: {e(v) if v not in (None, '') else '—'}" for k, v in basic]
    lines += ["", "<b>2-qadam: Body</b>"]
    for k, v in body:
        if v:
            lines += [f"<b>{k}</b>", e(v), ""]
    return "\n".join(lines).strip()


def chunks(text: str, limit: int = 3900) -> list[str]:
    """Telegram allows 4096 characters a message: split on blank lines, never inside a tag."""
    out, cur = [], ""
    for part in text.split("\n\n"):
        if cur and len(cur) + len(part) + 2 > limit:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n\n{part}" if cur else part
        while len(cur) > limit:          # one huge paragraph: cut on a line break
            cut = cur.rfind("\n", 0, limit)
            cut = cut if cut > 0 else limit
            out.append(cur[:cut])
            cur = cur[cut:].lstrip("\n")
    if cur:
        out.append(cur)
    return out


class Order:
    def __init__(self, bot, task_id: int, reply_to: int | None):
        self.bot, self.task_id, self.reply_to = bot, task_id, reply_to
        self.done: list[str] = []      # what the tools did, for the task row and a fallback answer


# --------------------------------------------------------------------------- what each tool does
async def t_check_opportunity(inp: dict, o: Order) -> dict:
    from .bot import STATE, find_keyboard
    from .controls import over_budget
    from .render import render_find
    db = _db()
    text = (inp.get("text") or "").strip() or None
    url = (inp.get("url") or "").strip() or first_opportunity_link(text)
    if not url and not text:
        return {"ok": False, "error": "no link and no text: ask the owner which opportunity he means"}
    if over_budget(db, "finder"):
        return {"ok": False, "error": f"{name('finder')}'s AI budget for today is used up"}
    item_id, research = owner_item(db, url, text, inp.get("title"))
    item = db.get(item_id)
    if not research:
        return {"ok": True, "item_id": item_id, "status": item["status"], "already": True,
                "note": "already written or published; not checked again", "facts": facts(json.loads(item["data_json"] or "{}"))}
    task_id = db.add_task("check", "finder", {"item_id": item_id, "url": url}, created_by="manager", status="working")
    await say("manager", f"{name('finder')}, buni tekshir: <i>{escape(item['title'][:120])}</i>", kind="task",
              task_id=task_id, fallback_bot=o.bot)
    try:
        await asyncio.to_thread(STATE["pipeline"].research_given, item, url, text)
    except Exception as e:
        log.exception("owner check failed")
        db.update(item_id, status="error", reason=f"check: {e}"[:300])
    item = db.get(item_id)
    if not (item["url"] or "").startswith("http") and item["official_url"]:
        db.update(item_id, url=item["official_url"])
        item = db.get(item_id)
    data = json.loads(item["data_json"] or "{}")
    if not data:
        db.update_task(task_id, "failed", {"error": (item["reason"] or "")[:300]})
        await say("finder", f"❌ #{item_id} ni o'qib bo'lmadi: {escape(item['reason'] or 'noma`lum xato')[:200]}",
                  kind="error", task_id=task_id, fallback_bot=o.bot)
        return {"ok": False, "item_id": item_id, "error": item["reason"] or "could not read it"}
    problem = item["reason"] if item["status"] in ("rejected", "duplicate") else None
    card = f"#{item_id} · " + render_find(item, data)
    if problem:
        card += f"\n\n⚠️ Kanal qoidasiga to'g'ri kelmaydi: {escape(problem[:200])}"
    await say("finder", card, kind="done", task_id=task_id, reply_markup=find_keyboard(item_id), fallback_bot=o.bot)
    db.update_task(task_id, "done", {"item_id": item_id, "status": item["status"], "problem": problem})
    o.done.append(f"#{item_id} tekshirildi")
    return {"ok": True, "item_id": item_id, "fits_channel_rules": problem is None, "rule_problem": problem,
            "facts": facts(data)}


async def t_write_post(inp: dict, o: Order) -> dict:
    from .bot import take, write_and_send
    from .controls import over_budget
    db = _db()
    item_id = inp.get("item_id")
    if not item_id:
        checked = await t_check_opportunity(inp, o)
        if not checked.get("ok"):
            return checked
        item_id = checked["item_id"]
    item = db.get(int(item_id))
    if item is None:
        return {"ok": False, "error": f"no item #{item_id}"}
    if not item["data_json"]:
        return {"ok": False, "error": f"#{item_id} has not been checked yet: call check_opportunity first"}
    if item["status"] in ("published", "publishing"):
        return {"ok": False, "error": f"#{item_id} is already published"}
    if over_budget(db, "writer"):
        return {"ok": False, "error": f"{name('writer')}'s AI budget for today is used up"}
    notes = (inp.get("notes") or "").strip() or None
    await say("manager", f"{name('writer')}, «{escape(item['title'][:100])}» uchun post va sayt ma'lumotini yoz"
              + (f". Hisobga ol: <i>{escape(notes[:300])}</i>" if notes else "."), kind="task", fallback_bot=o.bot)
    if item["status"] not in ("accepted", "drafted", "in_review"):
        take(item, 0)
    await write_and_send(o.bot, item["id"], settings.admin_chat_id, by=name("manager"), notes=notes)
    item = db.get(item["id"])
    if item["status"] != "in_review":
        return {"ok": False, "item_id": item["id"], "error": item["reason"] or f"not written (status {item['status']})"}
    o.done.append(f"#{item['id']} uchun post yozildi")
    return {"ok": True, "item_id": item["id"], "status": "in_review: draft with publish buttons is in the group",
            "post": plain(item["post_text"] or ""), "listing": json.loads(item["platform_json"] or "{}")}


async def t_show_platform_listing(inp: dict, o: Order) -> dict:
    db = _db()
    item = db.get(int(inp["item_id"]))
    if item is None:
        return {"ok": False, "error": f"no item #{inp['item_id']}"}
    p = json.loads(item["platform_json"] or "{}")
    if not p:
        return {"ok": False, "error": f"#{item['id']} has no listing yet: call write_post first"}
    for part in chunks(platform_text(item["id"], p)):
        await say("writer", part, kind="done", fallback_bot=o.bot)
    o.done.append(f"#{item['id']} sayt ma'lumoti yuborildi")
    return {"ok": True, "item_id": item["id"], "sent_to_group": True}


async def t_publish_post(inp: dict, o: Order) -> dict:
    from .bot import PublishError, publish_item
    try:
        url, extra = await publish_item(int(inp["item_id"]), o.bot, by=name("manager"))
    except PublishError as e:
        return {"ok": False, "error": str(e)}
    o.done.append(f"#{inp['item_id']} kanalda chop etildi")
    return {"ok": True, "url": url, "platform": "not sent" if extra else "sent"}


async def t_search_now(inp: dict, o: Order) -> dict:
    from .bot import STATE, run_cycle
    if STATE["lock"].locked():
        return {"ok": True, "started": False, "note": "a search is already running"}

    async def go():
        try:
            await run_cycle(o.bot, notify=True, head="🔎 Qidiruv tugadi")
        except Exception:
            log.exception("search ordered by the owner failed")
    asyncio.create_task(go())
    o.done.append("qidiruv boshlandi")
    return {"ok": True, "started": True, "note": "runs 2-5 minutes; the finder posts the result in the group"}


async def t_list_finds(inp: dict, o: Order) -> dict:
    from .bot import ordered_finds
    out = []
    for item, _ in ordered_finds(min(int(inp.get("limit") or 10), 20)):
        d = json.loads(item["data_json"] or "{}")
        out.append({"item_id": item["id"], "title": d.get("title") or item["title"], "deadline": d.get("deadline"),
                    "country": d.get("host_country"), "fit": item["fit_score"]})
    return {"ok": True, "finds": out}


async def t_find_items(inp: dict, o: Order) -> dict:
    q = f"%{(inp.get('query') or '').strip()}%"
    rows = _db().conn.execute("SELECT id, title, status, reason, updated_at FROM items WHERE title LIKE ? "
                              "ORDER BY id DESC LIMIT 10", (q,)).fetchall()
    return {"ok": True, "items": [{"item_id": r["id"], "title": r["title"], "status": r["status"],
                                   "reason": r["reason"], "updated": (r["updated_at"] or "")[:16]} for r in rows]}


async def t_skip_find(inp: dict, o: Order) -> dict:
    db = _db()
    item = db.get(int(inp["item_id"]))
    if item is None:
        return {"ok": False, "error": f"no item #{inp['item_id']}"}
    db.update(item["id"], status="skipped", reason=(inp.get("reason") or "Toshmat aka: kerak emas")[:200])
    o.done.append(f"#{item['id']} o'tkazib yuborildi")
    return {"ok": True, "item_id": item["id"]}


async def t_weekly_list(inp: dict, o: Order) -> dict:
    result = await weekly_list(created_by="owner")
    o.done.append("haftalik ro'yxat: " + result)
    return {"ok": True, "result": {"ready": "sent to the group with publish buttons",
                                   "empty": "no programme ends this week, nothing to post"}.get(result, result)}


async def t_daily_report(inp: dict, o: Order) -> dict:
    text = await daily_report(created_by="owner")
    o.done.append("hisobot yuborildi")
    return {"ok": True, "posted": bool(text)}


async def t_team_status(inp: dict, o: Order) -> dict:
    from .controls import spent_today
    db = _db()
    return {"ok": True, "status": status_text(db),
            "spent_today_usd": {name(a): round(spent_today(db, a), 2) for a in AGENTS},
            "broken_bots": {name(a): p for a, p in PROBLEMS.items()}}


async def t_set_focus(inp: dict, o: Order) -> dict:
    days = int(inp.get("days") or 7)
    _db().set_meta("focus", {"text": inp["text"], "until": (datetime.utcnow() + timedelta(days=days)).strftime("%Y-%m-%d")})
    await say("manager", f"{name('finder')}, {days} kun davomida shunga e'tibor ber: <i>{escape(inp['text'])}</i>",
              kind="task", fallback_bot=o.bot)
    o.done.append("fokus qo'yildi")
    return {"ok": True, "focus": inp["text"], "days": days}


async def t_clear_focus(inp: dict, o: Order) -> dict:
    _db().set_meta("focus", {})
    o.done.append("fokus olib tashlandi")
    return {"ok": True}


async def t_pause_agent(inp: dict, o: Order) -> dict:
    from .controls import set_paused
    agent = inp["agent"]
    if agent not in AGENTS:
        return {"ok": False, "error": f"unknown agent {agent}"}
    set_paused(_db(), agent, bool(inp["paused"]))
    note("manager", "info", f"{name(agent)} " + ("to'xtatildi" if inp["paused"] else "yana ishga tushirildi"), o.task_id)
    o.done.append(f"{name(agent)} " + ("to'xtatildi" if inp["paused"] else "ishga tushdi"))
    return {"ok": True, "agent": name(agent), "paused": bool(inp["paused"])}


async def t_change_setting(inp: dict, o: Order) -> dict:
    from . import controls
    key = inp["key"]
    if key not in controls.EDITABLE:
        return {"ok": False, "error": f"unknown setting {key}"}
    try:
        values = controls.save(_db(), {key: inp["value"]})
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    from .bot import STATE, plan_jobs
    if STATE.get("scheduler"):
        plan_jobs()
    note("manager", "info", f"Sozlama o'zgardi: {key} = {values.get(key)}", o.task_id)
    o.done.append(f"{key} = {values.get(key)}")
    return {"ok": True, "key": key, "value": values.get(key)}


async def t_retry_errors(inp: dict, o: Order) -> dict:
    from .bot import retry_errors
    r = await retry_errors(o.bot)
    o.done.append("xatolar qayta urinildi")
    return {"ok": True, **r}


async def t_read_page(inp: dict, o: Order) -> dict:
    from .fetch import fetch_page
    try:
        page = await asyncio.to_thread(fetch_page, inp["url"])
    except Exception as e:
        return {"ok": False, "error": f"could not open the page: {e}"[:300]}
    return {"ok": True, "url": page.final_url, "text": page.text[:5000],
            "links": [u for _, u in page.links[:20]]}


RUNNERS = {t["name"]: globals()["t_" + t["name"]] for t in TOOLS}


async def run_tool(tool: str, inp: dict, o: Order) -> dict:
    runner = RUNNERS.get(tool)
    if runner is None:
        return {"ok": False, "error": f"no tool {tool}"}
    try:
        return await runner(inp or {}, o)
    except Exception as e:
        log.exception("tool %s failed", tool)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}


# --------------------------------------------------------------------------- the order loop
def block_dict(b) -> dict:
    """A response block as the API wants it back (thinking blocks included, untouched)."""
    dump = getattr(b, "model_dump", None)
    if dump is not None:
        return dump(exclude_none=True)
    if b.type == "tool_use":
        return {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
    return {"type": "text", "text": getattr(b, "text", "")}


async def run_order(text: str, bot, replied: str | None = None, reply_to: int | None = None) -> None:
    from .bot import STATE
    from .controls import over_budget
    db = _db()
    task_id = db.add_task("order", "manager", {"text": plain(text)[:2000]}, created_by="owner", status="working")
    note("manager", "task", f"Buyruq: {plain(text)[:300]}", task_id)
    if over_budget(db, "manager"):
        db.update_task(task_id, "failed", {"error": "budget"})
        await say("manager", "💸 Bugungi AI byudjetim tugadi, ertaga bajaraman yoki dashboarddan oshiring.",
                  kind="error", task_id=task_id, reply_to=reply_to, fallback_bot=bot)
        return
    o = Order(bot, task_id, reply_to)
    llm = STATE["pipeline"].llm
    system = system_prompt(db)
    messages = [{"role": "user", "content": conversation_text(db, text, replied)}]
    reply, tools_used = "", []
    try:
        for _ in range(MAX_STEPS):
            resp = await asyncio.to_thread(llm.converse, "order", manager_model(), system, messages, TOOLS, 4000)
            uses = [b for b in resp.content if b.type == "tool_use"]
            if not uses:
                reply = "".join(getattr(b, "text", "") for b in resp.content if b.type == "text").strip()
                break
            messages.append({"role": "assistant", "content": [block_dict(b) for b in resp.content]})
            results = []
            for u in uses:
                tools_used.append(u.name)
                out = await run_tool(u.name, u.input, o)
                results.append({"type": "tool_result", "tool_use_id": u.id,
                                "content": json.dumps(out, ensure_ascii=False, default=str)[:RESULT_CHARS],
                                **({"is_error": True} if not out.get("ok", True) else {})})
            messages.append({"role": "user", "content": results})
        else:
            reply = "Ish ko'p bo'ldi, shu yerda to'xtadim. Bajarilganlar: " + ("; ".join(o.done) or "hech narsa")
    except Exception as e:
        log.exception("order failed")
        db.update_task(task_id, "failed", {"error": str(e)[:300], "tools": tools_used, "done": o.done})
        await say("manager", "Kechirasiz, buyruqni bajarolmadim: <code>" + escape(str(e))[:200] + "</code>"
                  + (("\nShungacha qilinganlar: " + escape("; ".join(o.done))) if o.done else ""),
                  kind="error", task_id=task_id, reply_to=reply_to, fallback_bot=bot)
        return
    if not reply:
        reply = ("Bajarildi: " + "; ".join(o.done)) if o.done else "Xo'p!"
    await say("manager", escape(reply), task_id=task_id, reply_to=reply_to, fallback_bot=bot)
    db.update_task(task_id, "done", {"tools": tools_used, "done": o.done})
