"""Learns from the channel's own history (a Telegram Desktop export).

Gives the finder three things:
1. every programme ever posted, so it never suggests something you already posted recently
2. which programmes come back every year, and when, so it watches their official pages
   around that time and catches the new round the day it opens
3. a taste profile (types, ages, countries, what gets reactions) used to judge new finds
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from statistics import median

from bs4 import BeautifulSoup

from .db import DB
from .dedupe import domain, normalize_title

OPP_RE = re.compile(r"Davlat\s*:|so.nggi (muddati|kuni|sanasi)|Ro.yxatdan o.tish", re.I)
NOT_OFFICIAL = re.compile(
    r"(t\.me|telegram\.|edugrants\.uz|forms\.gle|docs\.google|youtu|airtable|linktr\.ee|instagram|"
    r"facebook|bit\.ly|tinyurl|wa\.me|x\.com|twitter)", re.I)
FIELD = lambda name: re.compile(name + r"\s*:\s*\**\s*([^\n]+)", re.I)  # noqa: E731
F_COUNTRY, F_AGE = FIELD(r"Davlat"), FIELD(r"Yosh toifasi")
F_FUND, F_FORMAT = FIELD(r"Moliyaviy ta.minot"), FIELD(r"(?:Dastur|Tanlov) shakli")


# --------------------------------------------------------------------------- parsing
def parse_export(path: str | Path) -> list[dict]:
    """Supports both export formats: messages.html (possibly several files) and result.json."""
    path = Path(path)
    if path.suffix == ".json":
        return _parse_json(path)
    files = [path]
    if path.name == "messages.html" or path.name.endswith("messages.html"):
        files += sorted(path.parent.glob("messages[0-9]*.html"), key=lambda p: int(re.sub(r"\D", "", p.stem) or 0))
    posts = []
    for f in dict.fromkeys(files):
        posts += _parse_html(f)
    return posts


def _parse_html(path: Path) -> list[dict]:
    soup = BeautifulSoup(path.read_text(encoding="utf-8"), "lxml")
    out = []
    for m in soup.select("div.message.default"):
        t = m.select_one("div.text")
        if not t:
            continue
        d = m.select_one("div.date")
        links = [a["href"] for a in t.find_all("a", href=True)]
        for br in t.find_all("br"):
            br.replace_with("\n")
        reactions = sum(int(c.get_text()) for c in m.select("span.reaction span.count") if c.get_text().strip().isdigit())
        date = None
        if d and d.has_attr("title"):
            date = datetime.strptime(d["title"].split(" UTC")[0], "%d %B %Y, %H:%M:%S")
        out.append({"id": m.get("id"), "date": date, "text": t.get_text().strip(), "links": links, "reactions": reactions})
    return out


def _parse_json(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for m in data.get("messages", []):
        if m.get("type") != "message":
            continue
        parts = m.get("text_entities") or []
        text = "".join(p.get("text", "") for p in parts) if parts else (m.get("text") if isinstance(m.get("text"), str) else "")
        links = [p.get("href") or p.get("text") for p in parts if p.get("type") in ("text_link", "link")]
        reactions = sum(r.get("count", 0) for r in m.get("reactions", []) or [])
        out.append({"id": m.get("id"), "date": datetime.fromisoformat(m["date"]), "text": text.strip(),
                    "links": [l for l in links if l], "reactions": reactions})
    return out


# --------------------------------------------------------------------------- analysis
def title_of(text: str) -> str:
    first = next((l for l in text.splitlines() if l.strip()), "")
    return re.sub(r"^[^\w\"“'«]+", "", first).strip()[:200]


def categorize(text: str) -> str:
    country = (F_COUNTRY.search(text) or [None, ""])[1]
    if re.search(r"o.?.?zbekiston", country, re.I):
        return "domestic"
    if re.search(r"Tanlov shakli", text):
        return "competition"
    if F_FUND.search(text) and re.search(r"to.?liq", F_FUND.search(text)[1], re.I):
        return "funded"
    fmt = F_FORMAT.search(text)
    if fmt and re.search(r"onl", fmt[1], re.I):
        return "online"
    return "other"


def clean(v: str | None) -> str:
    return re.sub(r"[*]", "", v or "").strip()


def build_history(posts: list[dict]) -> list[dict]:
    """Groups posts by programme (normalised title). Each programme gets a `score`: its best
    post's reactions divided by the median of that month's posts. 1.0 = typical, 1.5 = a hit.
    (The channel grew ~4x, so raw reaction counts would make every 2024 post look like a flop.)"""
    by_month: dict[str, list[int]] = defaultdict(list)
    for p in posts:
        if p["date"] and OPP_RE.search(p["text"]):
            by_month[p["date"].strftime("%Y-%m")].append(p["reactions"])
    month_median = {m: max(median(v), 1) for m, v in by_month.items()}
    groups: dict[str, list[dict]] = defaultdict(list)
    for p in posts:
        if not p["date"] or not OPP_RE.search(p["text"]):
            continue
        title = title_of(p["text"])
        norm = normalize_title(title)
        if len(norm) < 4:
            continue
        groups[norm].append({**p, "title": title})
    programmes = []
    for norm, ps in groups.items():
        ps.sort(key=lambda p: p["date"])
        last = ps[-1]
        official = next((l for p in reversed(ps) for l in p["links"]
                         if l.startswith("http") and not NOT_OFFICIAL.search(l)), None)
        edugrants = next((l for p in reversed(ps) for l in p["links"] if "edugrants.uz/" in l), None)
        programmes.append({
            "norm_title": norm,
            "title": last["title"],
            "category": categorize(last["text"]),
            "country": clean((F_COUNTRY.search(last["text"]) or [None, ""])[1])[:60],
            "age": clean((F_AGE.search(last["text"]) or [None, ""])[1])[:60],
            "first_posted": ps[0]["date"].strftime("%Y-%m-%d"),
            "last_posted": last["date"].strftime("%Y-%m-%d"),
            "times_posted": len(ps),
            "post_dates": json.dumps([p["date"].strftime("%Y-%m-%d") for p in ps]),
            "official_url": official,
            "edugrants_url": edugrants,
            "reactions_max": max(p["reactions"] for p in ps),
            "score": round(max(p["reactions"] / month_median[p["date"].strftime("%Y-%m")] for p in ps), 2),
        })
    return programmes


def import_history(db: DB, path: str | Path) -> dict:
    posts = parse_export(path)
    programmes = build_history(posts)
    db.replace_history(programmes)
    return {"posts": len(posts), "opportunity_posts": sum(1 for p in posts if OPP_RE.search(p["text"])),
            "programmes": len(programmes),
            "recurring": sum(1 for p in programmes if p["times_posted"] > 1),
            "with_official_link": sum(1 for p in programmes if p["official_url"]),
            "with_edugrants_link": sum(1 for p in programmes if p["edugrants_url"] and not p["official_url"])}


# --------------------------------------------------------------------------- taste profile
TYPE_PATTERNS = {
    "essay contests": r"essay|insho|writing (contest|competition|award)",
    "fellowships": r"fellow",
    "olympiads": r"olymp|olimp",
    "hackathons": r"hackat|hakaton|xakaton",
    "camps": r"camp|oromgoh|lager",
    "summer and winter schools": r"summer|winter|yozgi|qishki",
    "forums, summits, conferences": r"forum|summit|conference|konferens|sammit|dialogue",
    "internships": r"intern|amaliyot|stajir",
    "research programmes": r"research|tadqiqot",
    "scholarships and grants": r"scholar|stipend|grant|burs",
    "exchange programmes": r"exchange|almashinuv|\bflex\b|ugrad|\buwc\b|united world college",
    "free online courses": r"kurs|course",
    "other competitions and prizes": r"competition|challenge|contest|prize|award|tanlov|musobaqa|mukofot",
}
LEVEL_PATTERNS = {"PhD / postdoc": r"phd|postdoc|doctoral|doktorantura",
                  "master's": r"master|magistratura|mba"}


def _fmt(rows) -> str:
    return "; ".join(f"{r['title'][:55]} ({r['score']:.1f})" for r in rows)


def build_profile(db: DB) -> str:
    rows = [r for r in db.history_rows() if r["score"] is not None]
    if not rows:
        return ""
    total = len(rows)
    lines = [
        f"THE CHANNEL'S VIBE, learned from its own {total} past programmes.",
        "Score = a programme's best post's reactions divided by the median of posts that month "
        "(1.0 typical, 1.3+ a hit, under 0.7 a flop).",
        "",
        "How the channel chooses (read the examples below as evidence for this):",
        "- It is selective about NAMES, not categories. Prestigious, recognisable organisers "
        "(top universities, governments, UN bodies, famous programmes) do well; small or unknown "
        "organisers flop even in a category the channel likes.",
        "- The core audience is school pupils (13-18) and bachelor students. Programmes for "
        "PhD/postdoc or mid-career professionals are almost never posted.",
        "- It wants a clear, attractive benefit: a fully funded trip, a big prize, a top-university "
        "certificate, a famous programme on a CV. 'Certificate of participation' alone is weak.",
        "- Uzbekistan's own camps, olympiads and stipends perform best of all.",
        "",
        "By type (count, median score; hits and flops with their scores):",
    ]
    for name, rx in TYPE_PATTERNS.items():
        m = [r for r in rows if re.search(rx, r["title"], re.I)]
        if len(m) < 3:
            continue
        m.sort(key=lambda r: -r["score"])
        lines.append(f"- {name}: {len(m)} programmes ({100 * len(m) // total}%), median score "
                     f"{median(r['score'] for r in m):.2f}")
        lines.append(f"    hits: {_fmt(m[:6])}")
        flops = [r for r in m if r["score"] < 0.8][-4:]
        if flops:
            lines.append(f"    flops: {_fmt(flops)}")
    for name, rx in LEVEL_PATTERNS.items():
        n = sum(1 for r in rows if re.search(rx, r["title"], re.I))
        lines.append(f"- aimed at {name}: {n} of {total} programmes")
    ages = Counter(r["age"] for r in rows if r["age"])
    lines.append("- most common age ranges: " + ", ".join(a for a, _ in ages.most_common(8)))
    countries = Counter(re.sub(r"[^\w' ]", "", r["country"]).strip() for r in rows if r["country"])
    lines.append("- most common countries: " + ", ".join(c for c, _ in countries.most_common(10)))
    top = sorted(rows, key=lambda r: -r["score"])[:20]
    lines.append("\nBiggest hits overall: " + _fmt(top))
    return "\n".join(lines)


def resolve_official_from_edugrants(fetch, edugrants_url: str) -> str | None:
    """Posts since Feb 2026 link to edugrants.uz. Read that page and take the organiser link."""
    page = fetch(edugrants_url)
    for text, href in page.links:
        if href.startswith("http") and not NOT_OFFICIAL.search(href) and domain(href) != "edugrants.uz":
            return href
    return None
