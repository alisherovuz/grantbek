"""Turns structured data into the exact @EduGrandsUz post and the platform form payload.

The model writes the words; this file controls the layout, so every post has
the same shape no matter what the model does.
"""
from __future__ import annotations

from datetime import date
from html import escape

UZ_MONTHS = ["yanvar", "fevral", "mart", "aprel", "may", "iyun", "iyul",
             "avgust", "sentyabr", "oktyabr", "noyabr", "dekabr"]

FUNDING_UZ = {"full": "To'liq", "partial": "Qisman", "none": "Moliyalashtirilmaydi", "unknown": "Aniqlanmagan"}
FORMAT_UZ = {"online": "Onlayn", "offline": "Oflayn", "hybrid": "Gibrid", "unknown": "Aniqlanmagan"}


def flag(iso2: str | None) -> str:
    if not iso2 or len(iso2) != 2 or not iso2.isalpha():
        return "🌍"
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in iso2.upper())


def uz_date(iso: str | None) -> str | None:
    if not iso:
        return None
    try:
        d = date.fromisoformat(iso[:10])
    except ValueError:
        return None
    return f"{d.day}-{UZ_MONTHS[d.month - 1]}"


def middle_meta_line(data: dict) -> tuple[str, str]:
    """Second meta line (label, value), the way the channel writes it:
    funded -> Moliyaviy ta'minot; competitions -> Tanlov shakli; otherwise -> Dastur shakli."""
    fmt = data.get("format", "unknown")
    funding = data.get("funding", "unknown")
    if funding in ("full", "partial"):
        return "Moliyaviy ta'minot", FUNDING_UZ[funding]
    if data.get("opportunity_type") in ("competition", "olympiad"):
        return "Tanlov shakli", FORMAT_UZ.get(fmt, "Onlayn")
    return "Dastur shakli", FORMAT_UZ.get(fmt, "Aniqlanmagan")


def deadline_value(data: dict) -> str:
    if data.get("deadline_type") == "rolling":
        return "Doimiy qabul"
    return uz_date(data.get("deadline")) or "aniqlanmagan"


def render_post(data: dict, written: dict, link: str, channel_handle: str) -> str:
    """Exactly the @EduGrandsUz layout (taken from the channel's real posts), as Telegram HTML:
    bold title and labels, italic description, 'Havola' as a link, linked channel handle."""
    def e(text: str, quote: bool = False) -> str:  # Telegram only needs < > & escaped
        return escape(str(text), quote=quote)

    benefits = "\n".join(f"- {e(b.strip().rstrip('.;'))}" for b in written["benefits_uz"][:4])
    label, value = middle_meta_line(data)
    handle = channel_handle.lstrip("@")
    lines = [
        f"<b>{e(written['title_uz'].strip())}</b>",
        "",
        f"<b>Davlat</b>: {e(written['country_uz'])} {flag(data.get('host_country_iso2'))}",
        f"<b>{e(label)}</b>: {e(value)}",
        f"<b>Yosh toifasi</b>: {e(written['age_category_uz'])}",
        "",
        f"<i>{e(written['description_uz'].strip())}</i>",
        "",
        "➡️<b>Imtiyozlari</b>:",
        benefits,
        "",
        f'🔗<b>Ro\'yxatdan o\'tish uchun</b>: <a href="{e(link, quote=True)}">Havola</a>',
        "",
        f"📌<b>Ro'yxatdan o'tishning so'nggi muddati</b>: {e(deadline_value(data))}",
        "",
        f'⚡️<a href="https://t.me/{e(handle, quote=True)}">@{e(handle)}</a>',
    ]
    return "\n".join(lines)


def build_platform_payload(data: dict, written: dict, official_url: str | None, link: str) -> dict:
    """Mirrors the 'Create Extracurriculars' form (step 1 Basic + step 2 Body)."""
    p = written["platform"]
    return {
        # Step 1: Basic
        "title": p["title"],
        "country": written["country_uz"],
        "official_link": official_url or link,
        "registration_link": data.get("registration_url") or link,
        "deadline_type": "rolling" if data.get("deadline_type") == "rolling" else "fixed",
        "deadline": data.get("deadline"),
        "opening_date": data.get("opening_date"),
        "imkoniyat_turi": p["imkoniyat_turi"],
        "daraja": p["daraja"],
        "moliyalashtirish": p["moliyalashtirish"],
        "format": p["format"],
        "davomiylik": p["davomiylik"],
        "ariza_tolovi": p["ariza_tolovi"],
        # Step 2: Body
        "description": p["description"],
        "eligibility": p["eligibility"],
        "benefits": p["benefits"],
        "application_process": p["application_process"],
        "additional_information": p["additional_information"],
    }


# --------------------------------------------------------------------------- finder cards
TYPE_UZ = {"scholarship": "Stipendiya", "exchange": "Almashinuv dasturi", "summer_school": "Yozgi maktab",
           "competition": "Tanlov", "olympiad": "Olimpiada", "fellowship": "Fellowship", "conference": "Konferensiya",
           "forum": "Forum", "internship": "Amaliyot", "course": "Kurs", "grant": "Grant",
           "volunteering": "Volontyorlik", "other": "Imkoniyat"}


