"""The Monday post: "🎓Bu hafta muddati tugaydigan dasturlar ro'yxati".

Every post in our own channel is remembered with its deadline (read from the
"Ro'yxatdan o'tishning so'nggi muddati: 6-oktyabr" line), its flag (from the "Davlat" line) and its
number in the channel. On Monday the Writer lists the ones whose deadline falls in this week, each
title linked to our own post about it, in exactly the channel's format.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from html import escape

UZ_MONTHS = {"yanvar": 1, "fevral": 2, "mart": 3, "aprel": 4, "may": 5, "iyun": 6, "iyul": 7, "avgust": 8,
             "sentyabr": 9, "sentabr": 9, "oktyabr": 10, "oktabr": 10, "noyabr": 11, "dekabr": 12,
             "january": 1, "february": 2, "march": 3, "april": 4, "june": 6, "july": 7, "august": 8,
             "september": 9, "october": 10, "november": 11, "december": 12}
DEADLINE_LINE = re.compile(r"so.nggi\s+(muddati|kuni|sanasi)[^\n:]*:?\s*([^\n]+)", re.I)
FLAG = re.compile(r"[\U0001F1E6-\U0001F1FF]{2}")
EMOJI = re.compile(r"[\U0001F300-\U0001FAFF☀-➿]️?")


def parse_deadline(text: str, posted: date) -> date | None:
    """'6-oktyabr', '15 noyabr 2026', '30.09.2026', '1-dekabr, 23:59' -> a date (next occurrence after posting)."""
    m = DEADLINE_LINE.search(text or "")
    if not m:
        return None
    s = m.group(2).lower()
    d = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", s)
    if d:
        try:
            return date(int(d.group(3)), int(d.group(2)), int(d.group(1)))
        except ValueError:
            return None
    d = re.search(r"(\d{1,2})\s*[-–\s]\s*([a-zʻ'`’]+)(?:[,\s]+(\d{4}))?", s)
    if not d or d.group(2) not in UZ_MONTHS:
        return None
    day, month = int(d.group(1)), UZ_MONTHS[d.group(2)]
    try:
        if d.group(3):
            return date(int(d.group(3)), month, day)
        guess = date(posted.year, month, day)
    except ValueError:
        return None
    return guess if guess >= posted - timedelta(days=30) else date(posted.year + 1, month, day)


def post_flag(text: str) -> str | None:
    for line in (text or "").splitlines():
        if re.match(r"\s*davlat\s*:", line, re.I):
            f = FLAG.search(line) or EMOJI.search(line)
            return f.group(0) if f else None
    return None


def post_title(text: str) -> str:
    for line in (text or "").splitlines():
        line = line.strip().strip("*_ ")
        if line:
            return EMOJI.sub("", line).strip(" -–:")[:150]
    return ""


def remember_post(db, msg_id: int, posted_at: datetime | None, text: str, link: str | None = None) -> bool:
    """Stores one of our channel's posts if it is an opportunity with a deadline. Returns True if stored."""
    posted = (posted_at or datetime.utcnow()).date()
    deadline = parse_deadline(text, posted)
    title = post_title(text)
    if not deadline or not title:
        return False
    db.save_channel_post(int(msg_id), posted_at.strftime("%Y-%m-%d %H:%M:%S") if posted_at else None,
                         title, post_flag(text), deadline.isoformat(), link, text[:4000])
    return True


def import_export_posts(db, path) -> int:
    """Fills the list from the Telegram export once (posts made before the agent was watching the channel)."""
    from .history import parse_export
    n = 0
    for p in parse_export(path):
        mid = re.sub(r"\D", "", str(p.get("id") or ""))
        if mid and remember_post(db, int(mid), p.get("date"), p.get("text") or ""):
            n += 1
    return n


def week_bounds(today: date) -> tuple[date, date]:
    """From today to this week's Sunday."""
    return today, today + timedelta(days=6 - today.weekday())


def build_weekly(db, handle: str, today: date | None = None) -> tuple[str | None, list]:
    """The post text (Telegram HTML) and the rows in it; (None, []) if nothing ends this week."""
    first, last = week_bounds(today or date.today())
    rows, seen = [], set()
    for r in db.channel_posts_between(first.isoformat(), last.isoformat()):
        key = re.sub(r"\W+", " ", r["title"].lower()).strip()
        if key in seen:
            continue
        seen.add(key)
        rows.append(r)
    if not rows:
        return None, []
    h = handle.lstrip("@")
    lines = ["🎓Bu hafta muddati tugaydigan dasturlar ro’yxati:", ""]
    for r in rows:
        url = f"https://t.me/{h}/{r['msg_id']}"
        lines.append(f'- <a href="{escape(url, quote=True)}">{escape(r["title"], quote=False)}</a> {r["flag"] or "🌍"}')
        lines.append("")
    lines += ["Imkoniyatdan foydalanib qoling. Tanlov o’z qo’lingizda!⚡️", "",
              f'⚡️<a href="https://t.me/{escape(h, quote=True)}">@{escape(h)}</a>']
    return "\n".join(lines), rows
