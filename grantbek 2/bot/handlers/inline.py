"""Inline queries: type `@GrantBekBot <query>` in any chat to get grant cards."""
from __future__ import annotations

import logging
from uuid import uuid4

from telegram import (
    InlineQueryResultArticle,
    InputTextMessageContent,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from bot.i18n import get_lang
from services import grant_search as gs

logger = logging.getLogger(__name__)


async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    iq = update.inline_query
    if iq is None:
        return
    user = iq.from_user
    lang = await get_lang(user.id, user.language_code if user else None)

    query = (iq.query or "").strip()
    grants = (
        await gs.search_grants(query, limit=10)
        if query
        else await gs.get_upcoming_deadlines(days=3650, limit=10)
    )

    results = []
    for g in grants:
        results.append(
            InlineQueryResultArticle(
                id=str(uuid4()),
                title=gs.grant_title(g, lang),
                description=gs.grant_snippet(g, lang),
                input_message_content=InputTextMessageContent(
                    gs.format_grant_card(g, lang),
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                ),
            )
        )
    await iq.answer(results, cache_time=30, is_personal=True)
