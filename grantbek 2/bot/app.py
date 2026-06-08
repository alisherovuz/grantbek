"""Build the python-telegram-bot Application and register handlers."""
from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
    MessageHandler,
    filters,
)

from config import settings
from bot.handlers import channel, commands, inline
from bot.handlers.conversations import build_find_conversation
from db import models

logger = logging.getLogger(__name__)


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Handler error", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text("⚠️ Something went wrong. Please try again.")
    except Exception:
        pass


async def _post_init(app: Application) -> None:
    """Runs after Application.initialize() in both polling and webhook modes."""
    await models.init_db()
    logger.info("Database initialised (FTS5=%s)", models.HAS_FTS5)


def build_application() -> Application:
    app = ApplicationBuilder().token(settings.require_token()).post_init(_post_init).build()

    # 1) Guided finder conversation (registered first so /find is captured).
    app.add_handler(build_find_conversation())

    # 2) Commands.
    app.add_handler(CommandHandler("start", commands.start))
    app.add_handler(CommandHandler("help", commands.help_cmd))
    app.add_handler(CommandHandler("about", commands.about))
    app.add_handler(CommandHandler("lang", commands.lang_cmd))
    app.add_handler(CommandHandler("search", commands.search))
    app.add_handler(CommandHandler("categories", commands.categories))
    app.add_handler(CommandHandler("deadlines", commands.deadlines))
    app.add_handler(CommandHandler("faq", commands.faq_cmd))

    # 3) Callback queries (inline keyboards).
    app.add_handler(CallbackQueryHandler(commands.on_lang_callback, pattern=r"^lang:"))
    app.add_handler(CallbackQueryHandler(commands.on_category_callback, pattern=r"^cat:"))
    app.add_handler(CallbackQueryHandler(commands.on_faq_callback, pattern=r"^faq:"))

    # 4) Group/discussion-group messages. The handler itself decides whether to
    #    reply (auto under a recognised grant post, or when @mentioned elsewhere).
    group_msgs = filters.ChatType.GROUPS & (filters.TEXT | filters.CAPTION) & ~filters.COMMAND
    app.add_handler(MessageHandler(group_msgs, channel.handle_group_message))

    # 5) Private free-text → FAQ / grant routing.
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
            commands.free_text,
        )
    )

    # 6) Inline queries.
    app.add_handler(InlineQueryHandler(inline.inline_query))

    app.add_error_handler(_on_error)
    return app
