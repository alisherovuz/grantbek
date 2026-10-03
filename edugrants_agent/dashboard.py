"""A small web dashboard served by the bot itself: which grants the agent checked, and what it did with them.

It lists only grants that pass the main requirements (Uzbeks can apply, deadline not passed, open to
ages 12-20),
grouped by what happened: waiting in the queue, taken, skipped by an editor, or dropped by the agent
(with the reason in Uzbek). Everything the agent cut earlier, from the title alone, appears only as a
number in the funnel at the top.

Open it with /dashboard in the editors' group. The link carries a key derived from BOT_TOKEN, so only
people who get the link from the bot can see it.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
from datetime import date
from pathlib import Path

from aiohttp import web

from .config import settings
from .pipeline import open_to_school_pupils
from .render import TYPE_UZ, flag, uz_date

log = logging.getLogger(__name__)

PAGE = Path(__file__).with_name("dashboard.html")
DB_KEY = web.AppKey("db", object)     # a function returning the DB
BOT_KEY = web.AppKey("bot", object)
MANUAL_REASON = "dashboarddan navbatga qaytarildi"   # bypasses the format and master's checks in the queue

TAKEN = ("accepted", "drafted", "in_review", "published", "declined", "publishing")
LEVEL_UZ = {"high_school": "maktab", "bachelor": "bakalavr", "master": "magistratura", "phd": "PhD",
            "young_professional": "mutaxassis", "any": "hamma"}
FORMAT_UZ = {"online": "Onlayn", "offline": "Oflayn", "hybrid": "Gibrid"}
GAP_UZ = {"no title": "nomi yo'q", "no country": "davlat noma'lum", "online/offline unknown": "onlayn/oflayn noma'lum",
          "no description": "tavsif yo'q", "no benefits": "imtiyozlar yo'q", "no official link": "rasmiy havola yo'q",
          "no deadline": "muddat yo'q"}


def dashboard_key() -> str:
    """Stable secret for the link, derived from the bot token (changing the token changes the link)."""
    return hmac.new((settings.bot_token or "local").encode(), b"edugrants-dashboard", hashlib.sha256).hexdigest()[:24]


def dashboard_url() -> str | None:
    base = os.getenv("DASHBOARD_URL") or (f"https://{os.environ['RAILWAY_PUBLIC_DOMAIN']}"
                                          if os.getenv("RAILWAY_PUBLIC_DOMAIN") else None)
    if not base:
        base = f"http://localhost:{port()}"
    return f"{base.rstrip('/')}/?key={dashboard_key()}"


def port() -> int:
    return int(os.getenv("DASHBOARD_PORT") or os.getenv("PORT") or 8080)


# --------------------------------------------------------------------------- data
def reason_uz(reason: str | None) -> str:
    """The agent's reject reasons, in plain Uzbek."""
    r = (reason or "").strip()
    low = r.lower()
    if low.startswith("post format:"):
        gaps = [GAP_UZ.get(g.strip(), g.strip()) for g in r.split(":", 1)[1].split(",")]
        return "Post formatiga mos emas: " + ", ".join(gaps)
    if low.startswith("level:"):
        return "Faqat magistratura/PhD uchun (mashhur nom emas)"
    if low.startswith("application fee"):
        return "Ariza to'lovi bor"
    if low.startswith("deadline too close"):
        m = re.search(r"\((-?\d+) days", r)
        return "Muddat juda yaqin" + (f" ({m.group(1)} kun qolgan edi)" if m else "")
    if low.startswith("participant pays"):
        return "Ishtirok pullik"
    if low.startswith("ages"):
        return "Yosh toifasi mos emas"
    if low.startswith("low confidence"):
        return "Ma'lumot ishonchsiz, rasmiy sahifa topilmadi"
    if low.startswith("not a specific opportunity"):
        return "Aniq bir dastur emas (maqola yoki ro'yxat)"
    if low.startswith("applications closed"):
        return "Qabul yopilgan"
    if low.startswith("no deadline"):
        return "Muddat topilmadi"
    if low.startswith("unreadable deadline"):
        return "Muddatni o'qib bo'lmadi"
    if low.startswith("uzbekistan eligibility"):
        return "O'zbekiston uchun ochiqligi aniq emas"
    if low.startswith("triage:") or low.startswith("low fit"):
        return "Kanal vibe'iga mos emas: " + r.split(":", 1)[-1].strip()
    if low.startswith("write:"):
        return "Post yozishda xato"
    return r or "—"


