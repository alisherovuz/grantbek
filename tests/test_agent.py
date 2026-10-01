import json
from datetime import date, timedelta

import pytest

from edugrants_agent import collectors
from edugrants_agent.collectors import Candidate
from edugrants_agent.config import settings
from edugrants_agent.db import DB
from edugrants_agent.dedupe import canonical_url, find_title_duplicate, normalize_title
from edugrants_agent.fetch import Page
from edugrants_agent.pipeline import Pipeline
from edugrants_agent.render import flag, render_post, uz_date

OPTIONS = {
    "imkoniyat_turi": ["Stipendiya", "Tanlov"], "daraja": ["Bakalavr", "Barcha darajalar"],
    "moliyalashtirish": ["To'liq", "Qisman"], "format": ["Oflayn", "Onlayn"],
    "davomiylik": ["1-4 hafta", "1 yildan ortiq"], "ariza_tolovi": ["Bepul", "Pullik"],
}
IN_60 = (date.today() + timedelta(days=60)).isoformat()
IN_3 = (date.today() + timedelta(days=3)).isoformat()


# ---------------------------------------------------------------- dedupe
def test_canonical_url_strips_tracking():
    a = canonical_url("https://www.example.org/grant/?utm_source=x&id=5#top")
    b = canonical_url("http://example.org/grant?id=5")
    assert a == b


def test_title_duplicates_across_aggregators():
    rows = [{"id": 1, "norm_title": normalize_title("Hong Kong PhD Fellowship Scheme (HKPFS) 2027/2028")}]
    same = normalize_title("Applications Open: Hong Kong PhD Fellowship Scheme 2027/2028 | Fully Funded")
    other = normalize_title("Chevening Scholarships 2027 (Fully Funded)")
    assert find_title_duplicate(same, rows, 88) == 1
    assert find_title_duplicate(other, rows, 88) is None


def test_similar_titles_of_different_programmes_are_kept():
    doc = [{"id": 1, "norm_title": normalize_title("Canada Graduate Research Scholarship – Doctoral Program: $40,000 Per Year")}]
    assert find_title_duplicate(normalize_title("Canada Graduate Research Scholarship – Master's Program"), doc, 88) is None
    c2 = [{"id": 2, "norm_title": normalize_title("METI UniPods AI Innovation Programme Cohort 2 2026")}]
    assert find_title_duplicate(normalize_title("METI UniPods AI Innovation Programme Cohort 3"), c2, 88) is None


# ---------------------------------------------------------------- render
def test_uz_date_and_flag():
    assert uz_date("2026-10-15") == "15-oktyabr"
    assert uz_date(None) is None
    assert flag("de") == "🇩🇪"
    assert flag(None) == "🌍"


def test_render_post_matches_real_channel_post():
    """Same layout as the real Chevening post on @EduGrandsUz (from the channel export)."""
    data = {"host_country_iso2": "GB", "opportunity_type": "scholarship", "format": "offline",
            "funding": "full", "deadline_type": "fixed", "deadline": "2026-10-06"}
    written = {"title_uz": "Chevening Scholarship", "country_uz": "Buyuk Britaniya",
               "description_uz": "Buyuk Britaniya hukumati tomonidan taqdim etiladigan Chevening granti.",
               "benefits_uz": ["O'qish kontrakt to'lovi to'liq qoplanadi.", "Yashash xarajatlari uchun stipendiya"],
               "age_category_uz": "18-35"}
    post = render_post(data, written, "https://www.chevening.org/apply?a=1&b=2", "@EduGrandsUz")
    assert post.split("\n") == [
        "<b>Chevening Scholarship</b>",
        "",
        "<b>Davlat</b>: Buyuk Britaniya 🇬🇧",
        "<b>Moliyaviy ta'minot</b>: To'liq",
        "<b>Yosh toifasi</b>: 18-35",
        "",
        "<i>Buyuk Britaniya hukumati tomonidan taqdim etiladigan Chevening granti.</i>",
        "",
        "➡️<b>Imtiyozlari</b>:",
        "- O'qish kontrakt to'lovi to'liq qoplanadi",
        "- Yashash xarajatlari uchun stipendiya",
        "",
        "🔗<b>Ro'yxatdan o'tish uchun</b>: <a href=\"https://www.chevening.org/apply?a=1&amp;b=2\">Havola</a>",
        "",
        "📌<b>Ro'yxatdan o'tishning so'nggi muddati</b>: 6-oktyabr",
        "",
        "⚡️<a href=\"https://t.me/EduGrandsUz\">@EduGrandsUz</a>",
    ]


