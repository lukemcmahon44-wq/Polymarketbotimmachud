"""Alert dispatch via Slack, Discord, or email webhooks."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import structlog

from polymarket_bot.config.settings import BotConfig

logger = structlog.get_logger(__name__)


class Alerter:
    """Sends alerts to configured webhooks for risk events."""

    def __init__(self, config: BotConfig) -> None:
        self._config = config
        self._sent: list[dict[str, Any]] = []  # in-memory alert history

    async def send(self, level: str, title: str, body: str, metadata: dict[str, Any] | None = None) -> None:
        """Send an alert to all configured channels."""
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": level,
            "title": title,
            "body": body,
            "metadata": metadata or {},
        }
        self._sent.append(record)
        logger.warning("alert_dispatched", level=level, title=title)

        # Dispatch to channels concurrently
        import asyncio
        tasks = []
        if self._config.alerts.slack_webhook_url:
            tasks.append(self._send_slack(record))
        if self._config.alerts.discord_webhook_url:
            tasks.append(self._send_discord(record))

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, Exception):
                    logger.error("alert_dispatch_error", error=str(r))

    async def circuit_breaker_alert(self, reason: str) -> None:
        await self.send(
            level="CRITICAL",
            title="⛔ Circuit Breaker Tripped",
            body=f"All trading has been halted.\nReason: {reason}",
        )

    async def copy_trade_alert(self, wallet: str, score: float, action: str) -> None:
        await self.send(
            level="WARNING",
            title="👁️ Copy Trade Detected",
            body=f"Wallet {wallet[:10]}... similarity={score:.2f}\nAction: {action}",
        )

    async def large_fill_alert(self, order_id: str, size_usd: float) -> None:
        if size_usd >= self._config.alerts.large_fill_threshold_usd:
            await self.send(
                level="INFO",
                title="💰 Large Fill",
                body=f"Order {order_id}: ${size_usd:.2f} filled",
            )

    def get_alert_history(self) -> list[dict[str, Any]]:
        return list(self._sent)

    async def _send_slack(self, record: dict[str, Any]) -> None:
        try:
            import httpx
            payload = {
                "text": f"[{record['level']}] *{record['title']}*\n{record['body']}",
            }
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    self._config.alerts.slack_webhook_url,
                    json=payload,
                    timeout=10.0,
                )
                resp.raise_for_status()
        except Exception as e:
            logger.debug("slack_alert_error", error=str(e))

    async def _send_discord(self, record: dict[str, Any]) -> None:
        try:
            import httpx
            payload = {
                "content": f"**[{record['level']}] {record['title']}**\n{record['body']}",
            }
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    self._config.alerts.discord_webhook_url,
                    json=payload,
                    timeout=10.0,
                )
                resp.raise_for_status()
        except Exception as e:
            logger.debug("discord_alert_error", error=str(e))
