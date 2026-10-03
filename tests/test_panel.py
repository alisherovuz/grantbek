"""The team panel: Home, Boshqaruv (settings), Postlar, Muloqot, budgets and pauses."""
import asyncio
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from edugrants_agent import agents, controls, dashboard
from edugrants_agent import bot as botmod
from edugrants_agent.config import settings
from edugrants_agent.db import DB


class Sent:
    def __init__(self, mid):
        self.message_id = mid


class Bot:
    def __init__(self):
        self.sent, self.edits = [], []

    async def send_message(self, chat_id, text, reply_markup=None, reply_to_message_id=None):
        self.sent.append((chat_id, text))
        return Sent(4000 + len(self.sent))

    async def edit_message_reply_markup(self, chat_id=None, message_id=None, reply_markup=None):
        self.edits.append(message_id)


@pytest.fixture
def panel(monkeypatch):
    monkeypatch.setattr(settings, "bot_token", "123:abc")
    monkeypatch.setattr(settings, "channel_id", "@EduGrandsUz")
    monkeypatch.setattr(settings, "channel_handle", "@EduGrandsUz")
    monkeypatch.setattr(settings, "admin_chat_id", -100)
    for k in controls.EDITABLE:                      # leave the real settings as they were
        monkeypatch.setattr(settings, k, getattr(settings, k))
    d = DB(":memory:")
    toshmat = Bot()
    agents.BOTS.clear()
    agents.BOTS["manager"] = toshmat
    agents._DB["get"] = lambda: d
    botmod.STATE.update(db=d, lock=asyncio.Lock(), scheduler=None)
    yield d, toshmat
    agents.BOTS.clear()


def call(d, bot, steps):
    async def go():
        client = TestClient(TestServer(dashboard.make_app(lambda: d, bot)))
        await client.start_server()
        try:
            return await steps(client, {"X-Key": dashboard.dashboard_key()})
        finally:
            await client.close()
    return asyncio.run(go())


def ready_post(d, title="Diamond Challenge 2027", review=77):
    iid = d.insert_item(source="s", url=f"https://x.example/{title}", canonical_url=f"https://x.example/{title}",
                        title=title, norm_title=title.lower(), summary="", published_at=None)
    d.update(iid, status="in_review", post_text=f"<b>{title}</b>\n\n📌Ro'yxatdan o'tishning so'nggi muddati: 14-yanvar",
             platform_json={}, review_message_id=review, data_json={"title": title})
    return iid


def test_home_shows_the_team_and_what_waits_for_you(panel):
    d, toshmat = panel
    ready_post(d)
    d.add_qa("dm", "Reklama narxi?", "Adminlar bog'lanadi", "reklama", escalated=True, who="Jasur")
    d.add_qa("comment", "Muddati?", "14-yanvar", "muddat", who="Ali")

    async def steps(c, k):
        return await (await c.get("/api/home", headers=k)).json()
    h = call(d, toshmat, steps)
    names = [a["name"] for a in h["agents"]]
    assert names == ["Toshmat aka", "Ergash", "Mirzo", "GrantBek"]
    grantbek = h["agents"][3]
    assert grantbek["state"] == "off" and grantbek["bot"] == "none"          # no token yet
    assert [p["title"] for p in h["inbox"]["posts"]] == ["Diamond Challenge 2027"]
    assert [x["who"] for x in h["inbox"]["people"]] == ["Jasur"]              # only the hand-off, not every answer
    assert h["week"]["answered"] == 2


def test_publishing_from_the_web_posts_to_the_channel_and_updates_telegram(panel):
    d, toshmat = panel
    iid = ready_post(d)

    async def steps(c, k):
        r = await c.post("/api/post", json={"action": "publish", "id": iid, "text": "<b>Diamond Challenge 2027</b> tahrirlangan"},
                         headers=k)
        return await r.json()
    out = call(d, toshmat, steps)
    assert out["url"] == "https://t.me/EduGrandsUz/4001"
    assert toshmat.sent == [("@EduGrandsUz", "<b>Diamond Challenge 2027</b> tahrirlangan")]   # the edited text
    assert d.get(iid)["status"] == "published" and toshmat.edits == [77]                 # Telegram card updated


