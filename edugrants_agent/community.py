"""GrantBek, the community agent: answers people in his own bot's DMs and in the comments under
our channel posts, by herself.

What he knows is only what the channel itself published: the post under which someone comments,
and, for DMs, our posts that match the question plus everything whose deadline is still open. She
never invents a deadline or a rule; if the posts don't say, he points to the official link. Thanks,
emoji and chit-chat in comments are left alone. Ad/partnership requests and anything he can't
handle get a polite answer, and Toshmat aka's group gets an alert.

Setup: COMMUNITY_BOT_TOKEN is his bot. People DM it; add it as an admin to the channel's comments
group (so it sees comments) and as a member of the agent group (for alerts).
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import date
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import CommandStart
from aiogram.types import Message

from .agents import AGENTS, BOTS, note, say
from .config import settings

log = logging.getLogger(__name__)
router = Router()
NAME = AGENTS["community"]["name"]


async def _is_gulsara(message: Message, bot: Bot) -> bool:
    return BOTS.get("community") is not None and bot is BOTS["community"]

router.message.filter(_is_gulsara)


def _db():
    from .bot import db
    return db()


REPLY_TOOL = {
    "name": "reply",
    "description": "Decide whether and how to answer this message.",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["reply", "ignore", "escalate"],
                       "description": "reply = answer it; ignore = thanks/emoji/chit-chat/spam that needs no answer; "
                                      "escalate = ad or partnership request, complaint, or something only a human "
                                      "admin can handle (still give a polite short reply)."},
            "reply": {"type": "string", "description": "The answer, in the same language and script as the person "
                                                       "(Uzbek Latin, Uzbek Cyrillic, Russian or English). Empty if ignore."},
            "topic": {"type": "string", "description": "3-6 words in Uzbek: what it was about (for the log)."},
        },
        "required": ["action", "reply", "topic"],
    },
}

RULES = (
    f"You are {NAME}, the friendly community helper of the EduGrants Telegram channel (@EduGrandsUz), which posts "
    "grants, olympiads, summer schools and other opportunities for young people in Uzbekistan (mostly ages 12-20). "
    "Answer warmly and briefly (1-4 sentences), politely with 'siz', like a helpful older brother, no emoji spam. "
    "STRICT RULES: use ONLY facts from the posts given to you. Never invent or guess a deadline, age limit, fee, "
    "country rule or result date. If the posts don't answer it, say so honestly and point to the official "
    "registration link in the post. Never promise someone will be accepted. Don't discuss politics or religion. "
    "Don't share any admin's personal contacts. When you mention one of our posts, give its t.me link. "
    "Ad, sponsorship and partnership requests: thank them, say the admins will get back to them, action=escalate."
)

STOP = {"uchun", "qanday", "qachon", "qaysi", "nima", "bormi", "kerak", "mumkin", "bilan", "haqida", "grant",
        "grantlar", "dastur", "dasturlar", "the", "and", "for", "what", "when", "how", "это", "как", "когда"}


def words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-zа-яёʻ'’`0-9]{3,}", (text or "").lower()) if w not in STOP}


def relevant_posts(db, question: str, k: int = 4) -> list:
    """Our posts that best match the question (open ones first), for DM answers."""
    q = words(question)
    rows = db.conn.execute("SELECT * FROM channel_posts WHERE text IS NOT NULL ORDER BY msg_id DESC LIMIT 1500").fetchall()
    today = date.today().isoformat()
    scored = []
    for r in rows:
        overlap = len(q & words(r["title"])) * 3 + len(q & words(r["text"][:1500]))
        if overlap:
            scored.append((overlap + (2 if (r["deadline"] or "") >= today else 0), r["msg_id"], r))
    scored.sort(key=lambda s: (-s[0], -s[1]))
    return [s[2] for s in scored[:k]]


def post_link(msg_id: int) -> str:
    return f"https://t.me/{settings.channel_handle.lstrip('@')}/{msg_id}"


def dm_context(db, question: str) -> str:
    today = date.today().isoformat()
    parts = []
    for r in relevant_posts(db, question):
        status = "OPEN" if (r["deadline"] or "") >= today else "CLOSED (deadline passed)"
        parts.append(f"--- Post {post_link(r['msg_id'])} [{status}, deadline {r['deadline']}]\n{r['text'][:1500]}")
    open_list = "\n".join(f"- {r['title']} (muddat {r['deadline']}) {post_link(r['msg_id'])}"
                          for r in db.open_channel_posts(today, 40))
    return (f"Today is {today}.\n\nMOST RELEVANT POSTS:\n" + ("\n\n".join(parts) or "(none matched)") +
            "\n\nALL OPPORTUNITIES WITH OPEN DEADLINES:\n" + (open_list or "(none)"))


def decide(context: str, message: str, where: str) -> dict:
    from .bot import STATE
    llm = STATE["pipeline"].llm
    brief = (_db().get_meta("community_brief") or {}).get("text", "")
    fixes = _db().corrections(15)
    if fixes:
        brief += "\nTHE OWNER CORRECTED THESE ANSWERS (answer like this):\n" + "\n".join(
            f"- Q: {f['question'][:200]} -> correct answer: {f['correction'][:300]}" for f in fixes)
    return llm._call("community", settings.community_model or settings.model_fast,
                     RULES + (f"\n\nWHAT PEOPLE ASKED RECENTLY AND HOW WE ANSWER (updated daily; follow this tone, "
                              f"but facts still come only from the posts):\n{brief}" if brief else "")
                     + f"\n\nThis message came as {where}.\n\n{context}", message, REPLY_TOOL, 700)


async def answer(m: Message, context: str, where: str) -> None:
    text = (m.text or m.caption or "").strip()
    if not text:
        return
    from .controls import may_work
    why = may_work(_db(), "community")
    if why:
        note("community", "info", f"Javob berilmadi ({why}): {text[:80]}")
        return
    try:
        d = await asyncio.to_thread(decide, context, text, where)
    except Exception as e:
        log.exception("community answer failed")
        note("community", "error", f"Javob bera olmadim ({where}): {e}")
        return
    who = m.from_user.full_name if m.from_user else "?"
    if d.get("action") == "ignore" or not (d.get("reply") or "").strip():
        note("community", "info", f"E'tiborsiz qoldirildi ({where}): {text[:80]}")
        return
    await m.reply(escape(d["reply"], quote=False))
    note("community", "done", f"Javob berdi ({where}, {d.get('topic', '')}): {who}")
    url = None
    if m.chat.type != "private":
        try:
            url = m.get_url()
        except Exception:
            url = None
    elif m.from_user and m.from_user.username:
        url = f"https://t.me/{m.from_user.username}"
    _db().add_qa("dm" if m.chat.type == "private" else "comment", text, d["reply"], d.get("topic"),
                 escalated=d.get("action") == "escalate", link=url, who=who)
    if d.get("action") == "escalate":
        link = f" · <a href=\"{url}\">izoh</a>" if m.chat.type != "private" and url else ""
        await say("community", f"🙋 {escape(who, quote=False)} ({where}) adminga murojaat qildi: "
                               f"<i>{escape(text[:300], quote=False)}</i>{link}", kind="task")


# --------------------------------------------------------------------------- DMs to his bot
@router.message(CommandStart(), F.chat.type == "private")
async def on_start(m: Message):
    await m.answer(f"Assalomu alaykum! Men {NAME}, EduGrants yordamchisiman 🙂\n"
                   "Grantlar, tanlovlar va dasturlar haqida savolingizni yozing: muddati, kimlar qatnasha oladi, "
                   "qanday topshiriladi.")


@router.message(F.chat.type == "private", F.text)
async def on_dm(m: Message):
    await answer(m, await asyncio.to_thread(dm_context, _db(), m.text), "shaxsiy xabar")


# --------------------------------------------------------------------------- comments under our posts
@router.message(F.chat.type.in_({"group", "supergroup"}))
async def on_comment(m: Message):
    if m.chat.id == settings.admin_chat_id:
        return                                   # the agent group is Toshmat aka's
    db = _db()
    if m.is_automatic_forward:                   # our channel post arriving in the comments group
        db.save_thread(m.chat.id, m.message_id, m.text or m.caption or "")
        return
    if m.from_user is None or m.from_user.is_bot or m.sender_chat is not None:
        return                                   # bots, the channel itself, anonymous admins
    root = m.reply_to_message
    post = None
    if root is not None and root.is_automatic_forward:
        post = root.text or root.caption
    if post is None and m.message_thread_id:
        post = db.thread_text(m.chat.id, m.message_thread_id)
    if not post:
        return                                   # not under one of our posts
    await answer(m, f"Today is {date.today().isoformat()}.\n\nTHE POST BEING COMMENTED ON:\n{post[:3000]}",
                 "post ostidagi izoh")


# --------------------------------------------------------------------------- the daily refresh
BRIEF_TOOL = {
    "name": "brief",
    "description": "Notes that help the community helper answer naturally.",
    "input_schema": {"type": "object", "properties": {
        "faq": {"type": "array", "items": {"type": "string"}, "maxItems": 12,
                "description": "The most common questions this week with a one-line answer pattern, in Uzbek."},
        "style": {"type": "array", "items": {"type": "string"}, "maxItems": 5,
                  "description": "How people write and what tone works with them (e.g. many write in Russian, many "
                                 "are 9th-11th graders), in English."},
    }, "required": ["faq", "style"]},
}


def build_brief(db) -> str:
    """Learns from the last week's questions: common questions and what tone fits (one cheap call)."""
    qa = db.recent_qa(7)
    if len(qa) < 3:
        return ""
    from .bot import STATE
    sample = "\n".join(f"[{r['place']}] Q: {r['question'][:200]} | A: {(r['answer'] or '')[:200]}" for r in qa[:120])
    out = STATE["pipeline"].llm._call("community_brief", settings.model_fast,
                                      "Summarise what followers of an Uzbek grants channel asked this week.", sample,
                                      BRIEF_TOOL, 900)
    return "\n".join([f"- {f}" for f in out.get("faq", [])] + [f"- style: {x}" for x in out.get("style", [])])[:2500]


