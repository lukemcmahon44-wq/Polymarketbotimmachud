"""Paper trading executor — simulates fills with realistic slippage and gas."""

from __future__ import annotations

import random
from datetime import datetime, timezone

import structlog

from polymarket_bot.types import Order, OrderStatus, OrderType, Side

logger = structlog.get_logger()


class PaperExecutor:
    """Simulates order execution for paper trading.

    Models:
    - Fill probability based on order type and price
    - Slippage (market orders: 0.5-2%, limit: 0-0.5%)
    - Gas costs (simulated Polygon gas)
    - Partial fills for large orders
    """

    def __init__(
        self,
        fill_probability: float = 0.85,
        base_slippage_bps: int = 50,
        gas_cost_usd: float = 0.01,
    ) -> None:
        self.fill_probability = fill_probability
        self.base_slippage_bps = base_slippage_bps
        self.gas_cost_usd = gas_cost_usd
        self._orders: dict[str, Order] = {}

    async def place_order(self, order: Order) -> Order:
        """Simulate placing an order."""
        order.is_paper = True
        order.status = OrderStatus.OPEN
        order.updated_at = datetime.now(timezone.utc)

        # Simulate fill
        if random.random() < self.fill_probability:
            order = self._simulate_fill(order)
        else:
            # Partial fill or no fill
            if random.random() < 0.3:
                order = self._simulate_partial_fill(order)
            # else remains OPEN

        order.gas_cost_usd = self.gas_cost_usd * (1 + random.random() * 0.5)
        self._orders[order.order_id] = order

        logger.info(
            "paper.order_placed",
            order_id=order.order_id[:8],
            status=order.status.value,
            fill_price=order.fill_price,
            slippage_bps=round(order.slippage * 10000, 1) if order.slippage else 0,
        )
        return order

    def _simulate_fill(self, order: Order) -> Order:
        """Full fill with slippage simulation."""
        slippage_bps = self.base_slippage_bps
        if order.order_type == OrderType.MARKET:
            slippage_bps = int(slippage_bps * (2 + random.random() * 2))
        else:
            slippage_bps = int(slippage_bps * random.random())

        slippage_pct = slippage_bps / 10000

        if order.side == Side.BUY:
            order.fill_price = order.price * (1 + slippage_pct)
        else:
            order.fill_price = order.price * (1 - slippage_pct)

        order.fill_price = round(max(0.001, min(0.999, order.fill_price)), 4)
        order.slippage = abs(order.fill_price - order.price)
        order.filled_size = order.size_shares
        order.status = OrderStatus.FILLED
        order.updated_at = datetime.now(timezone.utc)
        return order

    def _simulate_partial_fill(self, order: Order) -> Order:
        """Partial fill simulation."""
        fill_fraction = random.uniform(0.2, 0.8)
        order.filled_size = round(order.size_shares * fill_fraction, 4)

        slippage_bps = int(self.base_slippage_bps * random.random())
        slippage_pct = slippage_bps / 10000

        if order.side == Side.BUY:
            order.fill_price = order.price * (1 + slippage_pct)
        else:
            order.fill_price = order.price * (1 - slippage_pct)

        order.fill_price = round(max(0.001, min(0.999, order.fill_price)), 4)
        order.slippage = abs(order.fill_price - order.price)
        order.status = OrderStatus.PARTIALLY_FILLED
        order.updated_at = datetime.now(timezone.utc)
        return order

    async def cancel_order(self, order_id: str) -> bool:
        if order_id in self._orders:
            self._orders[order_id].status = OrderStatus.CANCELLED
            return True
        return False

    async def get_order_status(self, order_id: str) -> Order | None:
        return self._orders.get(order_id)
