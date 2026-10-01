"""Telegram admin bot: editors approve, edit or reject drafts with one tap.

Runs the pipeline on a schedule in the same process, so one small server is enough.
"""
from __future__ import annotations

import asyncio
import json
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

from .config import settings
from .db import DB
from .pipeline import AGGREGATOR_DOMAINS, Pipeline, format_gaps
from .publish import push_to_platform
from .render import finder_card, uz_date

log = logging.getLogger(__name__)
router = Router()

STATE: dict = {"db": None, "pipeline": None, "lock": asyncio.Lock(), "push_lock": asyncio.Lock(), "pending_edits": {}}


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
    msg = await bot.send_message(settings.admin_chat_id, review_header(item) + item["post_text"],
                                 reply_markup=keyboard(item["id"]), reply_to_message_id=reply_to)
    db().update(item["id"], status="in_review", review_message_id=msg.message_id)


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


def _expired(r) -> bool:
    """A find waiting in the queue whose deadline got too close since it was found."""
    data = json.loads(r["data_json"] or "{}")
    if data.get("deadline_type") == "rolling" or not data.get("deadline"):
        return False
    try:
        return (date.fromisoformat(data["deadline"][:10]) - date.today()).days < settings.min_days_left
    except ValueError:
        return False


def ordered_finds(limit: int | None = None) -> list[tuple]:
    """The queue of finds not yet shown, best first: not yet posted by other Uzbek channels, then new
    external finds before old programmes, then best fit, then the freshest at the source."""
    cards = []
    for r in db().by_status("extracted"):
        if _expired(r):
            db().update(r["id"], status="rejected", reason="deadline too close (waited in the queue)")
            continue
        gaps = format_gaps(json.loads(r["data_json"] or "{}"), r["official_url"], STATE["pipeline"].aggregators
                           if hasattr(STATE["pipeline"], "aggregators") else AGGREGATOR_DOMAINS)
        if gaps:   # finds queued before the format check existed
            db().update(r["id"], status="rejected", reason=f"post format: {', '.join(gaps)}")
            continue
        text, taken = finder_card(db(), r)
        cards.append((taken, 1 if r["history_id"] else 0, -(r["fit_score"] or 0),
                      -(datetime.strptime(r["published_at"][:19], "%Y-%m-%d %H:%M:%S").timestamp()
                        if r["published_at"] else 0), r["id"], r, text))
    cards.sort(key=lambda c: c[:5])
    return [(c[5], c[6]) for c in cards[:limit]]


MORE_TEXT = "➕ Yana 5 ta"


def more_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=MORE_TEXT, callback_data="more")]])


async def push_finds(bot: Bot, n: int | None = None) -> tuple[int, int]:
    """Sends the best `n` waiting finds (5 by default). Returns (sent, still waiting)."""
    if not settings.admin_chat_id:
        return 0, 0
    async with STATE["push_lock"]:   # two quick presses must not send the same cards twice
        queue = ordered_finds()
        batch = queue[: n or settings.cards_per_batch]
        sent = 0
        for item, text in batch:
            try:
                msg = await bot.send_message(settings.admin_chat_id, text, reply_markup=find_keyboard(item["id"]))
                db().update(item["id"], status="shown", review_message_id=msg.message_id)
                sent += 1
            except Exception as e:
                log.error("could not send find #%d: %s", item["id"], e)
            await asyncio.sleep(1.2)
        return sent, len(queue) - len(batch)


def waiting_line(left: int) -> str:
    return (f"📦 Navbatda yana {left} ta topilma bor. Ko'rish uchun «{MORE_TEXT}» ni bosing."
            if left else "📭 Navbatda boshqa topilma yo'q.")


async def send_more(bot: Bot, chat_id: int, user_id: int) -> None:
    if not allowed(user_id):
        await bot.send_message(chat_id, "Ruxsat yo'q.")
        return
    sent, left = await push_finds(bot)
    if not sent:
        await bot.send_message(chat_id, f"📭 Navbatda topilma qolmadi. Yangilarini qidirish uchun «{SEARCH_TEXT}».",
                               reply_markup=panel_markup())
        return
    await bot.send_message(chat_id, waiting_line(left), reply_markup=more_markup() if left else panel_markup())


