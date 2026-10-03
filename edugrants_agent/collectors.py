"""Where grants come from.

Every collector only *finds candidates* (a title + a link). It never decides
whether something is good; that happens later in the pipeline. This is the fix
for the old agent: discovery is deterministic crawling of sources you chose,
not an AI "searching the web".
"""
from __future__ import annotations

import asyncio
import email
import json
import imaplib
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.header import decode_header, make_header
from calendar import timegm

import feedparser
from bs4 import BeautifulSoup

from .config import settings
from .db import DB
from .dedupe import canonical_url, normalize_text, normalize_title
from .fetch import FEED_READER_UA, extract_links, fetch_page, fetch_raw

log = logging.getLogger(__name__)


@dataclass
class Candidate:
    source: str
    url: str
    title: str
    summary: str = ""
    published_at: str | None = None
    # Set when the unique key must differ from the URL (e.g. a watched page that changed)
    key: str | None = None
    # Set when this candidate is a programme the channel posted before (history watch)
    history_id: int | None = None


def _strip_html(s: str) -> str:
    return " ".join(BeautifulSoup(s or "", "lxml").get_text(" ").split())


def _skip(title: str, skip_keywords: list[str]) -> bool:
    t = title.lower()
    return any(k.lower() in t for k in skip_keywords)


# ---------------------------------------------------------------------------
RSS_ACCEPT = "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5"


def _looks_like_web_page(raw: str) -> bool:
    head = raw[:3000].lower()
    return "<html" in head and "<rss" not in head and "<feed" not in head


def _lenient_entries(raw: str) -> list[dict]:
    """When one malformed post breaks strict XML parsing, read the items with a forgiving parser."""
    soup = BeautifulSoup(raw, "xml")
    out = []
    for it in soup.find_all("item"):
        def text(tag):
            el = it.find(tag)
            return el.get_text(strip=True) if el else ""
        published = None
        if text("pubDate"):
            from email.utils import parsedate_to_datetime
            try:
                published = parsedate_to_datetime(text("pubDate")).timetuple()
            except (TypeError, ValueError):
                pass
        out.append({"title": text("title"), "link": text("link"), "summary": text("description"),
                    "published_parsed": published})
    return out


def _parse_feed(raw: str) -> list:
    feed = feedparser.parse(raw.encode())
    return feed.entries or _lenient_entries(raw)


def wordpress_entries(url: str) -> list[dict]:
    """Most opportunity sites run WordPress, which also lists posts as JSON at /wp-json/.
    Used when the RSS feed is blocked or broken."""
    import json
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    api = f"{parts.scheme}://{parts.netloc}/wp-json/wp/v2/posts?per_page=20&_fields=date_gmt,link,title,excerpt"
    raw, _ = fetch_raw(api)
    posts = json.loads(raw)
    out = []
    for post in posts:
        published = None
        if post.get("date_gmt"):
            published = datetime.fromisoformat(post["date_gmt"]).replace(tzinfo=timezone.utc).timetuple()
        out.append({"title": (post.get("title") or {}).get("rendered", ""), "link": post.get("link"),
                    "summary": (post.get("excerpt") or {}).get("rendered", ""), "published_parsed": published})
    return out


def fetch_feed(url: str) -> list:
    """RSS as a normal browser, then as a feed reader, then WordPress JSON. Says why if all fail."""
    problems = []
    for user_agent in (None, FEED_READER_UA):
        try:
            raw, _ = fetch_raw(url, user_agent=user_agent)
        except Exception as e:
            problems.append(f"{'feed reader' if user_agent else 'browser'}: {type(e).__name__} {str(e)[:80]}")
            continue
        if _looks_like_web_page(raw):
            problems.append("got a web page instead of the feed (bot protection)")
            continue
        entries = _parse_feed(raw)
        if entries:
            return entries
        problems.append("feed could not be read")
    try:
        entries = wordpress_entries(url)
        if entries:
            return entries
        problems.append("WordPress JSON: no posts")
    except Exception as e:
        problems.append(f"WordPress JSON: {type(e).__name__} {str(e)[:80]}")
    raise RuntimeError("; ".join(problems))


