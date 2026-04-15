"""Unit tests for backtest data generator, metrics, and engine."""

from __future__ import annotations

import pytest

from polymarket_bot.backtest.data_generator import SyntheticMarketGenerator
from polymarket_bot.backtest.metrics import BacktestMetrics


class TestSyntheticMarketGenerator:
    def test_generates_correct_shape(self):
        """Generator produces n_markets × n_steps snapshots."""
        gen = SyntheticMarketGenerator(n_markets=5, n_steps=50)
        all_snaps = gen.generate()
        assert len(all_snaps) == 5
        assert all(len(snaps) == 50 for snaps in all_snaps)

    def test_binary_markets(self):
        """All generated markets are binary YES/NO."""
        gen = SyntheticMarketGenerator(n_markets=3, n_steps=10)
        all_snaps = gen.generate()
        for market_snaps in all_snaps:
            for snap in market_snaps:
                assert len(snap.market.tokens) == 2
                assert snap.market.tokens[0].outcome == "Yes"
                assert snap.market.tokens[1].outcome == "No"

    def test_prices_between_zero_and_one(self):
        """Token prices are always in (0, 1)."""
        gen = SyntheticMarketGenerator(n_markets=3, n_steps=100)
        all_snaps = gen.generate()
        for market_snaps in all_snaps:
            for snap in market_snaps:
                for token in snap.market.tokens:
                    assert 0 < token.price < 1

    def test_order_books_have_bids_and_asks(self):
        """Each snapshot has order books with both bids and asks."""
        gen = SyntheticMarketGenerator(n_markets=2, n_steps=10)
        all_snaps = gen.generate()
        for market_snaps in all_snaps:
            for snap in market_snaps:
                for book in snap.order_books.values():
                    assert len(book.bids) > 0
                    assert len(book.asks) > 0

    def test_iter_steps_yields_correct_count(self):
        """iter_steps yields exactly n_steps tuples."""
        gen = SyntheticMarketGenerator(n_markets=3, n_steps=20)
        steps = list(gen.iter_steps())
        assert len(steps) == 20

    def test_deterministic_with_seed(self):
        """Same seed produces identical results."""
        gen1 = SyntheticMarketGenerator(n_markets=5, n_steps=30, seed=99)
        gen2 = SyntheticMarketGenerator(n_markets=5, n_steps=30, seed=99)
        snaps1 = gen1.generate()
        snaps2 = gen2.generate()
        assert snaps1[0][0].true_probability == snaps2[0][0].true_probability


class TestBacktestMetrics:
    def test_positive_pnl_series(self):
        """Metrics compute correctly for a winning series."""
        pnl = [10.0, -5.0, 8.0, -3.0, 12.0, 7.0, -4.0]
        equity = [1000.0 + sum(pnl[:i]) for i in range(len(pnl) + 1)]
        metrics = BacktestMetrics.compute(pnl, equity)
        assert metrics["total_pnl"] == pytest.approx(sum(pnl))
        assert metrics["win_rate"] == pytest.approx(4 / 7, rel=0.01)
        assert metrics["total_trades"] == 7

    def test_max_drawdown_calculation(self):
        """Max drawdown is computed from peak-to-trough."""
        equity = [1000, 1100, 1050, 900, 950, 1000]
        dd = BacktestMetrics.max_drawdown(equity)
        # Peak=1100, trough=900, dd=(1100-900)/1100=18.18%
        assert dd == pytest.approx((1100 - 900) / 1100, rel=0.01)

    def test_max_drawdown_no_decline(self):
        """Max drawdown is 0 for a monotonically increasing equity curve."""
        equity = [1000, 1010, 1020, 1030, 1040]
        assert BacktestMetrics.max_drawdown(equity) == 0.0

    def test_empty_pnl_returns_error(self):
        """Empty P&L series returns error indicator."""
        metrics = BacktestMetrics.compute([], [1000.0])
        assert "error" in metrics

    def test_monte_carlo_returns_percentiles(self):
        """Monte Carlo returns all expected percentile keys."""
        pnl = [float(i % 5 - 2) for i in range(50)]
        mc = BacktestMetrics.monte_carlo(pnl, n_simulations=100)
        for key in ["mc_pnl_p5", "mc_pnl_p50", "mc_pnl_p95", "mc_dd_p95"]:
            assert key in mc

    def test_profit_factor(self):
        """Profit factor = sum(wins) / |sum(losses)|."""
        pnl = [10.0, 10.0, -5.0, -5.0]
        metrics = BacktestMetrics.compute(pnl, [1000.0] * 5)
        assert metrics["profit_factor"] == pytest.approx(20.0 / 10.0, rel=0.01)


@pytest.mark.asyncio
class TestBacktestEngine:
    async def test_engine_runs_without_error(self, config):
        """BacktestEngine completes without raising an exception."""
        from polymarket_bot.backtest.engine import BacktestEngine

        engine = BacktestEngine(config)
        result = await engine.run(
            n_markets=5,
            n_steps=30,
            output_path="logs/test_backtest_results.json",
        )
        assert result is not None
        assert result.total_trades >= 0
        assert len(result.equity_curve) >= 1

    async def test_engine_equity_curve_length(self, config):
        """Equity curve has at least n_steps + 1 entries."""
        from polymarket_bot.backtest.engine import BacktestEngine

        engine = BacktestEngine(config)
        result = await engine.run(
            n_markets=3,
            n_steps=20,
            output_path="logs/test_backtest_results.json",
        )
        assert len(result.equity_curve) >= 20
