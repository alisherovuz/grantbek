"""Central configuration loaded from environment variables / .env file.

Railway-aware: if WEBHOOK_URL is not set explicitly, it is derived from
RAILWAY_PUBLIC_DOMAIN, and the listen port comes from Railway's $PORT.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _csv_ids(raw: str) -> set[int]:
    out: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError:
            pass
    return out


def _derive_webhook_url() -> str:
    explicit = os.getenv("WEBHOOK_URL", "").strip().rstrip("/")
    if explicit:
        return explicit
    # Railway exposes the public domain here (no scheme).
    railway = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip().rstrip("/")
    if railway:
        return f"https://{railway}"
    return ""


@dataclass(frozen=True)
class Settings:
    bot_token: str = os.getenv("BOT_TOKEN", "")
    webhook_url: str = _derive_webhook_url()
    webhook_secret: str = os.getenv("WEBHOOK_SECRET", "")
    bot_username: str = os.getenv("BOT_USERNAME", "GrantBekBot").lstrip("@")
    allowed_group_ids: set[int] = field(
        default_factory=lambda: _csv_ids(os.getenv("ALLOWED_GROUP_IDS", ""))
    )
    database_path: str = os.getenv("DATABASE_PATH", "grantbek.db")
    log_level: str = os.getenv("LOG_LEVEL", "INFO").upper()
    default_language: str = os.getenv("DEFAULT_LANGUAGE", "en")

    # Network. Railway injects $PORT; default 8080 locally.
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = int(os.getenv("PORT", "8080"))

    # ── Claude API (optional). If unset, the bot uses keyword-only FAQ. ──
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    llm_model: str = os.getenv("LLM_MODEL", "claude-haiku-4-5-20251001")
    llm_max_tokens: int = int(os.getenv("LLM_MAX_TOKENS", "400"))

    # Let Claude research grants on the web when the DB doesn't cover a question.
    # Costs $10 per 1,000 searches + token costs. Set ENABLE_WEB_SEARCH=false to disable.
    enable_web_search: bool = os.getenv("ENABLE_WEB_SEARCH", "true").lower() in (
        "1", "true", "yes", "on",
    )
    web_search_max_uses: int = int(os.getenv("WEB_SEARCH_MAX_USES", "3"))

    # Footer handle shown at the bottom of every grant post.
    channel_handle: str = os.getenv("CHANNEL_HANDLE", "@EduGrandsUz")

    # Auto-reply to every comment under a recognised grant post (vs only when
    # the bot is @mentioned/replied to). Set GROUP_AUTO_REPLY=false to disable.
    group_auto_reply: bool = os.getenv("GROUP_AUTO_REPLY", "true").lower() in (
        "1", "true", "yes", "on",
    )

    webhook_path: str = "/webhook"

    @property
    def full_webhook_url(self) -> str:
        return f"{self.webhook_url}{self.webhook_path}" if self.webhook_url else ""

    @property
    def llm_enabled(self) -> bool:
        return bool(self.anthropic_api_key)

    def require_token(self) -> str:
        if not self.bot_token:
            raise RuntimeError(
                "BOT_TOKEN is not set. Copy .env.example to .env and fill it in, "
                "or set it in Railway's Variables tab."
            )
        return self.bot_token


settings = Settings()