def collect_rss(src: dict, skip_keywords: list[str], db: DB | None = None) -> list[Candidate]:
    entries = fetch_feed(src["url"])
    max_age = timedelta(days=int(src.get("max_age_days", 14)))
    now = datetime.now(timezone.utc)
    out = []
    for e in entries:
        title = _strip_html(e.get("title", ""))
        link = e.get("link")
        if not title or not link or _skip(title, skip_keywords):
            continue
        published = None
        if e.get("published_parsed"):
            dt = datetime.fromtimestamp(timegm(e.get("published_parsed")), tz=timezone.utc)
            if now - dt > max_age:
                continue
            published = dt.strftime("%Y-%m-%d %H:%M:%S")
        summary = _strip_html(e.get("summary", ""))[:1500]
        if src.get("role") == "competitor" and db is not None:
            db.add_competitor_post(src["name"], canonical_url(link), published or "", title,
                                   normalize_text(title + " " + summary), [link])
            continue
        out.append(Candidate(source=src["name"], url=link, title=title, summary=summary, published_at=published))
    return out


def collect_page_watch(src: dict, db: DB) -> list[Candidate]:
    """Official pages of programmes that repeat every year. When the page text changes
    (new intake opens, deadline updated) it becomes a candidate again."""
    since = db.page_hours_since_check(src["url"])
    if since is not None and since < float(src.get("every_hours", 24)):
        return []
    page = fetch_page(src["url"])
    h = page.text_hash
    old = db.page_hash(src["url"])
    db.set_page_hash(src["url"], h)
    if not old or old == h:  # first look = baseline only; a later change means a new round
        return []
    return [Candidate(
        source=src["name"], url=src["url"], title=src.get("title", src["name"]),
        summary="Rasmiy sahifa yangilandi (official page changed). " + page.text[:1200],
        key=f"{canonical_url(src['url'])}#watch-{h[:12]}",
    )]


def due_for_watch(post_dates: list[str], today: date, before: int, after: int) -> bool:
    """True when today is within [anniversary - before, anniversary + after] days of any
    earlier posting. Programmes come back around the same time every year."""
    for ds in post_dates:
        d = date.fromisoformat(ds)
        for year in (today.year - 1, today.year, today.year + 1):
            try:
                ann = d.replace(year=year)
            except ValueError:  # 29 February
                ann = d.replace(year=year, day=28)
            if ann <= d:  # only future anniversaries of a posting
                continue
            if -after <= (ann - today).days <= before:
                return True
    return False


def collect_history_watch(src: dict, db: DB, fetch=fetch_page) -> list[Candidate]:
    """Re-checks official pages of programmes the channel posted in earlier years, around
    the time of year they were posted. A changed page means a possible new round."""
    from .history import resolve_official_from_edugrants

    today = date.today()
    recent_cut = (today - timedelta(days=settings.recent_post_days)).isoformat()
    rows = [r for r in db.history_rows()
            if r["last_posted"] < recent_cut and not r["excluded"]
            and due_for_watch(json.loads(r["post_dates"] or "[]"), today,
                              int(src.get("days_before", 75)), int(src.get("days_after", 45)))]
    rows.sort(key=lambda r: (-(r["times_posted"] or 1), -(r["reactions_max"] or 0)))
    out: list[Candidate] = []
    checked = 0
    for r in rows:
        if checked >= int(src.get("max_per_run", 80)):
            break
        url = r["official_url"]
        if url is None and r["edugrants_url"]:
            try:
                url = resolve_official_from_edugrants(fetch, r["edugrants_url"])
            except Exception as e:
                log.info("could not resolve %s: %s", r["edugrants_url"], e)
            db.history_set_official(r["id"], url)  # "" means tried and none found
        if not url:
            continue
        since = db.page_hours_since_check(url)
        if since is not None and since < float(src.get("every_hours", 24)):
            continue
        checked += 1
        try:
            page = fetch(url)
        except Exception as e:
            log.info("watch %s failed: %s", url, e)
            db.set_page_hash(url, db.page_hash(url) or "")  # still counts as checked today
            continue
        old = db.page_hash(url)
        db.set_page_hash(url, page.text_hash)
        if not old or old == page.text_hash:  # first look = baseline only
            continue
        out.append(Candidate(
            source="history-watch", url=url, title=r["title"], history_id=r["id"],
            summary=f"Kanalda {r['times_posted']} marta joylangan, oxirgi marta {r['last_posted']}. " + page.text[:1000],
            key=f"{canonical_url(url)}#watch-{page.text_hash[:12]}",
        ))
        if len(out) >= int(src.get("max_candidates", 5)):  # never let old programmes crowd out new finds
            break
    return out