def test_meta_line_follows_the_channel():
    from edugrants_agent.render import middle_meta_line
    assert middle_meta_line({"funding": "full", "format": "offline"}) == ("Moliyaviy ta'minot", "To'liq")
    assert middle_meta_line({"funding": "none", "format": "online", "opportunity_type": "competition"}) == ("Tanlov shakli", "Onlayn")
    assert middle_meta_line({"funding": "none", "format": "online", "opportunity_type": "course"}) == ("Dastur shakli", "Onlayn")
    w = {"title_uz": "T", "country_uz": "Onlayn", "description_uz": "D", "benefits_uz": ["a", "b"], "age_category_uz": "x"}
    assert "Doimiy qabul" in render_post({"deadline_type": "rolling", "funding": "none"}, w, "https://a.b", "@x")


# ---------------------------------------------------------------- RSS
RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>Global Youth Forum 2026 (Fully Funded)</title><link>https://agg.example/forum?utm_source=rss</link>
<pubDate>{now}</pubDate><description><![CDATA[<p>Deadline soon</p>]]></description></item>
<item><title>Emirates Cabin Crew Jobs 2026</title><link>https://agg.example/jobs</link><pubDate>{now}</pubDate></item>
<item><title>Old thing</title><link>https://agg.example/old</link><pubDate>Mon, 01 Jan 2024 00:00:00 +0000</pubDate></item>
</channel></rss>"""


def test_rss_collector_filters_keywords_and_age(monkeypatch):
    from email.utils import format_datetime
    from datetime import datetime, timezone
    now = format_datetime(datetime.now(timezone.utc))
    monkeypatch.setattr(collectors, "fetch_raw", lambda url, **k: (RSS.format(now=now), url))
    items = collectors.collect_rss({"name": "agg", "url": "https://agg.example/feed", "max_age_days": 10}, ["cabin crew"])
    assert [i.title for i in items] == ["Global Youth Forum 2026 (Fully Funded)"]
    assert items[0].summary == "Deadline soon"


# ---------------------------------------------------------------- full pipeline
class FakeLLM:
    """Stands in for Claude. Returns facts keyed by the official URL."""

    FACTS = {
        "https://org-a.example/program": dict(title="Alpha Scholarship 2027", deadline=IN_60, uzbekistan_eligible="yes"),
        "https://org-d.example/soon": dict(title="Delta Camp", deadline=IN_3, uzbekistan_eligible="yes"),
        "https://org-e.example/eu": dict(title="Epsilon EU Grant", deadline=IN_60, uzbekistan_eligible="no"),
    }

    def __init__(self):
        self.calls = {"triage": 0, "find_official": 0, "extract": 0, "write": 0}

    def triage(self, items, audience, profile="", feedback=None):
        self.calls["triage"] += 1
        return {i["id"]: (("job" not in i["title"].lower()), 4, "mos") for i in items}

    def find_official(self, title, text, links):
        self.calls["find_official"] += 1
        return {"official_url": links[0][1] if links else None, "registration_url": None}

    def extract(self, title, official_text, official_url, agg_text, agg_url):
        self.calls["extract"] += 1
        facts = self.FACTS[official_url]
        return {
            "is_opportunity": True, "organizer": "Org", "host_country": "Germany", "host_country_iso2": "DE",
            "opportunity_type": "scholarship", "level": ["bachelor"], "funding": "full", "format": "offline",
            "duration": "1 year", "application_fee": "free", "age_min": 18, "age_max": 30,
            "deadline_type": "fixed", "opening_date": None, "status": "open", "eligible_countries": "All",
            "summary": "A fully funded programme for young people from all countries.", "benefits": ["Full tuition"], "eligibility": "e", "application_process": "p",
            "registration_url": None, "verified_from_official": True, "conflicts": None, "confidence": "high",
            "program_fee": "free",
            **facts,
        }

    def write(self, data, options):
        self.calls["write"] += 1
        return {"title_uz": data["title"], "country_uz": "Germaniya", "description_uz": "Tavsif.",
                "benefits_uz": ["To'liq grant", "Yashash joyi"], "age_category_uz": "18-30 yosh",
                "platform": {"title": data["title"], "imkoniyat_turi": "Stipendiya", "daraja": "Bakalavr",
                             "moliyalashtirish": "To'liq", "format": "Oflayn", "davomiylik": "1 yildan ortiq",
                             "ariza_tolovi": "Bepul", "description": "d", "eligibility": "- e",
                             "benefits": "- b", "application_process": "1. p", "additional_information": ""}}


def fake_fetch(url):
    pages = {
        "https://agg1.example/alpha": Page(url, url, "Alpha article", [("Official", "https://org-a.example/program")]),
        "https://agg2.example/alpha-copy": Page(url, url, "Alpha repost", [("Apply", "https://org-a.example/program")]),
        "https://agg1.example/delta": Page(url, url, "Delta", [("site", "https://org-d.example/soon")]),
        "https://agg1.example/eps": Page(url, url, "Eps", [("site", "https://org-e.example/eu")]),
    }
    if url in pages:
        return pages[url]
    return Page(url, url, f"official page {url}", [])


def test_pipeline_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "min_days_left", 7)
    monkeypatch.setattr(settings, "mode", "full")
    db = DB(tmp_path / "t.db")
    llm = FakeLLM()
    p = Pipeline(db, llm=llm, config={"audience": "students"}, options=OPTIONS, fetcher=fake_fetch)
    p.aggregators |= {"agg1.example", "agg2.example"}

    added = p.ingest([
        Candidate("agg1", "https://agg1.example/alpha", "Alpha Scholarship 2027 (Fully Funded)"),
        # different title wording on another site, same official page -> caught after extraction
        Candidate("agg2", "https://agg2.example/alpha-copy", "Study in Germany: the Alpha programme for international students"),
        # same title as first -> caught immediately, no AI spent on it
        Candidate("agg2", "https://agg2.example/alpha-2", "Applications Open: Alpha Scholarship 2027"),
        Candidate("agg1", "https://agg1.example/job", "Marketing job at Company"),
        Candidate("agg1", "https://agg1.example/delta", "Delta Camp"),
        Candidate("agg1", "https://agg1.example/eps", "Epsilon EU Grant"),
        Candidate("agg1", "https://agg1.example/alpha?utm_source=tg", "Alpha again"),  # same URL -> ignored
    ])
    assert added == 6
    p.process()

    status = {r["url"]: (r["status"], r["reason"]) for r in db.conn.execute("SELECT * FROM items")}
    assert status["https://agg1.example/alpha"][0] == "drafted"
    assert status["https://agg2.example/alpha-copy"][0] == "duplicate"
    assert status["https://agg2.example/alpha-2"][0] == "duplicate"
    assert status["https://agg1.example/job"][0] == "rejected"
    assert "deadline too close" in status["https://agg1.example/delta"][1]
    assert "Uzbekistan eligibility no" in status["https://agg1.example/eps"][1]

    item = db.conn.execute("SELECT * FROM items WHERE status='drafted'").fetchone()
    assert 'href="https://org-a.example/program">Havola</a>' in item["post_text"]
    platform = json.loads(item["platform_json"])
    assert platform["official_link"] == "https://org-a.example/program"
    assert platform["deadline"] == IN_60 and platform["imkoniyat_turi"] == "Stipendiya"
    assert llm.calls["write"] == 1          # only the one good item cost a writer call
    assert llm.calls["triage"] == 1         # one batched call for all candidates


def test_new_intake_is_not_a_duplicate():
    old = {"data_json": json.dumps({"deadline": "2025-11-01"})}
    assert Pipeline.is_new_intake(old, {"deadline": IN_60}) is True
    still_open = {"data_json": json.dumps({"deadline": IN_60})}
    assert Pipeline.is_new_intake(still_open, {"deadline": IN_60}) is False


def test_bot_module_imports():
    import edugrants_agent.bot as bot  # noqa: F401
    assert bot.keyboard(5).inline_keyboard[0][0].callback_data == "ap:5"


# ---------------------------------------------------------------- feeds that misbehave
BROKEN = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>Good Summer School 2027</title><link>https://a.example/good</link>
<pubDate>{now}</pubDate><description>fine</description></item>
<item><title>Broken one</title><link>https://a.example/bad</link><pubDate>{now}</pubDate>
<description><p>unclosed paragraph <b>bold</description></item>
<item><title>Another Olympiad 2027</title><link>https://a.example/olymp</link><pubDate>{now}</pubDate></item>
</channel></rss>"""
BLOCK_PAGE = "<!DOCTYPE html><html><head><title>Just a moment...</title></head><body>checking</body></html>"


