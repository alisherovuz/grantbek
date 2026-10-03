"""GrantBek, the community agent: answers people in his own bot's DMs and in the comments under
our channel posts, by herself.

What he knows: our channel posts, plus the official page behind each post's "Havola" link (read
and cached for 3 days, so he knows the full details the short post leaves out). Under a post he
uses that post and its official page; in DMs and for general questions in the group he uses the
posts that match the question (with their official pages) and everything still open. He never
invents a deadline or a rule. In DMs he always answers; in the group, thanks/emoji and members
chatting with each other are left alone, but questions, mentions of him and replies to him get an
answer. Ad/partnership requests get a polite answer and an alert in Toshmat aka's group.

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

from .agents import AGENTS, BOTS, USERNAMES, note, say
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
            "reply": {"type": "string", "description": "The answer: 1-3 short, simple sentences (max ~300 characters), "
                                                       "in the same language and script as the person (Uzbek Latin, "
                                                       "Uzbek Cyrillic, Russian or English). Empty if ignore."},
            "topic": {"type": "string", "description": "3-6 words in Uzbek: what it was about (for the log)."},
        },
        "required": ["action", "reply", "topic"],
    },
}

RULES = (
    f"You are {NAME}, the friendly community helper of the EduGrants Telegram channel (@EduGrandsUz), which posts "
    "grants, olympiads, summer schools and other opportunities for young people in Uzbekistan (mostly ages 12-20). "
    "HOW TO WRITE: short and simple. Most readers are school pupils, so write like you talk to a 15-year-old: "
    "1-3 short sentences, about 300 characters at most. Simple everyday words, no official or bureaucratic "
    "language, no long lists, no copying whole paragraphs from the page. Answer exactly what was asked first, "
    "then (if useful) one link. Politely with 'siz', warm like a helpful older brother, at most one emoji. "
    "If they need more details, give the one key fact and send them to the link instead of explaining everything. "
    "STRICT RULES: use ONLY facts from the posts given to you. Never invent or guess a deadline, age limit, fee, "
    "country rule or result date. If the posts don't answer it, say so honestly and point to the official "
    "registration link in the post. Never promise someone will be accepted. Don't discuss politics or religion. "
    "Don't share any admin's personal contacts. When you mention one of our posts, give its t.me link. "
    "Ad, sponsorship and partnership requests: thank them, say the admins will get back to them, action=escalate. "
    "OFFICIAL PAGES: when the organiser's official page text is given, it has the full details (requirements, "
    "documents, fees, dates); use it for anything the short post leaves out, and if it disagrees with the post, "
    "trust the official page and say the details may have been updated. "
    "General questions (how to write a motivation letter, what IELTS is, how to prepare) may get short general "
    "advice, but every specific fact about a particular programme must come from the posts or official pages. "
    "In a PRIVATE CHAT never choose ignore: greet back, answer, or say what you can help with. "
    "In the GROUP, choose ignore for members chatting with each other, thanks, emoji and anything not asking "
    "about studies, grants or this channel; answer real questions."
)

PAGE_CHARS = 5000                 # how much of an official page goes to Claude (about 1,500 tokens)
_PAGE_FAIL_RETRY_HOURS = 6


def official_page(db, url: str | None, chars: int = PAGE_CHARS) -> str:
    """Text of a post's official page ("Havola"), from the cache when it is fresh (3 days)."""
    if not url:
        return ""
    row = db.cached_page(url)
    if row is not None and (row["ok"] or db.cached_page(url, _PAGE_FAIL_RETRY_HOURS) is not None):
        return (row["text"] or "")[:chars]
    try:
        from .fetch import fetch_page
        text = (fetch_page(url).text or "").strip()
    except Exception as e:
        log.info("official page %s not readable: %s", url, e)
        db.cache_page(url, "", False)
        return ""
    db.cache_page(url, text, len(text) > 100)
    return text[:chars]


def message_links(m) -> list[str]:
    """All links in a Telegram message, including hidden ones behind words like "Havola"."""
    html = getattr(m, "html_text", None) or ""
    plain = (getattr(m, "text", None) or getattr(m, "caption", None) or "")
    return re.findall(r'href="([^"]+)"', html) + re.findall(r"https?://[^\s<>\")]+", plain)


def page_block(db, url: str | None, chars: int = PAGE_CHARS) -> str:
    text = official_page(db, url, chars)
    if not text:
        return f"\n(Official page {url} could not be read; point people to it for details.)" if url else ""
    return f"\n\nOFFICIAL PAGE ({url}):\n{text}"

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
    for i, r in enumerate(relevant_posts(db, question)):
        status = "OPEN" if (r["deadline"] or "") >= today else "CLOSED (deadline passed)"
        page = page_block(db, r["link"], 3000) if i < 2 and status == "OPEN" else ""   # the 2 best open ones
        parts.append(f"--- Post {post_link(r['msg_id'])} [{status}, deadline {r['deadline']}]\n{r['text'][:1500]}"
                     + page)
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
                     + f"\n\nThis message came as {where}.\n\n{context}", message, REPLY_TOOL, 500)


MAX_REPLY = 450