async def run_cycle(bot: Bot, notify: bool = False, fast_only: bool = False, manual: bool = False) -> str:
    if STATE["lock"].locked():
        return "⏳ Qidiruv allaqachon ketmoqda, tugashini kuting."
    async with STATE["lock"]:
        result = await asyncio.to_thread(STATE["pipeline"].run, fast_only)
        if settings.mode == "finder":
            sent, left = await push_finds(bot)
        else:
            sent, left = await push_drafts(bot), 0
    seen = result["added"]
    total = sent + left
    found = (f"{seen} ta yangi e'lon ko'rildi, {total} ta mos topilma navbatda. "
             f"Eng yaxshi {sent} tasi yuborildi." if sent else
             f"{seen} ta yangi e'lon ko'rildi, mos keladigan yangisi yo'q.")
    if left:
        found += f"\n{waiting_line(left)}"
    if manual:
        return "✅ Qidiruv tugadi: " + found
    if not notify or (fast_only and sent == 0):
        return ""
    text = "☀️ Bugungi qidiruv: " + found
    await bot.send_message(settings.admin_chat_id, text, reply_markup=more_markup() if left else panel_markup())
    return text


# ---------------------------------------------------------------- the search button
SEARCH_TEXT = "🔎 Hozir qidirish"
STATS_TEXT = "📊 Statistika"


def main_keyboard() -> ReplyKeyboardMarkup:
    """Always-visible buttons at the bottom of the chat."""
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=SEARCH_TEXT), KeyboardButton(text=MORE_TEXT)],
                                         [KeyboardButton(text=STATS_TEXT)]],
                               resize_keyboard=True, is_persistent=True)


def panel_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=SEARCH_TEXT, callback_data="search"),
        InlineKeyboardButton(text=MORE_TEXT, callback_data="more"),
    ], [InlineKeyboardButton(text=STATS_TEXT, callback_data="stats")]])


async def manual_search(bot: Bot, chat_id: int, user_id: int) -> None:
    if not allowed(user_id):
        await bot.send_message(chat_id, "Ruxsat yo'q.")
        return
    if STATE["lock"].locked():
        await bot.send_message(chat_id, "⏳ Qidiruv allaqachon ketmoqda, tugashini kuting.")
        return
    status = await bot.send_message(chat_id, "🔎 Qidirilmoqda... odatda 2-5 daqiqa. Topilganlar guruhga keladi.")
    try:
        text = await run_cycle(bot, manual=True)
    except Exception as e:
        log.exception("manual search failed")
        text = f"❌ Qidiruvda xato: {escape(str(e))[:300]}"
    await status.edit_text(text, reply_markup=more_markup() if "📦" in text else panel_markup())


@router.message(F.text == SEARCH_TEXT)
async def on_search_button(m: Message, bot: Bot):
    await manual_search(bot, m.chat.id, m.from_user.id)


@router.callback_query(F.data == "search")
async def on_search_inline(cb: CallbackQuery, bot: Bot):
    await cb.answer("Qidiruv boshlandi")
    await manual_search(bot, cb.message.chat.id, cb.from_user.id)


@router.message(F.text == MORE_TEXT)
@router.message(Command("more"))
async def on_more_button(m: Message, bot: Bot):
    await send_more(bot, m.chat.id, m.from_user.id)


@router.callback_query(F.data == "more")
async def on_more_inline(cb: CallbackQuery, bot: Bot):
    await cb.answer()
    try:   # the old "more" button has done its job
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await send_more(bot, cb.message.chat.id, cb.from_user.id)


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
    msg = await m.answer("📌 EduGrants Finder\nHar kuni soat " + (settings.run_at or "?") + " da avtomatik qidiradi. "
                         "Istalgan vaqtda qidirish uchun tugmani bosing.", reply_markup=panel_markup())
    try:
        await bot.pin_chat_message(m.chat.id, msg.message_id, disable_notification=True)
    except Exception:
        await m.answer("Xabarni qadash uchun botni guruhda admin qiling.")
    await m.answer("Pastdagi tugmalar ham doim turadi 👇", reply_markup=main_keyboard())


