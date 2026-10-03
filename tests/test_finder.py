import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from edugrants_agent.collectors import Candidate, due_for_watch
from edugrants_agent.config import settings
from edugrants_agent.db import DB
from edugrants_agent.history import build_history, build_profile, import_history, parse_export
from edugrants_agent.pipeline import Pipeline, participant_pays_nothing
from edugrants_agent.render import render_find

from test_agent import IN_60, OPTIONS, FakeLLM, fake_fetch

REAL_EXPORT = Path("/root/.claude/uploads/29cff771-1455-5695-b8a3-1feda8badd86/d91faac1-1790757316903_messages.html")

BASE = {"is_opportunity": True, "status": "open", "uzbekistan_eligible": "yes", "deadline_type": "fixed",
        "deadline": IN_60, "confidence": "high", "verified_from_official": True, "application_fee": "free",
        "age_min": 14, "age_max": 18, "funding": "none", "program_fee": "free", "format": "online",
        "opportunity_type": "competition", "host_country_iso2": "US"}


@pytest.fixture(autouse=True)
def finder_mode(monkeypatch):
    monkeypatch.setattr(settings, "mode", "finder")
    monkeypatch.setattr(settings, "require_uz_yes", True)
    monkeypatch.setattr(settings, "allow_full_aid", False)
    monkeypatch.setattr(settings, "require_free_participation", True)
    monkeypatch.setattr(settings, "check_ages", True)


# ---------------------------------------------------------------- the channel's rules
@pytest.mark.parametrize("change,ok", [
    ({}, True),                                                          # free online competition
    ({"funding": "full", "program_fee": "unknown", "format": "offline"}, True),  # fully funded abroad
    ({"program_fee": "free", "format": "offline", "host_country_iso2": "UZ", "opportunity_type": "other"}, True),  # free camp in UZ
    ({"program_fee": "paid", "format": "online"}, False),                # tuition
    ({"program_fee": "paid_with_full_aid"}, False),                      # Lumiere-style, off by default
    ({"program_fee": "free", "format": "offline", "opportunity_type": "forum"}, False),  # free but you pay travel
])
def test_participant_pays_nothing(change, ok):
    assert participant_pays_nothing({**BASE, **change}) is ok


@pytest.mark.parametrize("change,fragment", [
    ({"application_fee": "paid"}, "application fee"),
    ({"age_min": 45, "age_max": 60}, "ages"),
    ({"age_min": 5, "age_max": 9}, "ages"),
    ({"uzbekistan_eligible": "unclear"}, "Uzbekistan"),
    ({"program_fee": "paid"}, "participant pays"),
])
def test_finder_rejects(change, fragment):
    assert fragment in Pipeline.reject_reason({**BASE, **change})


def test_finder_accepts_good_item():
    assert Pipeline.reject_reason(BASE) is None


def test_due_for_watch_uses_anniversary():
    today = date(2026, 9, 30)
    assert due_for_watch(["2025-11-15"], today, 75, 45)      # coming up in 46 days
    assert due_for_watch(["2025-09-01"], today, 75, 45)      # 29 days ago, still in window
    assert not due_for_watch(["2025-04-01"], today, 75, 45)  # half a year away


# ---------------------------------------------------------------- history import
HTML = """<html><body><div class="history">
<div class="message default clearfix" id="message5"><div class="body">
<div class="pull_right date details" title="{d1}">x</div>
<div class="text">Alpha Scholarship 2025<br><br><strong>Davlat</strong>: Germaniya 🇩🇪<br><strong>Moliyaviy ta'minot</strong>: To'liq<br>
<strong>Yosh toifasi</strong>: 18-30<br><br>Tavsif<br><br>🔗Ro'yxatdan o'tish uchun: <a href="https://org-a.example/program">Havola</a></div>
<div class="reactions"><span class="reaction"><span class="emoji">🔥</span><span class="count">90</span></span></div></div></div>
<div class="message default clearfix" id="message6"><div class="body">
<div class="pull_right date details" title="{d2}">x</div>
<div class="text">Beta Olympiad<br><strong>Davlat</strong>: AQSh<br><strong>Tanlov shakli</strong>: Onlayn<br><strong>Yosh toifasi</strong>: 13-18<br>
<a href="https://edugrants.uz/scholarships/beta">Havola</a></div></div></div>
<div class="message default clearfix" id="message7"><div class="body">
<div class="pull_right date details" title="{d2}">x</div><div class="text">Assalomu alaykum, bugun jonli efir!</div></div></div>
</div></body></html>"""


