"""Bilingual (English / Uzbek) UI strings and per-user language preferences.

Usage:
    lang = await get_lang(user_id)        # 'en' or 'uz'
    text = t("welcome", lang)
"""
from __future__ import annotations

from config import settings
from db import database

SUPPORTED = ("en", "uz")

STRINGS: dict[str, dict[str, str]] = {
    "welcome": {
        "en": (
            "👋 *Welcome to GrantBek!*\n\n"
            "I help you discover educational grants and scholarships and answer "
            "questions about applying.\n\n"
            "Try these:\n"
            "• /search <keyword> — find grants\n"
            "• /categories — browse by field\n"
            "• /deadlines — what's closing soon\n"
            "• /faq — common questions\n"
            "• /help — all commands\n\n"
            "🌐 Switch language anytime with /lang"
        ),
        "uz": (
            "👋 *GrantBekka xush kelibsiz!*\n\n"
            "Men ta'lim grantlari va stipendiyalarni topishga yordam beraman hamda "
            "ariza topshirish bo'yicha savollaringizga javob beraman.\n\n"
            "Sinab ko'ring:\n"
            "• /search <so'z> — grant qidirish\n"
            "• /categories — yo'nalish bo'yicha ko'rish\n"
            "• /deadlines — muddati yaqinlashayotganlari\n"
            "• /faq — ko'p so'raladigan savollar\n"
            "• /help — barcha buyruqlar\n\n"
            "🌐 Tilni istalgan vaqtda /lang orqali o'zgartiring"
        ),
    },
    "help": {
        "en": (
            "*GrantBek commands*\n\n"
            "/start — welcome message\n"
            "/search <keyword> — quick grant search\n"
            "/categories — browse grants by field\n"
            "/deadlines — grants closing in the next 30 days\n"
            "/faq — frequently asked questions\n"
            "/find — guided step-by-step grant finder\n"
            "/lang — change language (English / Uzbek)\n"
            "/about — about GrantBek\n\n"
            "💬 You can also just ask me a question in plain words."
        ),
        "uz": (
            "*GrantBek buyruqlari*\n\n"
            "/start — boshlang'ich xabar\n"
            "/search <so'z> — tezkor grant qidirish\n"
            "/categories — yo'nalish bo'yicha grantlar\n"
            "/deadlines — 30 kun ichida tugaydigan grantlar\n"
            "/faq — ko'p so'raladigan savollar\n"
            "/find — bosqichma-bosqich grant topish\n"
            "/lang — tilni o'zgartirish (Ingliz / O'zbek)\n"
            "/about — GrantBek haqida\n\n"
            "💬 Shunchaki oddiy so'zlar bilan savol ham bera olasiz."
        ),
    },
    "about": {
        "en": (
            "*GrantBek* 🎓\n\n"
            "An open educational-grants assistant for students. I search a curated "
            "grant database and answer application questions in English and Uzbek.\n\n"
            "Found a grant that should be here? Let the channel admins know."
        ),
        "uz": (
            "*GrantBek* 🎓\n\n"
            "Talabalar uchun ochiq ta'lim grantlari yordamchisi. Men grantlar "
            "bazasidan qidiraman va ariza savollariga ingliz hamda o'zbek tilida "
            "javob beraman.\n\n"
            "Bu yerda bo'lishi kerak bo'lgan grant topdingizmi? Kanal adminlariga ayting."
        ),
    },
    "lang_prompt": {
        "en": "🌐 Choose your language:",
        "uz": "🌐 Tilni tanlang:",
    },
    "lang_set": {
        "en": "✅ Language set to English.",
        "uz": "✅ Til o'zbekchaga o'rnatildi.",
    },
    "search_usage": {
        "en": "Usage: /search <keyword>\nExample: /search engineering",
        "uz": "Foydalanish: /search <so'z>\nMisol: /search muhandislik",
    },
    "searching": {
        "en": "🔎 Searching grants…",
        "uz": "🔎 Grantlar qidirilmoqda…",
    },
    "no_results": {
        "en": "No grants matched that. Try a broader keyword, or /categories.",
        "uz": "Mos grant topilmadi. Kengroq so'z sinab ko'ring yoki /categories.",
    },
    "results_header": {
        "en": "Found *{n}* grant(s):",
        "uz": "*{n}* ta grant topildi:",
    },
    "categories_header": {
        "en": "📚 Choose a field:",
        "uz": "📚 Yo'nalishni tanlang:",
    },
    "deadlines_header": {
        "en": "⏳ Grants closing in the next {days} days:",
        "uz": "⏳ Keyingi {days} kun ichida tugaydigan grantlar:",
    },
    "no_deadlines": {
        "en": "No grants are closing in the next {days} days.",
        "uz": "Keyingi {days} kun ichida tugaydigan grant yo'q.",
    },
    "faq_header": {
        "en": "❓ Frequently asked questions:",
        "uz": "❓ Ko'p so'raladigan savollar:",
    },
    "find_field": {
        "en": "Step 1/3 — What field are you interested in?",
        "uz": "1/3-bosqich — Qaysi yo'nalish sizni qiziqtiradi?",
    },
    "find_country": {
        "en": "Step 2/3 — Any preferred country? (type a name, or tap *Any*)",
        "uz": "2/3-bosqich — Afzal ko'rgan davlat bormi? (nom yozing yoki *Istalgan* ni bosing)",
    },
    "find_funding": {
        "en": "Step 3/3 — Funding preference?",
        "uz": "3/3-bosqich — Moliyalashtirish bo'yicha afzallik?",
    },
    "find_done": {
        "en": "Here's what I found for you:",
        "uz": "Mana siz uchun topilganlari:",
    },
    "cancelled": {
        "en": "Cancelled. Type /find to start again.",
        "uz": "Bekor qilindi. Qayta boshlash uchun /find.",
    },
    "any": {"en": "Any", "uz": "Istalgan"},
    "fully_funded": {"en": "Fully funded", "uz": "To'liq moliyalashtirilgan"},
    "doesnt_matter": {"en": "Doesn't matter", "uz": "Farqi yo'q"},
    "btn_more": {"en": "More ▸", "uz": "Yana ▸"},
    "btn_prev": {"en": "◂ Back", "uz": "◂ Orqaga"},
    "error": {
        "en": "⚠️ Something went wrong. Please try again.",
        "uz": "⚠️ Xatolik yuz berdi. Iltimos, qayta urinib ko'ring.",
    },
    "deadline_label": {"en": "Deadline", "uz": "Muddat"},
    "amount_label": {"en": "Funding", "uz": "Moliyalashtirish"},
    "eligibility_label": {"en": "Eligibility", "uz": "Shartlar"},
    "country_label": {"en": "Country", "uz": "Davlat"},
    "org_label": {"en": "By", "uz": "Tashkilotchi"},
}


