"""Telegram admin bot: editors approve, edit or reject drafts with one tap.

Runs the pipeline on a schedule in the same process, so one small server is enough.
"""
from __future__ import annotations

import asyncio
import json
import re
import logging
from datetime import date, datetime, timedelta
from html import escape

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (BotCommand, CallbackQuery, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, LinkPreviewOptions, Message, ReplyKeyboardMarkup)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .agents import AGENTS, BOTS, _DB, bot_for, is_manager_bot, note as log_event, say, setup_bots
from .config import database_is_temporary, settings
from .dashboard import MANUAL_REASON, dashboard_url
from .db import DB
from .pipeline import AGGREGATOR_DOMAINS, Pipeline, final_check, open_to_school_pupils
from .publish import push_to_platform
from .render import finder_card, uz_date

log = logging.getLogger(__name__)
router = Router()


async def _only_manager_bot(message: Message, bot: Bot) -> bool:
    """Several agent bots may sit in the group; only Toshmat aka's bot answers typed messages,
    so nothing gets answered twice. (Buttons always go to the bot that sent them.)"""
    return is_manager_bot(bot)

router.message.filter(_only_manager_bot)

STATE: dict = {"db": None, "pipeline": None, "lock": asyncio.Lock(), "push_lock": asyncio.Lock(), "pending_edits": {},
               "weekly_edits": {}}


def db() -> DB:
    return STATE["db"]


def allowed(user_id: int | None) -> bool:
    return not settings.admin_user_ids or (user_id in settings.admin_user_ids)


def keyboard(item_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Chop etish", callback_data=f"ap:{item_id}"),
         InlineKeyboardButton(text="✏️ Tahrirlash", callback_data=f"ed:{item_id}")],
        [InlineKeyboardButton(text="📋 Platforma", callback_data=f"pf:{item_id}"),
         InlineKeyboardButton(text="❌ Rad etish", callback_data=f"rj:{item_id}")],
    ])


def review_header(item) -> str:
    data = json.loads(item["data_json"] or "{}")
    verified = "✔️ rasmiy sahifada tekshirildi" if data.get("verified_from_official") else "⚠️ faqat agregatordan"
    deadline = "doimiy" if data.get("deadline_type") == "rolling" else (uz_date(data.get("deadline")) or "?")
    links = f'<a href="{escape(item["url"], quote=True)}">e\'lon</a>'
    if item["official_url"]:
        links += f' | <a href="{escape(item["official_url"], quote=True)}">rasmiy sahifa</a>'
    lines = [f"🆕 <b>#{item['id']}</b> · {escape(item['source'])} · muddat: {deadline}",
             f"{verified} · ishonch: {data.get('confidence', '?')}",
             f"Manbalar: {links}"]
    if data.get("uzbekistan_eligible") == "unclear":
        lines.append("❓ O'zbekiston uchun ochiqligi aniq emas: " + escape(data.get("eligible_countries", "")))
    if data.get("conflicts"):
        lines.append("⚠️ Manbalar farqi: " + escape(data["conflicts"][:300]))
    return "\n".join(lines) + "\n━━━━━━━━━━━━━━\n"


async def send_review(bot: Bot, item, reply_to: int | None = None) -> None:
    """Mirzo (the writer) brings a finished post to the group with the publish buttons."""
    msg = await bot_for("writer", bot).send_message(settings.admin_chat_id, review_header(item) + item["post_text"],
                                                    reply_markup=keyboard(item["id"]), reply_to_message_id=reply_to)
    db().update(item["id"], status="in_review", review_message_id=msg.message_id)
    log_event("writer", "done", f"Post tayyor: {item['title'][:80]}")


async def push_drafts(bot: Bot) -> int:
    if not settings.admin_chat_id:
        return 0
    drafts = db().by_status("drafted", limit=settings.max_drafts_per_run)
    for item in drafts:
        try:
            await send_review(bot, item)
        except Exception as e:
            log.error("could not send #%d for review: %s", item["id"], e)
        await asyncio.sleep(1.2)  # stay under Telegram's per-chat rate limit
    return len(drafts)


# ---------------------------------------------------------------- finder cards
def find_keyboard(item_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Olamiz", callback_data=f"ok:{item_id}"),
        InlineKeyboardButton(text="❌ Kerak emas", callback_data=f"no:{item_id}"),
    ]])


SKIP_REASONS = {"fee": "💸 Pullik", "fit": "🎯 Bizga mos emas", "dup": "🔁 Allaqachon bor",
                "late": "⏳ Kech / eski", "age": "👤 Yosh mos emas", "bad": "🚩 Ishonchsiz"}


def _expired(r, passed_only: bool = False) -> bool:
    """A find waiting in the queue whose deadline got too close since it was found
    (or, with passed_only, whose deadline has already passed)."""
    data = json.loads(r["data_json"] or "{}")
    if data.get("deadline_type") == "rolling" or not data.get("deadline"):
        return False
    try:
        return (date.fromisoformat(data["deadline"][:10]) - date.today()).days < (0 if passed_only else settings.min_days_left)
    except ValueError:
        return False


def ordered_finds(limit: int | None = None) -> list[tuple]:
    """The queue of finds not yet shown, best first: not yet posted by other Uzbek channels, then new
    external finds before old programmes, then best fit, then the freshest at the source."""
    cards = []
    for r in db().by_status("extracted"):
        manual = r["reason"] == MANUAL_REASON   # an editor put it back from the dashboard: no automatic checks
        if _expired(r, passed_only=manual):
            db().update(r["id"], status="rejected", reason="deadline too close (waited in the queue)")
            continue
        reason = None if manual else final_check(json.loads(r["data_json"] or "{}"), r["official_url"], r["fit_score"],
                                                 r["history_id"], getattr(STATE["pipeline"], "aggregators", AGGREGATOR_DOMAINS))
        if reason:   # rules got stricter since it was found
            db().update(r["id"], status="rejected", reason=reason)
            continue
        text, taken = finder_card(db(), r)
        school = 0 if open_to_school_pupils(json.loads(r["data_json"] or "{}")) else 1   # most subscribers are 12-20
        cards.append((taken, 1 if r["history_id"] else 0, -(r["fit_score"] or 0), school,
                      -(datetime.strptime(r["published_at"][:19], "%Y-%m-%d %H:%M:%S").timestamp()
                        if r["published_at"] else 0), r["id"], r, text))
    cards.sort(key=lambda c: c[:6])
    return [(c[6], c[7]) for c in cards[:limit]]


