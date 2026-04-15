"""Tests for the portfolio tracker."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from polymarket_bot.portfolio import PortfolioTracker, TradeRecord
from polymarket_bot.types import Order, OrderStatus, Position, Side


def _filled_order(
    price: float = 0.55,
    size_usd: float = 100.0,
    side: Side = Side.BUY,
) -> Order:
    order = Order(
        market_id="mkt_test",
        token_id="tok_test",
        side=side,
        price=price,
        size_usd=size_usd,
        size_shares=size_usd / price,
        status=OrderStatus.FILLED,
        fill_price=price,
        filled_size=size_usd / price,
        gas_cost_usd=0.01,
    )
    return order


class TestPortfolioTracker:
    def test_initial_state(self) -> None:
        pt = PortfolioTracker(initial_capital=10_000)
        assert pt.cash == 10_000
        assert pt.total_equity == 10_000
        assert pt.total_pnl == 0
        assert len(pt.positions) == 0

    def test_open_position(self) -> None:
        pt = PortfolioTracker(initial_capital=10_000)
        order = _filled_order(price=0.55, size_usd=100)
        pos = pt.open_position(order)

        assert pos.entry_price == 0.55
        assert pos.size_usd == 100
        assert len(pt.positions) == 1
        assert pt.cash < 10_000  # Spent some cash

    def test_close_position_profit(self) -> None:
        pt = PortfolioTracker(initial_capital=10_000)
        order = _filled_order(price=0.50, size_usd=100)
        pos = pt.open_position(order)

        # Close at higher price (profit)
        record = pt.close_position(pos.position_id, exit_price=0.70)
        assert record.realized_pnl > 0
        assert len(pt.trade_history) == 1
        assert pt.win_rate == 1.0

    def test_close_position_loss(self) -> None:
        pt = PortfolioTracker(initial_capital=10_000)
        order = _filled_order(price=0.50, size_usd=100)
        pos = pt.open_position(order)

        # Close at lower price (loss)
        record = pt.close_position(pos.position_id, exit_price=0.30)
        assert record.realized_pnl < 0
        assert pt.win_rate == 0.0

    def test_update_prices(self) -> None:
        pt = PortfolioTracker(initial_capital=10_000)
        order = _filled_order(price=0.50, size_usd=100)
        pos = pt.open_position(order)

        # Price goes up
        pt.update_prices({"tok_test": 0.60})
        assert pos.unrealized_pnl > 0
        assert pt.total_unrealized_pnl > 0

    def test_max_drawdown(self) -> None:
        pt = PortfolioTracker(initial_capital=10_000)
        order = _filled_order(price=0.50, size_usd=1000)
        pos = pt.open_position(order)

        # Price drops
        pt.update_prices({"tok_test": 0.30})
        assert pt.max_drawdown > 0

    def test_multiple_positions(self) -> None:
        pt = PortfolioTracker(initial_capital=10_000)

        o1 = _filled_order(price=0.50, size_usd=100)
        o1.market_id = "mkt_1"
        o1.token_id = "tok_1"
        pt.open_position(o1)

        o2 = _filled_order(price=0.60, size_usd=200)
        o2.market_id = "mkt_2"
        o2.token_id = "tok_2"
        pt.open_position(o2)

        assert len(pt.positions) == 2
        assert pt.cash < 9700

    def test_get_summary(self) -> None:
        pt = PortfolioTracker(initial_capital=5000)
        summary = pt.get_summary()
        assert summary["initial_capital"] == 5000
        assert summary["cash"] == 5000
        assert summary["open_positions"] == 0
        assert summary["total_trades"] == 0

    def test_export_trades_json(self) -> None:
        pt = PortfolioTracker(initial_capital=10_000)
        order = _filled_order(price=0.50, size_usd=100)
        pos = pt.open_position(order)
        pt.close_position(pos.position_id, exit_price=0.60)

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "trades.json"
            pt.export_trades_json(path)
            assert path.exists()
            import json
            data = json.loads(path.read_text())
            assert len(data["trades"]) == 1
            assert "summary" in data

    def test_export_equity_csv(self) -> None:
        pt = PortfolioTracker(initial_capital=10_000)
        order = _filled_order(price=0.50, size_usd=100)
        pt.open_position(order)
        pt.update_prices({"tok_test": 0.60})

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "equity.csv"
            pt.export_equity_csv(path)
            assert path.exists()
            lines = path.read_text().strip().split("\n")
            assert lines[0] == "timestamp,equity"
            assert len(lines) >= 2

    def test_return_pct(self) -> None:
        pt = PortfolioTracker(initial_capital=10_000)
        order = _filled_order(price=0.50, size_usd=1000)
        pos = pt.open_position(order)
        pt.close_position(pos.position_id, exit_price=0.70)
        assert pt.return_pct > 0

    def test_cannot_open_unfilled_order(self) -> None:
        pt = PortfolioTracker(initial_capital=10_000)
        order = Order(
            market_id="mkt", token_id="tok", side=Side.BUY,
            price=0.50, size_usd=100, status=OrderStatus.PENDING,
        )
        with pytest.raises(ValueError, match="PENDING"):
            pt.open_position(order)