def _now():
    from datetime import datetime, timezone
    from email.utils import format_datetime
    return format_datetime(datetime.now(timezone.utc))


def test_feed_with_a_broken_post_still_reads(monkeypatch):
    monkeypatch.setattr(collectors, "fetch_raw", lambda url, **k: (BROKEN.format(now=_now()), url))
    titles = [c.title for c in collectors.collect_rss({"name": "x", "url": "u", "max_age_days": 3}, [])]
    assert "Good Summer School 2027" in titles and "Another Olympiad 2027" in titles


def test_feed_retries_as_feed_reader_when_blocked(monkeypatch):
    calls = []

    def fake(url, user_agent=None):
        calls.append(user_agent)
        return (BLOCK_PAGE if user_agent is None else RSS.format(now=_now())), url
    monkeypatch.setattr(collectors, "fetch_raw", fake)
    items = collectors.collect_rss({"name": "x", "url": "u", "max_age_days": 3}, [])
    assert calls == [None, collectors.FEED_READER_UA] and items


def test_fully_blocked_feed_says_so(monkeypatch):
    monkeypatch.setattr(collectors, "fetch_raw", lambda url, **k: (BLOCK_PAGE, url))
    with pytest.raises(RuntimeError, match="web page instead of the feed"):
        collectors.collect_rss({"name": "x", "url": "https://site.example/feed/"}, [])


