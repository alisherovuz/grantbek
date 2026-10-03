"""The team panel's data: Home (agent cards, your inbox, the week, the timeline), Boshqaruv (settings),
Postlar (Mirzo) and Muloqot (GrantBek). The page itself is dashboard.html."""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta

from aiohttp import web

from . import controls
from .agents import AGENTS, BOTS, PROBLEMS, USERNAMES, has_own_bot
from .config import settings
from .dashboard import BOT_KEY, DB_KEY, TAKEN, _authorized, _tz_offset, local_time

ROLE_UZ = {"manager": "Boshliq: ishni taqsimlaydi, hisobot beradi", "finder": "Grantlarni topadi va saralaydi",
           "writer": "Postlar va dushanba ro'yxati", "community": "Shaxsiy xabarlar va izohlarga javob"}


def _plain(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html or "")


def _count_today(db, table: str, col: str, where: str = "1=1", *args) -> int:
    """Rows in `table` whose `col` (UTC) falls on today in Tashkent time."""
    tz = _tz_offset()
    return db.conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where} AND date({col}, ?) = date('now', ?)",
                           (*args, tz, tz)).fetchone()[0]


def agent_cards(db) -> list[dict]:
    from .bot import STATE
    last_search = db.get_meta("last_search") or {}
    reported = _count_today(db, "tasks", "created_at", "type='daily_report' AND status='done'")
    work = {
        "finder": [f"Bugun {_count_today(db, 'tasks', 'created_at', 'type=?', 'search')} marta qidirdi",
                   f"oxirgisida {last_search.get('seen', 0)} e'lon, {last_search.get('waiting', 0)} ta mos"],
        "writer": [f"Bugun {_count_today(db, 'tasks', 'updated_at', 'type=? AND status=?', 'write_post', 'done')} ta post yozdi",
                   f"{_count_today(db, 'items', 'updated_at', 'status=?', 'published')} ta chop etildi"],
        "community": [f"Bugun {_count_today(db, 'community_qa', 'at')} ta javob",
                      f"{db.conn.execute('SELECT COUNT(*) FROM community_qa WHERE escalated=1 AND handled=0').fetchone()[0]}"
                      " ta murojaat sizni kutmoqda"],
        "manager": [f"Bugun {_count_today(db, 'tasks', 'created_at', 'type=?', 'order')} ta buyruq bajardi",
                    "hisobot " + ("yuborildi" if reported else f"soat {settings.report_at or '—'} da")],
    }
    cutoff = (datetime.utcnow() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
    paused = controls.paused_agents(db)
    lock = STATE.get("lock")
    cards = []
    for key, a in AGENTS.items():
        last = db.conn.execute("SELECT * FROM events WHERE agent=? ORDER BY id DESC LIMIT 1", (key,)).fetchone()
        err = db.conn.execute("SELECT COUNT(*) FROM events WHERE agent=? AND kind='error' AND at >= ?", (key, cutoff)).fetchone()[0]
        busy = db.conn.execute("SELECT COUNT(*) FROM tasks WHERE agent=? AND status='working' AND updated_at >= ?",
                               (key, (datetime.utcnow() - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S"))).fetchone()[0]
        if key == "finder" and lock is not None and lock.locked():
            busy = 1
        if key in PROBLEMS:
            state = "error"
        elif key == "community" and key not in BOTS:
            state = "off"
        elif key in paused:
            state = "paused"
        elif controls.over_budget(db, key):
            state = "budget"
        elif err:
            state = "error"
        elif busy:
            state = "working"
        else:
            state = "idle"
        cards.append({
            "key": key, "name": a["name"], "emoji": a["emoji"], "role": ROLE_UZ.get(key, ""), "state": state,
            "bot": "own" if key in BOTS and has_own_bot(key) else ("none" if key == "community" else "shared"),
            "today": work.get(key, []), "spent": round(controls.spent_today(db, key), 3),
            "budget": float(getattr(settings, f"budget_{key}", 0) or 0),
            "last": {"text": last["text"][:160], "at": local_time(last["at"]), "kind": last["kind"]} if last else None,
            "paused": key in paused,
            "problem": PROBLEMS.get(key), "username": USERNAMES.get(key),
        })
    return cards


def inbox(db) -> dict:
    posts = [{"id": r["id"], "title": r["title"], "text": r["post_text"], "preview": _plain(r["post_text"])[:500],
              "auto": r["reason"] == "auto: Toshmat aka", "at": local_time(r["updated_at"])}
             for r in db.conn.execute("SELECT * FROM items WHERE status='in_review' ORDER BY updated_at DESC LIMIT 30")]
    weekly = [{"id": t["id"], "text": json.loads(t["result"] or "{}").get("text", ""),
               "items": json.loads(t["result"] or "{}").get("items", 0), "at": local_time(t["created_at"])}
              for t in db.conn.execute("SELECT * FROM tasks WHERE type='weekly_list' AND status='waiting' ORDER BY id DESC")]
    people = [dict(id=r["id"], who=r["who"] or "?", question=r["question"], answer=r["answer"], link=r["link"],
                   place=r["place"], at=local_time(r["at"]))
              for r in db.conn.execute("SELECT * FROM community_qa WHERE escalated=1 AND handled=0 ORDER BY id DESC LIMIT 30")]
    return {"posts": posts, "weekly": weekly, "people": people,
            "queue": db.conn.execute("SELECT COUNT(*) FROM items WHERE status='extracted'").fetchone()[0],
            "errors": db.conn.execute("SELECT COUNT(*) FROM items WHERE status='error'").fetchone()[0]}


def week(db) -> dict:
    since = "-7 days"
    published = db.conn.execute("SELECT COUNT(*) FROM items WHERE status='published' AND updated_at >= datetime('now', ?)",
                                (since,)).fetchone()[0]
    weekly = db.conn.execute("SELECT COUNT(*) FROM tasks WHERE type='weekly_list' AND status='done' AND "
                             "updated_at >= datetime('now', ?)", (since,)).fetchone()[0]
    found = db.conn.execute("SELECT COUNT(*) FROM items WHERE data_json IS NOT NULL AND discovered_at >= datetime('now', ?)",
                            (since,)).fetchone()[0]
    answered = db.conn.execute("SELECT COUNT(*) FROM community_qa WHERE at >= datetime('now', ?)", (since,)).fetchone()[0]
    cost = db.conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM llm_usage WHERE at >= datetime('now', ?)",
                           (since,)).fetchone()[0]
    posts = published + weekly
    return {"posts": posts, "found": found, "answered": answered, "cost": round(cost, 2),
            "per_post": round(cost / posts, 2) if posts else None}


def timeline(db, n: int = 30) -> list[dict]:
    return [{"at": local_time(e["at"]), "who": f"{AGENTS.get(e['agent'], {}).get('emoji', '')} "
             f"{AGENTS.get(e['agent'], {}).get('name', e['agent'])}", "kind": e["kind"], "text": e["text"][:300]}
            for e in db.events(n)]


# --------------------------------------------------------------------------- Postlar (Mirzo)
def posts_page(db) -> dict:
    published = [{"title": r["title"], "at": local_time(r["updated_at"]),
                  "url": f"https://t.me/{settings.channel_handle.lstrip('@')}/{r['channel_message_id']}"}
                 for r in db.conn.execute("SELECT * FROM items WHERE status='published' AND channel_message_id IS NOT NULL "
                                          "ORDER BY updated_at DESC LIMIT 40")]
    lists = [{"id": t["id"], "status": t["status"], "at": local_time(t["created_at"]),
              "items": json.loads(t["result"] or "{}").get("items", 0), "url": json.loads(t["result"] or "{}").get("url")}
             for t in db.conn.execute("SELECT * FROM tasks WHERE type='weekly_list' ORDER BY id DESC LIMIT 12")]
    return {"drafts": inbox(db)["posts"], "published": published, "lists": lists}


# --------------------------------------------------------------------------- Muloqot (GrantBek)
def community_page(db) -> dict:
    qa = [{"id": r["id"], "at": local_time(r["at"]), "place": r["place"], "who": r["who"] or "", "question": r["question"],
           "answer": r["answer"], "topic": r["topic"] or "", "escalated": bool(r["escalated"]),
           "handled": bool(r["handled"]), "correction": r["correction"] or "", "link": r["link"]}
          for r in db.conn.execute("SELECT * FROM community_qa ORDER BY id DESC LIMIT 150")]
    brief = db.get_meta("community_brief") or {}
    topics: dict[str, int] = {}
    for r in db.recent_qa(7, 500):
        t = (r["topic"] or "boshqa").strip().lower()
        topics[t] = topics.get(t, 0) + 1
    return {"qa": qa, "brief": brief.get("text", ""), "brief_at": brief.get("at"), "connected": "community" in BOTS,
            "topics": sorted(topics.items(), key=lambda kv: -kv[1])[:10],
            "open_posts": len(db.open_channel_posts(datetime.utcnow().strftime("%Y-%m-%d"), 1000))}


# --------------------------------------------------------------------------- endpoints
def _guard(handler):
    async def wrapped(request: web.Request):
        if not _authorized(request):
            return web.json_response({"error": "forbidden"}, status=403)
        try:
            return await handler(request)
        except (ValueError, KeyError) as e:
            return web.json_response({"error": str(e)}, status=400)
    return wrapped


@_guard
async def api_home(request):
    db = request.app[DB_KEY]()
    return web.json_response({"agents": agent_cards(db), "inbox": inbox(db), "week": week(db), "timeline": timeline(db)})


@_guard
async def api_settings(request):
    db = request.app[DB_KEY]()
    if request.method == "POST":
        values = controls.save(db, await request.json())
        from .bot import STATE, plan_jobs
        if STATE.get("scheduler"):
            plan_jobs()
        from .agents import note
        note("manager", "info", "Sozlamalar dashboarddan o'zgartirildi")
        return web.json_response({"ok": True, "message": "Saqlandi, darhol ishlaydi", "values": values})
    focus = db.get_meta("focus") or {}
    return web.json_response({"values": controls.current(), "fields": {k: {"type": v[0], "group": v[1], "label": v[2],
                                                                          "help": v[3]} for k, v in controls.EDITABLE.items()},
                              "focus": focus, "agents": {k: {"name": a["name"], "emoji": a["emoji"]} for k, a in AGENTS.items()}})


@_guard
async def api_agent(request):
    db = request.app[DB_KEY]()
    body = await request.json()
    agent = body["agent"]
    if agent not in AGENTS:
        raise ValueError("noma'lum agent")
    controls.set_paused(db, agent, bool(body.get("paused")))
    from .agents import note
    name = AGENTS[agent]["name"]
    note("manager", "info", f"{name} " + ("to'xtatildi" if body.get("paused") else "yana ishga tushirildi"))
    return web.json_response({"ok": True, "message": f"{name} " + ("to'xtatildi" if body.get("paused") else "ishga tushdi")})


@_guard
async def api_focus(request):
    db = request.app[DB_KEY]()
    body = await request.json()
    text = (body.get("text") or "").strip()
    days = int(body.get("days") or 7)
    db.set_meta("focus", {"text": text, "until": (datetime.utcnow() + timedelta(days=days)).strftime("%Y-%m-%d")} if text else {})
    from .agents import note
    note("manager", "task", f"Eshmatga fokus: {text} ({days} kun)" if text else "Fokus olib tashlandi")
    return web.json_response({"ok": True, "message": "Fokus saqlandi" if text else "Fokus olib tashlandi"})


@_guard
async def api_post(request):
    """Mirzo's posts from the web: publish, save an edit, or reject."""
    db = request.app[DB_KEY]()
    body = await request.json()
    from .bot import PublishError, publish_item, publish_weekly
    bot = request.app.get(BOT_KEY)
    kind, action, pid = body.get("kind", "post"), body["action"], int(body["id"])
    try:
        if kind == "weekly":
            t = db.task(pid)
            if action == "save":
                db.update_task(pid, "waiting", {**json.loads(t["result"] or "{}"), "text": body["text"]})
                return web.json_response({"ok": True, "message": "Saqlandi"})
            if action == "cancel":
                db.update_task(pid, "cancelled")
                return web.json_response({"ok": True, "message": "Bekor qilindi"})
            url = await publish_weekly(pid, bot, body.get("text"))
            return web.json_response({"ok": True, "message": "Chop etildi", "url": url})
        if action == "save":
            db.update(pid, post_text=body["text"])
            return web.json_response({"ok": True, "message": "Saqlandi"})
        if action == "reject":
            db.update(pid, status="declined", reason="declined on dashboard")
            return web.json_response({"ok": True, "message": "Rad etildi"})
        if body.get("text"):
            db.update(pid, post_text=body["text"])
        url, note = await publish_item(pid, bot, by="dashboard")
        return web.json_response({"ok": True, "message": "Chop etildi" + note, "url": url})
    except PublishError as e:
        return web.json_response({"error": str(e)}, status=409)


@_guard
async def api_posts(request):
    return web.json_response(posts_page(request.app[DB_KEY]()))


@_guard
async def api_community(request):
    db = request.app[DB_KEY]()
    if request.method == "POST":
        body = await request.json()
        if body["action"] == "handled":
            db.qa_update(int(body["id"]), handled=1)
            return web.json_response({"ok": True, "message": "Hal qilindi"})
        if body["action"] == "correct":
            db.qa_update(int(body["id"]), correction=(body.get("text") or "").strip())
            return web.json_response({"ok": True, "message": "Saqlandi: GrantBek bundan keyin shunday javob beradi"})
        if body["action"] == "refresh":
            from .community import refresh_base
            asyncio.create_task(refresh_base())
            return web.json_response({"ok": True, "message": "GrantBek bilimini yangilayapti, bir daqiqa"})
        raise ValueError("noma'lum amal")
    return web.json_response(community_page(db))


def add_routes(app: web.Application) -> None:
    app.router.add_get("/api/home", api_home)
    app.router.add_get("/api/settings", api_settings)
    app.router.add_post("/api/settings", api_settings)
    app.router.add_post("/api/agent", api_agent)
    app.router.add_post("/api/focus", api_focus)
    app.router.add_get("/api/posts", api_posts)
    app.router.add_post("/api/post", api_post)
    app.router.add_get("/api/community", api_community)
    app.router.add_post("/api/community", api_community)