def write_export(tmp_path, d1, d2):
    fmt = "%d %B %Y, %H:%M:%S"
    f = tmp_path / "messages.html"
    f.write_text(HTML.format(d1=d1.strftime(fmt), d2=d2.strftime(fmt)), encoding="utf-8")
    return f


def test_import_history(tmp_path):
    f = write_export(tmp_path, datetime(2025, 11, 1, 10), datetime.now() - timedelta(days=5))
    db = DB(tmp_path / "h.db")
    stats = import_history(db, f)
    assert stats["posts"] == 3 and stats["opportunity_posts"] == 2 and stats["programmes"] == 2
    rows = {r["title"]: r for r in db.history_rows()}
    assert rows["Alpha Scholarship 2025"]["official_url"] == "https://org-a.example/program"
    assert rows["Alpha Scholarship 2025"]["category"] == "funded"
    assert rows["Alpha Scholarship 2025"]["reactions_max"] == 90
    assert rows["Beta Olympiad"]["category"] == "competition"
    assert rows["Beta Olympiad"]["edugrants_url"].endswith("/beta")
    prof = build_profile(db)
    assert "THE CHANNEL'S VIBE" in prof and "aimed at PhD / postdoc: 0 of 2" in prof


def test_finder_uses_history(tmp_path, monkeypatch):
    """Recently posted -> skipped. Posted last year -> no triage call, labelled as a new round."""
    monkeypatch.setattr(settings, "profile_file", tmp_path / "none.md")
    f = write_export(tmp_path, datetime(2025, 11, 1, 10), datetime.now() - timedelta(days=5))
    db = DB(tmp_path / "f.db")
    import_history(db, f)
    llm = FakeLLM()
    p = Pipeline(db, llm=llm, config={"audience": "x"}, options=OPTIONS, fetcher=fake_fetch)
    p.aggregators |= {"agg1.example", "agg2.example"}
    p.ingest([
        Candidate("agg1", "https://agg1.example/alpha", "Applications open: Alpha Scholarship 2027"),
        Candidate("agg2", "https://agg2.example/beta", "Beta Olympiad 2026"),
    ])
    p.process()
    rows = {r["url"]: r for r in db.conn.execute("SELECT * FROM items")}
    assert rows["https://agg2.example/beta"]["status"] == "duplicate"
    assert "kanalda" in rows["https://agg2.example/beta"]["reason"]
    alpha = rows["https://agg1.example/alpha"]
    assert alpha["status"] == "extracted" and alpha["fit_score"] == 5
    assert llm.calls["triage"] == 0 and llm.calls["write"] == 0   # finder mode writes nothing
    card = render_find(alpha, json.loads(alpha["data_json"]), db.history_get(alpha["history_id"]))
    assert "Yangi bosqich" in card and "Rasmiy sahifa" in card and "kun qoldi" in card


@pytest.mark.skipif(not REAL_EXPORT.exists(), reason="real export not available")
def test_real_channel_export():
    posts = parse_export(REAL_EXPORT)
    programmes = build_history(posts)
    assert len(posts) > 1000 and len(programmes) > 500
    titles = {p["title"] for p in programmes}
    assert "Chevening Scholarship" in titles


def test_rule_rejection_stops_watching(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "profile_file", tmp_path / "none.md")
    f = write_export(tmp_path, datetime(2025, 11, 1, 10), datetime.now() - timedelta(days=5))
    db = DB(tmp_path / "x.db")
    import_history(db, f)
    hid = next(r["id"] for r in db.history_rows() if r["title"].startswith("Alpha"))

    class PaidLLM(FakeLLM):
        def extract(self, *a):
            return {**super().extract(*a), "funding": "none", "program_fee": "paid"}

    p = Pipeline(db, llm=PaidLLM(), config={"audience": "x"}, options=OPTIONS, fetcher=fake_fetch)
    p.ingest([Candidate("history-watch", "https://org-a.example/program", "Alpha Scholarship 2025",
                        key="https://org-a.example/program#watch-abc", history_id=hid)])
    p.process()
    assert db.history_get(hid)["excluded"].startswith("participant pays")


