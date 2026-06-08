"""Command, callback-query, and free-text handlers."""
from __future__ import annotations

import logging

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from bot.i18n import get_lang, set_lang, t
from services import faq as faq_svc
from services import grant_search as gs
from services import llm

logger = logging.getLogger(__name__)


async def _lang_of(update: Update) -> str:
    user = update.effective_user
    code = user.language_code if user else None
    return await get_lang(user.id if user else 0, code)


# ── Basic commands ──────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = await _lang_of(update)
    await update.effective_message.reply_text(t("welcome", lang), parse_mode=ParseMode.MARKDOWN)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = await _lang_of(update)
    await update.effective_message.reply_text(t("help", lang), parse_mode=ParseMode.MARKDOWN)


async def about(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = await _lang_of(update)
    await update.effective_message.reply_text(t("about", lang), parse_mode=ParseMode.MARKDOWN)


# ── Language switch ─────────────────────────────────────────────────────────
async def lang_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = await _lang_of(update)
    kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("🇬🇧 English", callback_data="lang:en"),
            InlineKeyboardButton("🇺🇿 O'zbekcha", callback_data="lang:uz"),
        ]]
    )
    await update.effective_message.reply_text(t("lang_prompt", lang), reply_markup=kb)


async def on_lang_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    new_lang = query.data.split(":", 1)[1]
    await set_lang(query.from_user.id, new_lang)
    await query.edit_message_text(t("lang_set", new_lang))


# ── Search ──────────────────────────────────────────────────────────────────
async def search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = await _lang_of(update)
    msg = update.effective_message
    q = " ".join(context.args) if context.args else ""
    if not q:
        await msg.reply_text(t("search_usage", lang))
        return
    await _send_results(update, q, lang)


async def _send_results(update: Update, query: str, lang: str) -> None:
    msg = update.effective_message
    results = await gs.search_grants(query, limit=6)
    await gs.log_search(update.effective_user.id, query, len(results))
    if not results:
        await msg.reply_text(t("no_results", lang))
        return
    await msg.reply_text(t("results_header", lang, n=len(results)), parse_mode=ParseMode.MARKDOWN)
    for g in results:
        await msg.reply_text(
            gs.format_grant_card(g, lang),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )


# ── Categories ──────────────────────────────────────────────────────────────
async def categories(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = await _lang_of(update)
    cats = await gs.list_categories()
    rows, row = [], []
    for c in cats:
        label = c["name_uz"] if lang == "uz" else c["name_en"]
        row.append(InlineKeyboardButton(label, callback_data=f"cat:{c['slug']}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    await update.effective_message.reply_text(
        t("categories_header", lang), reply_markup=InlineKeyboardMarkup(rows)
    )


async def on_category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    lang = await get_lang(query.from_user.id)
    slug = query.data.split(":", 1)[1]
    grants = await gs.get_grants_by_category(slug, limit=8)
    if not grants:
        await query.edit_message_text(t("no_results", lang))
        return
    await query.edit_message_text(t("results_header", lang, n=len(grants)), parse_mode=ParseMode.MARKDOWN)
    for g in grants:
        await query.message.reply_text(
            gs.format_grant_card(g, lang),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )


# ── Deadlines ───────────────────────────────────────────────────────────────
async def deadlines(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = await _lang_of(update)
    days = 30
    grants = await gs.get_upcoming_deadlines(days=days, limit=8)
    if not grants:
        await update.effective_message.reply_text(t("no_deadlines", lang, days=days))
        return
    await update.effective_message.reply_text(
        t("deadlines_header", lang, days=days), parse_mode=ParseMode.MARKDOWN
    )
    for g in grants:
        await update.effective_message.reply_text(
            gs.format_grant_card(g, lang),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )


# ── FAQ ─────────────────────────────────────────────────────────────────────
async def faq_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = await _lang_of(update)
    faqs = await faq_svc.get_all_faqs()
    rows = [
        [InlineKeyboardButton(faq_svc.faq_question(f, lang)[:60], callback_data=f"faq:{f['id']}")]
        for f in faqs[:12]
    ]
    await update.effective_message.reply_text(
        t("faq_header", lang), reply_markup=InlineKeyboardMarkup(rows)
    )


async def on_faq_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    lang = await get_lang(query.from_user.id)
    faq_id = int(query.data.split(":", 1)[1])
    faq = await faq_svc.get_faq_by_id(faq_id)
    if faq:
        await query.message.reply_text(
            faq_svc.format_faq_response(faq, lang), parse_mode=ParseMode.HTML
        )


# ── Free text (no command) ──────────────────────────────────────────────────
async def free_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route a plain message: try FAQ + grant retrieval, optionally phrase via Claude."""
    lang = await _lang_of(update)
    msg = update.effective_message
    text = (msg.text or "").strip()
    if not text:
        return

    faq = await faq_svc.find_faq(text)
    grants = await gs.search_grants(text, limit=4)

    # If Claude is configured, let it phrase a grounded answer.
    if llm.settings.llm_enabled and (faq or grants):
        answer = await llm.answer(text, grants, faq, lang)
        if answer:
            await msg.reply_text(answer, disable_web_page_preview=True)
            return

    # Fallback path (also the default zero-cost behaviour).
    if faq:
        await msg.reply_text(faq_svc.format_faq_response(faq, lang), parse_mode=ParseMode.HTML)
        return
    if grants:
        await _send_results(update, text, lang)
        return
    await msg.reply_text(t("no_results", lang))