def reason_group(reason_text: str) -> str:
    return reason_text.split(":")[0].split(" (")[0]


def local_time(utc_text: str | None) -> str:
    """The database stores UTC; editors read Tashkent time."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    try:
        t = datetime.strptime((utc_text or "")[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return t.astimezone(ZoneInfo(settings.timezone)).strftime("%d.%m %H:%M")
    except (ValueError, KeyError):
        return (utc_text or "")[:16]


def days_left(data: dict) -> int | None:
    if data.get("deadline_type") == "rolling" or not data.get("deadline"):
        return None
    try:
        return (date.fromisoformat(data["deadline"][:10]) - date.today()).days
    except ValueError:
        return None


def meets_main_rules(data: dict, let_through: bool = False) -> bool:
    """The three main requirements: Uzbeks can apply (yes, or not ruled out), the deadline hasn't
    passed, and it is open to the audience's ages (12-20 by default). `let_through`: the agent or an
    editor already accepted it (e.g. a famous master's programme), so the age rule doesn't hide it."""
    from .pipeline import fits_audience_age
    if not data or data.get("uzbekistan_eligible") == "no" or data.get("status") == "closed":
        return False
    left = days_left(data)
    if left is not None and left < 0:
        return False
    return let_through or not settings.check_ages or fits_audience_age(data)


def rules_text() -> list[str]:
    """What the agent is enforcing right now (so a wrong Railway variable is visible on the page)."""
    rules = ["O'zbekistonliklar topshira oladi", "Muddati o'tmagan"]
    rules.append(f"{settings.age_min}–{settings.age_max} yoshlilar uchun ochiq" if settings.check_ages
                 else "⚠️ Yosh tekshirilmayapti (CHECK_AGES=false)")
    rules.append("Ariza to'lovi yo'q")
    return rules


def verdict(row) -> str:
    s = row["status"]
    if s in ("extracted", "shown"):
        return "queue"
    if s in TAKEN or (s == "error" and (row["reason"] or "").startswith("write:")):
        return "taken"
    if s == "skipped":
        return "skipped"
    return "rejected"


def build(db, days: int = 14) -> dict:
    since = f"-{int(days)} days"
    rows = db.conn.execute("SELECT * FROM items WHERE discovered_at >= datetime('now', ?) ORDER BY id DESC",
                           (since,)).fetchall()
    funnel = {"found": len(rows), "duplicate": 0, "vibe_cut": 0, "waiting_check": 0, "checked": 0,
              "two_rules": 0, "shown_to_editors": 0}
    items = []
    for r in rows:
        if r["status"] == "duplicate":
            funnel["duplicate"] += 1
            continue
        if not r["data_json"]:
            if r["status"] == "rejected":
                funnel["vibe_cut"] += 1
            elif r["status"] in ("new", "triaged", "error"):
                funnel["waiting_check"] += 1
            continue
        funnel["checked"] += 1
        data = json.loads(r["data_json"] or "{}")
        v = verdict(r)
        if not meets_main_rules(data, let_through=v != "rejected"):
            continue
        funnel["two_rules"] += 1
        if v != "rejected":
            funnel["shown_to_editors"] += 1
        left = days_left(data)
        levels = [LEVEL_UZ.get(x, x) for x in data.get("level") or []]
        lo, hi = data.get("age_min"), data.get("age_max")
        ages = f"{lo}–{hi} yosh" if lo and hi else f"{lo}+ yosh" if lo else f"{hi} yoshgacha" if hi else ""
        items.append({
            "id": r["id"],
            "title": data.get("title") or r["title"],
            "org": data.get("organizer") or "",
            "where": f"{flag(data.get('host_country_iso2'))} {data.get('host_country') or ''}".strip(),
            "type": TYPE_UZ.get(data.get("opportunity_type"), "Imkoniyat"),
            "format": FORMAT_UZ.get(data.get("format"), ""),
            "levels": levels,
            "ages": ages,
            "school": open_to_school_pupils(data),
            "deadline": ("Doimiy qabul" if data.get("deadline_type") == "rolling"
                         else uz_date(data.get("deadline")) or "noma'lum"),
            "days_left": left,
            "uz": data.get("uzbekistan_eligible"),
            "fee": data.get("application_fee"),
            "funding": data.get("funding"),
            "fit": r["fit_score"],
            "fit_reason": r["fit_reason"] or "",
            "summary": (data.get("summary") or "")[:300],
            "verdict": v,
            "status": r["status"],
            "why": ("" if v == "queue" else
                    "Muharrir: " + (r["reason"] or "sababsiz") if v == "skipped" else
                    "" if v == "taken" else reason_uz(r["reason"])),
            "official": r["official_url"] or r["url"],
            "found_at": r["url"],
            "source": r["source"],
            "discovered": local_time(r["discovered_at"]),
        })
    reasons: dict[str, int] = {}
    for it in items:
        if it["verdict"] == "rejected":
            key = reason_group(it["why"])
            reasons[key] = reasons.get(key, 0) + 1
    return {"days": days, "funnel": funnel, "items": items, "rules": rules_text(),
            "reasons": sorted(reasons.items(), key=lambda kv: -kv[1]),
            "today": date.today().isoformat()}


# --------------------------------------------------------------------------- web
def _authorized(request: web.Request) -> bool:
    key = request.query.get("key") or request.headers.get("X-Key") or ""
    return hmac.compare_digest(key, dashboard_key())


async def page(request: web.Request) -> web.Response:
    if not _authorized(request):
        return web.Response(status=403, text="Kalit noto'g'ri. Havolani botdan /dashboard bilan oling.")
    return web.Response(text=PAGE.read_text(encoding="utf-8"), content_type="text/html")


async def api_data(request: web.Request) -> web.Response:
    if not _authorized(request):
        return web.json_response({"error": "forbidden"}, status=403)
    days = max(1, min(int(request.query.get("days", "14") or 14), 365))
    return web.json_response(build(request.app[DB_KEY](), days))


async def api_action(request: web.Request) -> web.Response:
    """take: ✅ Olamiz (the bot writes the post and sends it to the group); queue: put it back in the queue."""
    if not _authorized(request):
        return web.json_response({"error": "forbidden"}, status=403)
    body = await request.json()
    action, item_id = body.get("action"), int(body.get("id", 0))
    db = request.app[DB_KEY]()
    item = db.get(item_id)
    if not item or not item["data_json"]:
        return web.json_response({"error": "topilmadi"}, status=404)
    if item["status"] in TAKEN:
        return web.json_response({"error": "allaqachon olingan"}, status=409)
    if action == "queue":
        db.update(item_id, status="extracted", reason=MANUAL_REASON)
        return web.json_response({"ok": True, "message": "Navbatga qaytarildi: guruhdagi 🗂 Topilmalar'da chiqadi"})
    if action == "take":
        from .bot import take, write_and_send
        take(item, 0)
        bot = request.app.get(BOT_KEY)
        if bot is not None and settings.write_on_accept and settings.admin_chat_id:
            asyncio.create_task(write_and_send(bot, item_id, settings.admin_chat_id, by="Dashboard"))
            return web.json_response({"ok": True, "message": "Olindi: post yozilib, guruhga yuboriladi"})
        return web.json_response({"ok": True, "message": "Olindi"})
    return web.json_response({"error": "noma'lum amal"}, status=400)


async def health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


def make_app(db_getter, bot=None) -> web.Application:
    app = web.Application()
    app[DB_KEY] = db_getter
    app[BOT_KEY] = bot
    app.router.add_get("/", page)
    app.router.add_get("/api/data", api_data)
    app.router.add_post("/api/action", api_action)
    app.router.add_get("/health", health)
    return app


async def start(db_getter, bot=None) -> web.AppRunner | None:
    try:
        runner = web.AppRunner(make_app(db_getter, bot), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", port()).start()
        log.info("dashboard on port %d (send /dashboard in the group for the link)", port())
        return runner
    except Exception as e:   # the bot must keep working even if the port is taken
        log.error("dashboard could not start: %s", e)
        return None
