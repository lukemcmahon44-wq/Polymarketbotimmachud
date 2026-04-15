"""Execution engine — routes orders to paper or live executor."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Protocol

import structlog

from polymarket_bot.config import BotConfig
from polymarket_bot.types import Order, OrderStatus, OrderType, Signal, Side

logger = structlog.get_logger()


class OrderExecutor(Protocol):
    """Interface for order execution backends."""

    async def place_order(self, order: Order) -> Order: ...
    async def cancel_order(self, order_id: str) -> bool: ...
    async def get_order_status(self, order_id: str) -> Order | None: ...


class ExecutionEngine:
    """Routes signals to orders, manages order lifecycle, and reconciles fills.

    Responsibilities:
    - Convert signals to orders (respecting risk limits)
    - Route to paper or live executor
    - Track order lifecycle and TTL
    - Persist audit log of all order events
    """

    def __init__(self, executor: OrderExecutor, config: BotConfig) -> None:
        self.executor = executor
        self.config = config
        self.orders: dict[str, Order] = {}
        self.audit_log: list[dict] = []
        self._live_trade_count = 0

    def signal_to_order(self, signal: Signal, size_usd: float) -> Order:
        """Convert a signal to an order."""
        order_type = OrderType.LIMIT if self.config.prefer_limit_orders else OrderType.MARKET
        price = signal.metadata.get("effective_price", signal.market_price)

        # For limit orders, improve price slightly
        if order_type == OrderType.LIMIT:
            if signal.side == Side.BUY:
                price = min(price, signal.market_price * (1 + self.config.slippage_tolerance_pct))
            else:
                price = max(price, signal.market_price * (1 - self.config.slippage_tolerance_pct))

        shares = size_usd / price if price > 0 else 0

        ttl = timedelta(seconds=self.config.order_ttl_seconds)
        now = datetime.now(timezone.utc)

        return Order(
            signal_id=signal.signal_id,
            market_id=signal.market_id,
            token_id=signal.token_id,
            side=signal.side,
            order_type=order_type,
            price=round(price, 4),
            size_usd=round(size_usd, 2),
            size_shares=round(shares, 4),
            status=OrderStatus.PENDING,
            is_paper=self.config.paper_mode,
            expires_at=now + ttl,
        )

    async def execute_signal(self, signal: Signal, size_usd: float) -> Order:
        """Execute a signal: create order, submit, and log."""
        order = self.signal_to_order(signal, size_usd)

        self._log_event("order_created", order)
        logger.info(
            "execution.order_created",
            order_id=order.order_id[:8],
            market=order.market_id[:12],
            side=order.side.value,
            price=order.price,
            size_usd=order.size_usd,
            paper=order.is_paper,
        )

        # Submit to executor
        try:
            order = await self.executor.place_order(order)
            self.orders[order.order_id] = order
            self._log_event("order_submitted", order)

            if not order.is_paper:
                self._live_trade_count += 1

        except Exception as exc:
            order.status = OrderStatus.REJECTED
            order.metadata["rejection_reason"] = str(exc)
            self._log_event("order_rejected", order)
            logger.error("execution.order_rejected", error=str(exc))

        return order

    async def reconcile_orders(self) -> list[Order]:
        """Check status of all open orders, handle TTL expiry."""
        updated: list[Order] = []
        now = datetime.now(timezone.utc)

        for order_id, order in list(self.orders.items()):
            if order.status in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.EXPIRED):
                continue

            # TTL check
            if order.expires_at and now > order.expires_at:
                await self.executor.cancel_order(order_id)
                order.status = OrderStatus.EXPIRED
                order.updated_at = now
                self._log_event("order_expired", order)
                updated.append(order)
                continue

            # Status check
            current = await self.executor.get_order_status(order_id)
            if current and current.status != order.status:
                order.status = current.status
                order.filled_size = current.filled_size
                order.fill_price = current.fill_price
                order.slippage = abs(current.fill_price - order.price) if current.fill_price else 0
                order.updated_at = now
                self._log_event("order_updated", order)
                updated.append(order)

                # Check fill price deviation
                if order.fill_price > 0:
                    deviation = abs(order.fill_price - order.price) / order.price
                    if deviation > self.config.max_fill_deviation_pct:
                        logger.warning(
                            "execution.fill_deviation",
                            order_id=order_id[:8],
                            deviation=round(deviation, 4),
                        )

        return updated

    @property
    def open_orders(self) -> list[Order]:
        return [
            o for o in self.orders.values()
            if o.status in (OrderStatus.PENDING, OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED)
        ]

    @property
    def needs_manual_review(self) -> bool:
        """Whether next live trade requires manual approval."""
        return (
            not self.config.paper_mode
            and self._live_trade_count < self.config.first_live_trades_manual_review
        )

    def _log_event(self, event: str, order: Order) -> None:
        self.audit_log.append({
            "event": event,
            "order_id": order.order_id,
            "signal_id": order.signal_id,
            "market_id": order.market_id,
            "side": order.side.value,
            "price": order.price,
            "size_usd": order.size_usd,
            "status": order.status.value,
            "is_paper": order.is_paper,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
