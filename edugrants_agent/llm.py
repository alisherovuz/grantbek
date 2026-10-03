"""All Claude calls live here. Every call forces a tool so the answer is always valid JSON."""
from __future__ import annotations

import json
import logging
from datetime import date

import anthropic

from .config import settings
from .db import DB

log = logging.getLogger(__name__)

OPP_TYPES = ["scholarship", "exchange", "summer_school", "competition", "olympiad", "fellowship",
             "conference", "forum", "internship", "course", "grant", "volunteering", "other"]
LEVELS = ["high_school", "bachelor", "master", "phd", "young_professional", "any"]

TRIAGE_TOOL = {
    "name": "triage",
    "description": "Decide which candidates are worth researching for the channel.",
    "input_schema": {
        "type": "object",
        "properties": {
            "decisions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "keep": {"type": "boolean"},
                        "fit": {"type": "integer", "minimum": 1, "maximum": 5,
                                "description": "Would this channel post it? 5 = as strong as its biggest hits "
                                               "(famous organiser, big benefit, our audience). 4 = a typical post. "
                                               "3 = in our categories but a weaker name or benefit. 2 = like the "
                                               "flops. 1 = never our kind."},
                        "reason": {"type": "string", "description": "Uzbek, max 12 words: why it fits or why dropped."},
                    },
                    "required": ["id", "keep", "fit", "reason"],
                },
            }
        },
        "required": ["decisions"],
    },
}

OFFICIAL_TOOL = {
    "name": "official_link",
    "description": "Pick the organiser's own page for this opportunity from the page's outbound links.",
    "input_schema": {
        "type": "object",
        "properties": {
            "official_url": {"type": ["string", "null"], "description": "The organiser's page describing the programme. Null if none of the links is it."},
            "registration_url": {"type": ["string", "null"], "description": "Direct application/registration form if present."},
        },
        "required": ["official_url", "registration_url"],
    },
}

EXTRACT_TOOL = {
    "name": "opportunity",
    "description": "Structured facts about one opportunity.",
    "input_schema": {
        "type": "object",
        "properties": {
            "is_opportunity": {"type": "boolean", "description": "False if this is a job ad, news article, paid course sale, or not one specific programme."},
            "title": {"type": "string", "description": "Official programme name in English, with year/cohort if stated."},
            "organizer": {"type": "string"},
            "host_country": {"type": "string", "description": "Country where it takes place in English, or 'Online' or 'Multiple'."},
            "host_country_iso2": {"type": ["string", "null"], "description": "ISO 3166 alpha-2 code of host_country, null if online/multiple."},
            "opportunity_type": {"type": "string", "enum": OPP_TYPES},
            "level": {"type": "array", "items": {"type": "string", "enum": LEVELS}},
            "funding": {"type": "string", "enum": ["full", "partial", "none", "unknown"]},
            "format": {"type": "string", "enum": ["offline", "online", "hybrid", "unknown"]},
            "duration": {"type": ["string", "null"], "description": "e.g. '2 weeks', '1 academic year'"},
            "application_fee": {"type": "string", "enum": ["free", "paid", "unknown"],
                                "description": "Fee just to apply. 'free' only if stated or clearly implied."},
            "program_fee": {"type": "string", "enum": ["free", "paid", "paid_with_full_aid", "unknown"],
                            "description": "Cost of taking part (tuition, participation fee). 'free' if participation costs nothing; "
                                           "'paid_with_full_aid' if it charges tuition but offers need-based full aid."},
            "age_min": {"type": ["integer", "null"]},
            "age_max": {"type": ["integer", "null"]},
            "deadline_type": {"type": "string", "enum": ["fixed", "rolling", "unknown"]},
            "deadline": {"type": ["string", "null"], "description": "YYYY-MM-DD, the final application deadline for international applicants."},
            "opening_date": {"type": ["string", "null"], "description": "YYYY-MM-DD if applications have not opened yet."},
            "status": {"type": "string", "enum": ["open", "upcoming", "closed", "unknown"]},
            "eligible_countries": {"type": "string", "description": "Short description, e.g. 'All countries', 'Central Asia', 'EU only'."},
            "uzbekistan_eligible": {"type": "string", "enum": ["yes", "no", "unclear"]},
            "summary": {"type": "string", "description": "2-4 plain English sentences on what the programme is."},
            "benefits": {"type": "array", "items": {"type": "string"}},
            "eligibility": {"type": "string"},
            "application_process": {"type": "string"},
            "registration_url": {"type": ["string", "null"]},
            "verified_from_official": {"type": "boolean", "description": "True only if deadline and eligibility were read from SOURCE A (the organiser's page)."},
            "conflicts": {"type": ["string", "null"], "description": "Only if the sources disagree on something that matters (deadline, eligibility, funding): ONE short sentence in Uzbek, saying 'rasmiy sahifa' and 'agregator' instead of SOURCE A/B. Otherwise null."},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        },
        "required": ["is_opportunity", "title", "organizer", "host_country", "host_country_iso2", "opportunity_type",
                     "level", "funding", "format", "duration", "application_fee", "age_min", "age_max",
                     "deadline_type", "deadline", "opening_date", "status", "eligible_countries", "program_fee",
                     "uzbekistan_eligible", "summary", "benefits", "eligibility", "application_process",
                     "registration_url", "verified_from_official", "conflicts", "confidence"],
    },
}