# ---------------------------------------------------------------- the browser
# All waiting finds live in ONE message: ⬅️ Oldingisi / Keyingisi ➡️ flip through them, ✅ Olamiz and
# ❌ Kerak emas act on the one on screen and move on to the next. Nothing is stored about the message
# itself: every button carries the id of a find, and the order is rebuilt from the database.
BROWSE_TEXT = "🗂 Topilmalar"
OLD_MORE_TEXT = "➕ Yana 5 ta"   # the bottom button before the browser existed


def browser_view(item_id: int | None = None, index: int = 0, note: str = "") -> tuple[str, InlineKeyboardMarkup]:
    """Text and buttons showing one find: `item_id` if it is still waiting, otherwise the one at `index`."""
    finds = ordered_finds()
    if not finds:
        return ((note + "\n\n" if note else "") + f"📭 Navbatda topilma yo'q. Yangilarini qidirish uchun «{SEARCH_TEXT}».",
                panel_markup())
    ids = [item["id"] for item, _ in finds]
    i = ids.index(item_id) if item_id in ids else min(max(index, 0), len(ids) - 1)
    item, card = finds[i]
    n = len(ids)
    head = (note + "\n\n" if note else "") + f"🗂 <b>Topilma {i + 1} / {n}</b>\n\n"
    nav = [InlineKeyboardButton(text="⬅️ Oldingisi", callback_data=f"b:go:{ids[(i - 1) % n]}"),
           InlineKeyboardButton(text=f"{i + 1}/{n}", callback_data="noop"),
           InlineKeyboardButton(text="Keyingisi ➡️", callback_data=f"b:go:{ids[(i + 1) % n]}")]
    act = [InlineKeyboardButton(text="✅ Olamiz", callback_data=f"b:ok:{item['id']}"),
           InlineKeyboardButton(text="❌ Kerak emas", callback_data=f"b:no:{item['id']}")]
    return head + card, InlineKeyboardMarkup(inline_keyboard=[nav, act] if n > 1 else [act])


def queue_index(item_id: int) -> int:
    ids = [item["id"] for item, _ in ordered_finds()]
    return ids.index(item_id) if item_id in ids else 0


async def show_in(message: Message, item_id: int | None = None, index: int = 0, note: str = "") -> None:
    text, kb = browser_view(item_id, index, note)
    try:
        await message.edit_text(text, reply_markup=kb)
    except Exception as e:   # "message is not modified" when two people press at once
        if "not modified" not in str(e):
            raise


async def open_browser(bot: Bot, chat_id: int, note: str = "") -> None:
    text, kb = browser_view(note=note)
    await bot.send_message(chat_id, text, reply_markup=kb)


@router.callback_query(F.data.startswith("b:go:"))
async def on_browse(cb: CallbackQuery):
    await cb.answer()
    await show_in(cb.message, int(cb.data.split(":")[2]))


@router.callback_query(F.data.startswith("b:ok:"))
async def on_browse_take(cb: CallbackQuery, bot: Bot):
    if not allowed(cb.from_user.id):
        return await cb.answer("Ruxsat yo'q", show_alert=True)
    item_id = int(cb.data.split(":")[2])
    item = db().get(item_id)
    if not item or item["status"] != "extracted":
        await cb.answer("Buni boshqa muharrir allaqachon ko'rib chiqdi")
        return await show_in(cb.message)
    i = queue_index(item_id)
    take(item, cb.from_user.id)
    await cb.answer("✅ Olindi" + (", post yozilmoqda" if settings.write_on_accept else ""))
    await show_in(cb.message, index=i)   # the next find slides into this place
    if settings.write_on_accept:
        await write_and_send(bot, item_id, cb.message.chat.id, by=cb.from_user.first_name)


@router.callback_query(F.data.startswith("b:no:"))
async def on_browse_skip(cb: CallbackQuery):
    if not allowed(cb.from_user.id):
        return await cb.answer("Ruxsat yo'q", show_alert=True)
    item_id = int(cb.data.split(":")[2])
    buttons = [InlineKeyboardButton(text=label, callback_data=f"b:nr:{item_id}:{code}")
               for code, label in SKIP_REASONS.items()]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton(text="↩️ Orqaga", callback_data=f"b:go:{item_id}")])
    await cb.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer("Sababini tanlang, agent bundan o'rganadi")


@router.callback_query(F.data.startswith("b:nr:"))
async def on_browse_skip_reason(cb: CallbackQuery):
    if not allowed(cb.from_user.id):
        return await cb.answer("Ruxsat yo'q", show_alert=True)
    _, _, item_id, code = cb.data.split(":")
    item_id = int(item_id)
    i = queue_index(item_id)
    item = db().get(item_id)
    if item and item["status"] == "extracted":
        db().update(item_id, status="skipped", reason=SKIP_REASONS.get(code, code).split(" ", 1)[1])
    await cb.answer("❌ O'tkazib yuborildi")
    await show_in(cb.message, index=i)


