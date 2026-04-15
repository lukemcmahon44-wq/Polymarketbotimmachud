"""Live order executor — GATED behind explicit operator confirmation.

CRITICAL SAFETY RULES:
  1. ENABLE_LIVE_TRADING env var must be 'true'
  2. Operator must type the exact passphrase at runtime
  3. First N trades require manual approval (configurable)
  4. Every signing event is logged with full metadata
  5. Private key is NEVER logged or printed

This module deliberately refuses to execute if the gate is not cleared.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Any

import structlog

from polymarket_bot.config.settings import BotConfig
from polymarket_bot.execution.order_manager import OrderManager
from polymarket_bot.scanner.polymarket_client import PolymarketCLOBClient
from polymarket_bot.types import Order, OrderStatus, Signal

logger = structlog.get_logger(__name__)

LIVE_TRADING_PASSPHRASE = "I UNDERSTAND AND ENABLE LIVE TRADING"


class LiveTradingGateError(Exception):
    """Raised when live trading gate conditions are not met."""


class LiveExecutor:
    """Executes real signed transactions on Polymarket.

    Must be explicitly armed via arm() before any trade is placed.
    """

    def __init__(
        self,
        config: BotConfig,
        order_manager: OrderManager,
        clob_client: PolymarketCLOBClient,
    ) -> None:
        self._config = config
        self._order_manager = order_manager
        self._clob = clob_client
        self._armed = False
        self._live_trade_count = 0
        self._manual_review_limit = config.first_live_trades_manual_review

    def arm(self, passphrase: str) -> None:
        """Arm the live executor by verifying env flag + passphrase.

        Args:
            passphrase: Must exactly equal LIVE_TRADING_PASSPHRASE.

        Raises:
            LiveTradingGateError: If any gate condition fails.
        """
        # Gate 1: environment variable
        if os.environ.get("ENABLE_LIVE_TRADING", "").lower() != "true":
            raise LiveTradingGateError(
                "ENABLE_LIVE_TRADING env var is not set to 'true'. "
                "Export ENABLE_LIVE_TRADING=true to proceed."
            )

        # Gate 2: runtime passphrase
        if passphrase.strip() != LIVE_TRADING_PASSPHRASE:
            raise LiveTradingGateError(
                f"Passphrase mismatch. You must type exactly:\n  {LIVE_TRADING_PASSPHRASE}"
            )

        # Gate 3: config flag
        if not self._config.enable_live_trading:
            raise LiveTradingGateError(
                "config.enable_live_trading is False. "
                "Set enable_live_trading: true in your config file."
            )

        self._armed = True
        logger.warning(
            "LIVE_TRADING_ARMED",
            operator="confirmed",
            manual_review_count=self._manual_review_limit,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        print(
            "\n⚠️  LIVE TRADING ARMED. Real funds will be used. "
            f"First {self._manual_review_limit} trades require manual approval.\n"
        )

    def disarm(self) -> None:
        """Disarm live trading. Safe to call at any time."""
        self._armed = False
        logger.warning("LIVE_TRADING_DISARMED", timestamp=datetime.now(timezone.utc).isoformat())

    @property
    def is_armed(self) -> bool:
        return self._armed

    async def execute(self, signal: Signal, override_size_usd: float | None = None) -> Order:
        """Execute a live signed transaction.

        Raises:
            LiveTradingGateError: If not armed or manual review required.
        """
        if not self._armed:
            raise LiveTradingGateError("LiveExecutor is not armed. Call arm() first.")

        size_usd = override_size_usd if override_size_usd is not None else signal.recommended_size_usd

        # Manual review for first N trades
        if self._live_trade_count < self._manual_review_limit:
            await self._request_manual_approval(signal, size_usd)

        order = self._order_manager.create_order(signal, size_usd, is_paper=False)

        # Log signing event BEFORE signing (metadata only — never log key)
        self._log_signing_event(order, signal)

        try:
            if signal.token is None:
                raise ValueError("Signal missing token")

            resp = await self._clob.place_limit_order(
                token_id=signal.token.token_id,
                price=order.price,
                size=order.size,
                side=order.side.value,
            )

            exchange_order_id = str(resp.get("orderID", resp.get("id", "")))
            self._order_manager.update_order(
                order_id=order.order_id,
                status=OrderStatus.OPEN,
                exchange_order_id=exchange_order_id,
            )
            self._live_trade_count += 1

            logger.info(
                "live_order_submitted",
                order_id=order.order_id,
                exchange_id=exchange_order_id,
                trade_count=self._live_trade_count,
            )

        except Exception as e:
            self._order_manager.update_order(order.order_id, OrderStatus.REJECTED)
            logger.error("live_order_failed", order_id=order.order_id, error=str(e))
            raise

        return order

    async def cancel(self, order_id: str) -> bool:
        """Cancel a live order by exchange order ID."""
        order = self._order_manager.get_order(order_id)
        if not order or not order.exchange_order_id:
            return False

        try:
            await self._clob.cancel_order(order.exchange_order_id)
            self._order_manager.update_order(order_id, OrderStatus.CANCELLED)
            return True
        except Exception as e:
            logger.error("live_cancel_failed", order_id=order_id, error=str(e))
            return False

    async def cancel_all_open(self) -> int:
        """Emergency: cancel all open live orders."""
        cancelled = 0
        for order in self._order_manager.open_orders():
            if not order.is_paper:
                if await self.cancel(order.order_id):
                    cancelled += 1
        logger.warning("emergency_cancel_all", cancelled=cancelled)
        return cancelled

    def _log_signing_event(self, order: Order, signal: Signal) -> None:
        """Log signing event metadata. Never logs private key."""
        logger.warning(
            "SIGNING_EVENT",
            order_id=order.order_id,
            token_id=order.token_id,
            side=order.side.value,
            price=order.price,
            size=order.size,
            size_usd=order.size_usd,
            signal_id=signal.signal_id,
            ev=signal.expected_value,
            confidence=signal.confidence,
            trade_number=self._live_trade_count + 1,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    async def _request_manual_approval(self, signal: Signal, size_usd: float) -> None:
        """Prompt operator for manual trade approval (async-safe via executor)."""
        loop = asyncio.get_event_loop()

        def prompt() -> str:
            print(
                f"\n{'='*60}\n"
                f"MANUAL APPROVAL REQUIRED (trade #{self._live_trade_count + 1})\n"
                f"Market : {signal.market.question[:70] if signal.market else 'N/A'}\n"
                f"Token  : {signal.token.outcome if signal.token else 'N/A'}\n"
                f"Side   : {signal.side.value}\n"
                f"Price  : {signal.market_price:.4f}\n"
                f"Size   : ${size_usd:.2f}\n"
                f"EV     : {signal.expected_value:.4f}\n"
                f"{'='*60}\n"
                f"Type 'yes' to approve or 'no' to skip: "
            )
            return input()

        response = await loop.run_in_executor(None, prompt)
        if response.strip().lower() != "yes":
            raise LiveTradingGateError(f"Operator rejected trade #{self._live_trade_count + 1}")
