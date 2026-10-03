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
    return d


def test_lists_only_grants_that_meet_the_two_rules():
    out = dashboard.build(sample())
    titles = {i["title"]: i for i in out["items"]}
    assert set(titles) == {"Grant queue", "Grant taken", "Grant skipped", "Grant format", "Grant masters"}
    assert titles["Grant queue"]["verdict"] == "queue" and titles["Grant taken"]["verdict"] == "taken"
    assert titles["Grant format"]["why"] == "Post formatiga mos emas: imtiyozlar yo'q, rasmiy havola yo'q"
    assert titles["Grant masters"]["why"].startswith("Faqat magistratura")
    assert titles["Grant skipped"]["why"] == "Muharrir: Bizga mos emas"
    f = out["funnel"]
    assert (f["found"], f["duplicate"], f["vibe_cut"], f["checked"], f["two_rules"], f["shown_to_editors"]) == (9, 1, 1, 7, 5, 3)
    assert dict(out["reasons"]) == {"Post formatiga mos emas": 1, "Faqat magistratura/PhD uchun": 1}


def run(coro_fn):
    async def go():
        d = sample()
        client = TestClient(TestServer(dashboard.make_app(lambda: d)))
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
        assert len(data["items"]) == 5
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