def test_weekly_list_from_the_web(panel):
    d, toshmat = panel
    t = d.add_task("weekly_list", "writer", status="waiting")
    d.update_task(t, "waiting", {"items": 2, "text": "🎓Bu hafta..."})

    async def steps(c, k):
        await c.post("/api/post", json={"kind": "weekly", "action": "save", "id": t, "text": "🎓Bu hafta (tuzatilgan)"}, headers=k)
        return await (await c.post("/api/post", json={"kind": "weekly", "action": "publish", "id": t}, headers=k)).json()
    out = call(d, toshmat, steps)
    assert out["message"] == "Chop etildi" and toshmat.sent[-1][1] == "🎓Bu hafta (tuzatilgan)"
    assert d.task(t)["status"] == "done"


def test_settings_saved_on_the_dashboard_win_and_are_checked(panel):
    d, toshmat = panel

    async def steps(c, k):
        bad = await c.post("/api/settings", json={"run_at": "9 am"}, headers=k)
        good = await c.post("/api/settings", json={"min_days_left": "6", "check_ages": False, "run_at": "08:15",
                                                    "budget_finder": "0.5"}, headers=k)
        got = await (await c.get("/api/settings", headers=k)).json()
        return bad.status, (await bad.json())["error"], good.status, got
    bad_status, err, good_status, got = call(d, toshmat, steps)
    assert bad_status == 400 and "Kunlik qidiruv vaqti" in err
    assert good_status == 200 and settings.min_days_left == 6 and settings.check_ages is False
    assert settings.run_at == "08:15" and settings.budget_finder == 0.5
    assert got["values"]["min_days_left"] == 6 and got["fields"]["run_at"]["group"] == "finder"
    # after a restart the saved values come back over the variables
    settings.min_days_left = 4
    controls.apply(d)
    assert settings.min_days_left == 6


def test_pausing_and_budgets_stop_an_agent(panel):
    d, toshmat = panel

    async def steps(c, k):
        await c.post("/api/agent", json={"agent": "writer", "paused": True}, headers=k)
        return await (await c.get("/api/home", headers=k)).json()
    h = call(d, toshmat, steps)
    assert controls.is_paused(d, "writer") and h["agents"][2]["state"] == "paused"
    settings.budget_finder = 0.10
    d.log_usage("triage", "m", 1, 1, 0.12)
    assert controls.over_budget(d, "finder") and controls.may_work(d, "finder") == "bugungi byudjet tugadi"
    out = asyncio.run(botmod.run_cycle(toshmat, manual=True))
    assert out.startswith("💸 Ergash bugungi AI byudjetini tugatdi")


def test_your_correction_teaches_grantbek(panel):
    d, toshmat = panel
    qid = d.add_qa("comment", "Ruscha topshirsa bo'ladimi?", "Bilmayman", "til", who="Madina")

    async def steps(c, k):
        await c.post("/api/community", json={"action": "correct", "id": qid,
                                             "text": "Ha, ariza ingliz yoki rus tilida topshiriladi"}, headers=k)
        return await (await c.get("/api/community", headers=k)).json()
    page = call(d, toshmat, steps)
    assert page["qa"][0]["correction"].startswith("Ha, ariza")

    from edugrants_agent import community
    seen = {}

    class LLM:
        def _call(self, purpose, model, system, user, tool, max_tokens=700):
            seen["system"] = system
            return {"action": "ignore", "reply": "", "topic": ""}
    botmod.STATE["pipeline"] = type("P", (), {"llm": LLM()})()
    community.decide("context", "Ruschada bo'ladimi?", "izoh")
    assert "THE OWNER CORRECTED THESE ANSWERS" in seen["system"] and "rus tilida topshiriladi" in seen["system"]


def test_changing_times_replans_the_schedule(panel):
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    d, toshmat = panel
    botmod.STATE.update(scheduler=AsyncIOScheduler(timezone="Asia/Tashkent"), bot=toshmat)
    botmod.plan_jobs()
    assert {j.id for j in botmod.STATE["scheduler"].get_jobs()} >= {"search", "report", "weekly", "refresh"}

    async def steps(c, k):
        return (await c.post("/api/settings", json={"report_at": "20:15", "run_at": "07:45"}, headers=k)).status
    assert call(d, toshmat, steps) == 200
    jobs = {j.id: str(j.trigger) for j in botmod.STATE["scheduler"].get_jobs()}
    assert "hour='20', minute='15'" in jobs["report"] and "hour='7', minute='45'" in jobs["search"]
    botmod.STATE["scheduler"] = None
