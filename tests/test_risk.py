"""Tests for the layered risk management system."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from polymarket_bot.config import BotConfig
from polymarket_bot.risk.manager import RiskManager, RiskDecision
from polymarket_bot.types import Order, Position, Side, Signal, SignalType


def _make_signal(
    market_id: str = "mkt_1",
    token_id: str = "tok_1",
    ev: float = 0.05,
    model_prob: float = 0.70,
    market_price: float = 0.50,
    confidence: float = 0.80,
    liquidity: float = 50_000.0,
) -> Signal:
    return Signal(
        signal_type=SignalType.MISPRICING,
        market_id=market_id,
        token_id=token_id,
        side=Side.BUY,
        model_prob=model_prob,
        market_price=market_price,
        ev=ev,
        confidence=confidence,
        liquidity_usd=liquidity,
    )


class TestLayer0HardLimits:
    def test_per_trade_cap(self, config: BotConfig) -> None:
        config.per_trade_max_usd = 100
        rm = RiskManager(config)
        signal = _make_signal()
        decision = rm.check_hard_limits(signal, 500)
        assert decision.allowed
        assert decision.adjusted_size_usd <= 100

    def test_global_exposure_cap(self, config: BotConfig) -> None:
        config.global_exposure_usd = 500
        rm = RiskManager(config)
        # Fill up exposure
        rm.add_position(Position(market_id="a", size_usd=450))
        signal = _make_signal()
        decision = rm.check_hard_limits(signal, 100)
        assert decision.allowed
        assert decision.adjusted_size_usd <= 50

    def test_global_exposure_full(self, config: BotConfig) -> None:
        config.global_exposure_usd = 500
        rm = RiskManager(config)
        rm.add_position(Position(market_id="a", size_usd=500))
        signal = _make_signal()
        decision = rm.check_hard_limits(signal, 100)
        assert not decision.allowed

    def test_per_market_cap(self, config: BotConfig) -> None:
        config.per_market_exposure_usd = 200
        rm = RiskManager(config)
        rm.add_position(Position(market_id="mkt_1", size_usd=150))
        signal = _make_signal(market_id="mkt_1")
        decision = rm.check_hard_limits(signal, 100)
        assert decision.allowed
        assert decision.adjusted_size_usd <= 50

    def test_max_concurrent_positions(self, config: BotConfig) -> None:
        config.max_concurrent_positions = 2
        rm = RiskManager(config)
        rm.add_position(Position(market_id="a", size_usd=100))
        rm.add_position(Position(market_id="b", size_usd=100))
        signal = _make_signal(market_id="c")
        decision = rm.check_hard_limits(signal, 100)
        assert not decision.allowed

    def test_kill_switch_blocks_all(self, config: BotConfig) -> None:
        rm = RiskManager(config)
        rm.activate_kill_switch()
        signal = _make_signal()
        decision = rm.check_hard_limits(signal, 100)
        assert not decision.allowed


class TestLayer1PositionSizing:
    def test_kelly_sizing_positive(self, config: BotConfig) -> None:
        rm = RiskManager(config)
        signal = _make_signal(model_prob=0.70, market_price=0.50, confidence=0.80)
        size = rm.compute_position_size(signal)
        assert size > 0
        assert size <= config.per_trade_max_usd

    def test_kelly_sizing_no_edge(self, config: BotConfig) -> None:
        rm = RiskManager(config)
        signal = _make_signal(model_prob=0.50, market_price=0.50, confidence=0.80)
        size = rm.compute_position_size(signal)
        assert size == 0  # No edge = no bet

    def test_liquidity_filter(self, config: BotConfig) -> None:
        config.min_liquidity_usd = 100_000
        rm = RiskManager(config)
        signal = _make_signal(liquidity=50_000)
        size = rm.compute_position_size(signal)
        assert size == 0


class TestLayer3Stops:
    def test_stop_loss_triggered(self, config: BotConfig) -> None:
        config.stop_loss_pct = 0.10
        rm = RiskManager(config)
        pos = Position(
            market_id="a",
            side=Side.BUY,
            entry_price=0.60,
            current_price=0.50,  # -16.7% loss
            size_usd=100,
            size_shares=166.67,
        )
        rm.add_position(pos)
        exits = rm.check_stops()
        assert len(exits) == 1

    def test_time_decay_exit(self, config: BotConfig) -> None:
        config.time_decay_hours = 1  # 1 hour for testing
        rm = RiskManager(config)
        pos = Position(
            market_id="a",
            entry_price=0.50,
            current_price=0.50,
            size_usd=100,
            opened_at=datetime.now(timezone.utc) - timedelta(hours=2),
        )
        rm.add_position(pos)
        exits = rm.check_stops()
        assert len(exits) == 1


class TestLayer5CircuitBreakers:
    def test_drawdown_breaker(self, config: BotConfig) -> None:
        config.max_drawdown_pct = 0.10
        rm = RiskManager(config)
        rm._peak_equity = 10_000
        rm._current_equity = 8_500  # 15% drawdown
        assert rm.check_circuit_breakers()
        assert rm._circuit_breaker_active

    def test_consecutive_losses_breaker(self, config: BotConfig) -> None:
        config.max_consecutive_losses = 3
        rm = RiskManager(config)
        for _ in range(3):
            rm.record_trade_result(-10)
        assert rm.check_circuit_breakers()

    def test_rpc_failure_breaker(self, config: BotConfig) -> None:
        config.rpc_failure_threshold = 2
        rm = RiskManager(config)
        rm.record_rpc_failure()
        rm.record_rpc_failure()
        assert rm.check_circuit_breakers()

    def test_reset_circuit_breaker(self, config: BotConfig) -> None:
        rm = RiskManager(config)
        rm._circuit_breaker_active = True
        rm.reset_circuit_breaker()
        assert not rm._circuit_breaker_active


class TestLayer6Compliance:
    def test_wash_trading_prevention(self, config: BotConfig) -> None:
        config.min_time_between_opposing_trades_seconds = 60
        rm = RiskManager(config)

        # Record a BUY trade
        buy_order = Order(
            market_id="mkt_1",
            side=Side.BUY,
            created_at=datetime.now(timezone.utc),
        )
        rm.trade_history.append(buy_order)

        # Try to SELL on same market immediately
        sell_signal = _make_signal(market_id="mkt_1")
        sell_signal.side = Side.SELL
        decision = rm.check_compliance(sell_signal)
        assert not decision.allowed
        assert "Wash trading" in decision.reason


class TestFullPipeline:
    def test_evaluate_signal_passes(self, config: BotConfig) -> None:
        rm = RiskManager(config)
        signal = _make_signal(model_prob=0.75, market_price=0.50, confidence=0.85)
        decision = rm.evaluate_signal(signal)
        assert decision.allowed
        assert decision.adjusted_size_usd > 0

    def test_evaluate_signal_rejected_no_edge(self, config: BotConfig) -> None:
        rm = RiskManager(config)
        # Model agrees with market exactly — Kelly = 0, no edge to bet on
        signal = _make_signal(model_prob=0.50, market_price=0.50, confidence=0.80)
        decision = rm.evaluate_signal(signal)
        assert not decision.allowed
        assert "too small" in decision.reason.lower() or decision.adjusted_size_usd == 0
