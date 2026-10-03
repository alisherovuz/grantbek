import asyncio

from edugrants_agent import bot as botmod
from edugrants_agent.config import settings
from edugrants_agent.db import DB


# The fields every post needs (see pipeline.format_gaps)
POSTABLE = {"host_country": "Online", "summary": "An online programme for students from all countries.",
            "benefits": ["Certificate"], "format": "online"}


class FakeMsg:
    def __init__(self, bot, chat_id, text, reply_markup=None, reply_to=None):
        self.bot, self.chat_id, self.text, self.markup = bot, chat_id, text, reply_markup
        self.message_id = 1000 + len(bot.sent)
        self.chat = type("C", (), {"id": chat_id})()
        self.reply_to_message = reply_to
        self.deleted = False

    async def edit_text(self, text, reply_markup=None):
        self.text, self.markup = text, reply_markup
        self.bot.edits.append(text)

    async def edit_reply_markup(self, reply_markup=None):
        self.markup = reply_markup

    async def delete(self):
        self.deleted = True

    def buttons(self):
        return {b.text: b.callback_data for row in (self.markup.inline_keyboard if self.markup else []) for b in row}


class FakeBot:
    def __init__(self):
        self.sent, self.edits, self.msgs = [], [], []

    async def send_message(self, chat_id, text, reply_markup=None, reply_to_message_id=None):
        m = FakeMsg(self, chat_id, text, reply_markup)
        self.sent.append((chat_id, text))
        self.msgs.append(m)
        return m


