"""Live executor — signs and broadcasts real transactions.

SAFETY: This module will REFUSE to execute unless:
1. ENABLE_LIVE_TRADING=true environment variable is set
2. Runtime passphrase has been confirmed
3. Config paper_mode is False

Every signing event is logged. First N trades require manual approval.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import structlog

from polymarket_bot.config import BotConfig
from polymarket_bot.types import Order, OrderStatus

logger = structlog.get_logger()


class LiveTradingNotEnabled(Exception):
    """Raised when live trading safeguards have not been satisfied."""


class ManualApprovalRequired(Exception):
    """Raised when a trade requires manual operator approval."""


class LiveExecutor:
    """Executes real orders on Polymarket via the CLOB API.

    All signing events are logged. Private keys are NEVER logged.
    """

    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self._passphrase_confirmed = False
        self._trade_count = 0
        self._orders: dict[str, Order] = {}

    def confirm_passphrase(self, passphrase: str) -> bool:
        """Operator must type the exact passphrase to enable live trading."""
        if passphrase == self.config.live_passphrase:
            self._passphrase_confirmed = True
            logger.warning("live_executor.passphrase_confirmed")
            return True
        logger.error("live_executor.passphrase_rejected")
        return False

    def _check_safeguards(self) -> None:
        """Verify all live trading safeguards are satisfied."""
        if self.config.paper_mode:
            raise LiveTradingNotEnabled("Config paper_mode is True")

        env_flag = os.environ.get("ENABLE_LIVE_TRADING", "false").lower()
        if env_flag != "true":
            raise LiveTradingNotEnabled("ENABLE_LIVE_TRADING env var is not 'true'")

        if not self._passphrase_confirmed:
            raise LiveTradingNotEnabled("Runtime passphrase has not been confirmed")

        if not self.config.private_key:
            raise LiveTradingNotEnabled("No PRIVATE_KEY configured")

    async def place_order(self, order: Order) -> Order:
        """Place a real order on Polymarket.

        Raises LiveTradingNotEnabled if safeguards aren't met.
        Raises ManualApprovalRequired if trade needs manual review.
        """
        self._check_safeguards()

        # Manual review for first N trades
        if self._trade_count < self.config.first_live_trades_manual_review:
            logger.warning(
                "live_executor.manual_review_required",
                trade_number=self._trade_count + 1,
                required=self.config.first_live_trades_manual_review,
                order_id=order.order_id[:8],
                side=order.side.value,
                price=order.price,
                size_usd=order.size_usd,
            )
            raise ManualApprovalRequired(
                f"Trade #{self._trade_count + 1} requires manual approval "
                f"(first {self.config.first_live_trades_manual_review} trades)"
            )

        order.is_paper = False
        order.status = OrderStatus.OPEN

        # Log signing event (NEVER log the private key)
        logger.info(
            "live_executor.signing_order",
            order_id=order.order_id[:8],
            market_id=order.market_id[:12],
            side=order.side.value,
            price=order.price,
            size_usd=order.size_usd,
            wallet=self.config.wallet_address[:10] + "..." if self.config.wallet_address else "N/A",
        )

        # In production, this would use py-clob-client:
        # from py_clob_client.client import ClobClient
        # client = ClobClient(host, key=private_key, chain_id=137, ...)
        # resp = client.create_and_post_order(order_args)
        #
        # For safety, we log but do NOT execute without a real integration.
        # This stub returns the order as OPEN — a real implementation would
        # parse the CLOB response and update status accordingly.

        logger.warning(
            "live_executor.order_submitted_stub",
            order_id=order.order_id[:8],
            note="Real CLOB integration required for production use",
        )

        self._orders[order.order_id] = order
        self._trade_count += 1
        return order

    async def approve_manual_trade(self, order: Order) -> Order:
        """Approve a trade that was flagged for manual review."""
        self._check_safeguards()
        self._trade_count += 1
        order.is_paper = False
        order.status = OrderStatus.OPEN
        order.metadata["manually_approved"] = True
        order.metadata["approved_at"] = datetime.now(timezone.utc).isoformat()

        logger.info(
            "live_executor.manual_trade_approved",
            order_id=order.order_id[:8],
            trade_number=self._trade_count,
        )

        self._orders[order.order_id] = order
        return order

    async def cancel_order(self, order_id: str) -> bool:
        self._check_safeguards()
        if order_id in self._orders:
            self._orders[order_id].status = OrderStatus.CANCELLED
            logger.info("live_executor.order_cancelled", order_id=order_id[:8])
            return True
        return False

    async def get_order_status(self, order_id: str) -> Order | None:
        return self._orders.get(order_id)
