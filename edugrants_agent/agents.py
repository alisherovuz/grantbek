"""The agent team in the Telegram group: who says what, and from which bot.

Each agent can have its own Telegram bot (its own name and avatar in the group):
    BOT_TOKEN          -> Manager (also the bot that posts to the channel and opens the dashboard)
    FINDER_BOT_TOKEN   -> Finder
    WRITER_BOT_TOKEN   -> Writer
An agent without its own token speaks through the Manager's bot with a name tag ("🔎 Finder: ...").

Telegram doesn't let bots read each other's messages, so agents never "chat" through the group:
they pass work through the task board in the database, and the group shows you what happens.
Everything said is also written to the event log (dashboard > Tizim, and the daily report).
"""
from __future__ import annotations

import logging
import os
import re

from .config import settings

log = logging.getLogger(__name__)

AGENTS = {
    "manager": {"emoji": "🧭", "name": "Toshmat aka", "env": "BOT_TOKEN"},        # the boss: plans, checks, reports
    "finder": {"emoji": "🔎", "name": "Ergash", "env": "FINDER_BOT_TOKEN"},        # Toshmat's sidekick, finds grants
    "writer": {"emoji": "🖋", "name": "Mirzo", "env": "WRITER_BOT_TOKEN"},          # the scribe: posts, Monday list
    "community": {"emoji": "💬", "name": "GrantBek", "env": "COMMUNITY_BOT_TOKEN"},  # answers DMs and comments
}
# Coming later: "Jarchi Jo'ra" 📣 (publisher: schedules and posts to the channel)

BOTS: dict = {}       # agent -> aiogram Bot (filled by setup_bots; agents without a token are missing)
PROBLEMS: dict = {}   # agent -> why its bot doesn't work (bad token), shown on the dashboard
USERNAMES: dict = {}  # agent -> its bot's @username
_DB = {"get": None}   # function returning the DB (set by the bot at startup)


def token_for(agent: str) -> str | None:
    if agent == "manager":
        return settings.bot_token
    value = (os.getenv(AGENTS[agent]["env"]) or "").strip()
    return value or None


def setup_bots(make_bot) -> list:
    """Creates one Bot per distinct token. Returns the bots to poll (the Manager first)."""
    BOTS.clear()
    PROBLEMS.clear()
    USERNAMES.clear()
    by_token = {}
    for agent in AGENTS:
        tok = token_for(agent)
        if agent == "community" and tok and tok == settings.bot_token:
            log.error("COMMUNITY_BOT_TOKEN is the same as BOT_TOKEN: GrantBek needs a bot of his own (public DMs "
                      "must not reach the manager). GrantBek is off until it gets its own token.")
            continue
        if tok:
            if tok not in by_token:
                try:
                    by_token[tok] = make_bot(tok)
                except Exception as e:      # e.g. a token pasted with extra text: not even shaped like a token
                    PROBLEMS[agent] = f"{AGENTS[agent]['env']} tokenga o'xshamaydi ({type(e).__name__}). " \
                                      "BotFather'dan faqat '123456789:AA...' qismini nusxa oling."
                    log.error("%s: %s", agent, PROBLEMS[agent])
                    continue
            BOTS[agent] = by_token[tok]
    return list(dict.fromkeys(BOTS.values()))


async def check_bots() -> None:
    """Asks Telegram about every token once at startup. A token Telegram rejects is reported (logs,
    dashboard, and the group) instead of crashing the whole team; that agent falls back to Toshmat aka."""
    seen = {}
    for agent, bot in list(BOTS.items()):
        if id(bot) not in seen:
            try:
                me = await bot.get_me()
                first = getattr(me, "first_name", None)
                seen[id(bot)] = ("ok", (me.username, first.strip() if isinstance(first, str) and first.strip() else None))
            except Exception as e:
                seen[id(bot)] = ("bad", f"{type(e).__name__}: {e}"[:200])
        status, info = seen[id(bot)]
        if status == "ok":
            USERNAMES[agent] = info[0]
            # The name the group shows is the bot's name in Telegram: use it, so Toshmat aka never
            # talks about "Ergash" while the group shows "Ergash Topqir".
            if info[1] and has_own_bot(agent):
                AGENTS[agent]["name"] = info[1]
            continue
        PROBLEMS[agent] = (f"Telegram {AGENTS[agent]['env']} ni qabul qilmadi: token noto'g'ri, eskirgan yoki "
                           f"BotFather'da bekor qilingan. ({info})")
        log.error("%s: %s", agent, PROBLEMS[agent])
        if agent != "manager":
            BOTS.pop(agent, None)


def bot_for(agent: str, fallback=None):
    """The bot an agent speaks through: its own, else the Manager's, else whatever the caller has."""
    return BOTS.get(agent) or BOTS.get("manager") or fallback


def has_own_bot(agent: str) -> bool:
    return agent == "manager" or (agent in BOTS and BOTS.get(agent) is not BOTS.get("manager"))


def is_manager_bot(bot) -> bool:
    """Only the Manager's bot handles typed messages, so one message never gets several answers."""
    mgr = BOTS.get("manager")
    return mgr is None or bot is mgr or getattr(bot, "id", None) == getattr(mgr, "id", object())


def tagged(agent: str, text: str) -> str:
    if has_own_bot(agent):
        return text
    a = AGENTS[agent]
    return f"{a['emoji']} <b>{a['name']}</b>: {text}"


def plain(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html)


def note(agent: str, kind: str, text: str, task_id: int | None = None) -> None:
    """Writes to the event log only (no Telegram message)."""
    get = _DB["get"]
    if get:
        try:
            get().log_event(agent, kind, plain(text), task_id)
        except Exception:
            log.exception("event log failed")


async def say(agent: str, text: str, kind: str = "info", reply_markup=None, reply_to: int | None = None,
              task_id: int | None = None, fallback_bot=None, log_it: bool = True):
    """An agent posts in the agent group (and the event log). Returns the sent message, or None."""
    if log_it:
        note(agent, kind, text, task_id)
    bot = bot_for(agent, fallback_bot)
    if bot is None or not settings.admin_chat_id:
        return None
    try:
        return await bot.send_message(settings.admin_chat_id, tagged(agent, text), reply_markup=reply_markup,
                                      reply_to_message_id=reply_to)
    except Exception as e:
        log.error("%s could not post in the group: %s", agent, e)
        return None