async def run_cycle(bot: Bot, notify: bool = False, fast_only: bool = False, manual: bool = False,
                    status: Message | None = None, head: str | None = None) -> str:
    """Searches, then reports in one short message with a button to the dashboard, where the finds are.
    (`status`: the "Qidirilmoqda..." message to turn into the report.)"""
    if STATE["lock"].locked():
        return "⏳ Qidiruv allaqachon ketmoqda, tugashini kuting."
    finder = AGENTS["finder"]["name"]
    from .controls import over_budget
    if over_budget(db(), "finder"):
        return f"💸 {finder} bugungi AI byudjetini tugatdi. Boshqaruv bo'limida oshirish mumkin."
    async with STATE["lock"]:
        db().set_meta("search_started", datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))
        task_id = db().add_task("search", "finder", {"manual": manual}, status="working",
                                created_by="owner" if manual or head else "manager")
        log_event("manager", "task", f"{finder}, qidiruvni boshla", task_id)
        broken_before = failing_sources()
        try:
            result = await asyncio.to_thread(STATE["pipeline"].run, fast_only)
        except Exception as e:
            db().update_task(task_id, "failed", {"error": str(e)[:300]})
            await say("manager", f"⚠️ {finder}ning qidiruvi to'xtab qoldi: <code>{escape(str(e))[:300]}</code>",
                      kind="error", task_id=task_id, fallback_bot=bot)
            raise
        sent = await push_drafts(bot) if settings.mode != "finder" else 0
        newly_broken = {k: v for k, v in failing_sources().items() if k not in broken_before}
        if newly_broken:
            await say("finder", "⚠️ Bu manbalar ochilmadi: " + "; ".join(
                f"{escape(k)} ({escape((v or '')[:80])})" for k, v in newly_broken.items()),
                kind="error", task_id=task_id, fallback_bot=bot)
    seen = result["added"]
    waiting = len(ordered_finds()) if settings.mode == "finder" else sent
    db().set_meta("last_search", {"at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), "seen": seen, "waiting": waiting})
    db().update_task(task_id, "done", {"seen": seen, "waiting": waiting})
    head = head or ("✅ Qidiruv tugadi" if manual else "☀️ Bugungi qidiruv")
    note = (f"{head}: {seen} ta yangi e'lon ko'rildi, {waiting} ta mos topilma dashboardda kutmoqda."
            if waiting else f"{head}: {seen} ta yangi e'lon ko'rildi, mos keladigan yangisi yo'q.")
    try:
        from .dashboard import costs
        note += f"\n💵 Bugungi AI xarajati: ${costs(db(), 1)['today']:.2f}"
    except Exception:
        log.exception("cost line failed")
    if settings.mode != "finder" or not settings.admin_chat_id:
        return note
    if not manual and (not notify or (fast_only and waiting == 0)):
        log_event("finder", "done", note, task_id)
        return ""
    if status is not None:
        log_event("finder", "done", note, task_id)
        await status.edit_text(note, reply_markup=panel_markup())
    else:
        await say("finder", note, kind="done", reply_markup=panel_markup(), task_id=task_id, fallback_bot=bot)
    try:
        await auto_write(bot, task_id)
    except Exception:
        log.exception("auto write failed")
    return note


def learn_post(msg_id: int, html_text: str) -> None:
    """A post we just published goes into the Monday list and GrantBek's knowledge right away."""
    try:
        from .weekly import remember_post
        from .weekly import havola
        remember_post(db(), msg_id, datetime.utcnow(), re.sub(r"<[^>]+>", "", html_text or ""),
                      havola(re.findall(r'href="([^"]+)"', html_text or "")))
    except Exception:
        log.exception("could not remember the published post")


async def auto_write(bot: Bot, task_id: int | None = None) -> int:
    """Toshmat aka hands the strongest finds straight to Mirzo, so the posts are ready before anyone looks.
    At most AUTO_WRITE_PER_DAY a day; you still tap "Chop etish" (or "Rad etish")."""
    if not settings.auto_write_per_day or not settings.write_on_accept or settings.mode != "finder":
        return 0
    from .controls import may_work
    if may_work(db(), "writer"):
        return 0
    from .dashboard import _tz_offset
    tz = _tz_offset()
    done_today = db().conn.execute(
        "SELECT COUNT(*) FROM items WHERE reason='auto: Toshmat aka' AND date(updated_at, ?) >= date('now', ?, '-0 days')",
        (tz, tz)).fetchone()[0]
    room = settings.auto_write_per_day - done_today
    strong = [item for item, _ in ordered_finds() if (item["fit_score"] or 0) >= settings.auto_write_min_fit][:max(room, 0)]
    if not strong:
        return 0
    mirzo, eshmat = AGENTS["writer"]["name"], AGENTS["finder"]["name"]
    await say("manager", f"{eshmat} {len(strong)} ta kuchli grant topdi. {mirzo}, postlarini yoz: " +
              ", ".join(escape(i["title"][:60], quote=False) for i in strong), kind="task", task_id=task_id, fallback_bot=bot)
    for item in strong:
        take(item, 0)
        db().update(item["id"], reason="auto: Toshmat aka")
        await write_and_send(bot, item["id"], settings.admin_chat_id, by="Toshmat aka (avto)")
    return len(strong)


def failing_sources() -> dict:
    return {r["source"]: r["last_error"] for r in db().health()
            if r["last_error_at"] and (not r["last_ok"] or r["last_error_at"] > r["last_ok"])}


async def scheduled_search(bot: Bot, fast_only: bool = False) -> None:
    """The daily search, unless Toshmat aka was told to pause it."""
    from .controls import may_work
    why = may_work(db(), "finder")
    if why:
        log_event("manager", "info", f"Kunlik qidiruv o'tkazib yuborildi: Eshmat {why}")
        return
    await run_cycle(bot, True, fast_only)


async def retry_errors(bot: Bot) -> dict:
    """Errors get another chance: failed reads go back in line, failed posts are written again now."""
    rows = db().by_status("error")
    posts = [r for r in rows if (r["reason"] or "").startswith("write:")]
    for r in rows:
        if r not in posts:
            db().update(r["id"], status="triaged" if not r["data_json"] else "extracted", reason=None)
    written = failed = 0
    for r in posts:
        db().update(r["id"], status="accepted", reason=None)
        await asyncio.to_thread(STATE["pipeline"].write, db().get(r["id"]))
        item = db().get(r["id"])
        if item["status"] == "drafted":
            await send_review(bot, item)
            written += 1
        else:
            db().update(r["id"], status="accepted")
            failed += 1
    return {"requeued": len(rows) - len(posts), "written": written, "failed": failed}


def next_search_at() -> str | None:
    sched = STATE.get("scheduler")
    jobs = sched.get_jobs() if sched else []
    times = [j.next_run_time for j in jobs if j.next_run_time]
    return min(times).strftime("%d.%m %H:%M") if times else None


# ---------------------------------------------------------------- the buttons under the chat
SEARCH_TEXT = "🔎 Hozir qidirish"
STATS_TEXT = "📊 Statistika"
DASH_TEXT = "📈 Dashboard"


def main_keyboard() -> ReplyKeyboardMarkup:
    """The only button under the chat: everything else lives on the dashboard."""
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=DASH_TEXT)]], resize_keyboard=True, is_persistent=True)


