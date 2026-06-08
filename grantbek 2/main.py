"""GrantBek entry point.

Webhook mode (production, e.g. Railway):
    python main.py
    # or: uvicorn main:app --host 0.0.0.0 --port $PORT

Polling mode (local dev, no HTTPS needed):
    python main.py --polling
"""
from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request, Response
from telegram import Update

from config import settings
from bot.app import build_application
from db import bootstrap, database

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("grantbek")

# Single shared PTB application instance (used by the webhook endpoint).
application = build_application()


@asynccontextmanager
async def lifespan(_: FastAPI):
    # ── Startup ──
    # NOTE: PTB's initialize() does NOT call post_init (only run_polling/run_webhook
    # do), so we initialise the schema and seed here, in the app's own process.
    await bootstrap.ensure_seeded()
    await application.initialize()
    await application.start()
    if settings.full_webhook_url:
        await application.bot.set_webhook(
            url=settings.full_webhook_url,
            secret_token=settings.webhook_secret or None,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
        logger.info("Webhook set to %s", settings.full_webhook_url)
    else:
        logger.warning(
            "No WEBHOOK_URL / RAILWAY_PUBLIC_DOMAIN set — webhook NOT registered. "
            "Telegram won't deliver updates until you set one (or use --polling)."
        )
    try:
        yield
    finally:
        # ── Shutdown ──
        try:
            await application.bot.delete_webhook()
        except Exception:
            pass
        await application.stop()
        await application.shutdown()
        await database.close_connection()
        logger.info("Shutdown complete")


app = FastAPI(title="GrantBek", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(settings.webhook_path)
async def webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> Response:
    if settings.webhook_secret and x_telegram_bot_api_secret_token != settings.webhook_secret:
        raise HTTPException(status_code=403, detail="Invalid secret token")
    data = await request.json()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return Response(status_code=200)


def run_polling() -> None:
    """Local development mode — long polling, no public URL required."""
    logger.info("Starting in POLLING mode (Ctrl+C to stop)")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


def run_webhook_server() -> None:
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port, log_level=settings.log_level.lower())


if __name__ == "__main__":
    if "--polling" in sys.argv:
        run_polling()
    else:
        run_webhook_server()
