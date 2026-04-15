"""Tests for the backtest engine and synthetic data generation."""

from __future__ import annotations

import pytest

from polymarket_bot.backtest.data_generator import SyntheticDataGenerator
from polymarket_bot.backtest.engine import BacktestEngine
from polymarket_bot.backtest.metrics import compute_max_drawdown, monte_carlo_stress_test
from polymarket_bot.config import BotConfig
from polymarket_bot.types import Order, OrderStatus


class TestSyntheticDataGenerator:
    def test_generate_market(self) -> None:
        gen = SyntheticDataGenerator(seed=42)
        market = gen.generate_market(category="crypto")
        assert market.market_id
        assert len(market.tokens) == 2
        assert 0 < market.tokens[0].price < 1
        assert abs(market.tokens[0].price + market.tokens[1].price - 1.0) < 0.01

    def test_generate_price_series(self) -> None:
        gen = SyntheticDataGenerator(seed=42)
        prices = gen.generate_price_series(0.5, steps=50, volatility=0.02)
        assert len(prices) == 50
        assert all(0.01 <= p <= 0.99 for p in prices)

    def test_generate_price_series_resolution(self) -> None:
        gen = SyntheticDataGenerator(seed=42)
        prices = gen.generate_price_series(0.5, steps=100, resolution=True)
        # Should converge toward 1.0
        assert prices[-1] > prices[0] or prices[-1] > 0.7

    def test_generate_order_book(self) -> None:
        gen = SyntheticDataGenerator(seed=42)
        book = gen.generate_order_book("tok_1", 0.50)
        assert len(book.bids) > 0
        assert len(book.asks) > 0
        assert book.best_bid < book.best_ask

    def test_generate_scenario(self) -> None:
        gen = SyntheticDataGenerator(seed=42)
        scenario = gen.generate_scenario(num_markets=5, time_steps=20)
        assert len(scenario["markets"]) == 5
        assert scenario["time_steps"] == 20
        assert len(scenario["price_series"]) == 5
        assert len(scenario["resolutions"]) == 5


class TestMetrics:
    def test_max_drawdown_simple(self) -> None:
        curve = [100, 110, 105, 120, 95, 130]
        dd = compute_max_drawdown(curve)
        # Peak was 120, trough was 95 => 20.83%
        assert 0.20 < dd < 0.22

    def test_max_drawdown_monotonic_up(self) -> None:
        curve = [100, 110, 120, 130]
        dd = compute_max_drawdown(curve)
        assert dd == 0.0

    def test_max_drawdown_empty(self) -> None:
        assert compute_max_drawdown([]) == 0.0

    def test_monte_carlo_runs(self) -> None:
        trades = []
        for i in range(20):
            o = Order(status=OrderStatus.FILLED)
            o.metadata["realized_pnl"] = 10 if i % 2 == 0 else -8
            trades.append(o)

        result = monte_carlo_stress_test(trades, num_simulations=100, seed=42)
        assert result["num_simulations"] == 100
        assert "mean_pnl" in result
        assert "worst_max_drawdown" in result


class TestBacktestEngine:
    @pytest.mark.asyncio
    async def test_full_backtest_run(self, config: BotConfig) -> None:
        config.min_liquidity_usd = 0
        config.min_ev_threshold = 0.001
        config.confidence_floor = 0.1
        config.backtest.monte_carlo_runs = 50  # Speed up for test

        gen = SyntheticDataGenerator(seed=42)
        scenario = gen.generate_scenario(num_markets=5, time_steps=20)

        engine = BacktestEngine(config)
        result = await engine.run(scenario, initial_capital=10_000)

        assert result.total_trades >= 0
        assert len(result.equity_curve) > 0
        assert "monte_carlo" in result.metadata
