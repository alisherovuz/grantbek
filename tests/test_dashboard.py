import asyncio
from datetime import date, timedelta

from aiohttp.test_utils import TestClient, TestServer

from edugrants_agent import dashboard
from edugrants_agent.config import settings
from edugrants_agent.db import DB

SOON = (date.today() + timedelta(days=10)).isoformat()
PAST = (date.today() - timedelta(days=3)).isoformat()


def item(d, n, status, data=None, reason=None, fit=3):
    iid = d.insert_item(source="opportunitydesk", url=f"https://agg.example/{n}", canonical_url=f"https://agg.example/{n}",
                        title=f"Grant {n}", norm_title=f"grant {n}", summary="", published_at=None)
    fields = {"status": status, "reason": reason, "fit_score": fit}
    if data is not None:
        fields.update(data_json={"title": f"Grant {n}", "uzbekistan_eligible": "yes", "deadline_type": "fixed",
                                 "deadline": SOON, "level": ["bachelor"], **data},
                      official_url=f"https://org.example/{n}")
    d.update(iid, **fields)
    return iid


POSTABLE = {"host_country": "Online", "format": "online", "summary": "An online programme for pupils from all countries.",
            "benefits": ["Certificate"], "level": ["high_school"]}


def sample():
    d = DB(":memory:")
    item(d, "queue", "extracted", {})
    item(d, "taken", "in_review", {})
    item(d, "skipped", "skipped", {}, reason="Bizga mos emas")
    item(d, "format", "rejected", {}, reason="post format: no benefits, no official link")
    item(d, "masters", "rejected", {"level": ["master"]}, reason="level: only master (fit 3)")
    item(d, "eu-only", "rejected", {"uzbekistan_eligible": "no"}, reason="Uzbekistan eligibility no (EU only)")
    item(d, "expired", "rejected", {"deadline": PAST}, reason="deadline too close (-3 days left)")
    item(d, "vibe", "rejected", None, reason="triage: small essay contest")
    item(d, "dup", "duplicate", None)
    item(d, "adults", "rejected", {"age_min": 25, "age_max": 35, "level": []}, reason="ages 25-35 outside 10-20")
    item(d, "famous-masters", "accepted", {"level": ["master"]}, fit=5)
    item(d, "fee-only", "rejected", {**POSTABLE, "application_fee": "paid"}, reason="application fee", fit=4)
    item(d, "fee-and-adults", "rejected", {**POSTABLE, "application_fee": "paid", "age_min": 25, "age_max": 35,
                                           "level": []}, reason="application fee", fit=4)
    return d


def test_lists_only_grants_that_meet_the_main_rules(monkeypatch):
    monkeypatch.setattr(settings, "check_ages", True)
    out = dashboard.build(sample())
    titles = {i["title"]: i for i in out["items"]}
    # dropped finds are hidden, except the one whose ONLY problem is the application fee
    assert set(titles) == {"Grant queue", "Grant taken", "Grant skipped", "Grant famous-masters", "Grant fee-only"}
    assert titles["Grant queue"]["verdict"] == "queue" and titles["Grant taken"]["verdict"] == "taken"
    assert titles["Grant fee-only"]["verdict"] == "fee" and "Ariza to'lovi" in titles["Grant fee-only"]["why"]
    assert titles["Grant skipped"]["why"] == "Muharrir: Bizga mos emas"
    f = out["funnel"]
    assert (f["found"], f["duplicate"], f["vibe_cut"], f["checked"], f["two_rules"], f["shown_to_editors"]) == (13, 1, 1, 11, 5, 4)
    reasons = dict(out["reasons"])   # still counted in the chart, just not listed
    assert reasons["Post formatiga mos emas"] == 1 and reasons["Ariza to'lovi bor"] == 2 and reasons["Faqat magistratura/PhD yoki kattalar uchun"] == 1
    assert any(r.startswith("20 yoshgacha") for r in out["rules"])


def run(coro_fn, bot=None):
    async def go():
        d = sample()
        client = TestClient(TestServer(dashboard.make_app(lambda: d, bot)))
        await client.start_server()
        try:
            return await coro_fn(client, d)
        finally:
            await client.close()
    return asyncio.run(go())


def test_needs_the_key(monkeypatch):
    monkeypatch.setattr(settings, "bot_token", "123:abc")

    async def check(client, d):
        assert (await client.get("/")).status == 403
        assert (await client.get("/api/data", headers={"X-Key": "wrong"})).status == 403
        ok = await client.get(f"/?key={dashboard.dashboard_key()}")
        assert ok.status == 200 and "EduGrants Finder" in await ok.text()
        data = await (await client.get("/api/data?days=7", headers={"X-Key": dashboard.dashboard_key()})).json()
        assert len(data["items"]) >= 5
    run(check)


def test_put_back_in_queue_skips_the_automatic_checks(monkeypatch):
    monkeypatch.setattr(settings, "bot_token", "123:abc")
    from edugrants_agent import bot as botmod

    async def check(client, d):
        rid = d.conn.execute("SELECT id FROM items WHERE title='Grant masters'").fetchone()[0]
        r = await client.post("/api/action", json={"action": "queue", "id": rid}, headers={"X-Key": dashboard.dashboard_key()})
        assert r.status == 200 and d.get(rid)["status"] == "extracted"
        botmod.STATE.update(db=d, pipeline=None)
        assert rid in [i["id"] for i, _ in botmod.ordered_finds()]   # not re-rejected by the master's rule
        taken = d.conn.execute("SELECT id FROM items WHERE title='Grant taken'").fetchone()[0]
        r = await client.post("/api/action", json={"action": "queue", "id": taken}, headers={"X-Key": dashboard.dashboard_key()})
        assert r.status == 409
    run(check)