def tg_client():
    """Telethon client from a string session (env) or a session file."""
    from telethon import TelegramClient  # installed from requirements.txt
    from telethon.sessions import StringSession

    if not (settings.tg_api_id and settings.tg_api_hash):
        raise RuntimeError("TG_API_ID / TG_API_HASH not set (get them at my.telegram.org)")
    session = StringSession(settings.tg_string_session) if settings.tg_string_session else settings.tg_session
    return TelegramClient(session, int(settings.tg_api_id), settings.tg_api_hash)


def message_urls(msg) -> list[str]:
    """Links in a post: hidden text links first (that's where "Havola"-style links live), then raw URLs."""
    text = msg.message or ""
    urls = [getattr(e, "url", None) for e in (getattr(msg, "entities", None) or [])]
    urls = [u for u in urls if u] + re.findall(r"https?://[^\s)\]]+", text)
    return list(dict.fromkeys(u.rstrip(".,") for u in urls))


async def read_channels(client, channels: list[str], db: DB, limit: int, max_age_days: float, from_start: bool = False):
    """Yields (handle, message) for posts newer than the last run and not older than max_age_days
    (from_start: ignore the last run, e.g. to fill the Monday list once)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    for channel in channels:
        handle = channel.lstrip("@")
        key = f"tg:{handle.lower()}"
        last_id = 0 if from_start else int(db.page_hash(key) or 0)
        newest = last_id
        async for msg in client.iter_messages(channel, limit=limit, min_id=last_id):
            newest = max(newest, msg.id)
            if msg.date and msg.date < cutoff:
                break  # newest first, so everything after this is older
            yield handle, msg
        db.set_page_hash(key, str(newest))


def handle_telegram_posts(src: dict, db: DB, posts, skip_keywords: list[str]) -> list[Candidate]:
    """posts: list of (handle, message). What happens depends on the source's role:
    source     -> candidates (the international channels you translate from)
    competitor -> remembered, so cards can say whether another Uzbek channel already posted it
    own        -> your own channel: keeps the 'already posted' history current without re-exporting
    """
    from .history import OPP_RE, title_of

    role = src.get("role", "source")
    out: list[Candidate] = []
    for handle, msg in posts:
        text = (msg.message or "").strip()
        if len(text) < 60:
            continue
        title = title_of(text)
        urls = [u for u in message_urls(msg) if "t.me/" not in u]
        post_url = f"https://t.me/{handle}/{msg.id}"
        when = msg.date.strftime("%Y-%m-%d %H:%M:%S") if msg.date else None
        if role == "competitor":
            db.add_competitor_post("@" + handle, canonical_url(post_url), when or "", title,
                                   normalize_text(text), urls)
        elif role == "own":
            from .weekly import remember_post
            from .weekly import havola
            remember_post(db, msg.id, msg.date.replace(tzinfo=None) if msg.date else None, text,
                          havola(message_urls(msg)))     # for the Monday list and GrantBek
            if OPP_RE.search(text):
                db.history_mark_posted(normalize_title(title), title, (when or "")[:10],
                                       next((u for u in urls if "edugrants.uz" not in u), None))
        else:
            if _skip(title, skip_keywords):
                continue
            out.append(Candidate(source=f"@{handle}", url=urls[0] if urls else post_url, title=title,
                                 summary=text[:2000], published_at=when, key=canonical_url(post_url)))
    return out


def collect_telegram(src: dict, db: DB, skip_keywords: list[str], client_factory=None) -> list[Candidate]:
    # Our own channel, the first time: read back 60 days so the Monday list knows recent deadlines
    backfill = src.get("role") == "own" and not db.get_meta("own_channel_backfilled")

    async def run():
        posts = []
        async with (client_factory or tg_client)() as client:
            async for item in read_channels(client, src["channels"], db,
                                            400 if backfill else int(src.get("limit", 50)),
                                            60 if backfill else float(src.get("max_age_days", 2)),
                                            from_start=backfill):
                posts.append(item)
        return posts

    out = handle_telegram_posts(src, db, asyncio.run(run()), skip_keywords)
    if backfill:
        db.set_meta("own_channel_backfilled", True)
    return out


def collect_imap(src: dict, skip_keywords: list[str]) -> list[Candidate]:
    """Reads unread newsletters from a dedicated inbox and turns their links into candidates."""
    if not (settings.imap_host and settings.imap_user and settings.imap_password):
        raise RuntimeError("IMAP_* not set; skipping newsletter inbox")
    bad = re.compile(r"unsubscribe|preferences|view.*browser|privacy|mailchimp|list-manage|sendgrid|manage", re.I)
    out: list[Candidate] = []
    box = imaplib.IMAP4_SSL(settings.imap_host)
    try:
        box.login(settings.imap_user, settings.imap_password)
        box.select(src.get("folder", "INBOX"))
        _, data = box.search(None, "UNSEEN")
        for num in data[0].split()[: int(src.get("limit", 30))]:
            _, msg_data = box.fetch(num, "(RFC822)")
            msg = email.message_from_bytes(msg_data[0][1])
            sender = str(make_header(decode_header(msg.get("From", ""))))
            html = next((p.get_payload(decode=True).decode(p.get_content_charset() or "utf-8", "replace")
                         for p in msg.walk() if p.get_content_type() == "text/html"), None)
            if not html:
                continue
            for text, href in extract_links(html, "https://mail.invalid/", limit=80):
                if len(text) < 20 or bad.search(text) or bad.search(href) or _skip(text, skip_keywords):
                    continue
                out.append(Candidate(source=f"{src['name']}:{sender[:40]}", url=href, title=text))
    finally:
        try:
            box.logout()
        except Exception:
            pass
    return out


# ---------------------------------------------------------------------------
def collect_all(db: DB, config: dict, fast_only: bool = False) -> list[Candidate]:
    """fast_only: just the sources marked `fast: true` (Telegram), checked every FAST_EVERY_MINUTES."""
    skip = config.get("skip_title_keywords", [])
    found: list[Candidate] = []
    for src in config.get("sources", []):
        if not src.get("enabled", True) or (fast_only and not src.get("fast")):
            continue
        name, kind = src["name"], src["type"]
        try:
            if kind == "rss":
                items = collect_rss(src, skip, db)
            elif kind == "page_watch":
                items = collect_page_watch(src, db)
            elif kind == "history_watch":
                items = collect_history_watch(src, db)
            elif kind == "telegram":
                items = collect_telegram(src, db, skip)
            elif kind == "imap":
                items = collect_imap(src, skip)
            else:
                raise ValueError(f"unknown source type {kind}")
            db.source_ok(name, len(items))
            found.extend(items)
            log.info("source %s: %d candidates", name, len(items))
        except Exception as e:  # one broken source must never stop the others
            log.warning("source %s failed: %s", name, e)
            db.source_error(name, f"{type(e).__name__}: {e}")
    return found
