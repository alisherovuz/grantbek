"""Toshmat aka (manager), Eshmat (finder), Mirzo (writer): the agent team in the Telegram group."""
import asyncio
import json
from datetime import date, datetime

import pytest

from edugrants_agent import agents, manager
from edugrants_agent import bot as botmod
from edugrants_agent.config import settings
from edugrants_agent.db import DB
from edugrants_agent.weekly import build_weekly, parse_deadline, post_flag, remember_post

CHEVENING = """Chevening Scholarship

Davlat: Buyuk Britaniya 🇬🇧
Moliyaviy ta'minot: To'liq
Yosh toifasi: 18+

Buyuk Britaniyada magistratura.

🔗Ro'yxatdan o'tish uchun: Havola

📌Ro'yxatdan o'tishning so'nggi muddati: 6-oktyabr

⚡️@EduGrandsUz"""


class Msg:
    _n = 100

    def __init__(self, bot, text, markup):
        Msg._n += 1
        self.bot, self.text, self.markup, self.message_id = bot, text, markup, Msg._n

    async def edit_reply_markup(self, reply_markup=None):
        self.markup = reply_markup

    async def answer(self, text, reply_markup=None):
        return await self.bot.send_message(-100, text, reply_markup=reply_markup)

    def buttons(self):
        return {b.text: (b.callback_data or b.url) for row in (self.markup.inline_keyboard if self.markup else []) for b in row}


class Bot:
    def __init__(self, name, bot_id):
        self.name, self.id, self.sent = name, bot_id, []

    async def send_message(self, chat_id, text, reply_markup=None, reply_to_message_id=None):
        m = Msg(self, text, reply_markup)
        self.sent.append((chat_id, text, m))
        return m


@pytest.fixture
def team(monkeypatch):
    monkeypatch.setattr(settings, "admin_chat_id", -100)
    monkeypatch.setattr(settings, "channel_id", "@EduGrandsUz")
    monkeypatch.setattr(settings, "channel_handle", "@EduGrandsUz")
    monkeypatch.setattr(settings, "admin_user_ids", {7})
    d = DB(":memory:")
    botmod.STATE.update(db=d, lock=asyncio.Lock(), weekly_edits={}, pending_edits={})
    agents._DB["get"] = lambda: d
    toshmat, mirzo = Bot("toshmat", 1), Bot("mirzo", 3)
    agents.BOTS.clear()
    agents.BOTS.update(manager=toshmat, writer=mirzo)      # Eshmat has no bot of his own yet
    yield d, toshmat, mirzo
    agents.BOTS.clear()


# --------------------------------------------------------------------------- reading our channel's posts
@pytest.mark.parametrize("line,posted,expected", [
    ("so'nggi muddati: 6-oktyabr", date(2026, 9, 28), date(2026, 10, 6)),
    ("so‘nggi muddati: 15 noyabr 2026", date(2026, 9, 28), date(2026, 11, 15)),
    ("so'nggi muddati: 30.09.2026", date(2026, 9, 1), date(2026, 9, 30)),
    ("so'nggi muddati: 10-yanvar", date(2026, 12, 20), date(2027, 1, 10)),       # next January
    ("so'nggi muddati: 1-dekabr, 23:59", date(2026, 10, 1), date(2026, 12, 1)),
    ("so'nggi muddati: e'lon qilinadi", date(2026, 10, 1), None),
])
def test_deadline_is_read_from_our_post(line, posted, expected):
    assert parse_deadline("📌Ro'yxatdan o'tishning " + line, posted) == expected


def test_flag_comes_from_the_davlat_line():
    assert post_flag(CHEVENING) == "🇬🇧"
    assert post_flag("Title\nDavlat: Onlayn\n") is None


def test_monday_list_is_in_the_channel_format():
    d = DB(":memory:")
    remember_post(d, 3668, datetime(2026, 9, 28, 22, 30), CHEVENING)
    remember_post(d, 3643, datetime(2026, 9, 20), CHEVENING.replace("Chevening Scholarship", "Lumiere Research Scholars Fall")
                  .replace("Buyuk Britaniya 🇬🇧", "AQSh 🇺🇸").replace("6-oktyabr", "8-oktyabr"))
    remember_post(d, 3600, datetime(2026, 9, 1), CHEVENING.replace("Chevening Scholarship", "Next week thing")
                  .replace("6-oktyabr", "14-oktyabr"))
    text, rows = build_weekly(d, "@EduGrandsUz", today=date(2026, 10, 5))       # Monday
    assert [r["msg_id"] for r in rows] == [3668, 3643]
    assert text == ("🎓Bu hafta muddati tugaydigan dasturlar ro’yxati:\n\n"
                    '- <a href="https://t.me/EduGrandsUz/3668">Chevening Scholarship</a> 🇬🇧\n\n'
                    '- <a href="https://t.me/EduGrandsUz/3643">Lumiere Research Scholars Fall</a> 🇺🇸\n\n'
                    "Imkoniyatdan foydalanib qoling. Tanlov o’z qo’lingizda!⚡️\n\n"
                    '⚡️<a href="https://t.me/EduGrandsUz">@EduGrandsUz</a>')


def test_own_channel_posts_are_remembered_by_the_collector():
    from edugrants_agent.collectors import handle_telegram_posts

    class M:
        id, message, entities = 3700, CHEVENING, []
        date = datetime(2026, 10, 1, 10, 0)
    d = DB(":memory:")
    handle_telegram_posts({"role": "own"}, d, [("EduGrandsUz", M())], [])
    assert d.conn.execute("SELECT deadline FROM channel_posts WHERE msg_id=3700").fetchone()[0] == "2026-10-06"


