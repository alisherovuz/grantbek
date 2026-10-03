"""Toshmat aka (manager), Ergash (finder), Mirzo (writer): the agent team in the Telegram group."""
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
    agents.BOTS.update(manager=toshmat, writer=mirzo)      # Ergash has no bot of his own yet
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


class Block:
    def __init__(self, type, **kw):
        self.type = type
        self.__dict__.update(kw)


def tool(name, **inp):
    tool._n = getattr(tool, "_n", 0) + 1
    return Block("tool_use", id=f"tu{tool._n}", name=name, input=inp)


class ScriptLLM:
    """Plays Toshmat aka's brain: each converse() call returns the next scripted step and records what
    it was shown (the system prompt, the conversation, the tool results)."""

    def __init__(self, *steps):
        self.steps, self.seen = list(steps), []

    def converse(self, purpose, model, system, messages, tools, max_tokens=4000):
        assert purpose == "order"
        self.seen.append({"system": system, "messages": [dict(m) for m in messages],
                          "tools": [t["name"] for t in tools]})
        return type("R", (), {"content": self.steps.pop(0)})()


def results_of(llm, step):
    """The tool results Toshmat aka was given before his step number `step`."""
    return [json.loads(r["content"]) for r in llm.seen[step]["messages"][-1]["content"]]


def test_order_sets_a_focus_for_ergash(team):
    d, toshmat, _ = team
    llm = ScriptLLM([tool("set_focus", text="school olympiads", days=5)], [Block("text", text="Xo'p, Ergashga aytdim!")])
    botmod.STATE["pipeline"] = type("P", (), {"llm": llm})()
    asyncio.run(manager.handle_order("bu hafta olimpiadalarni ko'proq top", toshmat))
    assert "Ergash" in llm.seen[0]["system"] and "set_focus" in llm.seen[0]["tools"]
    assert manager.focus_text(d) == "school olympiads"
    texts = [t for _, t, _ in toshmat.sent]
    assert "Ergash, 5 kun davomida" in texts[0] and texts[-1] == "Xo'p, Ergashga aytdim!"   # the answer comes last
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
    botmod.STATE["pipeline"] = type("P", (), {"llm": ScriptLLM([tool("pause_agent", agent="finder", paused=True)],
                                                               [Block("text", text="To'xtatdim.")])})()
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
    assert ran == [] and "Ergash to'xtatilgan" in d.events()[0]["text"]


BAKU_POST = ("International Dialogue on SDG 17, Baku 2026 forumi\n\nDavlat: Ozarbayjon 🇦🇿\n"
             "Moliyaviy ta'minot: To'liq va qisman\nYosh toifasi: 16–35 yosh\n\n"
             "Voice For Rights International Association (VFRI) tomonidan 23–26-noyabr kunlari Bokuda "
             "o'tkaziladigan 4 kunlik xalqaro forum.\n\n"
             '🔗Ro\'yxatdan o\'tish uchun: <a href="https://www.vfri.ca/international-dialogue-on-sdg17-baku-2026/">Havola</a>'
             "\n\n📌Ro'yxatdan o'tishning so'nggi muddati: 24-oktyabr\n\n⚡️@EduGrandsUz")

BAKU_FACTS = {
    "is_opportunity": True, "title": "International Dialogue on SDG17 - Baku 2026", "organizer": "VFRI",
    "host_country": "Azerbaijan", "host_country_iso2": "AZ", "opportunity_type": "forum", "level": ["any"],
    "funding": "partial", "format": "offline", "duration": "4 days", "application_fee": "paid",
    "program_fee": "paid", "age_min": None, "age_max": None, "deadline_type": "fixed", "deadline": "2026-10-24",
    "opening_date": None, "status": "open", "eligible_countries": "All countries", "uzbekistan_eligible": "yes",
    "summary": "A four-day forum in Baku on SDG 17 partnerships for young leaders.",
    "benefits": ["airfare", "accommodation", "certificate"], "eligibility": "All countries",
    "application_process": "Online form", "registration_url": "https://www.cognitoforms.com/VFRI/Baku2026",
    "verified_from_official": True, "conflicts": None, "confidence": "high"}

WRITTEN = {"title_uz": "International Dialogue on SDG 17, Baku 2026 forumi", "country_uz": "Ozarbayjon",
           "description_uz": "Bokuda 4 kunlik xalqaro forum.", "benefits_uz": ["Aviachipta", "Turar joy"],
           "age_category_uz": "16–35 yosh",
           "platform": {"title": "International Dialogue on SDG17, Baku 2026", "imkoniyat_turi": "Konferensiya",
                        "daraja": "Barcha", "moliyalashtirish": "Qisman", "format": "Oflayn", "davomiylik": "4 kun",
                        "ariza_tolovi": "Pullik", "description": "Bokuda forum.", "eligibility": "- 16-35 yosh",
                        "benefits": "- Aviachipta", "application_process": "1. Onlayn forma",
                        "additional_information": ""}}