@router.callback_query(F.data.startswith("ok:"))
async def on_take(cb: CallbackQuery, bot: Bot):
    if not allowed(cb.from_user.id):
        return await cb.answer("Ruxsat yo'q", show_alert=True)
    item_id = int(cb.data.split(":")[1])
    item = db().get(item_id)
    data = json.loads(item["data_json"] or "{}")
    db().update(item_id, status="accepted", reason=f"accepted by {cb.from_user.id}")
    # So it is never suggested again
    db().history_mark_posted(item["norm_title"], data.get("title") or item["title"],
                             datetime.now().strftime("%Y-%m-%d"), item["official_url"])
    await cb.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✅ Olindi — {cb.from_user.first_name}", callback_data="noop")]]))
    await cb.answer("Olindi")
    if settings.write_on_accept:
        await write_and_send(bot, item_id, cb.message)


async def write_and_send(bot: Bot, item_id: int, card: Message) -> None:
    """Writes the channel post for an accepted find and sends it for review under its card."""
    note = await card.reply("✍️ Post yozilmoqda, ~20 soniya...")
    try:
        await asyncio.to_thread(STATE["pipeline"].write, db().get(item_id))
    except Exception as e:
        log.exception("write failed")
        db().update(item_id, status="error", reason=f"write: {e}"[:300])
    item = db().get(item_id)
    if item["status"] == "drafted":
        await note.delete()
        await send_review(bot, item, reply_to=card.message_id)
    else:
        db().update(item_id, status="accepted")   # still taken; only the writing failed
        await note.edit_text(
            f"❌ Post yozilmadi: {escape(item['reason'] or '')[:200]}",
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
    await cb.message.delete()
    card = cb.message.reply_to_message or cb.message
    await write_and_send(bot, item_id, card)


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


# ---------------------------------------------------------------- commands
@router.message(Command("start", "help"))
async def cmd_help(m: Message):
    await m.answer(
        "EduGrants agenti.\n\n"
        f"{SEARCH_TEXT} tugmasi yoki /find — hozir qidirish\n{MORE_TEXT} tugmasi yoki /more — navbatdagi keyingi 5 ta topilma\n/panel — qidirish tugmasini guruhda qadab qo'yish\n/profile — kanal profili\n/queue — navbat holati\n/stats — 30 kunlik statistika va xarajat\n"
        "/health — manbalar holati\n/errors — oxirgi xatolar\n/retry — xato bo'lganlarni qayta urinish\n\n"
        f"Chat ID: <code>{m.chat.id}</code>, sizning ID: <code>{m.from_user.id}</code>",
        reply_markup=main_keyboard(),
    )


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
        f"Navbatda kutmoqda: {c.get('extracted', 0)} ({MORE_TEXT})\n"
        f"Olindi: {taken}\nKerak emas: {c.get('skipped', 0)}\n"
        f"AI xarajati: ~${s['cost_usd']} ({s['tokens_in']:,} in / {s['tokens_out']:,} out tokens)"
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
    rows = db().by_status("error")
    posts = [r for r in rows if (r["reason"] or "").startswith("write:")]
    for r in rows:
        if r not in posts:
            db().update(r["id"], status="triaged" if not r["data_json"] else "extracted", reason=None)
    if len(rows) > len(posts):
        await m.answer(f"{len(rows) - len(posts)} ta element qayta navbatga qo'yildi. /run bosing.")
    for r in posts:   # taken finds whose post failed to write: write them now
        db().update(r["id"], status="accepted", reason=None)
        await asyncio.to_thread(STATE["pipeline"].write, db().get(r["id"]))
        item = db().get(r["id"])
        if item["status"] == "drafted":
            await send_review(m.bot, item)
        else:
            db().update(r["id"], status="accepted")
            await m.answer(f"❌ {escape(item['title'][:80])}: {escape(item['reason'] or '')[:200]}")
    if not rows:
        await m.answer("Xato bo'lgan element yo'q.")


# ---------------------------------------------------------------- buttons
@router.callback_query(F.data.startswith("ap:"))
async def on_approve(cb: CallbackQuery, bot: Bot):
    if not allowed(cb.from_user.id):
        return await cb.answer("Ruxsat yo'q", show_alert=True)
    item_id = int(cb.data.split(":")[1])
    item = db().get(item_id)
    if not item or item["status"] != "in_review":
        return await cb.answer(f"Holati: {item['status'] if item else 'topilmadi'}", show_alert=True)
    db().update(item_id, status="publishing")
    try:
        msg = await bot.send_message(settings.channel_id, item["post_text"])
    except Exception as e:
        db().update(item_id, status="in_review")
        return await cb.answer(f"Kanalga yuborib bo'lmadi: {e}"[:190], show_alert=True)
    post_url = f"https://t.me/{settings.channel_handle.lstrip('@')}/{msg.message_id}"
    ref, note = None, ""
    try:
        ref = await asyncio.to_thread(push_to_platform, item_id, json.loads(item["platform_json"]), post_url)
    except Exception as e:
        note = " (platformaga yuborilmadi, /errors)"
        log.error("platform push failed for #%d: %s", item_id, e)
    db().update(item_id, status="published", channel_message_id=msg.message_id, platform_ref=ref,
                reason=None if ref else "platform push failed")
    await cb.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✅ Chop etildi — {cb.from_user.first_name}", url=post_url)]]))
    await cb.answer("Chop etildi" + note)


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
    item_id = STATE["pending_edits"].pop(m.reply_to_message.message_id, None)
    if item_id is None or not allowed(m.from_user.id):
        return
    db().update(item_id, post_text=m.html_text, status="drafted")
    await send_review(bot, db().get(item_id))


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


async def main() -> None:
    if not settings.bot_token:
        raise SystemExit("Set BOT_TOKEN in .env")
    STATE["db"] = DB(settings.db_path)
    auto_import_history(STATE["db"])
    STATE["pipeline"] = Pipeline(STATE["db"])
    props = DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True)
    bot = Bot(settings.bot_token, default=props)
    dp = Dispatcher()
    dp.include_router(router)

    if settings.admin_chat_id:
        scheduler = AsyncIOScheduler(timezone=settings.timezone)
        if settings.run_at:
            hour, minute = (int(x) for x in settings.run_at.split(":"))
            scheduler.add_job(run_cycle, "cron", hour=hour, minute=minute, args=[bot, True],
                              max_instances=1, coalesce=True, misfire_grace_time=3600)
            when = f"daily at {settings.run_at} {settings.timezone}"
        else:
            scheduler.add_job(run_cycle, "interval", hours=settings.run_every_hours, args=[bot, True],
                              next_run_time=datetime.now() + timedelta(minutes=1), max_instances=1, coalesce=True)
            when = f"every {settings.run_every_hours}h"
        if settings.fast_every_minutes > 0:  # optional extra Telegram-only checks
            scheduler.add_job(run_cycle, "interval", minutes=settings.fast_every_minutes, args=[bot, True, True],
                              max_instances=1, coalesce=True)
        scheduler.start()
        log.info("bot started; search %s (use /find to search now)", when)
    else:
        log.warning("ADMIN_CHAT_ID not set: setup mode. Add the bot to your editors group, send /help "
                    "there, put the chat id in .env and restart.")
    await bot.set_my_commands([
        BotCommand(command="find", description="Hozir qidirish"),
        BotCommand(command="more", description="Navbatdagi yana 5 ta topilma"),
        BotCommand(command="panel", description="Qidirish tugmasini qadash"),
        BotCommand(command="stats", description="Statistika"),
        BotCommand(command="health", description="Manbalar holati"),
        BotCommand(command="help", description="Yordam"),
    ])
    await dp.start_polling(bot)
