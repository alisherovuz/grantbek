"""GrantBek: answers DMs to her bot and comments under our channel posts."""
import asyncio
from datetime import datetime

import pytest

from edugrants_agent import agents, community
from edugrants_agent import bot as botmod
from edugrants_agent.config import settings
from edugrants_agent.db import DB
from edugrants_agent.weekly import remember_post

POST = """Diamond Challenge 2027

Davlat: AQSh 🇺🇸
Tanlov shakli: Onlayn
Yosh toifasi: 14-18

Maktab o'quvchilari uchun biznes va ijtimoiy tadbirkorlik tanlovi.

🔗Ro'yxatdan o'tish uchun: Havola

📌Ro'yxatdan o'tishning so'nggi muddati: 14-yanvar

⚡️@EduGrandsUz"""


class LLM:
    def __init__(self, decision):
        self.decision, self.calls = decision, []

    def _call(self, purpose, model, system, user, tool, max_tokens=700):
        self.calls.append((system, user))
        return self.decision


class User:
    def __init__(self, uid=55, is_bot=False):
        self.id, self.is_bot, self.full_name, self.username = uid, is_bot, "Ali Valiyev", "ali_v"


class Chat:
    def __init__(self, cid, kind):
        self.id, self.type = cid, kind


class Msg:
    def __init__(self, text, chat, user=None, reply_to=None, thread=None, auto=False, mid=10):
        self.text, self.caption, self.chat, self.from_user = text, None, chat, user
        self.reply_to_message, self.message_thread_id, self.is_automatic_forward = reply_to, thread, auto
        self.sender_chat, self.message_id, self.replies = None, mid, []

    async def reply(self, text):
        self.replies.append(text)

    def get_url(self):
        return "https://t.me/c/1/10"


class Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None, reply_to_message_id=None):
        self.sent.append(text)


@pytest.fixture
def gulsara(monkeypatch):
    monkeypatch.setattr(settings, "admin_chat_id", -100)
    monkeypatch.setattr(settings, "channel_handle", "@EduGrandsUz")
    d = DB(":memory:")
    remember_post(d, 3672, datetime(2026, 9, 30), POST)
    toshmat = Bot()
    agents.BOTS.clear()
    agents.BOTS.update(manager=toshmat, community=Bot())
    agents._DB["get"] = lambda: d
    botmod.STATE.update(db=d)
    yield d, toshmat
    agents.BOTS.clear()


def test_dm_is_answered_from_our_posts(gulsara):
    d, _ = gulsara
    llm = LLM({"action": "reply", "reply": "Diamond Challenge'ga 14-18 yoshdagilar qatnasha oladi: https://t.me/EduGrandsUz/3672",
               "topic": "Diamond Challenge yoshi"})
    botmod.STATE["pipeline"] = type("P", (), {"llm": llm})()
    m = Msg("Diamond Challenge ga necha yoshdan qatnashsa bo'ladi?", Chat(55, "private"), User())
    asyncio.run(community.on_dm(m))
    system, user = llm.calls[0]
    assert "https://t.me/EduGrandsUz/3672" in system and "Yosh toifasi: 14-18" in system   # the post was found
    assert "Never invent" in system                                                       # and the rules are there
    assert m.replies == ["Diamond Challenge'ga 14-18 yoshdagilar qatnasha oladi: https://t.me/EduGrandsUz/3672"]
    assert d.events()[0]["agent"] == "community" and "shaxsiy xabar" in d.events()[0]["text"]


def test_comment_is_answered_from_the_post_it_is_under(gulsara):
    d, _ = gulsara
    llm = LLM({"action": "reply", "reply": "Muddati 14-yanvar.", "topic": "muddat"})
    botmod.STATE["pipeline"] = type("P", (), {"llm": llm})()
    group = Chat(-200, "supergroup")
    forwarded = Msg(POST, group, auto=True, mid=500)
    asyncio.run(community.on_comment(forwarded))              # our post arrives in the comments group
    assert d.thread_text(-200, 500) == POST
    # a reply to another comment in the same thread: the post is found through the thread id
    c = Msg("Qachongacha topshirsa bo'ladi?", group, User(), reply_to=Msg("hi", group, User()), thread=500)
    asyncio.run(community.on_comment(c))
    assert "THE POST BEING COMMENTED ON:\nDiamond Challenge 2027" in llm.calls[0][0]
    assert c.replies == ["Muddati 14-yanvar."]