# ---------------------------------------------------------------- vibe
def test_scores_are_relative_to_the_month():
    """A 2024 post with 40 reactions when the month's median was 20 beats a 2026 post with 70 vs 70."""
    from edugrants_agent.history import build_history
    def post(day, title, reactions):
        return {"date": day, "reactions": reactions, "links": [],
                "text": f"{title}\nDavlat: AQSh\nYosh toifasi: 15-18"}
    old, new = datetime(2024, 5, 3), datetime(2026, 5, 3)
    posts = [post(old, "Old Hit Camp", 40), post(old, "Filler One School", 20), post(old, "Filler Two Forum", 20),
             post(new, "New Typical Camp", 70), post(new, "Other Typical Forum", 70)]
    s = {p["title"]: p["score"] for p in build_history(posts)}
    assert s["Old Hit Camp"] == 2.0 and s["New Typical Camp"] == 1.0


# ---------------------------------------------------------------- Telegram roles
class Msg:
    def __init__(self, id, text, minutes_ago=10, url=None):
        from datetime import timezone
        self.id, self.message = id, text
        self.date = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
        self.entities = [type("E", (), {"url": url})()] if url else []


class FakeClient:
    def __init__(self, posts):
        self.posts = posts
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False
    async def iter_messages(self, channel, limit, min_id):
        for m in sorted(self.posts.get(channel, []), key=lambda m: -m.id):
            if m.id > min_id:
                yield m


RU = "Летняя школа Global Youth Science Camp 2027\nПолностью финансируется, для школьников 15-18 лет. Подробнее по ссылке."


def test_telegram_source_competitor_and_own(tmp_path):
    from edugrants_agent.collectors import collect_telegram
    db = DB(tmp_path / "t.db")
    posts = {
        "@grantscholar": [Msg(5, RU, url="https://camp.example/apply"), Msg(4, "реклама", 5),
                          Msg(3, RU.replace("2027", "2026"), minutes_ago=60 * 24 * 5)],   # too old
        "@grantgouz": [Msg(9, "🇺🇸 Global Youth Science Camp 2027 — to'liq moliyalashtirilgan lager, 15-18 yosh. "
                              "Batafsil: https://camp.example/apply")],
        "@EduGrandsUz": [Msg(77, "Chevening Scholarship\nDavlat: Buyuk Britaniya\nYosh toifasi: 18+\n"
                                 "🔗Ro'yxatdan o'tish uchun: Havola", url="https://www.chevening.org/")],
    }
    fc = lambda: FakeClient(posts)  # noqa: E731
    found = collect_telegram({"name": "intl", "channels": ["@grantscholar"], "max_age_days": 2}, db, [], fc)
    assert len(found) == 1 and found[0].url == "https://camp.example/apply" and found[0].published_at
    assert found[0].title.startswith("Летняя школа")
    assert collect_telegram({"name": "intl", "channels": ["@grantscholar"]}, db, [], fc) == []  # only new posts

    collect_telegram({"name": "c", "role": "competitor", "channels": ["@grantgouz"], "max_age_days": 45}, db, [], fc)
    assert db.competitor_channels() == 1
    collect_telegram({"name": "o", "role": "own", "channels": ["@EduGrandsUz"], "max_age_days": 7}, db, [], fc)
    assert any(r["title"] == "Chevening Scholarship" for r in db.history_rows())