# --------------------------------------------------------------------------- Mirzo: Monday list
def test_monday_list_goes_to_the_group_then_to_the_channel(team):
    d, toshmat, mirzo = team
    remember_post(d, 3668, datetime.now(), CHEVENING.replace("6-oktyabr", date.today().strftime("%d.%m.%Y")))
    assert asyncio.run(manager.weekly_list()) == "ready"
    assert "Mirzo, bu haftaning" in toshmat.sent[0][1]                       # the boss hands out the task
    post = [m for _, t, m in mirzo.sent if t.startswith("🎓Bu hafta")][0]     # Mirzo posts it with his own bot
    assert set(post.buttons()) == {"✅ Chop etish", "✏️ Tahrirlash", "❌ Bekor"}
    task_id = int(post.buttons()["✅ Chop etish"].split(":")[2])
    assert d.task(task_id)["status"] == "waiting"

    class CB:
        data = post.buttons()["✅ Chop etish"]
        message = post
        from_user = type("U", (), {"id": 7, "first_name": "Nurbek"})()
        async def answer(self, *a, **k): pass
    asyncio.run(botmod.on_weekly(CB(), mirzo))
    channel = [t for c, t, _ in toshmat.sent if c == "@EduGrandsUz"]           # the channel post comes from Toshmat's bot
    assert channel and channel[0].startswith("🎓Bu hafta")
    assert d.task(task_id)["status"] == "done" and "✅ Chop etildi" in post.buttons()


def test_empty_week_says_so(team):
    d, toshmat, mirzo = team
    assert asyncio.run(manager.weekly_list()) == "empty"
    assert "topilmadi" in mirzo.sent[-1][1]


# --------------------------------------------------------------------------- Toshmat: report and orders
def test_daily_report_has_numbers_and_tips(team, monkeypatch):
    d, toshmat, _ = team
    d.log_usage("triage", "m", 1, 1, 0.12)
    monkeypatch.setattr(manager, "suggestions", lambda n: ["opportunitydesk 2 kundan beri ishlamayapti"])
    text = asyncio.run(manager.daily_report())
    assert "Kunlik hisobot" in text and "$0.12" in text and "opportunitydesk" in text
    assert toshmat.sent[-1][1] == text                       # Toshmat posts it himself, no tag
    assert d.tasks()[0]["type"] == "daily_report" and d.tasks()[0]["status"] == "done"


class OrderLLM:
    def __init__(self, decision):
        self.decision = decision

    def _call(self, purpose, model, system, user, tool, max_tokens=500):
        assert "Eshmat" in system and purpose == "order"
        return self.decision


def test_order_sets_a_focus_for_eshmat(team):
    d, toshmat, _ = team
    botmod.STATE["pipeline"] = type("P", (), {"llm": OrderLLM({"action": "set_focus", "focus_text": "school olympiads",
                                                                "focus_days": 5, "reply": "Xo'p, Eshmatga aytaman!"})})()
    asyncio.run(manager.handle_order("bu hafta olimpiadalarni ko'proq top", toshmat))
    assert manager.focus_text(d) == "school olympiads"
    texts = [t for _, t, _ in toshmat.sent]
    assert texts[0] == "Xo'p, Eshmatga aytaman!" and "Eshmat, 5 kun davomida" in texts[1]
    from edugrants_agent.pipeline import Pipeline

    class LLM:
        def triage(self, items, audience, *a, **k):
            LLM.audience = audience
            return {}
    d.insert_item(source="s", url="https://a.example/1", canonical_url="https://a.example/1", title="T",
                  norm_title="t", summary="", published_at=None)
    Pipeline(d, llm=LLM(), config={}, options={}).triage()
    assert "THIS WEEK THE OWNER ASKED FOR MORE OF: school olympiads" in LLM.audience


def test_pause_stops_the_daily_search_but_not_manual_ones(team):
    d, toshmat, _ = team
    botmod.STATE["pipeline"] = type("P", (), {"llm": OrderLLM({"action": "pause_daily_search", "reply": "To'xtatdim."})})()
    asyncio.run(manager.handle_order("qidiruvni to'xtat", toshmat))
    ran = []

    async def fake_cycle(*a, **k):
        ran.append(1)
    botmod_run = botmod.run_cycle
    botmod.run_cycle = fake_cycle
    try:
        asyncio.run(botmod.scheduled_search(toshmat))
    finally:
        botmod.run_cycle = botmod_run
    assert ran == [] and "Eshmat to'xtatilgan" in d.events()[0]["text"]


# --------------------------------------------------------------------------- who speaks through which bot
def test_agents_without_their_own_bot_speak_with_a_name_tag(team):
    d, toshmat, mirzo = team
    asyncio.run(agents.say("finder", "3 ta grant topdim"))
    asyncio.run(agents.say("writer", "Post tayyor"))
    assert toshmat.sent[-1][1] == "🔎 <b>Eshmat</b>: 3 ta grant topdim"     # through Toshmat's bot, tagged
    assert mirzo.sent[-1][1] == "Post tayyor"                               # his own bot: no tag needed
    assert [e["agent"] for e in d.events()][:2] == ["writer", "finder"]     # and both are in the event log


def test_only_toshmat_answers_typed_messages(team):
    _, toshmat, mirzo = team
    assert agents.is_manager_bot(toshmat) and not agents.is_manager_bot(mirzo)