def dash_button() -> InlineKeyboardButton:
    url = dashboard_url()
    return (InlineKeyboardButton(text=DASH_TEXT, url=url) if url.startswith("https://")
            else InlineKeyboardButton(text=DASH_TEXT, callback_data="dash"))


def panel_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[dash_button()]])


async def manual_search(bot: Bot, chat_id: int, user_id: int) -> None:
    if not allowed(user_id):
        await bot.send_message(chat_id, "Ruxsat yo'q.")
        return
    if STATE["lock"].locked():
        await bot.send_message(chat_id, "⏳ Qidiruv allaqachon ketmoqda, tugashini kuting.")
        return
    status = await bot.send_message(chat_id, "🔎 Qidirilmoqda... odatda 2-5 daqiqa. Natija shu xabarda chiqadi.")
    try:
        text = await run_cycle(bot, manual=True, status=status)
        if text.startswith("⏳"):
            await status.edit_text(text, reply_markup=panel_markup())
    except Exception as e:
        log.exception("manual search failed")
        await status.edit_text(f"❌ Qidiruvda xato: {escape(str(e))[:300]}", reply_markup=panel_markup())


@router.message(F.text == SEARCH_TEXT)
async def on_search_button(m: Message, bot: Bot):
    await manual_search(bot, m.chat.id, m.from_user.id)


@router.callback_query(F.data == "search")
async def on_search_inline(cb: CallbackQuery, bot: Bot):
    await cb.answer("Qidiruv boshlandi")
    await manual_search(bot, cb.message.chat.id, cb.from_user.id)


@router.message(F.text.in_({DASH_TEXT, BROWSE_TEXT, OLD_MORE_TEXT}))   # old buttons lead to the dashboard too
@router.message(Command("more", "list"))
async def on_dash_button(m: Message):
    await cmd_dashboard(m)


@router.callback_query(F.data.in_({"more", "dash"}))
async def on_dash_inline(cb: CallbackQuery):
    await cb.answer()
    await cmd_dashboard(cb.message, user_id=cb.from_user.id)


@router.message(F.text == STATS_TEXT)
async def on_stats_button(m: Message):
    await cmd_stats(m)


@router.callback_query(F.data == "stats")
async def on_stats_inline(cb: CallbackQuery):
    await cb.answer()
    await cmd_stats(cb.message)


@router.message(Command("panel"))
async def cmd_panel(m: Message, bot: Bot):
    """Posts and pins a message with the search button, so it is always one tap away."""
    msg = await m.answer("📌 EduGrants Finder\nHar kuni soat " + (settings.run_at or "?") + " da o'zi qidiradi. "
                         "Topilmalar, qidirish va xarajatlar dashboardda.", reply_markup=panel_markup())
    try:
        await bot.pin_chat_message(m.chat.id, msg.message_id, disable_notification=True)
    except Exception:
        await m.answer("Xabarni qadash uchun botni guruhda admin qiling.")
    await m.answer("Pastdagi tugma ham doim turadi 👇", reply_markup=main_keyboard())


def take(item, user_id: int) -> None:
    """Marks a find as taken, so it is never suggested again."""
    data = json.loads(item["data_json"] or "{}")
    db().update(item["id"], status="accepted", reason=f"accepted by {user_id}")
    db().history_mark_posted(item["norm_title"], data.get("title") or item["title"],
                             datetime.now().strftime("%Y-%m-%d"), item["official_url"])


@router.callback_query(F.data.startswith("ok:"))
async def on_take(cb: CallbackQuery, bot: Bot):
    """✅ Olamiz on a single card (cards sent before the browser existed)."""
    if not allowed(cb.from_user.id):
        return await cb.answer("Ruxsat yo'q", show_alert=True)
    item_id = int(cb.data.split(":")[1])
    take(db().get(item_id), cb.from_user.id)
    await cb.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✅ Olindi — {cb.from_user.first_name}", callback_data="noop")]]))
    await cb.answer("Olindi")
    if settings.write_on_accept:
        await write_and_send(bot, item_id, cb.message.chat.id, reply_to=cb.message.message_id)