def test_the_baku_order_really_reaches_mirzo(team, monkeypatch):
    """The screenshot case: the owner replies to Mirzo's post with "Toshmat, Mirzoga ayt bunga vebsaytga moslab
    data yozib bersin". Before, Toshmat said he would and nothing happened. Now the finder reads the page, Mirzo
    writes the post and the listing, the listing is posted in the group, and only then Toshmat answers."""
    from edugrants_agent.fetch import Page
    from edugrants_agent.pipeline import Pipeline
    d, toshmat, mirzo = team
    monkeypatch.setattr(settings, "mode", "finder")
    monkeypatch.setattr(settings, "write_on_accept", True)

    async def delete(self):
        return True
    monkeypatch.setattr(Msg, "delete", delete, raising=False)

    class LLM(ScriptLLM):
        written_with = None

        def extract(self, title, official_text, official_url, agg_text, agg_url):
            assert "Voice For Rights" in official_text and "Davlat: Ozarbayjon" in agg_text   # page AND the post
            return dict(BAKU_FACTS)

        def find_official(self, *a):
            raise AssertionError("the link is the organiser's own page, no need to look for it")

        def write(self, data, options, notes=None):
            LLM.written_with = notes
            return WRITTEN

    llm = LLM([tool("write_post", text=BAKU_POST, notes="Yosh toifasi: 16-35")],
              [tool("show_platform_listing", item_id=1)],
              [Block("text", text="Tayyor! Mirzo #1 uchun post va sayt ma'lumotini yozdi. Ariza pullik, e'tibor bering.")])
    page = Page(url="u", final_url="https://www.vfri.ca/international-dialogue-on-sdg17-baku-2026/",
                text="Voice For Rights International. " * 20)
    botmod.STATE["pipeline"] = Pipeline(d, llm=llm, config={}, options={}, fetcher=lambda url: page)

    asyncio.run(manager.handle_order("Toshmat, Mirzoga ayt bunga vebsaytga moslab data yozib bersin", toshmat,
                                     replied="Mirzo Qalami O'tkir: " + BAKU_POST, reply_to=55))

    shown = llm.seen[0]["messages"][0]["content"]
    assert "The owner is replying to this message" in shown and "vfri.ca" in shown   # he sees the post and its link
    item = d.get(1)
    assert item["status"] == "in_review" and item["source"] == "owner"
    assert json.loads(item["platform_json"])["imkoniyat_turi"] == "Konferensiya"
    assert LLM.written_with == "Yosh toifasi: 16-35"                                # the owner's correction reached Mirzo
    first = results_of(llm, 1)[0]
    assert first["ok"] and first["item_id"] == 1 and "listing" in first
    assert results_of(llm, 2)[0] == {"ok": True, "item_id": 1, "sent_to_group": True}
    mirzo_said = [t for _, t, _ in mirzo.sent]
    assert any("Ozarbayjon" in t and "Chop etish" not in t for t in mirzo_said)       # the draft with publish buttons
    assert any(t.startswith("📋 <b>edugrants.uz uchun ma'lumot</b> · #1") and "Konferensiya" in t for t in mirzo_said)
    toshmat_said = [t for _, t, _ in toshmat.sent]
    assert any(t.startswith("Mirzo, «International Dialogue") and "16-35" in t for t in toshmat_said)
    assert toshmat_said[-1].startswith("Tayyor! Mirzo #1")                          # the answer comes after the work
    assert any("Kanal qoidasiga to'g'ri kelmaydi: application fee" in t for t in toshmat_said)  # the fee is flagged
    tasks = {t["type"]: t for t in d.tasks()}
    assert json.loads(tasks["order"]["result"])["tools"] == ["write_post", "show_platform_listing"]
    assert tasks["check"]["agent"] == "finder" and tasks["write_post"]["status"] == "done"


def test_a_failed_tool_is_reported_as_failed(team):
    """Toshmat aka is told the tool failed, so he can't say it worked."""
    d, toshmat, _ = team
    llm = ScriptLLM([tool("show_platform_listing", item_id=999)], [Block("text", text="#999 topilmadi.")])
    botmod.STATE["pipeline"] = type("P", (), {"llm": llm})()
    asyncio.run(manager.handle_order("999 ning sayt ma'lumotini ber", toshmat))
    out = llm.seen[1]["messages"][-1]["content"][0]
    assert out["is_error"] and json.loads(out["content"]) == {"ok": False, "error": "no item #999"}
    assert toshmat.sent[-1][1] == "#999 topilmadi."


def test_the_hidden_havola_link_reaches_toshmat():
    """A forwarded post's "Havola" is a hidden link: the order text keeps its address."""
    class M:
        html_text = 'Toshmat buni tekshir <a href="https://x.org/apply">Havola</a>'
        reply_to_message = type("R", (), {"html_text": "Eski post", "from_user": type("U", (), {"first_name": "Mirzo"})()})()
    assert "https://x.org/apply" in botmod.message_html(M())
    assert botmod.replied_context(M()) == "Mirzo: Eski post"
    from edugrants_agent.orders import first_opportunity_link
    assert first_opportunity_link('<a href="https://t.me/EduGrandsUz">x</a> <a href="https://x.org/apply">H</a>') \
        == "https://x.org/apply"


# --------------------------------------------------------------------------- who speaks through which bot
def test_agents_without_their_own_bot_speak_with_a_name_tag(team):
    d, toshmat, mirzo = team
    asyncio.run(agents.say("finder", "3 ta grant topdim"))
    asyncio.run(agents.say("writer", "Post tayyor"))
    assert toshmat.sent[-1][1] == "🔎 <b>Ergash</b>: 3 ta grant topdim"     # through Toshmat's bot, tagged
    assert mirzo.sent[-1][1] == "Post tayyor"                               # his own bot: no tag needed
    assert [e["agent"] for e in d.events()][:2] == ["writer", "finder"]     # and both are in the event log


def test_only_toshmat_answers_typed_messages(team):
    _, toshmat, mirzo = team
    assert agents.is_manager_bot(toshmat) and not agents.is_manager_bot(mirzo)