def test_card_says_who_posted_first(tmp_path):
    from edugrants_agent.dedupe import canonical_url
    from edugrants_agent.render import finder_card
    db = DB(tmp_path / "c.db")
    data = {**BASE, "title": "Global Youth Science Camp 2027", "summary": "s"}
    iid = db.insert_item(source="@grantscholar", url="https://camp.example/apply",
                         canonical_url="https://t.me/grantscholar/5", title="Летняя школа",
                         norm_title="x", summary="", published_at="2026-10-01 05:00:00")
    db.update(iid, status="extracted", data_json=data, official_url="https://camp.example/apply",
              official_canonical=canonical_url("https://camp.example/apply"))
    text, taken = finder_card(db, db.get(iid))
    assert "🥇" not in text and "⏱ Manbada" in text           # no competitors tracked yet: no claim

    db.add_competitor_post("@other", "k1", "2026-10-01 04:00:00", "Unrelated", "boshqa dastur haqida", [])
    text, taken = finder_card(db, db.get(iid))
    assert "🥇 O'zbek kanallarida hali yo'q" in text and not taken

    db.add_competitor_post("@grantgouz", "k2", "2026-10-01 04:30:00", "x",
                           "global youth science camp 2027 to liq moliyalashtirilgan", [])
    text, taken = finder_card(db, db.get(iid))
    assert "⚠️ @grantgouz" in text and taken


def test_fast_only_skips_slow_sources(monkeypatch):
    from edugrants_agent import collectors
    called = []
    monkeypatch.setattr(collectors, "collect_rss", lambda src, skip, db=None: called.append(src["name"]) or [])
    monkeypatch.setattr(collectors, "collect_telegram", lambda src, db, skip: called.append(src["name"]) or [])
    cfg = {"sources": [{"name": "feed", "type": "rss", "url": "x"},
                       {"name": "tg", "type": "telegram", "fast": True, "channels": []}]}
    collectors.collect_all(DB(":memory:"), cfg, fast_only=True)
    assert called == ["tg"]



# ---------------------------------------------------------------- new finds must come first
def test_first_look_at_old_programme_page_is_only_a_baseline(tmp_path, monkeypatch):
    """The bug from the first live run: every old programme counted as 'changed' on day one."""
    from edugrants_agent.collectors import collect_history_watch
    from edugrants_agent.fetch import Page
    f = write_export(tmp_path, datetime(2025, 11, 1, 10), datetime(2025, 11, 2, 10))
    db = DB(tmp_path / "w.db")
    import_history(db, f)
    monkeypatch.setattr("edugrants_agent.collectors.due_for_watch", lambda *a: True)
    text = {"v": "Applications for 2026 are closed."}
    fetch = lambda url: Page(url, url, text["v"], [])  # noqa: E731
    src = {"every_hours": 0, "max_candidates": 5}
    assert collect_history_watch(src, db, fetch) == []          # day one: snapshot only
    assert collect_history_watch(src, db, fetch) == []          # nothing changed
    text["v"] = "Applications for 2027 are now open! Deadline 15 January 2027."
    found = collect_history_watch(src, db, fetch)
    assert [c.title for c in found] == ["Alpha Scholarship 2025"]  # page changed: a new round


def test_old_programmes_are_capped_per_search(tmp_path, monkeypatch):
    from edugrants_agent.collectors import collect_history_watch
    from edugrants_agent.fetch import Page
    db = DB(tmp_path / "cap.db")
    db.replace_history([{"norm_title": f"prog {i}", "title": f"Prog {i}", "category": "online", "country": "",
                         "age": "", "first_posted": "2025-01-01", "last_posted": "2025-01-01", "times_posted": 1,
                         "post_dates": '["2025-01-01"]', "official_url": f"https://p{i}.example/",
                         "edugrants_url": None, "reactions_max": 1, "score": 1.0} for i in range(20)])
    monkeypatch.setattr("edugrants_agent.collectors.due_for_watch", lambda *a: True)
    for row in db.history_rows():
        db.set_page_hash(row["official_url"], "old")
    fetch = lambda url: Page(url, url, "changed", [])  # noqa: E731
    assert len(collect_history_watch({"every_hours": 0, "max_candidates": 5}, db, fetch)) == 5