def normalize_lang(code: str | None) -> str:
    """Map a Telegram language_code to a supported language."""
    if not code:
        return settings.default_language if settings.default_language in SUPPORTED else "en"
    code = code.lower()
    if code.startswith("uz"):
        return "uz"
    return "en"


def t(key: str, lang: str = "en", **kwargs) -> str:
    lang = lang if lang in SUPPORTED else "en"
    table = STRINGS.get(key, {})
    text = table.get(lang) or table.get("en") or key
    return text.format(**kwargs) if kwargs else text


async def get_lang(user_id: int, fallback_code: str | None = None) -> str:
    """Return the user's stored language, seeding from Telegram code if new."""
    row = await database.fetch_one(
        "SELECT language FROM user_prefs WHERE user_id = ?", (user_id,)
    )
    if row and row["language"] in SUPPORTED:
        return row["language"]
    lang = normalize_lang(fallback_code)
    await set_lang(user_id, lang)
    return lang


async def set_lang(user_id: int, lang: str) -> None:
    lang = lang if lang in SUPPORTED else "en"
    await database.execute(
        "INSERT INTO user_prefs (user_id, language, updated_at) "
        "VALUES (?, ?, datetime('now')) "
        "ON CONFLICT(user_id) DO UPDATE SET language=excluded.language, "
        "updated_at=datetime('now')",
        (user_id, lang),
    )
