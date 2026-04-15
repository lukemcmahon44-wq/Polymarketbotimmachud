"""Portfolio tracker — real-time P&L, position management, and performance analytics."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from polymarket_bot.types import Order, OrderStatus, Position, Side

logger = structlog.get_logger()


@dataclass
class TradeRecord:
    """Immutable record of a completed trade for analytics."""
    order_id: str
    market_id: str
    token_id: str
    side: Side
    entry_price: float
    exit_price: float | None = None
    size_usd: float = 0.0
    size_shares: float = 0.0
    realized_pnl: float = 0.0
    fees: float = 0.0
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    closed_at: datetime | None = None
    is_paper: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


class PortfolioTracker:
    """Tracks all positions, P&L, and performance metrics in real time.

    Maintains:
    - Open positions with live mark-to-market
    - Closed trade history with realized P&L
    - Running equity curve
    - Performance statistics (win rate, avg P&L, Sharpe estimate)
    """

    def __init__(self, initial_capital: float = 10_000.0) -> None:
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.positions: dict[str, Position] = {}
        self.trade_history: list[TradeRecord] = []
        self.equity_curve: list[tuple[datetime, float]] = [
            (datetime.now(timezone.utc), initial_capital)
        ]
        self._peak_equity = initial_capital

    @property
    def total_equity(self) -> float:
        """Cash + mark-to-market value of all positions."""
        position_value = sum(
            p.current_price * p.size_shares for p in self.positions.values()
        )
        return self.cash + position_value

    @property
    def total_unrealized_pnl(self) -> float:
        return sum(p.unrealized_pnl for p in self.positions.values())

    @property
    def total_realized_pnl(self) -> float:
        return sum(t.realized_pnl for t in self.trade_history)

    @property
    def total_pnl(self) -> float:
        return self.total_realized_pnl + self.total_unrealized_pnl

    @property
    def return_pct(self) -> float:
        if self.initial_capital == 0:
            return 0.0
        return (self.total_equity - self.initial_capital) / self.initial_capital

    @property
    def max_drawdown(self) -> float:
        if self._peak_equity == 0:
            return 0.0
        return (self._peak_equity - self.total_equity) / self._peak_equity

    @property
    def win_rate(self) -> float:
        if not self.trade_history:
            return 0.0
        winners = sum(1 for t in self.trade_history if t.realized_pnl > 0)
        return winners / len(self.trade_history)

    def open_position(self, order: Order) -> Position:
        """Create a new position from a filled order."""
        if order.status != OrderStatus.FILLED:
            raise ValueError(f"Cannot open position from {order.status} order")

        pos = Position(
            market_id=order.market_id,
            token_id=order.token_id,
            side=order.side,
            entry_price=order.fill_price,
            current_price=order.fill_price,
            size_usd=order.size_usd,
            size_shares=order.filled_size,
        )

        # Deduct from cash
        self.cash -= order.size_usd + order.gas_cost_usd

        self.positions[pos.position_id] = pos
        self._record_equity()

        logger.info(
            "portfolio.position_opened",
            position_id=pos.position_id[:8],
            market=pos.market_id[:12],
            side=pos.side.value,
            entry=pos.entry_price,
            size_usd=pos.size_usd,
            cash_remaining=round(self.cash, 2),
        )
        return pos

    def close_position(
        self,
        position_id: str,
        exit_price: float,
        fees: float = 0.0,
    ) -> TradeRecord:
        """Close a position and record the trade."""
        pos = self.positions.pop(position_id, None)
        if not pos:
            raise KeyError(f"Position {position_id} not found")

        # Calculate realized P&L
        if pos.side == Side.BUY:
            realized_pnl = (exit_price - pos.entry_price) * pos.size_shares - fees
        else:
            realized_pnl = (pos.entry_price - exit_price) * pos.size_shares - fees

        # Return capital + P&L to cash
        self.cash += pos.size_usd + realized_pnl

        record = TradeRecord(
            order_id=pos.position_id,
            market_id=pos.market_id,
            token_id=pos.token_id,
            side=pos.side,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            size_usd=pos.size_usd,
            size_shares=pos.size_shares,
            realized_pnl=round(realized_pnl, 4),
            fees=fees,
            opened_at=pos.opened_at,
            closed_at=datetime.now(timezone.utc),
        )
        self.trade_history.append(record)
        self._record_equity()

        logger.info(
            "portfolio.position_closed",
            position_id=position_id[:8],
            entry=pos.entry_price,
            exit=exit_price,
            pnl=round(realized_pnl, 4),
            total_pnl=round(self.total_realized_pnl, 2),
        )
        return record

    def update_prices(self, prices: dict[str, float]) -> None:
        """Update position mark-to-market from latest prices."""
        for pos in self.positions.values():
            if pos.token_id in prices:
                pos.current_price = prices[pos.token_id]
                if pos.side == Side.BUY:
                    pos.unrealized_pnl = (pos.current_price - pos.entry_price) * pos.size_shares
                else:
                    pos.unrealized_pnl = (pos.entry_price - pos.current_price) * pos.size_shares

        self._record_equity()

    def _record_equity(self) -> None:
        equity = self.total_equity
        self._peak_equity = max(self._peak_equity, equity)
        self.equity_curve.append((datetime.now(timezone.utc), equity))

    def get_summary(self) -> dict[str, Any]:
        """Comprehensive portfolio summary."""
        return {
            "initial_capital": self.initial_capital,
            "cash": round(self.cash, 2),
            "total_equity": round(self.total_equity, 2),
            "return_pct": f"{self.return_pct:.2%}",
            "open_positions": len(self.positions),
            "total_trades": len(self.trade_history),
            "win_rate": f"{self.win_rate:.1%}",
            "total_realized_pnl": round(self.total_realized_pnl, 2),
            "total_unrealized_pnl": round(self.total_unrealized_pnl, 2),
            "max_drawdown": f"{self.max_drawdown:.2%}",
            "peak_equity": round(self._peak_equity, 2),
        }

    def export_trades_json(self, path: str | Path) -> None:
        """Export trade history to JSON file."""
        records = []
        for t in self.trade_history:
            records.append({
                "order_id": t.order_id,
                "market_id": t.market_id,
                "side": t.side.value,
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "size_usd": t.size_usd,
                "realized_pnl": t.realized_pnl,
                "fees": t.fees,
                "opened_at": t.opened_at.isoformat(),
                "closed_at": t.closed_at.isoformat() if t.closed_at else None,
                "is_paper": t.is_paper,
            })

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump({"trades": records, "summary": self.get_summary()}, f, indent=2)
        logger.info("portfolio.exported", path=str(path), trades=len(records))

    def export_equity_csv(self, path: str | Path) -> None:
        """Export equity curve to CSV."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            f.write("timestamp,equity\n")
            for ts, eq in self.equity_curve:
                f.write(f"{ts.isoformat()},{eq:.2f}\n")
        logger.info("portfolio.equity_exported", path=str(path), points=len(self.equity_curve))