def test_new_external_finds_are_researched_before_old_programmes(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "max_process_per_run", 2)
    db = DB(tmp_path / "o.db")
    db.replace_history([{"norm_title": "old prog", "title": "Old Prog", "category": "online", "country": "",
                         "age": "", "first_posted": "2025-01-01", "last_posted": "2025-01-01", "times_posted": 3,
                         "post_dates": "[]", "official_url": None, "edugrants_url": None, "reactions_max": 99,
                         "score": 2.0}])
    hid = db.history_rows()[0]["id"]
    for i, (title, hist, fit, when) in enumerate([("Old Prog", hid, 5, None),
                                                  ("New A", None, 4, "2026-09-30 10:00:00"),
                                                  ("New B", None, 4, "2026-10-01 10:00:00")]):
        iid = db.insert_item(source="s", url=f"https://x.example/{i}", canonical_url=f"https://x.example/{i}",
                             title=title, norm_title=title.lower(), summary="", published_at=when)
        db.update(iid, status="triaged", history_id=hist, fit_score=fit)
    researched = []
    p = Pipeline(db, llm=FakeLLM(), config={"audience": "x"}, options=OPTIONS, fetcher=fake_fetch)
    monkeypatch.setattr(p, "triage", lambda: None)
    monkeypatch.setattr(p, "research", lambda item: researched.append(item["title"]))
    p.process()
    assert researched == ["New B", "New A"]   # freshest new find first; old programme waits



def test_default_rules_are_only_uzbeks_and_no_application_fee(monkeypatch):
    """Nurbek's rule: if Uzbeks can apply and there's no application fee, show it."""
    monkeypatch.setattr(settings, "require_uz_yes", False)
    monkeypatch.setattr(settings, "require_free_participation", False)
    monkeypatch.setattr(settings, "check_ages", False)
    paid_tuition_any_age = {**BASE, "program_fee": "paid", "funding": "none", "age_min": 25, "age_max": 60,
                            "uzbekistan_eligible": "unclear"}
    assert Pipeline.reject_reason(paid_tuition_any_age) is None
    assert Pipeline.reject_reason({**BASE, "application_fee": "paid"}) == "application fee"
    assert "Uzbekistan eligibility no" in Pipeline.reject_reason({**BASE, "uzbekistan_eligible": "no"})
    from edugrants_agent.pipeline import hard_rules_text
    text = hard_rules_text()
    assert "Uzbekistan" in text and "application fee" in text and "NOT reasons to drop" in text


POST_READY = {**BASE, "title": "Global Youth Science Camp 2027", "host_country": "Online",
              "summary": "An online science camp for school pupils from all countries.",
              "benefits": ["Certificate", "Mentorship from scientists"], "registration_url": None}


def test_post_ready_find_fits_the_format():
    from edugrants_agent.pipeline import format_gaps
    assert format_gaps(POST_READY, "https://camp.example/apply") == []


@pytest.mark.parametrize("change,official,gap", [
    ({"benefits": []}, "https://camp.example/", "no benefits"),
    ({"host_country": "unknown"}, "https://camp.example/", "no country"),
    ({"format": "unknown"}, "https://camp.example/", "online/offline unknown"),
    ({"summary": ""}, "https://camp.example/", "no description"),
    ({}, None, "no official link"),
    ({}, "https://opportunitydesk.org/2026/10/01/camp/", "no official link"),   # aggregator is not a link we post
    ({"deadline": None, "deadline_type": "unknown"}, "https://camp.example/", "no deadline"),
])
def test_format_gaps(change, official, gap):
    from edugrants_agent.pipeline import format_gaps
    assert gap in format_gaps({**POST_READY, **change}, official)


def test_funded_programme_needs_no_online_offline():
    from edugrants_agent.pipeline import format_gaps
    assert format_gaps({**POST_READY, "funding": "full", "format": "unknown"}, "https://x.example/") == []


def test_post_links_to_the_organiser_not_the_aggregator():
    from edugrants_agent.pipeline import post_link
    d = {**POST_READY, "registration_url": "https://opportunitydesk.org/x"}
    assert post_link(d, "https://camp.example/") == "https://camp.example/"
    assert post_link({**d, "registration_url": "https://forms.gle/abc"}, "https://camp.example/") == "https://forms.gle/abc"