def cost_line(data: dict) -> str:
    if data.get("funding") == "full":
        cost = "To'liq moliyalashtirilgan"
    elif data.get("program_fee") == "free":
        cost = "Ishtirok bepul"
    elif data.get("program_fee") == "paid_with_full_aid":
        cost = "Pullik, to'liq moliyaviy yordam bor"
    else:
        cost = "Moliyalashtirish aniq emas"
    fee = {"free": "ariza bepul", "unknown": "ariza to'lovi aniq emas ❓", "paid": "ariza pullik"}.get(
        data.get("application_fee"), "")
    return f"💰 {cost} · {fee}"


def ages_text(data: dict) -> str:
    lo, hi = data.get("age_min"), data.get("age_max")
    if lo and hi:
        return f"{lo}-{hi} yosh"
    if lo:
        return f"{lo} yoshdan"
    if hi:
        return f"{hi} yoshgacha"
    levels = {"high_school": "o'quvchilar", "bachelor": "bakalavr", "master": "magistratura", "phd": "PhD",
              "young_professional": "yosh mutaxassislar", "any": "barcha uchun"}
    return ", ".join(levels.get(l, l) for l in data.get("level") or []) or "yosh aniq emas"


def ago_uz(when: str | None) -> str | None:
    if not when:
        return None
    from datetime import datetime, timezone
    try:
        t = datetime.strptime(when[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    mins = int((datetime.now(timezone.utc) - t).total_seconds() // 60)
    if mins < 60:
        return f"{max(mins, 1)} daqiqa"
    if mins < 48 * 60:
        return f"{mins // 60} soat"
    return f"{mins // 1440} kun"


def first_status(db, item, data: dict):
    """Returns (line, already_posted_by_someone). Empty line if no competitor sources are set up."""
    from .dedupe import competitor_match, normalize_title
    if not db.competitor_channels():
        return "", False
    norm = normalize_title(data.get("title") or item["title"])
    hit = competitor_match(db.recent_competitor_posts(45), item["official_canonical"], norm)
    if hit is None:
        return "🥇 O'zbek kanallarida hali yo'q", False
    return f"⚠️ {escape(hit['channel'])} {ago_uz(hit['posted_at']) or ''} oldin joylagan", True


def finder_card(db, item) -> tuple[str, bool]:
    """Card text plus whether a competitor already has it (used for ordering)."""
    import json as _json
    data = _json.loads(item["data_json"])
    hist = db.history_get(item["history_id"]) if item["history_id"] else None
    first, taken = first_status(db, item, data)
    extra = []
    age = ago_uz(item["published_at"])
    if age:
        extra.append(f"⏱ Manbada {age} oldin · {escape(item['source'])}")
    if first:
        extra.append(first)
    return render_find(item, data, hist, extra), taken


def render_find(item, data: dict, history=None, extra_lines: list[str] | None = None) -> str:
    """The card an editor sees for each find (HTML)."""
    e = lambda s: escape(str(s or ""), quote=False)  # noqa: E731
    from datetime import date as _d
    lines = [f"🆕 <b>{e(data.get('title') or item['title'])}</b>"]
    country = data.get("host_country") or ""
    lines.append(f"{flag(data.get('host_country_iso2'))} {e(country)} · {TYPE_UZ.get(data.get('opportunity_type'), 'Imkoniyat')}"
                 f" · {e({'online': 'Onlayn', 'offline': 'Oflayn', 'hybrid': 'Gibrid'}.get(data.get('format'), ''))}"
                 f" · 👤 {e(ages_text(data))}")
    lines.append(cost_line(data))
    if data.get("deadline_type") == "rolling":
        lines.append("📅 Doimiy qabul")
    elif data.get("deadline"):
        left = (_d.fromisoformat(data["deadline"][:10]) - _d.today()).days
        lines.append(f"📅 Muddat: {uz_date(data['deadline'])} ({left} kun qoldi)")
    lines.extend(extra_lines or [])
    if history is not None:
        lines.append(f"🔁 Yangi bosqich: kanalda {history['times_posted']} marta joylangan, oxirgi {history['last_posted']}, "
                     f"eng ko'p {history['reactions_max']} reaksiya")
    if item["fit_reason"] and history is None:
        lines.append(f"💡 {e(item['fit_reason'])}")
    if data.get("summary"):
        lines.append(f"\n{e(data['summary'][:400])}")
    check = "✔️ rasmiy sahifadan tekshirildi" if data.get("verified_from_official") else "⚠️ faqat agregatordan, tekshiring"
    lines.append(f"\n{check}")
    if data.get("uzbekistan_eligible") == "unclear":
        lines.append(f"❓ O'zbekiston: {e(data.get('eligible_countries'))}")
    if data.get("conflicts"):
        lines.append(f"⚠️ {e(data['conflicts'][:200])}")
    found_at = item["url"] if (item["url"] or "").startswith("http") else None   # owner-given text has no URL
    official = item["official_url"] or found_at
    links = [f'🔗 <a href="{escape(official, quote=True)}">Rasmiy sahifa</a>'] if official else []
    if data.get("registration_url") and data["registration_url"] != official:
        links.append(f'<a href="{escape(data["registration_url"], quote=True)}">Ariza</a>')
    if found_at and item["official_url"] and found_at != item["official_url"] and "#watch-" not in item["canonical_url"]:
        links.append(f'<a href="{escape(found_at, quote=True)}">Topilgan joy</a>')
    if links:
        lines.append(" · ".join(links) if official else "🔗 " + " · ".join(links))
    return "\n".join(lines)