async def write_and_send(bot: Bot, item_id: int, chat_id: int, reply_to: int | None = None, by: str = "") -> None:
    """Writes the channel post for a taken find and sends it with the publish buttons."""
    title = escape((db().get(item_id)["title"] or "")[:80])
    task_id = db().add_task("write_post", "writer", {"item_id": item_id}, status="working",
                            created_by="owner" if by else "manager")
    writer = bot_for("writer", bot)
    from .agents import tagged
    note = await writer.send_message(chat_id, tagged("writer", f"«{title}» uchun post yozyapman, ~20 soniya...")
                                     + (f" (oldi: {escape(by)})" if by else ""), reply_to_message_id=reply_to)
    log_event("writer", "task", f"Post yozish: {title}", task_id)
    try:
        await asyncio.to_thread(STATE["pipeline"].write, db().get(item_id))
    except Exception as e:
        log.exception("write failed")
        db().update(item_id, status="error", reason=f"write: {e}"[:300])
    item = db().get(item_id)
    if item["status"] == "drafted":
        await note.delete()
        await send_review(bot, item, reply_to=reply_to)
        db().update_task(task_id, "done")
    else:
        db().update_task(task_id, "failed", {"error": (item["reason"] or "")[:300]})
        log_event("writer", "error", f"Post yozilmadi: {title}: {item['reason'] or ''}", task_id)
        db().update(item_id, status="accepted")   # still taken; only the writing failed
        await note.edit_text(
            f"❌ «{title}» uchun post yozilmadi: {escape(item['reason'] or '')[:200]}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔁 Qayta yozish", callback_data=f"wr:{item_id}")]]))


@router.callback_query(F.data.startswith("wr:"))
async def on_rewrite(cb: CallbackQuery, bot: Bot):
    if not allowed(cb.from_user.id):
        return await cb.answer("Ruxsat yo'q", show_alert=True)
    item_id = int(cb.data.split(":")[1])
    item = db().get(item_id)
    if not item or item["status"] != "accepted":
        return await cb.answer("Allaqachon yozilgan")
    await cb.answer()
    reply_to = cb.message.reply_to_message.message_id if cb.message.reply_to_message else None
    await cb.message.delete()
    await write_and_send(bot, item_id, cb.message.chat.id, reply_to=reply_to)


@router.callback_query(F.data.startswith("no:"))
async def on_skip(cb: CallbackQuery):
    if not allowed(cb.from_user.id):
        return await cb.answer("Ruxsat yo'q", show_alert=True)
    item_id = int(cb.data.split(":")[1])
    buttons = [InlineKeyboardButton(text=label, callback_data=f"nr:{item_id}:{code}")
               for code, label in SKIP_REASONS.items()]
    await cb.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[buttons[i:i + 2] for i in range(0, len(buttons), 2)]))
    await cb.answer("Sababini tanlang, agent bundan o'rganadi")


@router.callback_query(F.data.startswith("nr:"))
async def on_skip_reason(cb: CallbackQuery):
    if not allowed(cb.from_user.id):
        return await cb.answer("Ruxsat yo'q", show_alert=True)
    _, item_id, code = cb.data.split(":")
    label = SKIP_REASONS.get(code, code)
    db().update(int(item_id), status="skipped", reason=label.split(" ", 1)[1])
    await cb.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"❌ {label}", callback_data="noop")]]))
    await cb.answer()


TEMP_DB_WARNING = ("\n\n⚠️ Railway'da volume ulanmagan: har yangilanishda statistika va topilmalar nolga tushadi. "
                   "Railway → service → Cmd+K → Volume → mount path /app/data.")

# ---------------------------------------------------------------- commands
@router.message(Command("start", "help"))
async def cmd_help(m: Message):
    t = AGENTS
    await m.answer(
        "EduGrants agentlari guruhi.\n\n"
        f"{t['manager']['emoji']} <b>{t['manager']['name']}</b>: boshliq. Eng kuchli topilmalarni o'zi {t['writer']['name']}ga "
        f"beradi (post tayyor bo'lib keladi, siz faqat «Chop etish»ni bosasiz), har kuni soat "
        f"{settings.report_at or '—'} da hisobot beradi. Unga oddiy so'zlar bilan yozing, masalan: "
        "<i>«bugun olimpiadalarni ko'proq top»</i>, <i>«hozir qidir»</i>, <i>«qidiruvni to'xtat»</i>.\n"
        f"{t['finder']['emoji']} <b>{t['finder']['name']}</b>: grantlarni topadi va saralaydi (har kuni {settings.run_at}).\n"
        f"{t['writer']['emoji']} <b>{t['writer']['name']}</b>: postlarni yozadi, dushanba kuni muddatlar ro'yxatini tayyorlaydi.\n"
        f"{t['community']['emoji']} <b>{t['community']['name']}</b>: o'z botidagi xabarlarga va kanal izohlariga o'zi javob beradi, har tong bilimini yangilaydi.\n\n"
        "/dashboard · /status · /report · /weekly\n\n"
        f"<i>Chat ID: <code>{m.chat.id}</code>, sizning ID: <code>{m.from_user.id}</code></i>",
        reply_markup=main_keyboard(),
    )


@router.message(Command("dashboard"))
async def cmd_dashboard(m: Message, user_id: int | None = None):
    if not allowed(user_id if user_id is not None else m.from_user.id):
        return
    url = dashboard_url()
    text = "📈 Topilmalar, qidirish, xarajatlar, manbalar va xatolar: hammasi dashboardda."
    if url.startswith("https://"):
        await m.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📈 Dashboardni ochish", url=url)]]))
    else:
        await m.answer(f"{text}\nHavola (faqat shu kompyuterda ochiladi): {escape(url)}")


@router.message(Command("profile"))
async def cmd_profile(m: Message):
    f = settings.profile_file
    text = f.read_text(encoding="utf-8") if f.exists() else "Profil yo'q. `python -m edugrants_agent import-history` bilan yarating."
    await m.answer(escape(text[:3800]))


@router.message(Command("run", "find"))
async def cmd_run(m: Message, bot: Bot):
    await manual_search(bot, m.chat.id, m.from_user.id)


@router.message(Command("queue"))
async def cmd_queue(m: Message):
    c = db().stats(days=3650)["counts"]
    keys = ["new", "triaged", "extracted", "shown", "accepted", "skipped", "drafted", "in_review", "published",
            "declined", "duplicate", "rejected", "error"]
    await m.answer("\n".join(f"{k}: {c.get(k, 0)}" for k in keys))