@pytest.mark.parametrize("ages,level,ok", [
    ((14, 18), [], True), ((18, 35), [], True), ((16, 25), [], True), ((None, None), [], True),
    ((21, 30), [], False),                        # starts after 20: not our audience
    ((25, 35), [], False), ((5, 9), [], False),
    ((25, 35), ["bachelor"], True),               # school or bachelor students: fine whatever the ages
    ((None, None), ["master", "phd"], False),     # master's/PhD only
])
def test_audience_must_accept_20_or_younger_or_be_school_or_bachelor(monkeypatch, ages, level, ok):
    from edugrants_agent.config import Settings
    for k in ("CHECK_AGES", "AGE_MIN", "AGE_MAX", "MIN_DAYS_LEFT"):
        monkeypatch.delenv(k, raising=False)
    fresh = Settings()
    assert fresh.check_ages and (fresh.age_min, fresh.age_max, fresh.min_days_left) == (10, 20, 4)
    monkeypatch.setattr(settings, "age_min", 10)
    monkeypatch.setattr(settings, "age_max", 20)
    data = {**BASE, "age_min": ages[0], "age_max": ages[1], "level": level}
    assert (Pipeline.reject_reason(data) is None) is ok


GOOD = {**POST_READY, "level": ["high_school"]}


@pytest.mark.parametrize("change,fit,history,why", [
    ({}, 4, None, None),                                                  # a good find
    ({}, 3, None, "fit 3"),                                               # only average for the channel
    ({}, 3, 7, None),                                                     # ...unless the channel posted it before
    ({"deadline": (date.today() + timedelta(days=10)).isoformat()}, 5, None, "deadline too close"),
    ({"age_min": 25, "age_max": 35, "level": []}, 5, None, "ages"),
    ({"age_min": 25, "age_max": 35}, 5, None, None),                     # but school pupils: fine
    ({"level": ["master", "phd"], "age_min": None, "age_max": None}, 5, None, "ages"),
    ({"level": [], "age_min": None, "age_max": None}, 5, None, "no age or level"),
])
def test_only_good_finds_reach_editors(monkeypatch, change, fit, history, why):
    from edugrants_agent.pipeline import final_check
    monkeypatch.setattr(settings, "min_days_left", 14)
    monkeypatch.setattr(settings, "min_show_fit", 4)
    monkeypatch.setattr(settings, "age_max", 20)
    reason = final_check({**GOOD, **change}, "https://camp.example/", fit, history)
    assert (reason is None) if why is None else (why in reason)


def test_quiet_day_still_researches_the_best_of_the_rest(monkeypatch, tmp_path):
    """Vibe filter keeps only 1 of 8: the best 'fit 2' ones are researched too (fit 1 never)."""
    from edugrants_agent.db import DB
    monkeypatch.setattr(settings, "min_fit_score", 3)
    monkeypatch.setattr(settings, "research_min_per_run", 4)
    monkeypatch.setattr(settings, "profile_file", tmp_path / "none.md")
    db = DB(":memory:")
    ids = [db.insert_item(source="s", url=f"https://a.example/{i}", canonical_url=f"https://a.example/{i}",
                          title=f"T{i}", norm_title=f"t{i}", summary="", published_at=None) for i in range(8)]
    fits = {ids[0]: 4, ids[1]: 2, ids[2]: 2, ids[3]: 2, ids[4]: 2, ids[5]: 1, ids[6]: 1, ids[7]: 1}

    class LLM:
        def triage(self, items, *a, **k):
            return {it["id"]: (fits[it["id"]] > 1, fits[it["id"]], "r") for it in items}
    p = Pipeline(db, llm=LLM(), config={}, options={})
    p.triage()
    triaged = [r["id"] for r in db.by_status("triaged")]
    assert len(triaged) == 4 and ids[0] in triaged and not set(triaged) & {ids[5], ids[6], ids[7]}


def test_short_day_also_revives_recent_near_misses(monkeypatch, tmp_path):
    from edugrants_agent.db import DB
    monkeypatch.setattr(settings, "research_min_per_run", 3)
    monkeypatch.setattr(settings, "profile_file", tmp_path / "none.md")
    db = DB(":memory:")
    old = db.insert_item(source="s", url="https://a.example/old", canonical_url="https://a.example/old",
                         title="Old near miss", norm_title="old", summary="", published_at=None)
    db.update(old, status="rejected", fit_score=2, reason="triage: low fit (2): small but ok")

    class LLM:
        def triage(self, items, *a, **k):
            return {}
    Pipeline(db, llm=LLM(), config={}, options={}).triage()
    assert db.get(old)["status"] == "triaged" and "small but ok" in db.get(old)["fit_reason"]