def press(data, message, user=7):
    async def answer(*a, **k):
        pass
    return type("CB", (), {"data": data, "message": message, "answer": staticmethod(answer),
                           "from_user": type("U", (), {"id": user, "first_name": "Nurbek"})()})()


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
    assert b.edits[0].startswith("✅ Qidiruv tugadi: 12 ta yangi e'lon ko'rildi, mos keladigan yangisi yo'q.")
    assert "Bugungi AI xarajati: $0.00" in b.edits[0]


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
    assert len(b.sent) == 1 and b.sent[0][1].startswith("☀️ Bugungi qidiruv: 12 ta yangi e'lon ko'rildi, mos")


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
    monkeypatch.setenv("DASHBOARD_PORT", "0")   # any free port
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
    assert started == {"commands": ["dashboard", "help"],
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


async def _noop():
    pass


def add_finds(d, n, dl_days=60):
    from datetime import date, timedelta
    dl = (date.today() + timedelta(days=dl_days)).isoformat()
    ids = []
    for i in range(n):
        iid = d.insert_item(source="s", url=f"https://x{i}.example/", canonical_url=f"https://x{i}.example/",
                            title=f"Find {i}", norm_title=f"find {i}", summary="", published_at=None)
        d.update(iid, status="extracted", fit_score=5 if i == n - 1 else 3, official_url=f"https://x{i}.example/",
                 data_json={**POSTABLE, "title": f"Find {i}", "deadline_type": "fixed", "deadline": dl})
        ids.append(iid)
    return ids


def test_search_reports_with_a_dashboard_button(monkeypatch):
    """Finds live on the dashboard now: the search only reports how many and links there."""
    setup(monkeypatch)
    monkeypatch.setenv("DASHBOARD_URL", "https://grantbek.example")
    add_finds(botmod.STATE["db"], 12)
    b = FakeBot()
    asyncio.run(botmod.manual_search(b, -100, 7))
    assert len(b.msgs) == 1
    m = b.msgs[0]
    assert "12 ta mos topilma dashboardda kutmoqda" in m.text and "Find" not in m.text
    urls = [btn.url for row in m.markup.inline_keyboard for btn in row if btn.url]
    assert urls and urls[0].startswith("https://grantbek.example/?key=")

def test_next_and_previous_flip_through_the_queue(monkeypatch):
    setup(monkeypatch)
    add_finds(botmod.STATE["db"], 3)
    b = FakeBot()
    asyncio.run(botmod.open_browser(b, -100))
    m = b.msgs[0]
    asyncio.run(botmod.on_browse(press(m.buttons()["Keyingisi ➡️"], m)))
    assert "Topilma 2 / 3" in m.text
    asyncio.run(botmod.on_browse(press(m.buttons()["Keyingisi ➡️"], m)))
    asyncio.run(botmod.on_browse(press(m.buttons()["Keyingisi ➡️"], m)))
    assert "Topilma 1 / 3" in m.text             # wraps around
    asyncio.run(botmod.on_browse(press(m.buttons()["⬅️ Oldingisi"], m)))
    assert "Topilma 3 / 3" in m.text


def test_olamiz_in_the_browser_writes_the_post_and_moves_on(monkeypatch):
    setup(monkeypatch)
    monkeypatch.setattr(settings, "write_on_accept", True)
    d = botmod.STATE["db"]
    add_finds(d, 3)

    class Writer(FakePipeline):
        def write(self, item):
            d.update(item["id"], status="drafted", post_text=f"<b>{item['title']}</b>", platform_json={})
    botmod.STATE["pipeline"] = Writer()
    b = FakeBot()
    asyncio.run(botmod.open_browser(b, -100))
    m = b.msgs[0]
    taken = int(m.buttons()["✅ Olamiz"].split(":")[2])
    asyncio.run(botmod.on_browse_take(press(m.buttons()["✅ Olamiz"], m), b))
    assert "Topilma 1 / 2" in m.text and f"b:ok:{taken}" not in m.buttons().values()
    review = [x for x in b.msgs if "Chop etish" in str(x.buttons())]
    assert review and "Find 2" in review[0].text and d.get(taken)["status"] == "in_review"


def test_kerak_emas_asks_why_then_moves_on(monkeypatch):
    setup(monkeypatch)
    d = botmod.STATE["db"]
    add_finds(d, 2)
    b = FakeBot()
    asyncio.run(botmod.open_browser(b, -100))
    m = b.msgs[0]
    first = int(m.buttons()["❌ Kerak emas"].split(":")[2])
    asyncio.run(botmod.on_browse_skip(press(m.buttons()["❌ Kerak emas"], m)))
    assert "↩️ Orqaga" in m.buttons() and "🎯 Bizga mos emas" in m.buttons()
    asyncio.run(botmod.on_browse_skip_reason(press(m.buttons()["🎯 Bizga mos emas"], m)))
    assert d.get(first)["status"] == "skipped" and d.get(first)["reason"] == "Bizga mos emas"
    assert "Topilma 1 / 1" in m.text


def test_last_find_leaves_an_empty_browser(monkeypatch):
    setup(monkeypatch)
    d = botmod.STATE["db"]
    add_finds(d, 1)
    b = FakeBot()
    asyncio.run(botmod.open_browser(b, -100))
    m = b.msgs[0]
    assert "Oldingisi" not in str(m.buttons())     # nothing to flip through
    asyncio.run(botmod.on_browse_skip(press(m.buttons()["❌ Kerak emas"], m)))
    asyncio.run(botmod.on_browse_skip_reason(press(m.buttons()["💸 Pullik"], m)))
    assert "Navbatda topilma yo'q" in m.text


def test_failed_write_offers_a_rewrite_button(monkeypatch):
    setup(monkeypatch)
    monkeypatch.setattr(settings, "write_on_accept", True)
    d = botmod.STATE["db"]
    add_finds(d, 1)
    attempts = []

    class Writer(FakePipeline):
        def write(self, item):
            attempts.append(1)
            if len(attempts) == 1:
                d.update(item["id"], status="error", reason="write: 400 tool_choice")
            else:
                d.update(item["id"], status="drafted", post_text="<b>Camp</b>", platform_json={})
    botmod.STATE["pipeline"] = Writer()
    b = FakeBot()
    asyncio.run(botmod.open_browser(b, -100))
    m = b.msgs[0]
    asyncio.run(botmod.on_browse_take(press(m.buttons()["✅ Olamiz"], m), b))
    note = [x for x in b.msgs if "🔁 Qayta yozish" in x.buttons()][0]
    assert "post yozilmadi" in note.text
    asyncio.run(botmod.on_rewrite(press(note.buttons()["🔁 Qayta yozish"], note), b))
    assert note.deleted and any(x.text.endswith("<b>Camp</b>") for x in b.msgs)


def test_masters_only_waits_for_a_famous_name(monkeypatch):
    from edugrants_agent.pipeline import level_reason
    assert level_reason({"level": ["master"]}, 3).startswith("level: only master")
    assert level_reason({"level": ["master", "phd"]}, 5) is None        # Chevening-class names pass
    assert level_reason({"level": ["bachelor", "master"]}, 2) is None   # bachelors too: normal rules
    monkeypatch.setattr(settings, "grad_only_min_fit", 0)
    assert level_reason({"level": ["phd"]}, 1) is None
