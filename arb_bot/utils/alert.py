"""Discord / Telegram webhook alerts for trades and errors."""

import asyncio
import json
from typing import Optional

import httpx

from arb_bot.utils.logger import get_logger

logger = get_logger(__name__)


async def send_alert(message: str, webhook_url: Optional[str] = None) -> None:
    """POST a plain-text message to a Discord or Telegram webhook.

    Discord expects: {"content": "..."}
    Telegram bot API: /sendMessage?chat_id=...&text=...

    For simplicity, defaults to Discord payload format. If the URL contains
    'telegram' it switches to Telegram's format automatically.
    """
    if webhook_url is None:
        from arb_bot.config import ALERT_WEBHOOK_URL
        webhook_url = ALERT_WEBHOOK_URL

    if not webhook_url:
        logger.debug(f"Alert (no webhook configured): {message}")
        return

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            if "telegram" in webhook_url.lower():
                resp = await client.post(webhook_url, params={"text": message})
            else:
                # Discord format
                resp = await client.post(
                    webhook_url,
                    content=json.dumps({"content": message[:2000]}),
                    headers={"Content-Type": "application/json"},
                )
            resp.raise_for_status()
    except Exception as exc:
        logger.warning(f"Failed to send alert: {exc}")


def send_alert_sync(message: str, webhook_url: Optional[str] = None) -> None:
    """Synchronous wrapper — use when not already inside an event loop."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.create_task(send_alert(message, webhook_url))
        else:
            loop.run_until_complete(send_alert(message, webhook_url))
    except Exception as exc:
        logger.warning(f"send_alert_sync error: {exc}")
