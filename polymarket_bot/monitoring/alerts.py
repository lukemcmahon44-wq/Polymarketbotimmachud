"""Alert system — sends notifications via webhook/Slack/email."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
import structlog

from polymarket_bot.config import BotConfig

logger = structlog.get_logger()


class AlertManager:
    """Sends alerts for risk events, circuit breakers, and anomalies.

    Supports webhook (Slack-compatible), with extensible transport.
    """

    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self.webhook_url = config.monitoring.alert_webhook_url
        self._alert_history: list[dict[str, Any]] = []

    async def send_alert(
        self,
        title: str,
        message: str,
        severity: str = "warning",
        details: dict[str, Any] | None = None,
    ) -> bool:
        """Send an alert via configured channels."""
        alert = {
            "title": title,
            "message": message,
            "severity": severity,
            "details": details or {},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._alert_history.append(alert)

        logger.log(
            severity,
            "alert.fired",
            title=title,
            message=message,
        )

        # Send to webhook if configured
        if self.webhook_url:
            return await self._send_webhook(alert)

        return True

    async def _send_webhook(self, alert: dict[str, Any]) -> bool:
        """Send alert to Slack-compatible webhook."""
        severity_emoji = {
            "info": "ℹ️",
            "warning": "⚠️",
            "error": "🔴",
            "critical": "🚨",
        }
        emoji = severity_emoji.get(alert["severity"], "📢")

        payload = {
            "text": f"{emoji} *{alert['title']}*\n{alert['message']}",
            "attachments": [{
                "color": {"info": "#36a64f", "warning": "#ffcc00", "error": "#ff0000", "critical": "#ff0000"}.get(alert["severity"], "#808080"),
                "fields": [
                    {"title": k, "value": str(v), "short": True}
                    for k, v in alert.get("details", {}).items()
                ],
                "ts": alert["timestamp"],
            }],
        }

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(self.webhook_url, json=payload)
                return resp.status_code == 200
        except Exception as exc:
            logger.error("alert.webhook_failed", error=str(exc))
            return False

    async def alert_circuit_breaker(self, reason: str, details: dict[str, Any]) -> bool:
        if self.config.monitoring.alert_on_circuit_breaker:
            return await self.send_alert(
                "Circuit Breaker Triggered",
                reason,
                severity="critical",
                details=details,
            )
        return False

    async def alert_large_fill(self, order_id: str, size_usd: float) -> bool:
        if (
            self.config.monitoring.alert_on_large_fill
            and size_usd >= self.config.monitoring.large_fill_threshold_usd
        ):
            return await self.send_alert(
                "Large Fill",
                f"Order {order_id[:8]} filled for ${size_usd:.2f}",
                severity="info",
                details={"order_id": order_id, "size_usd": size_usd},
            )
        return False

    async def alert_copy_trade(self, address: str, similarity: float) -> bool:
        return await self.send_alert(
            "Copy-Trade Detected",
            f"Wallet {address[:10]}... similarity={similarity:.2f}",
            severity="warning",
            details={"address": address[:10] + "...", "similarity": similarity},
        )

    @property
    def recent_alerts(self) -> list[dict[str, Any]]:
        return self._alert_history[-50:]
