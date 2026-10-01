"""Sending approved items to the edugrants.uz platform."""
from __future__ import annotations

import json
import logging

import httpx

from .config import settings

log = logging.getLogger(__name__)


def push_to_platform(item_id: int, payload: dict, telegram_post_url: str | None) -> str:
    """POSTs to the platform webhook if configured, otherwise saves a JSON file an admin
    can import. Returns a reference (platform id or file path)."""
    body = {**payload, "source_item_id": item_id, "telegram_post_url": telegram_post_url}
    if settings.platform_webhook_url:
        headers = {"Accept": "application/json"}
        if settings.platform_token:
            headers["Authorization"] = f"Bearer {settings.platform_token}"
        r = httpx.post(settings.platform_webhook_url, json=body, headers=headers, timeout=30)
        r.raise_for_status()
        try:
            return str(r.json().get("id", "ok"))
        except ValueError:
            return "ok"
    settings.platform_export_dir.mkdir(parents=True, exist_ok=True)
    path = settings.platform_export_dir / f"{item_id}.json"
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)
