"""Paper trading executor — simulates fills with realistic slippage and latency."""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timezone

import structlog

from polymarket_bot.config.settings import BotConfig
from polymarket_bot.execution.order_manager import OrderManager
from polymarket_bot.types import Order, OrderStatus, OrderType, Signal

logger = structlog.get_logger(__name__)


class PaperExecutor:
    """Simulates order execution without placing real orders.

    Models:
    - Slippage: random uniform in [0, slippage_tolerance_pct]
    - Partial fills: 10% chance of partial fill at 60–90% of requested size
    - Latency: random 50–200ms simulated network delay
    - Gas: ~$0.10 simulated Polygon transaction cost
    """

    SIMULATED_GAS_USD = 0.10
    LATENCY_MS_RANGE = (50, 200)

    def __init__(self, config: BotConfig, order_manager: OrderManager) -> None:
        self._config = config
        self._order_manager = order_manager

    async def execute(self, signal: Signal, override_size_usd: float | None = None) -> Order:
        """Execute a signal in paper mode.

        Returns the resulting Order with simulated fill details.
        """
        size_usd = override_size_usd if override_size_usd is not None else signal.recommended_size_usd

        order = self._order_manager.create_order(signal, size_usd, is_paper=True)
        order.status = OrderStatus.OPEN

        # Simulate network latency
        latency_ms = random.uniform(*self.LATENCY_MS_RANGE)
        await asyncio.sleep(latency_ms / 1000)

        # Simulate slippage
        max_slip = self._config.risk.slippage_tolerance_pct
        slippage = random.uniform(0, max_slip) * order.price
        if signal.side.value == "BUY":
            fill_price = order.price + slippage
        else:
            fill_price = order.price - slippage

        fill_price = max(0.001, min(0.999, fill_price))

        # Simulate occasional partial fills
        if random.random() < 0.10:
            fill_ratio = random.uniform(0.6, 0.9)
            fill_size = order.size * fill_ratio
            status = OrderStatus.PARTIALLY_FILLED
            logger.info(
                "paper_partial_fill",
                order_id=order.order_id,
                requested=round(order.size, 4),
                filled=round(fill_size, 4),
            )
        else:
            fill_size = order.size
            status = OrderStatus.FILLED

        self._order_manager.update_order(
            order_id=order.order_id,
            status=status,
            filled_size=fill_size,
            filled_price=fill_price,
        )

        if status == OrderStatus.FILLED or status == OrderStatus.PARTIALLY_FILLED:
            self._order_manager.open_position(order)

        logger.info(
            "paper_fill",
            order_id=order.order_id,
            signal_id=signal.signal_id,
            token_id=order.token_id,
            side=order.side.value,
            requested_price=round(order.price, 4),
            fill_price=round(fill_price, 4),
            slippage=round(slippage, 5),
            fill_size=round(fill_size, 4),
            fill_usd=round(fill_size * fill_price, 2),
            latency_ms=round(latency_ms, 1),
            status=status.value,
        )

        return order

    async def cancel(self, order_id: str) -> Order | None:
        """Cancel an open paper order."""
        order = self._order_manager.get_order(order_id)
        if order and order.status in (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED, OrderStatus.PENDING):
            self._order_manager.update_order(order_id, OrderStatus.CANCELLED)
            logger.info("paper_order_cancelled", order_id=order_id)
            return order
        return None

    async def cancel_stale_orders(self) -> list[str]:
        """Cancel any open orders that have exceeded their TTL."""
        cancelled: list[str] = []
        now = datetime.now(timezone.utc)
        ttl = self._config.execution.order_timeout_seconds

        for order in self._order_manager.open_orders():
            if order.is_paper:
                age = (now - order.created_at.replace(tzinfo=timezone.utc)).total_seconds()
                if age > ttl:
                    await self.cancel(order.order_id)
                    cancelled.append(order.order_id)

        return cancelled