@router.message(Command("stats"))
async def cmd_stats(m: Message):
    s = db().stats(days=30)
    c = s["counts"]
    fmt = db().conn.execute("SELECT COUNT(*) FROM items WHERE status='rejected' AND reason LIKE 'post format%'"
                            " AND discovered_at >= datetime('now', '-30 days')").fetchone()[0]
    taken = sum(c.get(k, 0) for k in ("accepted", "drafted", "in_review", "published", "declined"))
    await m.answer(
        "📊 Oxirgi 30 kun\n"
        f"Topildi: {sum(c.values())}\nTakrorlar: {c.get('duplicate', 0)}\n"
        f"Filtrdan o'tmadi: {c.get('rejected', 0)} (shundan post formatiga to'g'ri kelmadi: {fmt})\n"
        f"Sizga ko'rsatildi: {c.get('shown', 0) + taken + c.get('skipped', 0)}\n"
        f"Navbatda kutmoqda: {c.get('extracted', 0)} ({DASH_TEXT})\n"
        f"Olindi: {taken}\nKerak emas: {c.get('skipped', 0)}\n"
        f"AI xarajati: ~${s['cost_usd']} ({s['tokens_in']:,} in / {s['tokens_out']:,} out tokens)"
        + (TEMP_DB_WARNING if database_is_temporary() else "")
    )


@router.message(Command("health"))
async def cmd_health(m: Message):
    rows = db().health()
    if not rows:
        await m.answer("Hali ishga tushirilmagan.")
        return
    out = []
    for r in rows:
        bad = r["last_error_at"] and (not r["last_ok"] or r["last_error_at"] > r["last_ok"])
        out.append(("🔴 " if bad else "🟢 ") + f"{escape(r['source'])}: {r['items_last_run'] or 0}"
                   + (f" — {escape((r['last_error'] or '')[:120])}" if bad else ""))
    await m.answer("\n".join(out))


@router.message(Command("errors"))
async def cmd_errors(m: Message):
    rows = db().by_status("error", limit=15)
    await m.answer("\n".join(f"#{r['id']} {escape(r['title'][:50])}: {escape(r['reason'] or '')[:120]}" for r in rows)
                   or "Xato yo'q.")


@router.message(Command("retry"))
async def cmd_retry(m: Message):
    if not allowed(m.from_user.id):
        return
    r = await retry_errors(m.bot)
    await m.answer(f"Qayta navbatga: {r['requeued']} · yozildi: {r['written']} · yana xato: {r['failed']}")


# ---------------------------------------------------------------- buttons
@router.callback_query(F.data.startswith("ap:"))
async def on_approve(cb: CallbackQuery, bot: Bot):
    if not allowed(cb.from_user.id):
        return await cb.answer("Ruxsat yo'q", show_alert=True)
    try:
        post_url, note = await publish_item(int(cb.data.split(":")[1]), bot, by=cb.from_user.first_name)
    except PublishError as e:
        return await cb.answer(str(e)[:190], show_alert=True)
    await cb.answer("Chop etildi" + note)


class PublishError(Exception):
    pass


async def publish_item(item_id: int, bot: Bot | None = None, by: str = "") -> tuple[str, str]:
    """Posts a ready post to the channel (through Toshmat aka's bot, the channel admin), sends it to the
    platform, marks it everywhere. Used by the Telegram button and the dashboard. Returns (url, note)."""
    item = db().get(item_id)
    if not item or item["status"] != "in_review":
        raise PublishError(f"Holati: {item['status'] if item else 'topilmadi'}")
    db().update(item_id, status="publishing")
    try:
        msg = await bot_for("manager", bot).send_message(settings.channel_id, item["post_text"])
    except Exception as e:
        db().update(item_id, status="in_review")
        raise PublishError(f"Kanalga yuborib bo'lmadi: {e}") from None
    post_url = f"https://t.me/{settings.channel_handle.lstrip('@')}/{msg.message_id}"
    ref, note = None, ""
    try:
        ref = await asyncio.to_thread(push_to_platform, item_id, json.loads(item["platform_json"] or "{}"), post_url)
    except Exception as e:
        note = " (platformaga yuborilmadi)"
        log.error("platform push failed for #%d: %s", item_id, e)
    db().update(item_id, status="published", channel_message_id=msg.message_id, platform_ref=ref,
                reason=None if ref else "platform push failed")
    log_event("writer", "done", f"Kanalda chop etildi: {item['title'][:80]}")
    learn_post(msg.message_id, item["post_text"])
    if item["review_message_id"] and settings.admin_chat_id:   # the Telegram card shows it too
        try:
            await bot_for("writer", bot).edit_message_reply_markup(
                chat_id=settings.admin_chat_id, message_id=item["review_message_id"],
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                    text=f"✅ Chop etildi" + (f" — {by}" if by else ""), url=post_url)]]))
        except Exception:
            pass
    return post_url, note


async def publish_weekly(task_id: int, bot: Bot | None = None, text: str | None = None) -> str:
    t = db().task(task_id)
    if not t or t["status"] != "waiting":
        raise PublishError("Bu ro'yxat allaqachon hal qilingan")
    result = json.loads(t["result"] or "{}")
    text = text or result.get("text", "")
    try:
        msg = await bot_for("manager", bot).send_message(settings.channel_id, text)
    except Exception as e:
        raise PublishError(f"Kanalga yuborib bo'lmadi: {e}") from None
    url = f"https://t.me/{settings.channel_handle.lstrip('@')}/{msg.message_id}"
    db().update_task(task_id, "done", {**result, "text": text, "url": url})
    log_event("writer", "done", "Haftalik ro'yxat kanalda chop etildi", task_id)
    return url


