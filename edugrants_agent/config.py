"""Settings loaded from environment variables (.env supported)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

import os as _os

ROOT_FOR_ENV = Path(__file__).resolve().parent.parent
# Values in .env win over anything already set in the terminal (an old exported key is a common trap)
SHELL_HAD_ANTHROPIC_KEY = bool(_os.environ.get("ANTHROPIC_API_KEY"))
load_dotenv(ROOT_FOR_ENV / ".env", override=True)

ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is not None:   # hosting dashboards don't strip "  # comment" or quotes like .env files do
        value = value.split(" #", 1)[0].split("\t#", 1)[0].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
    return value if value not in (None, "") else default


def _int(name: str, default: int) -> int:
    return int(_env(name, str(default)))


def _float(name: str, default: float) -> float:
    return float(_env(name, str(default)))


def on_railway() -> bool:
    return bool(os.getenv("RAILWAY_PROJECT_ID") or os.getenv("RAILWAY_ENVIRONMENT_NAME"))


def database_path() -> Path:
    """Where the database lives. On Railway, a container's own disk is wiped on every deploy, so the
    database must sit on the attached volume: if one is attached, it is used wherever it is mounted."""
    volume = os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
    configured = _env("DB_PATH")
    if volume and not (configured and Path(configured).is_absolute() and configured.startswith(volume.rstrip("/") + "/")):
        return Path(volume) / "agent.db"
    return Path(configured) if configured else ROOT / "data" / "agent.db"


def database_is_temporary() -> bool:
    """True on Railway without a volume: everything is forgotten at the next deploy."""
    return on_railway() and not os.getenv("RAILWAY_VOLUME_MOUNT_PATH")


@dataclass
class Settings:
    anthropic_api_key: str | None = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    # Only for account-wide keys that aren't tied to a workspace (Console > Settings > Workspaces > ID)
    anthropic_workspace_id: str | None = field(default_factory=lambda: _env("ANTHROPIC_WORKSPACE_ID"))
    # Cheap model: triage, finding the official link, extracting facts
    model_fast: str = field(default_factory=lambda: _env("MODEL_FAST", "claude-haiku-4-5-20251001"))
    # Strong model: writing Uzbek text (Uzbek quality matters, so don't use the cheap one here)
    model_writer: str = field(default_factory=lambda: _env("MODEL_WRITER", "claude-sonnet-5-5"))
    # Toshmat aka's brain: understands orders and runs the team's tools. Empty = the writer's model.
    model_manager: str | None = field(default_factory=lambda: _env("MODEL_MANAGER"))

    # USD per million tokens, used only for the /stats cost estimate. Check current prices.
    price_fast_in: float = field(default_factory=lambda: _float("PRICE_FAST_IN", 1.0))
    price_fast_out: float = field(default_factory=lambda: _float("PRICE_FAST_OUT", 5.0))
    price_writer_in: float = field(default_factory=lambda: _float("PRICE_WRITER_IN", 3.0))
    price_writer_out: float = field(default_factory=lambda: _float("PRICE_WRITER_OUT", 15.0))

    bot_token: str | None = field(default_factory=lambda: _env("BOT_TOKEN"))
    admin_chat_id: int | None = field(default_factory=lambda: int(_env("ADMIN_CHAT_ID")) if _env("ADMIN_CHAT_ID") else None)
    admin_user_ids: set[int] = field(default_factory=lambda: {int(x) for x in (_env("ADMIN_USER_IDS", "") or "").split(",") if x.strip()})
    channel_id: str = field(default_factory=lambda: _env("CHANNEL_ID", "@EduGrandsUz"))
    channel_handle: str = field(default_factory=lambda: _env("CHANNEL_HANDLE", "@EduGrandsUz"))

    db_path: Path = field(default_factory=lambda: database_path())
    sources_file: Path = field(default_factory=lambda: Path(_env("SOURCES_FILE", str(ROOT / "config" / "sources.yaml"))))
    options_file: Path = field(default_factory=lambda: Path(_env("OPTIONS_FILE", str(ROOT / "config" / "platform_options.yaml"))))

    # One search a day at this local time. Set RUN_AT empty to use RUN_EVERY_HOURS instead.
    run_at: str | None = field(default_factory=lambda: _env("RUN_AT", "09:00"))
    community_model: str | None = field(default_factory=lambda: _env("COMMUNITY_MODEL"))   # GrantBek; default MODEL_FAST
    community_refresh_at: str | None = field(default_factory=lambda: _env("COMMUNITY_REFRESH_AT", "06:30"))
    # Strong finds go straight to Mirzo: the post is written before you look, you only tap "Chop etish"
    auto_write_min_fit: int = field(default_factory=lambda: _int("AUTO_WRITE_MIN_FIT", 5))
    auto_write_per_day: int = field(default_factory=lambda: _int("AUTO_WRITE_PER_DAY", 3))   # 0 = off
    # Daily AI budget per agent in USD (0 = no limit); usually set on the dashboard
    budget_finder: float = field(default_factory=lambda: _float("BUDGET_FINDER", 0))
    budget_writer: float = field(default_factory=lambda: _float("BUDGET_WRITER", 0))
    budget_community: float = field(default_factory=lambda: _float("BUDGET_COMMUNITY", 0))
    budget_manager: float = field(default_factory=lambda: _float("BUDGET_MANAGER", 0))
    report_at: str | None = field(default_factory=lambda: _env("REPORT_AT", "21:00"))      # Toshmat aka's daily report
    weekly_at: str | None = field(default_factory=lambda: _env("WEEKLY_AT", "mon 08:30"))  # Mirzo's Monday list
    timezone: str = field(default_factory=lambda: _env("TIMEZONE", "Asia/Tashkent"))
    run_every_hours: int = field(default_factory=lambda: _int("RUN_EVERY_HOURS", 24))
    max_drafts_per_run: int = field(default_factory=lambda: _int("MAX_DRAFTS_PER_RUN", 25))
    # Programmes only for master's/PhD students: the channel posted 16 of 850, so they need this fit
    # score (5 = famous names like Chevening or Erasmus Mundus). 0 = no limit.
    grad_only_min_fit: int = field(default_factory=lambda: _int("GRAD_ONLY_MIN_FIT", 6))   # 6 = never (only past posts)
    # Only finds the AI scored at least this high (1-5) reach the editors; the rest wait under "Agent tashladi"
    min_show_fit: int = field(default_factory=lambda: _int("MIN_SHOW_FIT", 4))
    max_process_per_run: int = field(default_factory=lambda: _int("MAX_PROCESS_PER_RUN", 60))
    # Each search researches at least this many finds, topping up with the best "fit 2" ones when the
    # vibe filter keeps fewer, so a quiet day still produces candidates (each costs about a cent)
    research_min_per_run: int = field(default_factory=lambda: _int("RESEARCH_MIN_PER_RUN", 12))
    min_days_left: int = field(default_factory=lambda: _int("MIN_DAYS_LEFT", 4))
    # Title matching window. Keep it well under a year so next year's round of the same
    # programme isn't mistaken for a repost (the official-page check handles that case).
    dedupe_days: int = field(default_factory=lambda: _int("DEDUPE_DAYS", 120))
    dedupe_threshold: int = field(default_factory=lambda: _int("DEDUPE_THRESHOLD", 88))

    # Finder rules (what counts as "our vibe")
    mode: str = field(default_factory=lambda: _env("MODE", "finder"))  # finder | full
    age_min: int = field(default_factory=lambda: _int("AGE_MIN", 10))
    # A programme must accept someone aged AGE_MIN..AGE_MAX: 21-30 is out, 18-35 is in (633 of 642 past posts)
    age_max: int = field(default_factory=lambda: _int("AGE_MAX", 20))
    # Hard rules. Only "Uzbeks can apply" and "no application fee" are always on; the rest are optional.
    require_uz_yes: bool = field(default_factory=lambda: _env("REQUIRE_UZ_YES", "false").lower() == "true")
    require_free_participation: bool = field(default_factory=lambda: _env("REQUIRE_FREE_PARTICIPATION", "false").lower() == "true")
    # ~80% of subscribers are 12-20: programmes must accept someone aged 10-20, or be for school/bachelor students
    check_ages: bool = field(default_factory=lambda: _env("CHECK_AGES", "true").lower() == "true")
    allow_full_aid: bool = field(default_factory=lambda: _env("ALLOW_FULL_AID", "false").lower() == "true")
    min_fit_score: int = field(default_factory=lambda: _int("MIN_FIT_SCORE", 3))
    recent_post_days: int = field(default_factory=lambda: _int("RECENT_POST_DAYS", 60))
    write_on_accept: bool = field(default_factory=lambda: _env("WRITE_ON_ACCEPT", "true").lower() == "true")
    history_file: Path = field(default_factory=lambda: Path(_env("HISTORY_FILE", str(ROOT / "config" / "messages.html"))))
    profile_file: Path = field(default_factory=lambda: Path(_env("PROFILE_FILE", str(ROOT / "config" / "channel_profile.md"))))

    # Platform (edugrants.uz admin). If the webhook is empty, approved items are saved as JSON files instead.
    platform_webhook_url: str | None = field(default_factory=lambda: _env("PLATFORM_WEBHOOK_URL"))
    platform_token: str | None = field(default_factory=lambda: _env("PLATFORM_TOKEN"))
    platform_export_dir: Path = field(default_factory=lambda: Path(_env("PLATFORM_EXPORT_DIR", str(ROOT / "data" / "platform_exports"))))

    # Optional collectors
    tg_api_id: str | None = field(default_factory=lambda: _env("TG_API_ID"))
    tg_api_hash: str | None = field(default_factory=lambda: _env("TG_API_HASH"))
    tg_session: str = field(default_factory=lambda: _env("TG_SESSION", str(ROOT / "data" / "reader")))
    # Printed by `python -m edugrants_agent tg-login`; easier than a session file on Railway
    tg_string_session: str | None = field(default_factory=lambda: _env("TG_STRING_SESSION"))
    fast_every_minutes: int = field(default_factory=lambda: _int("FAST_EVERY_MINUTES", 0))  # 0 = off
    imap_host: str | None = field(default_factory=lambda: _env("IMAP_HOST"))
    imap_user: str | None = field(default_factory=lambda: _env("IMAP_USER"))
    imap_password: str | None = field(default_factory=lambda: _env("IMAP_PASSWORD"))

    user_agent: str = field(default_factory=lambda: _env("USER_AGENT", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"))


def load_yaml(path: Path) -> dict:
    """Missing optional config files (like platform_options.yaml in finder mode) count as empty."""
    if not Path(path).exists():
        import logging
        logging.getLogger(__name__).warning("%s not found, using defaults", path)
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


settings = Settings()


def anthropic_client():
    import anthropic
    headers = {"anthropic-workspace-id": settings.anthropic_workspace_id} if settings.anthropic_workspace_id else None
    return anthropic.Anthropic(api_key=settings.anthropic_api_key, max_retries=3, default_headers=headers)
