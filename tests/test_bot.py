import asyncio

from edugrants_agent import bot as botmod
from edugrants_agent.config import settings
from edugrants_agent.db import DB


# The fields every post needs (see pipeline.format_gaps)
POSTABLE = {"host_country": "Online", "summary": "An online programme for students from all countries.",
            "benefits": ["Certificate"], "format": "online"}


class FakeMsg:
    def __init__(self, bot, chat_id, text):
        self.bot, self.chat_id, self.text, self.message_id = bot, chat_id, text, len(bot.sent)

    async def edit_text(self, text, reply_markup=None):
        self.bot.edits.append(text)


class FakeBot:
    def __init__(self):
        self.sent, self.edits = [], []

    async def send_message(self, chat_id, text, reply_markup=None):
        m = FakeMsg(self, chat_id, text)
        self.sent.append((chat_id, text))
        return m


class FakePipeline:
    def __init__(self, delay=0):
        self.runs, self.delay = 0, delay

    def run(self, fast_only=False):
        import time
        time.sleep(self.delay)
        self.runs += 1
        return {"added": 12, "today": {}}


def setup(monkeypatch, delay=0):
    monkeypatch.setattr(settings, "admin_chat_id", -100)
    monkeypatch.setattr(settings, "admin_user_ids", {7})
    monkeypatch.setattr(settings, "mode", "finder")
    botmod.STATE.update(db=DB(":memory:"), pipeline=FakePipeline(delay), lock=asyncio.Lock(),
                        push_lock=asyncio.Lock())


def test_button_runs_a_search(monkeypatch):
    setup(monkeypatch)
    b = FakeBot()
    asyncio.run(botmod.manual_search(b, -100, 7))
    assert botmod.STATE["pipeline"].runs == 1
    assert b.sent[0][1].startswith("🔎 Qidirilmoqda")
    assert b.edits == ["✅ Qidiruv tugadi: 12 ta yangi e'lon ko'rildi, mos keladigan yangisi yo'q."]


def test_button_refuses_strangers(monkeypatch):
    setup(monkeypatch)
    b = FakeBot()
    asyncio.run(botmod.manual_search(b, -100, 999))
    assert botmod.STATE["pipeline"].runs == 0 and b.sent == [(-100, "Ruxsat yo'q.")]


def test_second_press_while_running_does_not_start_another(monkeypatch):
    setup(monkeypatch, delay=0.3)
    b = FakeBot()

    async def both():
        first = asyncio.create_task(botmod.manual_search(b, -100, 7))
        await asyncio.sleep(0.1)
        await botmod.manual_search(b, -100, 7)
        await first
    asyncio.run(both())
    assert botmod.STATE["pipeline"].runs == 1
    assert any("allaqachon ketmoqda" in t for _, t in b.sent)


def test_daily_run_always_reports(monkeypatch):
    setup(monkeypatch)
    b = FakeBot()
    asyncio.run(botmod.run_cycle(b, notify=True))
    assert b.sent == [(-100, "☀️ Bugungi qidiruv: 12 ta yangi e'lon ko'rildi, mos keladigan yangisi yo'q.")]


def test_check_reports_instead_of_crashing(monkeypatch, tmp_path, capsys):
    from edugrants_agent import check, collectors
    monkeypatch.setattr(settings, "anthropic_api_key", None)
    monkeypatch.setattr(settings, "bot_token", None)
    monkeypatch.setattr(settings, "db_path", tmp_path / "c.db")

    def boom(*a, **k):
        raise RuntimeError("403 Forbidden")
    monkeypatch.setattr(collectors, "collect_rss", boom)
    assert check.run_check(2) == 1
    out = capsys.readouterr().out
    assert "ANTHROPIC_API_KEY missing" in out and "403 Forbidden" in out and "problem(s)" in out


def test_first_start_imports_history(monkeypatch, tmp_path):
    from test_finder import write_export
    from datetime import datetime
    f = write_export(tmp_path, datetime(2025, 11, 1, 10), datetime(2026, 9, 1, 10))
    monkeypatch.setattr(settings, "history_file", f)
    monkeypatch.setattr(settings, "profile_file", tmp_path / "profile.md")
    d = DB(tmp_path / "x.db")
    botmod.auto_import_history(d)
    assert len(d.history_rows()) == 2 and (tmp_path / "profile.md").exists()
    botmod.auto_import_history(d)          # second start: nothing happens
    assert len(d.history_rows()) == 2