def test_take_from_dashboard_marks_it_taken(monkeypatch):
    monkeypatch.setattr(settings, "bot_token", "123:abc")
    monkeypatch.setattr(settings, "write_on_accept", False)
    from edugrants_agent import bot as botmod

    async def check(client, d):
        botmod.STATE.update(db=d)
        rid = d.conn.execute("SELECT id FROM items WHERE title='Grant format'").fetchone()[0]
        r = await client.post("/api/action", json={"action": "take", "id": rid}, headers={"X-Key": dashboard.dashboard_key()})
        assert r.status == 200 and d.get(rid)["status"] == "accepted"
    run(check)


def test_cost_tracker_counts_today_and_per_job(monkeypatch):
    monkeypatch.setattr(settings, "timezone", "Asia/Tashkent")
    d = sample()
    d.log_usage("triage", "haiku", 1000, 100, 0.02)
    d.log_usage("extract", "haiku", 1000, 100, 0.05)
    d.log_usage("write", "sonnet", 1000, 100, 0.03)
    d.conn.execute("INSERT INTO llm_usage(at,purpose,model,input_tokens,output_tokens,cost_usd)"
                   " VALUES (datetime('now','-3 days'),'triage','haiku',1,1,0.5)")
    c = dashboard.costs(d, 7)
    assert c["today"] == 0.1 and c["week"] == 0.6 and c["total"] == 0.6
    assert c["jobs"][0] == {"job": "Saralash (vibe filtri)", "usd": 0.52, "calls": 2}
    assert len(c["daily"]) == 7 and c["daily"][-1]["usd"] == 0.1
    assert c["taken"] == 2 and c["per_taken"] == 0.3        # $0.60 / 2 taken grants


def test_skip_from_dashboard(monkeypatch):
    monkeypatch.setattr(settings, "bot_token", "123:abc")

    async def check(client, d):
        rid = d.conn.execute("SELECT id FROM items WHERE title='Grant queue'").fetchone()[0]
        r = await client.post("/api/action", json={"action": "skip", "id": rid, "reason": "fee"},
                              headers={"X-Key": dashboard.dashboard_key()})
        assert r.status == 200 and d.get(rid)["status"] == "skipped" and d.get(rid)["reason"] == "Pullik"
        assert "Grant queue (Pullik)" in d.feedback_examples()["skipped"]   # the agent learns from it
    run(check)


def test_search_button_and_system_panel(monkeypatch):
    """The dashboard can start a search; the short report still goes to Telegram."""
    monkeypatch.setattr(settings, "bot_token", "123:abc")
    monkeypatch.setattr(settings, "admin_chat_id", -100)
    monkeypatch.setattr(settings, "mode", "finder")
    from edugrants_agent import bot as botmod

    class Pipe:
        aggregators = set()
        def run(self, fast_only=False):
            import time
            time.sleep(0.2)
            return {"added": 7}

    sent = []

    class B:
        async def send_message(self, chat_id, text, reply_markup=None, reply_to_message_id=None):
            sent.append(text)

    async def check(client, d):
        botmod.STATE.update(db=d, pipeline=Pipe(), lock=asyncio.Lock())
        k = {"X-Key": dashboard.dashboard_key()}
        r = await client.post("/api/search", headers=k)
        assert r.status == 200
        sysinfo = await (await client.get("/api/system", headers=k)).json()
        assert sysinfo["searching"] is True
        assert (await client.post("/api/search", headers=k)).status == 409      # one at a time
        await asyncio.sleep(0.5)
        sysinfo = await (await client.get("/api/system", headers=k)).json()
        assert sysinfo["searching"] is False and sysinfo["last_search"]["seen"] == 7
        assert sent and sent[0].startswith("🔎 Dashboarddan qidiruv: 7 ta yangi e'lon")
    run(check, bot=B())


def test_reset_from_dashboard_keeps_channel_history_and_costs(monkeypatch):
    monkeypatch.setattr(settings, "bot_token", "123:abc")
    from edugrants_agent import bot as botmod

    async def check(client, d):
        botmod.STATE.update(db=d, lock=asyncio.Lock())
        d.log_usage("triage", "m", 1, 1, 0.1)
        d.replace_history([{"norm_title": "chevening", "title": "Chevening", "category": "", "country": "", "age": "",
                            "first_posted": "2025-01-01", "last_posted": "2025-01-01", "times_posted": 1, "post_dates": "[]",
                            "official_url": None, "edugrants_url": None, "reactions_max": 1, "score": 1.0}])
        k = {"X-Key": dashboard.dashboard_key()}
        r = await client.post("/api/reset", json={"costs": False}, headers=k)
        assert r.status == 200 and "13 ta topilma" in (await r.json())["message"]
        assert d.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
        assert len(d.history_rows()) == 1 and dashboard.costs(d, 7)["total"] == 0.1
        await client.post("/api/reset", json={"costs": True}, headers=k)
        assert dashboard.costs(d, 7)["total"] == 0
    run(check)