def shorten(text: str, limit: int = MAX_REPLY) -> str:
    """Keeps answers short even if Claude writes too much: cut at a sentence end, keep the first link."""
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "), cut.rfind(".\n"))
    short = cut[:end + 1] if end > limit // 3 else cut.rsplit(" ", 1)[0] + "…"
    links = re.findall(r"https?://\S+", text)
    if links and links[0] not in short:
        short += f"\n{links[0]}"
    return short


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
        if m.chat.type == "private":
            raise                                # on_dm sends a polite fallback, a DM never goes unanswered
        log.exception("community answer failed")
        note("community", "error", f"Javob bera olmadim ({where}): {e}")
        return
    who = m.from_user.full_name if m.from_user else "?"
    if (d.get("action") == "ignore" or not (d.get("reply") or "").strip()) and m.chat.type == "private":
        d = {"action": "reply", "topic": d.get("topic") or "salomlashish",
             "reply": d.get("reply") or (f"Assalomu alaykum! Men {NAME}, EduGrants yordamchisiman 🙂 Grantlar, "
                                         "tanlovlar va dasturlar haqida savolingizni yozing: muddati, kimlar "
                                         "qatnasha oladi, qanday topshiriladi.")}
    if d.get("action") == "ignore" or not (d.get("reply") or "").strip():
        note("community", "info", f"E'tiborsiz qoldirildi ({where}): {text[:80]}")
        return
    d["reply"] = shorten(d["reply"])
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
    who = m.from_user.full_name if m.from_user else "?"
    note("community", "info", f"Shaxsiy xabar keldi: {who}: {(m.text or '')[:80]}")
    try:
        context = await asyncio.to_thread(dm_context, _db(), m.text)
        await answer(m, context, "shaxsiy xabar")
    except Exception as e:                      # never leave a person without any answer
        log.exception("DM answer failed")
        note("community", "error", f"Shaxsiy xabarga javob bera olmadim: {type(e).__name__}: {e}")
        try:
            await m.reply("Kechirasiz, hozir javob bera olmadim 🙏 Birozdan so'ng qayta yozib ko'ring. "
                          f"Barcha imkoniyatlar: https://t.me/{settings.channel_handle.lstrip('@')}")
        except Exception:
            log.exception("fallback reply failed")


# --------------------------------------------------------------------------- the comments group
QUESTION = re.compile(r"\?|\b(qanday|qachon|qaysi|nima|nimalar|necha|qayerda|kim|bormi|bo'ladimi|mumkinmi|kerakmi|"
                      r"qanaqa|qancha|как|когда|где|сколько|можно|нужно|какие|how|when|where|what|which|can)\b",
                      re.IGNORECASE)


def _addressed_to_him(m) -> bool:
    """He was mentioned, or someone replied to one of his messages."""
    me = BOTS.get("community")
    root = m.reply_to_message
    if root is not None and root.from_user is not None and me is not None \
            and getattr(root.from_user, "id", None) == getattr(me, "id", object()):
        return True
    name = USERNAMES.get("community")
    text = (m.text or m.caption or "").lower()
    return bool(name and f"@{name.lower()}" in text) or NAME.lower() in text


@router.message(F.chat.type.in_({"group", "supergroup"}))
async def on_comment(m: Message):
    if m.chat.id == settings.admin_chat_id:
        return                                   # the agent group is Toshmat aka's
    db = _db()
    if m.is_automatic_forward:                   # our channel post arriving in the comments group
        text = m.text or m.caption or ""
        from .weekly import havola
        link = havola(message_links(m))
        if not link:
            known = db.post_by_text(text)
            link = known["link"] if known else None
        db.save_thread(m.chat.id, m.message_id, text, link)
        if link:                                 # read the official page now, so the first answer is quick
            await asyncio.to_thread(official_page, db, link)
        return
    if m.from_user is None or m.from_user.is_bot or m.sender_chat is not None:
        return                                   # bots, the channel itself, anonymous admins
    text = (m.text or m.caption or "").strip()
    if not text:
        return
    root = m.reply_to_message
    post, link = None, None
    if root is not None and root.is_automatic_forward:
        post = root.text or root.caption
        from .weekly import havola
        link = havola(message_links(root))
    if post is None and m.message_thread_id:
        post = db.thread_text(m.chat.id, m.message_thread_id)
        link = db.thread_link(m.chat.id, m.message_thread_id)
    if post:
        if not link:
            known = db.post_by_text(post)
            link = known["link"] if known else None
        page = await asyncio.to_thread(page_block, db, link)
        await answer(m, f"Today is {date.today().isoformat()}.\n\nTHE POST BEING COMMENTED ON:\n{post[:3000]}{page}",
                     "post ostidagi izoh")
        return
    # a message in the group that isn't under one of our posts
    direct = _addressed_to_him(m)
    if not direct and not QUESTION.search(text):
        return                                   # members chatting: don't spend anything on it
    context = await asyncio.to_thread(dm_context, db, text)
    await answer(m, context, "guruhdagi savol (unga murojaat qilingan)" if direct else
                 "guruhdagi umumiy savol (a.zolar o'zaro gaplashayotgan bo'lsa ignore)")


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
    read = 0
    for r in db.open_channel_posts(date.today().isoformat(), 80):     # official pages of what's still open
        if r["link"] and db.cached_page(r["link"], 60) is None:
            if await asyncio.to_thread(official_page, db, r["link"]):
                read += 1
    open_now = len(db.open_channel_posts(date.today().isoformat(), 1000))
    db.update_task(task_id, "done", {"new_posts": new, "open": open_now, "pages": read})
    note("community", "done", f"Bilim bazasi yangilandi: {new} ta yangi post, {open_now} ta dastur hali ochiq, "
                              f"{read} ta rasmiy sahifa o'qildi", task_id)
