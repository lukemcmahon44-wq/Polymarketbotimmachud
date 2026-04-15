"""Unit tests for the layered risk manager."""

from __future__ import annotations

import pytest

from polymarket_bot.config.settings import BotConfig
from polymarket_bot.execution.order_manager import OrderManager
from polymarket_bot.risk.risk_manager import RiskAction, RiskManager
from polymarket_bot.types import Market, Side, Signal, SignalType, Token


def make_signal(
    market: Market,
    price: float = 0.40,
    model_prob: float = 0.60,
    size_usd: float = 50.0,
    liquidity: float = 10_000,
    confidence: float = 0.75,
) -> Signal:
    token = market.tokens[0] if market.tokens else Token("t1", "Yes", price)
    token.price = price
    return Signal(
        signal_type=SignalType.MISPRICING,
        market=market,
        token=token,
        side=Side.BUY,
        model_probability=model_prob,
        market_price=price,
        expected_value=model_prob - price,
        confidence=confidence,
        liquidity_usd=liquidity,
        recommended_size_usd=size_usd,
    )


class TestRiskManager:
    def test_allow_valid_signal(self, config: BotConfig, binary_market: Market):
        """A well-formed signal with good liquidity is approved."""
        om = OrderManager(audit_log_path="logs/test_audit.jsonl")
        rm = RiskManager(config, om)
        signal = make_signal(binary_market, size_usd=50.0)
        decision = rm.evaluate(signal)
        assert decision.is_approved

    def test_block_zero_size(self, config: BotConfig, binary_market: Market):
        """Signals with zero recommended size are blocked."""
        om = OrderManager(audit_log_path="logs/test_audit.jsonl")
        rm = RiskManager(config, om)
        signal = make_signal(binary_market, size_usd=0.0)
        decision = rm.evaluate(signal)
        assert not decision.is_approved

    def test_reduce_to_per_trade_max(self, config: BotConfig, binary_market: Market):
        """Size is reduced to per_trade_max_usd when over the cap."""
        om = OrderManager(audit_log_path="logs/test_audit.jsonl")
        rm = RiskManager(config, om)
        signal = make_signal(binary_market, size_usd=999.0)  # Way over 100 limit
        decision = rm.evaluate(signal)
        assert decision.is_approved or decision.action == RiskAction.REDUCE
        assert decision.approved_size_usd <= config.risk.per_trade_max_usd

    def test_circuit_breaker_blocks_all(self, config: BotConfig, binary_market: Market):
        """Once circuit breaker trips, all signals are blocked."""
        om = OrderManager(audit_log_path="logs/test_audit.jsonl")
        rm = RiskManager(config, om)
        rm.trip_circuit_breaker("Test trigger")
        signal = make_signal(binary_market, size_usd=10.0)
        decision = rm.evaluate(signal)
        assert decision.action == RiskAction.CIRCUIT_BREAK
        assert not decision.is_approved

    def test_circuit_breaker_reset(self, config: BotConfig, binary_market: Market):
        """After reset, signals are approved again."""
        om = OrderManager(audit_log_path="logs/test_audit.jsonl")
        rm = RiskManager(config, om)
        rm.trip_circuit_breaker("Test trigger")
        rm.reset_circuit_breaker()
        signal = make_signal(binary_market, size_usd=10.0)
        decision = rm.evaluate(signal)
        assert decision.action != RiskAction.CIRCUIT_BREAK

    def test_block_illiquid_signal(self, config: BotConfig, binary_market: Market):
        """Signals with low liquidity are blocked."""
        om = OrderManager(audit_log_path="logs/test_audit.jsonl")
        rm = RiskManager(config, om)
        signal = make_signal(binary_market, liquidity=100.0)  # Below 500 threshold
        decision = rm.evaluate(signal)
        assert not decision.is_approved
        assert "liquidity" in decision.checks

    def test_block_extreme_price(self, config: BotConfig, binary_market: Market):
        """Signals at extreme prices are blocked due to slippage risk."""
        om = OrderManager(audit_log_path="logs/test_audit.jsonl")
        rm = RiskManager(config, om)
        signal = make_signal(binary_market, price=0.995, size_usd=50.0)
        decision = rm.evaluate(signal)
        assert not decision.is_approved

    def test_max_positions_limit(self, config: BotConfig, binary_market: Market):
        """No new trades allowed when max_concurrent_positions is reached."""
        om = OrderManager(audit_log_path="logs/test_audit.jsonl")
        rm = RiskManager(config, om)
        config.risk.max_concurrent_positions = 0  # No positions allowed
        signal = make_signal(binary_market, size_usd=10.0)
        decision = rm.evaluate(signal)
        assert not decision.is_approved

    def test_daily_loss_circuit_breaker(self, config: BotConfig, binary_market: Market):
        """Circuit breaker trips when daily loss exceeds limit."""
        om = OrderManager(audit_log_path="logs/test_audit.jsonl")
        rm = RiskManager(config, om)
        rm.record_pnl(-600.0)  # Exceeds max_daily_loss_usd=500
        assert rm.circuit_breaker_tripped

    def test_signal_missing_token_blocked(self, config: BotConfig, binary_market: Market):
        """Signal with no token is blocked by Layer 6."""
        om = OrderManager(audit_log_path="logs/test_audit.jsonl")
        rm = RiskManager(config, om)
        signal = Signal(
            signal_type=SignalType.MISPRICING,
            market=binary_market,
            token=None,
            side=Side.BUY,
            recommended_size_usd=50.0,
            liquidity_usd=10_000,
        )
        decision = rm.evaluate(signal)
        assert not decision.is_approved
