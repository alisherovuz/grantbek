"""Guided grant-finder conversation: /find -> field -> country -> funding."""
from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot.i18n import get_lang, t
from services import grant_search as gs

FIELD, COUNTRY, FUNDING = range(3)


async def _lang(update: Update) -> str:
    u = update.effective_user
    return await get_lang(u.id, u.language_code if u else None)


async def find_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = await _lang(update)
    context.user_data["find"] = {}
    cats = await gs.list_categories()
    rows, row = [], []
    for c in cats:
        label = c["name_uz"] if lang == "uz" else c["name_en"]
        row.append(InlineKeyboardButton(label, callback_data=f"ff:{c['slug']}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(t("doesnt_matter", lang), callback_data="ff:any")])
    await update.effective_message.reply_text(
        t("find_field", lang), reply_markup=InlineKeyboardMarkup(rows)
    )
    return FIELD


async def on_field(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    lang = await get_lang(query.from_user.id)
    context.user_data["find"]["category"] = query.data.split(":", 1)[1]
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton(t("any", lang), callback_data="fc:any")]]
    )
    await query.edit_message_text(t("find_country", lang), parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    return COUNTRY


async def on_country_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = await _lang(update)
    context.user_data["find"]["country"] = (update.effective_message.text or "").strip()
    return await _ask_funding(update.effective_message, lang)


async def on_country_any(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    lang = await get_lang(query.from_user.id)
    context.user_data["find"]["country"] = ""
    return await _ask_funding(query.message, lang)


async def _ask_funding(message, lang: str) -> int:
    kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(t("fully_funded", lang), callback_data="fu:full"),
            InlineKeyboardButton(t("doesnt_matter", lang), callback_data="fu:any"),
        ]]
    )
    await message.reply_text(t("find_funding", lang), reply_markup=kb)
    return FUNDING


async def on_funding(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    lang = await get_lang(query.from_user.id)
    choice = query.data.split(":", 1)[1]
    data = context.user_data.get("find", {})
    data["funding"] = choice

    grants = await _run_find(data)
    await query.edit_message_text(t("find_done", lang), parse_mode=ParseMode.MARKDOWN)
    if not grants:
        await query.message.reply_text(t("no_results", lang))
    for g in grants:
        await query.message.reply_text(
            gs.format_grant_card(g, lang),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    context.user_data.pop("find", None)
    return ConversationHandler.END


async def _run_find(data: dict) -> list[dict]:
    category = data.get("category", "any")
    country = (data.get("country") or "").strip()
    funding = data.get("funding", "any")

    if category and category != "any":
        grants = await gs.get_grants_by_category(category, limit=20)
    elif country:
        grants = await gs.search_grants(country, limit=20)
    else:
        grants = await gs.get_upcoming_deadlines(days=3650, limit=20)

    if country:
        cl = country.lower()
        filtered = [g for g in grants if cl in (g.get("country", "").lower())]
        grants = filtered or grants
    if funding == "full":
        grants = [g for g in grants if "full" in (g.get("amount", "").lower())] or grants
    return grants[:6]


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = await _lang(update)
    context.user_data.pop("find", None)
    await update.effective_message.reply_text(t("cancelled", lang))
    return ConversationHandler.END


def build_find_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("find", find_start)],
        states={
            FIELD: [CallbackQueryHandler(on_field, pattern=r"^ff:")],
            COUNTRY: [
                CallbackQueryHandler(on_country_any, pattern=r"^fc:any$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_country_text),
            ],
            FUNDING: [CallbackQueryHandler(on_funding, pattern=r"^fu:")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