def write_tool(options: dict) -> dict:
    def enum(key):  # dropdown values from platform_options.yaml; free text if that file is missing
        return {"type": "string", "enum": options[key]} if options.get(key) else {"type": "string"}

    return {
        "name": "edugrants_post",
        "description": "Uzbek texts for the Telegram post and the edugrants.uz listing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title_uz": {"type": "string", "description": "Plain title line for the post. Keep the official programme name as-is (Latin script), you may add a short Uzbek descriptor."},
                "country_uz": {"type": "string", "description": "Host country in Uzbek (Latin), e.g. 'AQSh', 'Germaniya', 'Onlayn'."},
                "description_uz": {"type": "string", "description": "ONE paragraph, 2-3 sentences, what the programme is and who it is for."},
                "benefits_uz": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 4, "description": "3-4 very short benefit phrases, no trailing period."},
                "age_category_uz": {"type": "string", "description": "Like the channel writes it: '14-18', '18-35', '18 va undan katta', 'Bakalavr talabalari', 'Barcha uchun'."},
                "platform": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "imkoniyat_turi": enum("imkoniyat_turi"),
                        "daraja": enum("daraja"),
                        "moliyalashtirish": enum("moliyalashtirish"),
                        "format": enum("format"),
                        "davomiylik": enum("davomiylik"),
                        "ariza_tolovi": enum("ariza_tolovi"),
                        "description": {"type": "string", "description": "2 short paragraphs in Uzbek."},
                        "eligibility": {"type": "string", "description": "Uzbek, bullet lines starting with '- '."},
                        "benefits": {"type": "string", "description": "Uzbek, bullet lines starting with '- '."},
                        "application_process": {"type": "string", "description": "Uzbek, numbered steps."},
                        "additional_information": {"type": "string", "description": "Uzbek, anything else useful. Empty string if nothing."},
                    },
                    "required": ["title", "imkoniyat_turi", "daraja", "moliyalashtirish", "format", "davomiylik",
                                 "ariza_tolovi", "description", "eligibility", "benefits", "application_process",
                                 "additional_information"],
                },
            },
            "required": ["title_uz", "country_uz", "description_uz", "benefits_uz", "age_category_uz", "platform"],
        },
    }


WRITER_SYSTEM = """Siz EduGrants (@EduGrandsUz) Telegram kanali va edugrants.uz platformasi muharririsiz.
Auditoriya: O'zbekistonlik maktab o'quvchilari, talabalar va yosh mutaxassislar.

Qoidalar:
- Faqat o'zbek tilida, lotin yozuvida, tabiiy va sodda yozing. Rus yoki ingliz so'zlarini keraksiz aralashtirmang.
- Dastur nomini tarjima qilmang, asl nomini saqlang.
- Faqat berilgan faktlardan foydalaning. Hech narsa o'ylab topmang. Fakt yo'q bo'lsa, uni yozmang.
- Post qisqa bo'lsin: tavsif 2-3 gap, imtiyozlar 3-4 ta qisqa band.
- Reklama ohangi, bosh harflar bilan qichqirish, ko'p emoji ishlatmang.
- Platforma maydonlari uchun faqat ruxsat etilgan qiymatlardan tanlang.

Kanalning haqiqiy postlaridan namunalar (uslub, uzunlik va so'z tanlash uchun; faktlarni ko'chirmang):

1) Chevening Scholarship
Tavsif: "Buyuk Britaniya hukumati tomonidan taqdim etiladigan Chevening granti magistraturani Buyuk Britaniya universitetlaridan birida 1 yil davomida to'liq moliyalashtirilgan holda o'qish imkoniyatini beradi."
Imtiyozlari: O'qish kontrakt to'lovi to'liq qoplanadi / Yashash xarajatlari uchun stipendiya / Safar xarajatlari to'lab beriladi / Viza olishda yordam beriladi

2) Diamond Challenge (Yosh toifasi: 14-18)
Tavsif: "Bunda o'rta maktab o'quvchilari bir yillik dasturda ishtirok etadilar va ular aniqlagan dolzarb muammolarni hal qilish uchun g'oyalarni ishlab chiqadilar."
Imtiyozlari: 100,000$ dan oshiq pul mukofotlari / Tanlov hamkorlaridan esdalik sovg'alari / Final bosqichi Delaware universitetida bo'ladi

3) ARAL SCHOOL 2027 (Yosh toifasi: Bakalavr bitiruvchilari)
Tavsif: "Orolbo'yi hududining ekologik va madaniy tiklanishiga hissa qo'shishni xohlovchilar uchun 5 oylik xalqaro dastur. Ishtirokchilar iqlim, suv, ekologiya, infratuzilma va mahalliy jamoalar bilan bog'liq muammolar ustida ishlab, real loyihalar va prototiplar yaratadilar."
Imtiyozlari: Turar joy, oylik stipendiya va tadqiqot safarlari qoplanadi / Loyiha va prototiplar uchun materiallar / Xalqaro networking va ishlarni nashr qilish imkoniyati

Imtiyozlar aniq va foydali bo'lsin: pul mukofoti miqdori, qoplanadigan xarajatlar, nufuzli joy yoki tashkilot nomi.
"""