def refresh_channel(db) -> int:
    """Reads our own channel again, so new posts and edits are known even on days without a search."""
    from .collectors import collect_telegram
    from .config import load_yaml
    n_before = db.conn.execute("SELECT COUNT(*) FROM channel_posts").fetchone()[0]
    for src in load_yaml(settings.sources_file).get("sources", []):
        if src.get("type") == "telegram" and src.get("role") == "own" and src.get("enabled", True):
            collect_telegram(src, db, [])
    return db.conn.execute("SELECT COUNT(*) FROM channel_posts").fetchone()[0] - n_before


async def refresh_base() -> None:
    """Every morning: new channel posts in, closed deadlines out (by date), and a fresh FAQ from the week."""
    db = _db()
    from .controls import is_paused
    if is_paused(db, "community"):
        return
    task_id = db.add_task("community_refresh", "community", status="working")
    new = 0
    if settings.tg_api_id and settings.tg_api_hash and settings.tg_string_session:
        try:
            from .bot import STATE
            async with STATE["lock"]:          # never read Telegram at the same time as a search
                new = await asyncio.to_thread(refresh_channel, db)
        except Exception as e:
            log.exception("channel refresh failed")
            note("community", "error", f"Kanalni o'qib bo'lmadi: {e}", task_id)
    try:
        brief = await asyncio.to_thread(build_brief, db)
        if brief:
            db.set_meta("community_brief", {"text": brief, "at": date.today().isoformat()})
    except Exception as e:
        log.exception("brief failed")
        note("community", "error", f"FAQ yangilanmadi: {e}", task_id)
    open_now = len(db.open_channel_posts(date.today().isoformat(), 1000))
    db.update_task(task_id, "done", {"new_posts": new, "open": open_now})
    note("community", "done", f"Bilim bazasi yangilandi: {new} ta yangi post, {open_now} ta dastur hali ochiq", task_id)