@router.callback_query(F.data.startswith("rj:"))
async def on_reject(cb: CallbackQuery):
    if not allowed(cb.from_user.id):
        return await cb.answer("Ruxsat yo'q", show_alert=True)
    item_id = int(cb.data.split(":")[1])
    db().update(item_id, status="declined", reason=f"declined by {cb.from_user.id}")
    await cb.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"❌ Rad etildi — {cb.from_user.first_name}", callback_data="noop")]]))
    await cb.answer("Rad etildi")


@router.callback_query(F.data.startswith("ed:"))
async def on_edit(cb: CallbackQuery, bot: Bot):
    if not allowed(cb.from_user.id):
        return await cb.answer("Ruxsat yo'q", show_alert=True)
    item_id = int(cb.data.split(":")[1])
    item = db().get(item_id)
    await cb.message.answer(f"✏️ #{item_id} uchun joriy matn quyida. Nusxa olib, tuzating va shu xabarga "
                            "javob (reply) qilib yuboring. Havolalar saqlanadi.")
    prompt = await cb.message.answer(item["post_text"], reply_markup=ForceReply(selective=True))
    STATE["pending_edits"][prompt.message_id] = item_id
    await cb.answer()


@router.message(F.reply_to_message)
async def on_edit_reply(m: Message, bot: Bot):
    key = m.reply_to_message.message_id
    if not allowed(m.from_user.id):
        return
    if key in STATE["weekly_edits"]:            # a new text for the Monday list
        task_id = STATE["weekly_edits"].pop(key)
        t = db().task(task_id)
        result = json.loads(t["result"] or "{}")
        result["text"] = m.html_text
        db().update_task(task_id, "waiting", result)
        from .manager import weekly_markup
        await say("writer", m.html_text, reply_markup=weekly_markup(task_id), log_it=False, fallback_bot=bot)
        return
    item_id = STATE["pending_edits"].pop(key, None)
    if item_id is None:
        return await on_order(m, bot)            # a reply that isn't an edit: treat it as a message to Toshmat aka
    db().update(item_id, post_text=m.html_text, status="drafted")
    await send_review(bot, db().get(item_id))


# ---------------------------------------------------------------- the Monday list (Mirzo)
@router.callback_query(F.data.startswith("wk:"))
async def on_weekly(cb: CallbackQuery, bot: Bot):
    if not allowed(cb.from_user.id):
        return await cb.answer("Ruxsat yo'q", show_alert=True)
    _, action, task_id = cb.data.split(":")
    t = db().task(int(task_id))
    if not t or t["status"] != "waiting":
        return await cb.answer("Bu ro'yxat allaqachon hal qilingan")
    text = json.loads(t["result"] or "{}").get("text", "")
    if action == "pub":
        try:
            url = await publish_weekly(int(task_id), bot)
        except PublishError as e:
            return await cb.answer(str(e)[:190], show_alert=True)
        await cb.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Chop etildi", url=url)]]))
        await cb.answer("Chop etildi")
    elif action == "no":
        db().update_task(int(task_id), "cancelled")
        log_event("writer", "info", "Haftalik ro'yxat bekor qilindi", int(task_id))
        await cb.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Bekor qilindi", callback_data="noop")]]))
        await cb.answer()
    elif action == "ed":
        await cb.answer()
        prompt = await cb.message.answer("✏️ Matnni nusxa oling, tuzating va shu xabarga javob (reply) qilib yuboring:")
        copy = await cb.message.answer(text, reply_markup=ForceReply(selective=True))
        STATE["weekly_edits"][copy.message_id] = int(task_id)
        STATE["weekly_edits"][prompt.message_id] = int(task_id)


@router.message(Command("weekly"))
async def cmd_weekly(m: Message):
    if allowed(m.from_user.id):
        from .manager import weekly_list
        await weekly_list(created_by="owner")


@router.message(Command("report"))
async def cmd_report(m: Message):
    if allowed(m.from_user.id):
        from .manager import daily_report
        await daily_report(created_by="owner")


@router.message(Command("status"))
async def cmd_status(m: Message):
    if allowed(m.from_user.id):
        from .manager import status_text
        await say("manager", status_text(db()), fallback_bot=m.bot)


# ---------------------------------------------------------------- orders to Toshmat aka in plain words
@router.message(F.text & ~F.text.startswith("/"))
async def on_order(m: Message, bot: Bot):
    if not allowed(m.from_user.id) or m.chat.id != settings.admin_chat_id:
        return
    from .manager import handle_order
    await handle_order(m.text, bot)


@router.callback_query(F.data.startswith("pf:"))
async def on_platform(cb: CallbackQuery):
    item = db().get(int(cb.data.split(":")[1]))
    p = json.loads(item["platform_json"] or "{}")
    short = {k: (v[:160] + "…" if isinstance(v, str) and len(v) > 160 else v) for k, v in p.items()}
    await cb.message.answer("<pre>" + escape(json.dumps(short, ensure_ascii=False, indent=1))[:3900] + "</pre>")
    await cb.answer()


@router.callback_query(F.data == "noop")
async def on_noop(cb: CallbackQuery):
    await cb.answer()


# ---------------------------------------------------------------- entry
def auto_import_history(database: DB) -> None:
    """On a fresh server the database is empty. If the channel export is in the project folder,
    load it once so nothing has to be typed on the server."""
    if (not database.get_meta("export_posts_v2") and settings.history_file.exists()):
        # (re)filled once, now with the full text and the "Havola" link of every post, for GrantBek
        database.set_meta("export_posts_v2", True)
        from .weekly import import_export_posts
        log.info("Monday list: %d past channel posts with deadlines",
                 import_export_posts(database, settings.history_file))
    if database.history_rows():
        if not settings.profile_file.exists():  # profile deleted or never made: rebuild it from the database
            from .history import build_profile
            settings.profile_file.parent.mkdir(parents=True, exist_ok=True)
            settings.profile_file.write_text(build_profile(database), encoding="utf-8")
            log.info("rebuilt the vibe profile at %s", settings.profile_file)
        return
    f = settings.history_file
    if not f.exists():
        log.warning("no channel history loaded and %s not found; run import-history", f)
        return
    from .history import build_profile, import_history
    log.info("first start: importing channel history from %s: %s", f, import_history(database, f))
    settings.profile_file.write_text(build_profile(database), encoding="utf-8")


