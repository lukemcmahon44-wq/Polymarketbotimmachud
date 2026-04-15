"""Order lifecycle management and audit logging."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from polymarket_bot.types import Order, OrderStatus, Position, Side, Signal

logger = structlog.get_logger(__name__)


class OrderManager:
    """Tracks all orders and positions, maintains audit logs.

    All order state is kept in-memory for speed, and persisted to a
    JSON audit log on every state change.
    """

    def __init__(self, audit_log_path: str = "logs/audit.jsonl") -> None:
        self._orders: dict[str, Order] = {}
        self._positions: dict[str, Position] = {}
        self._audit_path = Path(audit_log_path)
        self._audit_path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ orders

    def create_order(self, signal: Signal, size_usd: float, is_paper: bool = True) -> Order:
        """Create a new Order from a Signal."""
        if signal.token is None or signal.market is None:
            raise ValueError("Signal must have token and market populated")

        price = signal.market_price
        size_shares = size_usd / price if price > 0 else 0

        order = Order(
            order_id=str(uuid.uuid4())[:12],
            signal_id=signal.signal_id,
            token_id=signal.token.token_id,
            market_condition_id=signal.market.condition_id,
            side=signal.side,
            price=price,
            size=size_shares,
            size_usd=size_usd,
            is_paper=is_paper,
        )
        self._orders[order.order_id] = order
        self._audit("order_created", order=order)
        logger.info(
            "order_created",
            order_id=order.order_id,
            token_id=order.token_id,
            side=order.side.value,
            price=price,
            size_usd=size_usd,
            is_paper=is_paper,
        )
        return order

    def update_order(
        self,
        order_id: str,
        status: OrderStatus,
        filled_size: float = 0.0,
        filled_price: float = 0.0,
        exchange_order_id: str = "",
    ) -> Order:
        """Update order status after execution attempt."""
        order = self._orders[order_id]
        order.status = status
        order.updated_at = datetime.now(timezone.utc)
        if filled_size:
            order.filled_size = filled_size
        if filled_price:
            order.filled_price = filled_price
            order.slippage = filled_price - order.price
        if exchange_order_id:
            order.exchange_order_id = exchange_order_id

        self._audit("order_updated", order=order)
        logger.info(
            "order_updated",
            order_id=order_id,
            status=status.value,
            filled=filled_size,
            slippage=order.slippage,
        )
        return order

    def get_order(self, order_id: str) -> Order | None:
        return self._orders.get(order_id)

    def open_orders(self) -> list[Order]:
        return [
            o for o in self._orders.values()
            if o.status in (OrderStatus.OPEN, OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED)
        ]

    def filled_orders(self) -> list[Order]:
        return [o for o in self._orders.values() if o.status == OrderStatus.FILLED]

    # --------------------------------------------------------------- positions

    def open_position(self, order: Order) -> Position:
        """Open or add to a position after a fill."""
        key = f"{order.token_id}:{order.side.value}"
        existing = self._positions.get(key)

        if existing:
            # Average into existing position
            total_cost = existing.size * existing.entry_price + order.filled_size * order.filled_price
            existing.size += order.filled_size
            existing.entry_price = total_cost / existing.size if existing.size > 0 else 0
            self._audit("position_updated", position=existing)
            return existing

        pos = Position(
            token_id=order.token_id,
            market_condition_id=order.market_condition_id,
            side=order.side,
            size=order.filled_size,
            entry_price=order.filled_price,
            current_price=order.filled_price,
            is_paper=order.is_paper,
        )
        self._positions[key] = pos
        self._audit("position_opened", position=pos)
        logger.info(
            "position_opened",
            position_id=pos.position_id,
            token_id=pos.token_id,
            size=pos.size,
            entry_price=pos.entry_price,
        )
        return pos

    def close_position(self, token_id: str, side: Side, close_price: float) -> float:
        """Close a position and return realized P&L."""
        key = f"{token_id}:{side.value}"
        pos = self._positions.get(key)
        if not pos:
            return 0.0

        if side == Side.BUY:
            pnl = pos.size * (close_price - pos.entry_price)
        else:
            pnl = pos.size * (pos.entry_price - close_price)

        pos.realized_pnl += pnl
        self._audit("position_closed", position=pos, close_price=close_price, pnl=pnl)
        logger.info(
            "position_closed",
            position_id=pos.position_id,
            pnl=round(pnl, 4),
            close_price=close_price,
        )
        del self._positions[key]
        return pnl

    def get_open_positions(self) -> list[Position]:
        return list(self._positions.values())

    def total_exposure_usd(self) -> float:
        return sum(p.exposure_usd for p in self._positions.values())

    def exposure_by_market(self) -> dict[str, float]:
        exp: dict[str, float] = {}
        for pos in self._positions.values():
            exp[pos.market_condition_id] = exp.get(pos.market_condition_id, 0) + pos.exposure_usd
        return exp

    def total_unrealized_pnl(self) -> float:
        return sum(p.unrealized_pnl for p in self._positions.values())

    # ------------------------------------------------------------------- audit

    def _audit(self, event: str, **kwargs: Any) -> None:
        """Append a structured audit log entry."""
        record: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
        }
        for k, v in kwargs.items():
            if hasattr(v, "__dict__"):
                record[k] = {
                    kk: str(vv) if not isinstance(vv, (int, float, bool, str, type(None))) else vv
                    for kk, vv in v.__dict__.items()
                    if not kk.startswith("_")
                }
            else:
                record[k] = v

        with open(self._audit_path, "a") as f:
            f.write(json.dumps(record) + "\n")