def test_thanks_and_emoji_are_left_alone(gulsara):
    d, _ = gulsara
    botmod.STATE["pipeline"] = type("P", (), {"llm": LLM({"action": "ignore", "reply": "", "topic": "rahmat"})})()
    group = Chat(-200, "supergroup")
    c = Msg("Rahmat 🔥", group, User(), reply_to=Msg(POST, group, auto=True))
    asyncio.run(community.on_comment(c))
    assert c.replies == []


def test_ad_requests_are_answered_and_handed_to_the_owner(gulsara):
    d, toshmat = gulsara
    botmod.STATE["pipeline"] = type("P", (), {"llm": LLM({"action": "escalate", "topic": "reklama",
                                                           "reply": "Rahmat! Adminlarimiz siz bilan bog'lanadi."})})()
    m = Msg("Kanalingizda reklama qancha turadi?", Chat(55, "private"), User())
    asyncio.run(community.on_dm(m))
    assert m.replies == ["Rahmat! Adminlarimiz siz bilan bog'lanadi."]
    alert = agents.BOTS["community"].sent[-1]                 # GrantBek tells the agent group herself
    assert "adminga murojaat qildi" in alert and "reklama qancha" in alert


def test_bots_the_channel_and_the_agent_group_are_ignored(gulsara):
    d, _ = gulsara
    llm = LLM({"action": "reply", "reply": "x", "topic": "x"})
    botmod.STATE["pipeline"] = type("P", (), {"llm": llm})()
    group = Chat(-200, "supergroup")
    post = Msg(POST, group, auto=True)
    for m in (Msg("bot says hi", group, User(is_bot=True), reply_to=post),
              Msg("hello team", Chat(-100, "supergroup"), User(), reply_to=post),   # the agent group
              Msg("random chat", group, User())):                                 # not under one of our posts
        asyncio.run(community.on_comment(m))
    assert llm.calls == []


def test_only_her_bot_handles_her_messages(gulsara):
    assert asyncio.run(community._is_gulsara(None, agents.BOTS["community"]))
    assert not asyncio.run(community._is_gulsara(None, agents.BOTS["manager"]))


def test_daily_refresh_learns_from_the_weeks_questions(gulsara, monkeypatch):
    d, _ = gulsara
    monkeypatch.setattr(settings, "tg_string_session", None)          # no Telegram reading in tests
    for q in ("Diamond Challenge muddati qachon?", "Necha yoshdan?", "Ruscha yozsam bo'ladimi?"):
        d.add_qa("dm", q, "javob", "savol")
    llm = LLM({"faq": ["Muddat so'rashadi: postdagi sanani ayt"], "style": ["many are 9th-11th graders"]})
    botmod.STATE["pipeline"] = type("P", (), {"llm": llm})()
    botmod.STATE["lock"] = asyncio.Lock()
    asyncio.run(community.refresh_base())
    brief = d.get_meta("community_brief")["text"]
    assert "postdagi sanani ayt" in brief and "9th-11th graders" in brief
    assert "Bilim bazasi yangilandi" in d.events()[0]["text"]
    # and the next answer uses it
    llm.decision = {"action": "reply", "reply": "ok", "topic": "t"}
    asyncio.run(community.on_dm(Msg("Diamond Challenge?", Chat(55, "private"), User())))
    assert "WHAT PEOPLE ASKED RECENTLY" in llm.calls[-1][0] and "postdagi sanani ayt" in llm.calls[-1][0]


def test_a_post_we_publish_is_known_at_once(gulsara):
    d, _ = gulsara
    botmod.learn_post(3800, "<b>Yale Young Global Scholars</b>\n\nDavlat: AQSh 🇺🇸\n\n📌Ro'yxatdan o'tishning "
                            "so'nggi muddati: 15-noyabr")
    row = d.conn.execute("SELECT title, flag, text FROM channel_posts WHERE msg_id=3800").fetchone()
    assert row["title"] == "Yale Young Global Scholars" and row["flag"] == "🇺🇸" and "<b>" not in row["text"]


def test_grantbek_never_shares_the_managers_bot(monkeypatch):
    monkeypatch.setattr(settings, "bot_token", "1:AAA")
    monkeypatch.setenv("COMMUNITY_BOT_TOKEN", "1:AAA")
    bots = agents.setup_bots(lambda tok: object())
    assert "community" not in agents.BOTS and len(bots) == 1
    agents.BOTS.clear()