def plan_jobs() -> str:
    """(Re)creates every scheduled job from the current settings. Called at startup and whenever the
    times are changed on the dashboard."""
    from .community import refresh_base
    from .manager import daily_report, weekly_list
    scheduler, bot = STATE["scheduler"], STATE["bot"]
    scheduler.remove_all_jobs()
    if settings.run_at:
        hour, minute = (int(x) for x in settings.run_at.split(":"))
        scheduler.add_job(scheduled_search, "cron", hour=hour, minute=minute, args=[bot], id="search",
                          max_instances=1, coalesce=True, misfire_grace_time=3600)
        when = f"daily at {settings.run_at} {settings.timezone}"
    else:
        scheduler.add_job(scheduled_search, "interval", hours=settings.run_every_hours, args=[bot], id="search",
                          next_run_time=datetime.now() + timedelta(minutes=1), max_instances=1, coalesce=True)
        when = f"every {settings.run_every_hours}h"
    if settings.fast_every_minutes > 0:  # optional extra Telegram-only checks
        scheduler.add_job(scheduled_search, "interval", minutes=settings.fast_every_minutes, args=[bot, True],
                          id="fast", max_instances=1, coalesce=True)
    if settings.community_refresh_at:   # GrantBek re-reads the channel and relearns the week's questions
        h, mi = (int(x) for x in settings.community_refresh_at.split(":"))
        scheduler.add_job(refresh_base, "cron", hour=h, minute=mi, id="refresh", max_instances=1, coalesce=True,
                          misfire_grace_time=3600)
    if settings.report_at:      # Toshmat aka's evening report
        h, mi = (int(x) for x in settings.report_at.split(":"))
        scheduler.add_job(daily_report, "cron", hour=h, minute=mi, id="report", max_instances=1, coalesce=True,
                          misfire_grace_time=3600)
    if settings.weekly_at:      # Mirzo's Monday deadline list, e.g. "mon 08:30"
        day, hm = settings.weekly_at.split()
        h, mi = (int(x) for x in hm.split(":"))
        scheduler.add_job(weekly_list, "cron", day_of_week=day, hour=h, minute=mi, id="weekly", max_instances=1,
                          coalesce=True, misfire_grace_time=6 * 3600)
    return when


async def main() -> None:
    if not settings.bot_token:
        raise SystemExit("BOT_TOKEN is missing (Railway: Variables; on your computer: .env)")
    STATE["db"] = DB(settings.db_path)
    _DB["get"] = lambda: STATE["db"]
    log.info("database: %s", settings.db_path)
    if database_is_temporary():
        log.warning("NO RAILWAY VOLUME: the database is on the container's own disk and will be wiped at the next "
                    "deploy. Add a volume (Cmd+K > Volume, mount path /app/data) to keep statistics and finds.")
    auto_import_history(STATE["db"])
    from .controls import apply as apply_controls
    apply_controls(STATE["db"])              # settings changed on the dashboard win over the variables
    STATE["pipeline"] = Pipeline(STATE["db"])
    props = DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True)
    setup_bots(lambda token: Bot(token, default=props))
    from .agents import PROBLEMS, check_bots
    await check_bots()
    bot = BOTS.get("manager")
    from .dashboard import start as start_dashboard
    await start_dashboard(lambda: STATE["db"], bot)   # the dashboard runs even if Telegram refuses a token
    if bot is None or "manager" in PROBLEMS:
        log.error("BOT_TOKEN is not working: %s. Fix it in Railway > Variables; the dashboard stays up meanwhile.",
                  PROBLEMS.get("manager", "missing"))
        await asyncio.Event().wait()                  # no crash-restart loop; Railway redeploys when you fix it
    bots = list(dict.fromkeys(BOTS.values()))
    dp = Dispatcher()
    dp.include_router(router)
    from .community import router as community_router
    dp.include_router(community_router)      # GrantBek: DMs to his bot and comments under our posts

    if settings.admin_chat_id:
        STATE["scheduler"] = AsyncIOScheduler(timezone=settings.timezone)
        STATE["bot"] = bot
        when = plan_jobs()
        STATE["scheduler"].start()
        log.info("team started (%s); search %s", ", ".join(a for a in AGENTS if a in BOTS), when)
    else:
        log.warning("ADMIN_CHAT_ID not set: setup mode. Add the bot to your agents group, send /help "
                    "there, put the chat id in the variables and restart.")
    await bot.set_my_commands([
        BotCommand(command="dashboard", description="Dashboardni ochish"),
        BotCommand(command="status", description="Jamoa holati"),
        BotCommand(command="report", description="Hisobot hozir"),
        BotCommand(command="weekly", description="Haftalik muddatlar ro'yxati"),
        BotCommand(command="help", description="Yordam"),
    ])
    if settings.admin_chat_id:
        own = [f"{AGENTS[a]['emoji']} {AGENTS[a]['name']}" + (
            "" if a in BOTS else " (o'z boti hali yo'q, ishlamaydi)" if a == "community" else " (Toshmat akaning boti orqali)")
            for a in AGENTS]
        text = "Jamoa ishga tushdi: " + ", ".join(own)
        bad = [f"⚠️ {AGENTS[a]['name']}: {p}" for a, p in PROBLEMS.items()]
        await say("manager", text + ("\n" + "\n".join(bad) if bad else ""), kind="error" if bad else "info")
    await dp.start_polling(*bots)
