"""Discussion-group handling.

Two behaviours in one handler:

1. A comment under a channel post -> match that post to a grant in our DB and
   answer the comment *about that grant* (auto, no mention needed). Claude
   phrases the reply grounded in the grant (plus the closest FAQ); if no API
   key is set it falls back to an FAQ answer or a short grant brief.

2. Any other group message -> only respond when the bot is @mentioned or
   replied to, using a general search. Random chatter is ignored.
"""
from __future__ import annotations

import logging
import re

from telegram import Message, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from config import settings
from bot import filters as custom_filters
from bot.i18n import get_lang, t
from services import faq as faq_svc
from services import grant_search as gs
from services import llm

logger = logging.getLogger(__name__)


def _strip_mention(text: str) -> str:
    return re.sub(rf"@{re.escape(settings.bot_username)}", "", text, flags=re.IGNORECASE).strip()


def _looks_substantive(text: str) -> bool:
    """Skip stickers, lone emoji, and one-word reactions like 'rahmat'/'thanks'."""
    t_ = text.strip()
    if "?" in t_:
        return True
    words = re.findall(r"\w+", t_)
    return len(words) >= 2


_ACK_WORDS = {
    "rahmat", "raxmat", "rahmet", "tashakkur", "tnx", "thanks", "thank", "thankyou",
    "thx", "ok", "okay", "okey", "good", "nice", "cool", "great", "spasibo",
    "спасибо", "спс", "salom", "assalom", "hi", "hello", "hey", "yes", "yeah",
    "ha", "yoq", "no", "omad", "zor",
}


def _is_trivial(text: str) -> bool:
    """True for emoji-only messages and short acknowledgments/greetings.

    These get NO reply, even when they're a reply to the bot — so a simple
    'rahmat' doesn't trigger a wall of text.
    """
    cleaned = re.sub(r"[^\w\s\u0400-\u04FF]", "", text.strip().lower()).strip()
    if not cleaned:  # emoji / punctuation only
        return True
    if len(cleaned) < 3:
        return True
    words = cleaned.split()
    if len(words) <= 2 and all(w in _ACK_WORDS for w in words):
        return True
    return False


# Substrings (en / uz / ru) that signal a message is actually about
# grants, scholarships, or studying. Used to ignore off-topic group chatter.
_GRANT_TERMS = (
    "grant", "scholar", "fellowship", "fund", "tuition", "stipend", "deadline",
    "apply", "applicat", "eligib", "univers", "college", "study", "studies",
    "abroad", "ielts", "toefl", "gpa", "admission", "bachelor", "master", "phd",
    "program", "camp", "exchange", "internship", "course", "degree", "visa",
    "stipendiya", "ariza", "muddat", "o'qish", "oqish", "talaba", "magistratura",
    "bakalavr", "dastur", "qatnash", "ro'yxat", "royxat", "tanlov", "imkoniyat",
    "xorij", "chet", "ingliz", "til", "yosh", "universitet", "stipend",
    "грант", "стипенди", "учеб", "универ", "заявк", "дедлайн", "обучен",
    "стажировк", "программ", "поступл",
)


def _is_grant_related(text: str) -> bool:
    t = text.lower()
    return any(term in t for term in _GRANT_TERMS)


def _extract_post_text(message: Message) -> str | None:
    """If this message is a comment under a channel post, return that post's text.

    First-level comments reply to the auto-forwarded channel post; that forward
    has is_automatic_forward=True (and a channel sender_chat).
    """
    r = message.reply_to_message
    if r is None:
        return None
    if getattr(r, "is_automatic_forward", False) or r.sender_chat is not None:
        return r.text or r.caption
    return None


async def _answer_under_post(message: Message, post_text: str, question: str, lang: str) -> None:
    """Answer a comment using the channel post it's under as the source of truth."""
    faq = await faq_svc.find_faq(question)

    if settings.llm_enabled:
        answer = await llm.answer(
            question, [], faq, lang, allow_web=True, post_text=post_text
        )
        if answer:
            await message.reply_text(answer, disable_web_page_preview=True)
            return

    # Fallback when Claude is unavailable.
    if faq:
        await message.reply_text(
            faq_svc.format_faq_response(faq, lang), parse_mode=ParseMode.HTML
        )
        return
    await message.reply_text(t("no_results", lang))


async def _answer_general(message: Message, question: str, lang: str) -> None:
    faq = await faq_svc.find_faq(question)
    grants = await gs.search_grants(question, limit=3)

    if settings.llm_enabled and (faq or grants or settings.enable_web_search):
        answer = await llm.answer(question, grants, faq, lang, allow_web=True)
        if answer:
            await message.reply_text(answer, disable_web_page_preview=True)
            return
    if faq:
        await message.reply_text(faq_svc.format_faq_response(faq, lang), parse_mode=ParseMode.HTML)
        return
    if grants:
        await message.reply_text(
            gs.format_grant_card(grants[0], lang),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return
    await message.reply_text(t("no_results", lang))


async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None:
        return

    chat_id = update.effective_chat.id
    if settings.allowed_group_ids and chat_id not in settings.allowed_group_ids:
        return

    # Ignore the auto-forwarded channel post itself (it's not a comment).
    if getattr(msg, "is_automatic_forward", False):
        return

    user = update.effective_user
    if user is None or user.is_bot:  # skip channel/bot-authored messages
        return

    text = (msg.text or msg.caption or "").strip()
    if not text:
        return

    # Never reply to bare thanks/greetings/emoji — even if it's a reply to us.
    if _is_trivial(text):
        return

    lang = await get_lang(user.id, user.language_code)
    mentioned = custom_filters.bot_mentioned.filter(msg)

    # Case 1: comment under a channel post → answer about that post's grant,
    # using the post text itself as the source of truth.
    post_text = _extract_post_text(msg)
    if post_text:
        if not (settings.group_auto_reply or mentioned):
            return
        if not mentioned and not _looks_substantive(text):
            return  # don't reply to a lone emoji / "rahmat" under a post
        await _answer_under_post(msg, post_text, _strip_mention(text), lang)
        return

    # Case 2: not under a post — only respond to an explicit mention/reply AND
    # only when the message is actually about grants. Ignore off-topic chatter.
    if mentioned and _is_grant_related(text):
        await _answer_general(msg, _strip_mention(text), lang)
