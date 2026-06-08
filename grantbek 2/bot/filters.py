"""Custom message filters."""
from __future__ import annotations

from telegram import Message, MessageEntity
from telegram.ext.filters import MessageFilter

from config import settings


class BotMentionedFilter(MessageFilter):
    """True when the bot is @mentioned or the message replies to the bot.

    Used so the channel/group handler only reacts when explicitly addressed,
    rather than to every keyword (which would be spammy and risk a ban).
    """

    def filter(self, message: Message) -> bool:
        username = settings.bot_username.lower()

        # 1. Reply to one of the bot's own messages.
        reply = message.reply_to_message
        if reply and reply.from_user and reply.from_user.is_bot:
            ru = (reply.from_user.username or "").lower()
            if ru == username:
                return True

        text = message.text or message.caption or ""
        if not text:
            return False

        # 2. Explicit @username mention anywhere in the text.
        if f"@{username}" in text.lower():
            return True

        # 3. Mention entities (covers @mentions Telegram parsed for us).
        entities = message.entities or message.caption_entities or []
        for ent in entities:
            if ent.type == MessageEntity.MENTION:
                fragment = text[ent.offset : ent.offset + ent.length].lstrip("@").lower()
                if fragment == username:
                    return True
        return False


bot_mentioned = BotMentionedFilter()