def test_wordpress_json_fallback(monkeypatch):
    import json
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    posts = [{"date_gmt": now, "link": "https://site.example/camp", "title": {"rendered": "Global Camp 2027 &#8211; Fully Funded"},
              "excerpt": {"rendered": "<p>For pupils</p>"}}]

    def fake(url, user_agent=None):
        if "/wp-json/" in url:
            return json.dumps(posts), url
        raise RuntimeError("415 Unsupported Media Type")
    monkeypatch.setattr(collectors, "fetch_raw", fake)
    items = collectors.collect_rss({"name": "x", "url": "https://site.example/feed/", "max_age_days": 3}, [])
    assert len(items) == 1 and items[0].title == "Global Camp 2027 – Fully Funded" and items[0].summary == "For pupils"


def test_missing_options_file_is_ok(tmp_path):
    from edugrants_agent.config import load_yaml
    assert load_yaml(tmp_path / "nope.yaml") == {}


def test_lenient_parser_entries_have_dates(monkeypatch):
    raw = BROKEN.format(now=_now())
    monkeypatch.setattr(collectors.feedparser, "parse", lambda b: type("F", (), {"entries": [], "bozo": 1})())
    monkeypatch.setattr(collectors, "fetch_raw", lambda url, **k: (raw, url))
    items = collectors.collect_rss({"name": "x", "url": "u", "max_age_days": 3}, [])
    assert items and all(i.published_at for i in items)


def test_model_without_forced_tool_choice_falls_back_to_auto():
    import anthropic
    import httpx
    from types import SimpleNamespace
    from edugrants_agent.db import DB
    from edugrants_agent.llm import LLM

    calls = []

    class Msgs:
        def create(self, **kw):
            calls.append(kw)
            if kw["tool_choice"]["type"] == "tool":
                req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
                raise anthropic.BadRequestError(
                    'tool_choice: type "tool" and "any" are not supported with thinking',
                    response=httpx.Response(400, request=req), body=None)
            return SimpleNamespace(
                usage=SimpleNamespace(input_tokens=10, output_tokens=5),
                content=[SimpleNamespace(type="thinking"),
                         SimpleNamespace(type="tool_use", input={"text": "ok"})])

    LLM._no_forced_tool.clear()
    llm = LLM(DB(":memory:"), client=SimpleNamespace(messages=Msgs()))
    tool = {"name": "say", "description": "", "input_schema": {"type": "object", "properties": {}}}
    assert llm._call("t", "new-model", "sys", "u", tool) == {"text": "ok"}
    assert [c["tool_choice"]["type"] for c in calls] == ["tool", "auto"]
    # remembered: the next call goes straight to auto
    llm._call("t", "new-model", "sys", "u", tool)
    assert calls[-1]["tool_choice"]["type"] == "auto" and len(calls) == 3
    LLM._no_forced_tool.clear()


def test_env_values_pasted_with_comments_still_work(monkeypatch):
    from edugrants_agent.config import _env
    monkeypatch.setenv("RUN_AT", "09:00                    # one search a day at this time")
    monkeypatch.setenv("CHANNEL_ID", '"@EduGrandsUz"')
    monkeypatch.setenv("ADMIN_USER_IDS", "   # comma separated ids")
    assert _env("RUN_AT") == "09:00" and _env("CHANNEL_ID") == "@EduGrandsUz"
    assert _env("ADMIN_USER_IDS", "none") == "none"


def test_reset_forgets_finds_but_keeps_channel_history(monkeypatch, tmp_path, capsys):
    import sys
    from edugrants_agent import __main__ as cli
    from edugrants_agent.config import settings
    from edugrants_agent.db import DB
    monkeypatch.setattr(settings, "db_path", tmp_path / "r.db")
    db = DB(settings.db_path)
    db.replace_history([{"norm_title": "chevening", "title": "Chevening", "category": "", "country": "", "age": "",
                         "first_posted": "2025-01-01", "last_posted": "2025-01-01", "times_posted": 1,
                         "post_dates": "[]", "official_url": None, "edugrants_url": None, "reactions_max": 1,
                         "score": 1.0}])
    db.insert_item(source="s", url="https://x.example/", canonical_url="https://x.example/", title="X",
                   norm_title="x", summary="", published_at=None)
    monkeypatch.setattr(sys, "argv", ["edugrants_agent", "reset"])
    cli.main()
    db = DB(settings.db_path)
    assert db.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0 and len(db.history_rows()) == 1
    assert "Forgot 1 finds" in capsys.readouterr().out