def test_bot_main_starts(monkeypatch, tmp_path):
    """Runs the real startup code (bot object, scheduler, command menu) with Telegram's network calls faked."""
    from aiogram import Bot, Dispatcher
    from aiogram.client.default import DefaultBotProperties  # noqa: F401
    monkeypatch.setattr(settings, "bot_token", "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
    monkeypatch.setattr(settings, "admin_chat_id", -100)
    monkeypatch.setattr(settings, "db_path", tmp_path / "b.db")
    monkeypatch.setattr(settings, "history_file", tmp_path / "none.html")
    started = {}

    async def fake_commands(self, commands, **k):
        started["commands"] = [c.command for c in commands]
        return True

    async def fake_polling(self, bot, **k):
        started["parse_mode"] = bot.default.parse_mode
        started["no_preview"] = bot.default.link_preview_is_disabled

    monkeypatch.setattr(Bot, "set_my_commands", fake_commands)
    monkeypatch.setattr(Dispatcher, "start_polling", fake_polling)
    monkeypatch.setattr(botmod, "Pipeline", lambda db: FakePipeline())
    asyncio.run(botmod.main())
    assert started == {"commands": ["find", "more", "panel", "stats", "health", "help"],
                       "parse_mode": "HTML", "no_preview": True}



def test_cards_show_new_external_finds_before_old_programmes(monkeypatch, tmp_path):
    import json as _json
    setup(monkeypatch)
    d = botmod.STATE["db"]
    d.replace_history([{"norm_title": "old", "title": "Old", "category": "online", "country": "", "age": "",
                        "first_posted": "2025-01-01", "last_posted": "2025-01-01", "times_posted": 2,
                        "post_dates": "[]", "official_url": None, "edugrants_url": None, "reactions_max": 50,
                        "score": 1.5}])
    hid = d.history_rows()[0]["id"]
    data = {**POSTABLE, "title": "t", "deadline_type": "rolling", "format": "online", "funding": "none", "program_fee": "free",
            "application_fee": "free", "verified_from_official": True}
    for i, (hist, fit) in enumerate([(hid, 5), (None, 4)]):
        iid = d.insert_item(source="s", url=f"https://y.example/{i}", canonical_url=f"https://y.example/{i}",
                            title=f"T{i}", norm_title=f"t{i}", summary="", published_at="2026-10-01 08:00:00")
        d.update(iid, status="extracted", history_id=hist, fit_score=fit, data_json={**data, "title": f"T{i}"},
                 official_url=f"https://y.example/{i}", official_canonical=f"https://y.example/{i}")
    order = [item["title"] for item, _ in botmod.ordered_finds(10)]
    assert order == ["T1", "T0"]



def test_olamiz_writes_the_post_and_sends_it_for_review(monkeypatch):
    """Pressing ✅ Olamiz: marks it taken, writes the post, sends the draft with publish buttons."""
    setup(monkeypatch)
    monkeypatch.setattr(settings, "write_on_accept", True)
    d = botmod.STATE["db"]
    data = {"title": "Global Camp 2027", "deadline_type": "rolling", "format": "online", "funding": "none"}
    iid = d.insert_item(source="s", url="https://c.example/", canonical_url="https://c.example/", title="Global Camp 2027",
                        norm_title="global camp", summary="", published_at=None)
    d.update(iid, status="shown", data_json=data, official_url="https://c.example/")

    class Writer(FakePipeline):
        def write(self, item):
            d.update(item["id"], status="drafted", post_text="<b>Global Camp 2027</b>", platform_json={})
    botmod.STATE["pipeline"] = Writer()
    sent, edits = [], []

    class Msg:
        message_id = 50
        async def edit_reply_markup(self, reply_markup=None):
            edits.append(reply_markup.inline_keyboard[0][0].text)
        async def reply(self, text):
            sent.append(text)
            class Note:
                async def delete(self_inner): sent.append("deleted")
                async def edit_text(self_inner, t): sent.append(t)
            return Note()

    class CB:
        data = f"ok:{iid}"
        from_user = type("U", (), {"id": 7, "first_name": "Nurbek"})()
        message = Msg()
        async def answer(self, *a, **k): pass

    class B:
        async def send_message(self, chat_id, text, reply_markup=None, reply_to_message_id=None):
            sent.append(("review", text, [b.callback_data for row in reply_markup.inline_keyboard for b in row],
                         reply_to_message_id))
            return type("M", (), {"message_id": 99})()

    asyncio.run(botmod.on_take(CB(), B()))
    assert edits == ["✅ Olindi — Nurbek"]
    review = [x for x in sent if isinstance(x, tuple)][0]
    assert "<b>Global Camp 2027</b>" in review[1] and f"ap:{iid}" in review[2] and review[3] == 50
    assert d.get(iid)["status"] == "in_review"


def test_failed_write_offers_a_rewrite_button(monkeypatch):
    setup(monkeypatch)
    monkeypatch.setattr(settings, "write_on_accept", True)
    d = botmod.STATE["db"]
    iid = d.insert_item(source="s", url="https://c.example/", canonical_url="https://c.example/", title="Camp",
                        norm_title="camp", summary="", published_at=None)
    d.update(iid, status="shown", data_json={"title": "Camp"}, official_url="https://c.example/")
    attempts = []

    class Writer(FakePipeline):
        def write(self, item):
            attempts.append(1)
            if len(attempts) == 1:
                d.update(item["id"], status="error", reason="write: 400 tool_choice")
            else:
                d.update(item["id"], status="drafted", post_text="<b>Camp</b>", platform_json={})
    botmod.STATE["pipeline"] = Writer()
    notes, sent = [], []

    class Note:
        def __init__(self, card): self.reply_to_message = card
        async def delete(self): pass
        async def edit_text(self, t, reply_markup=None):
            notes.append((t, reply_markup.inline_keyboard[0][0].callback_data))

    class Card:
        message_id = 50
        async def edit_reply_markup(self, reply_markup=None): pass
        async def reply(self, text): return Note(self)

    card = Card()

    class B:
        async def send_message(self, chat_id, text, reply_markup=None, reply_to_message_id=None):
            sent.append(text)
            return type("M", (), {"message_id": 99})()

    def cb(data, message):
        return type("CB", (), {"data": data, "message": message, "answer": lambda self, *a, **k: _noop(),
                               "from_user": type("U", (), {"id": 7, "first_name": "Nurbek"})()})()

    asyncio.run(botmod.on_take(cb(f"ok:{iid}", card), B()))
    assert notes[0][1] == f"wr:{iid}" and d.get(iid)["status"] == "accepted"
    asyncio.run(botmod.on_rewrite(cb(f"wr:{iid}", Note(card)), B()))
    assert sent == ["<b>Camp</b>"] or "<b>Camp</b>" in sent[0]
    assert d.get(iid)["status"] == "in_review"


async def _noop():
    pass


def test_finds_go_out_five_at_a_time(monkeypatch):
    """12 good finds: the search sends the best 5, each "➕ Yana 5 ta" sends the next ones."""
    from datetime import date, timedelta
    setup(monkeypatch)
    monkeypatch.setattr(botmod.asyncio, "sleep", lambda s: _noop())
    d = botmod.STATE["db"]
    dl = (date.today() + timedelta(days=60)).isoformat()
    for i in range(12):
        iid = d.insert_item(source="s", url=f"https://x{i}.example/", canonical_url=f"https://x{i}.example/",
                            title=f"Find {i}", norm_title=f"find {i}", summary="", published_at=None)
        d.update(iid, status="extracted", fit_score=5 if i == 11 else 3, official_url=f"https://x{i}.example/",
                 data_json={**POSTABLE, "title": f"Find {i}", "deadline_type": "fixed", "deadline": dl})
    b = FakeBot()
    asyncio.run(botmod.run_cycle(b, notify=True))
    cards = [t for _, t in b.sent if "Find" in t]
    assert len(cards) == 5 and "Find 11" in cards[0]          # best fit first
    assert "12 ta mos topilma" in b.sent[-1][1] and "yana 7 ta" in b.sent[-1][1]

    b.sent.clear()
    asyncio.run(botmod.send_more(b, -100, 7))
    assert len([t for _, t in b.sent if "Find" in t]) == 5 and "yana 2 ta" in b.sent[-1][1]
    b.sent.clear()
    asyncio.run(botmod.send_more(b, -100, 7))
    assert len([t for _, t in b.sent if "Find" in t]) == 2 and "boshqa topilma yo'q" in b.sent[-1][1]
    b.sent.clear()
    asyncio.run(botmod.send_more(b, -100, 7))
    assert "qolmadi" in b.sent[0][1]
    assert not d.by_status("extracted") and len(d.by_status("shown")) == 12


def test_queued_find_drops_when_its_deadline_gets_close(monkeypatch):
    from datetime import date, timedelta
    setup(monkeypatch)
    d = botmod.STATE["db"]
    iid = d.insert_item(source="s", url="https://x.example/", canonical_url="https://x.example/",
                        title="Old", norm_title="old", summary="", published_at=None)
    d.update(iid, status="extracted", data_json={"deadline_type": "fixed",
                                                 "deadline": (date.today() + timedelta(days=2)).isoformat()})
    assert botmod.ordered_finds() == [] and d.get(iid)["status"] == "rejected"


def test_queued_find_that_cannot_fill_a_post_is_dropped(monkeypatch):
    setup(monkeypatch)
    d = botmod.STATE["db"]
    iid = d.insert_item(source="s", url="https://x.example/", canonical_url="https://x.example/",
                        title="Vague", norm_title="vague", summary="", published_at=None)
    d.update(iid, status="extracted", official_url="https://x.example/",
             data_json={**POSTABLE, "title": "Vague", "deadline_type": "rolling", "benefits": []})
    assert botmod.ordered_finds() == [] and d.get(iid)["reason"] == "post format: no benefits"