class LLM:
    def __init__(self, db: DB, client: anthropic.Anthropic | None = None):
        self.db = db
        from .config import anthropic_client
        self.client = client or anthropic_client()

    # ------------------------------------------------------------------
    # Models that refuse a forced tool_choice (newer models think first, and then only "auto" is
    # allowed). Remembered so we don't pay for the failing request every time.
    _no_forced_tool: set[str] = set()

    def _request(self, model: str, system: str, user: str, tool: dict, max_tokens: int):
        forced = model not in self._no_forced_tool
        if not forced:
            system += (f"\n\nAnswer ONLY by calling the `{tool['name']}` tool once. "
                       "Do not write anything outside the tool call.")
        return self.client.messages.create(
            model=model,
            max_tokens=max_tokens + (0 if forced else 4000),   # room for the model's thinking
            system=system,
            messages=[{"role": "user", "content": user}],
            tools=[tool],
            tool_choice={"type": "tool", "name": tool["name"]} if forced else {"type": "auto"},
        )

    def _log_usage(self, purpose: str, model: str, u) -> None:
        fast = model == settings.model_fast
        cost = (u.input_tokens * (settings.price_fast_in if fast else settings.price_writer_in)
                + u.output_tokens * (settings.price_fast_out if fast else settings.price_writer_out)) / 1_000_000
        self.db.log_usage(purpose, model, u.input_tokens, u.output_tokens, cost)

    def converse(self, purpose: str, model: str, system: str, messages: list, tools: list[dict],
                 max_tokens: int = 4000):
        """One step of a tool-using conversation (the manager's order loop). Returns the raw response;
        the caller runs the tools it asked for and calls again with the results."""
        resp = self.client.messages.create(model=model, max_tokens=max_tokens, system=system, messages=messages,
                                           tools=tools, tool_choice={"type": "auto"})
        self._log_usage(purpose, model, resp.usage)
        return resp

    def _call(self, purpose: str, model: str, system: str, user: str, tool: dict, max_tokens: int = 2000) -> dict:
        try:
            resp = self._request(model, system, user, tool, max_tokens)
        except anthropic.BadRequestError as e:
            if "tool_choice" not in str(e) or model in self._no_forced_tool:
                raise
            log.warning("%s does not accept a forced tool call; retrying with tool_choice=auto", model)
            self._no_forced_tool.add(model)
            resp = self._request(model, system, user, tool, max_tokens)
        self._log_usage(purpose, model, resp.usage)
        for block in resp.content:
            if block.type == "tool_use":
                return block.input
        raise RuntimeError(f"{purpose}: model returned no tool call")

    # ------------------------------------------------------------------
    def triage(self, items: list[dict], audience: str, profile: str = "",
               feedback: dict[str, list[str]] | None = None) -> dict[int, tuple[bool, int, str]]:
        fb = feedback or {}
        system = (
            "You screen opportunity listings for the EduGrants Telegram channel (Uzbekistan).\n"
            f"Audience and hard rules:\n{audience}\n\n{profile}\n"
            + (("\nEditors recently ACCEPTED: " + "; ".join(fb["accepted"])) if fb.get("accepted") else "")
            + (("\nEditors recently SKIPPED (reason): " + "; ".join(fb["skipped"])) if fb.get("skipped") else "")
            + "\n\nDrop anything that breaks a hard rule. Score the rest 1-5 against the channel's vibe: "
              "compare each candidate with the hits and flops of its type. Ask: is the organiser a name our "
              "audience recognises or would be proud to put on a CV? Is the benefit concrete and attractive? "
              "Is it for school or bachelor students? A small essay contest from an unknown site, or a "
              "fellowship for researchers or professionals, is a 2 even if it is free and open to Uzbeks. "
              "Programmes ONLY for master's or PhD students are rare on the channel (16 master's and 0 PhD "
              "of 850 posts): give them 2, unless it is a world-famous fully funded name such as Chevening, "
              "Erasmus Mundus, DAAD, Fulbright, Stipendium Hungaricum or Global Korea Scholarship: then 5. "
              "When unsure about a hard rule, keep it: the organiser's page is checked next."
        )
        user = "Candidates:\n" + json.dumps(items, ensure_ascii=False, indent=1)
        out = self._call("triage", settings.model_fast, system, user, TRIAGE_TOOL, max_tokens=3500)
        return {d["id"]: (bool(d["keep"]), int(d.get("fit", 3)), d.get("reason", "")) for d in out.get("decisions", [])}

    def find_official(self, title: str, page_text: str, links: list[tuple[str, str]]) -> dict:
        system = ("You are given an aggregator article about an opportunity and its outbound links. "
                  "Return the organiser's own page for this programme (not another aggregator, not a news site, "
                  "not social media), and the direct application form if there is one.")
        link_lines = "\n".join(f"- [{t}] {u}" for t, u in links) or "(no outbound links)"
        user = f"Title: {title}\n\nArticle (excerpt):\n{page_text[:3500]}\n\nOutbound links:\n{link_lines}"
        return self._call("find_official", settings.model_fast, system, user, OFFICIAL_TOOL, max_tokens=400)

    def extract(self, title: str, official_text: str | None, official_url: str | None,
                aggregator_text: str, aggregator_url: str) -> dict:
        system = (
            f"Today is {date.today().isoformat()}. Extract facts about ONE opportunity.\n"
            "SOURCE A is the organiser's own page and is the authority. SOURCE B is a third-party "
            "aggregator that is often outdated or wrong. When they disagree, use A and describe the "
            "disagreement in `conflicts`. Never guess a deadline: use null if no source states it. "
            "If the year of a deadline is ambiguous, prefer the next upcoming occurrence. "
            "uzbekistan_eligible: 'yes' if open to all countries or Uzbekistan/Central Asia/CIS/developing "
            "countries are included, 'no' if restricted to a list that excludes Uzbekistan, otherwise 'unclear'.\n"
            "The facts fill a Telegram post that always shows: country (or Online), whether it is online or "
            "offline, what participants get (benefits: list each concrete one, e.g. certificate, prizes, "
            "flights, stipend), the organiser's registration link and the deadline. Look for each carefully."
        )
        a = (f"SOURCE A (organiser page, {official_url}):\n{official_text}" if official_text
             else "SOURCE A: not available (could not identify or fetch the organiser page).")
        user = f"Listing title: {title}\n\n{a}\n\n---\nSOURCE B (aggregator, {aggregator_url}):\n{aggregator_text}"
        return self._call("extract", settings.model_fast, system, user, EXTRACT_TOOL, max_tokens=2500)

    def write(self, data: dict, options: dict, notes: str | None = None) -> dict:
        user = ("Quyidagi faktlar asosida Telegram post matni va platforma maydonlarini yozing.\n\n"
                + json.dumps(data, ensure_ascii=False, indent=1))
        if notes:   # the owner's corrections and extra facts, given through Toshmat aka: they win over the data
            user += ("\n\nKANAL EGASINING KO'RSATMALARI (yuqoridagi faktlardan ustun, albatta bajaring):\n" + notes)
        try:
            return self._call("write", settings.model_writer, WRITER_SYSTEM, user, write_tool(options), max_tokens=3000)
        except (anthropic.BadRequestError, anthropic.NotFoundError, RuntimeError) as e:
            if settings.model_writer == settings.model_fast:
                raise
            log.warning("writer model %s failed (%s); writing with %s instead",
                        settings.model_writer, str(e)[:200], settings.model_fast)
            return self._call("write", settings.model_fast, WRITER_SYSTEM, user, write_tool(options), max_tokens=3000)
