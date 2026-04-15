"""Unit tests for EV calculator and arbitrage detector."""

from __future__ import annotations

import pytest

from polymarket_bot.config.settings import BotConfig
from polymarket_bot.models.base import ModelEstimate
from polymarket_bot.signals.arbitrage import ArbitrageDetector
from polymarket_bot.signals.ev_calculator import EVCalculator, kelly_fraction
from polymarket_bot.types import Market, OrderBook, PriceLevel, Side, SignalType, Token


# --------------------------------------------------------------------- Kelly

def test_kelly_fraction_positive_edge():
    """Quarter-Kelly gives positive fraction when we have edge."""
    f = kelly_fraction(prob=0.60, price=0.45, kelly_f=0.25)
    assert f > 0
    assert f <= 1.0


def test_kelly_fraction_no_edge():
    """Kelly fraction is zero when model prob <= market price."""
    f = kelly_fraction(prob=0.40, price=0.45, kelly_f=0.25)
    assert f == 0.0


def test_kelly_fraction_extreme_price():
    """Kelly fraction is zero at extreme prices."""
    assert kelly_fraction(prob=0.5, price=0.0) == 0.0
    assert kelly_fraction(prob=0.5, price=1.0) == 0.0


def test_kelly_fraction_full_vs_fractional():
    """Quarter-Kelly is always <= Full-Kelly."""
    f_full = kelly_fraction(prob=0.65, price=0.50, kelly_f=1.0)
    f_quarter = kelly_fraction(prob=0.65, price=0.50, kelly_f=0.25)
    assert f_quarter == pytest.approx(f_full * 0.25, rel=0.01)


# ---------------------------------------------------------------- EVCalculator

class TestEVCalculator:
    def test_generates_buy_signal_when_underpriced(self, config: BotConfig, mispriced_market: Market):
        """EV calculator generates BUY signal when model prob > market price."""
        calc = EVCalculator(config)
        estimates = [
            ModelEstimate(
                market_condition_id=mispriced_market.condition_id,
                token_id="yes_002",
                outcome="Yes",
                probability=0.52,  # Model says 52%
                confidence=0.75,
                model_name="test",
            )
        ]
        signals = calc.compute_signals(mispriced_market, estimates)
        assert len(signals) == 1
        assert signals[0].side == Side.BUY
        assert signals[0].expected_value > 0

    def test_no_signal_below_ev_threshold(self, config: BotConfig, binary_market: Market):
        """No signal generated when EV is below threshold."""
        calc = EVCalculator(config)
        estimates = [
            ModelEstimate(
                market_condition_id=binary_market.condition_id,
                token_id="yes_001",
                outcome="Yes",
                probability=0.455,  # Only 0.5% edge — below 2% threshold
                confidence=0.80,
                model_name="test",
            )
        ]
        signals = calc.compute_signals(binary_market, estimates)
        assert len(signals) == 0

    def test_no_signal_below_confidence_threshold(self, config: BotConfig, mispriced_market: Market):
        """No signal when confidence is below minimum."""
        calc = EVCalculator(config)
        estimates = [
            ModelEstimate(
                market_condition_id=mispriced_market.condition_id,
                token_id="yes_002",
                outcome="Yes",
                probability=0.55,
                confidence=0.30,  # Below min_confidence=0.5
                model_name="test",
            )
        ]
        signals = calc.compute_signals(mispriced_market, estimates)
        assert len(signals) == 0

    def test_size_capped_at_per_trade_max(self, config: BotConfig, mispriced_market: Market):
        """Recommended size never exceeds per_trade_max_usd."""
        calc = EVCalculator(config)
        estimates = [
            ModelEstimate(
                market_condition_id=mispriced_market.condition_id,
                token_id="yes_002",
                outcome="Yes",
                probability=0.90,  # Huge edge → large Kelly fraction
                confidence=0.95,
                model_name="test",
            )
        ]
        signals = calc.compute_signals(mispriced_market, estimates)
        if signals:
            assert signals[0].recommended_size_usd <= config.risk.per_trade_max_usd

    def test_signals_sorted_by_ev(self, config: BotConfig):
        """Signals are returned sorted by absolute EV descending."""
        market = Market(
            condition_id="multi_001",
            question="Multi-signal test",
            tokens=[
                Token(token_id="tok_a", outcome="A", price=0.30, book_depth_usd=20000),
                Token(token_id="tok_b", outcome="B", price=0.40, book_depth_usd=20000),
            ],
            liquidity_usd=40000,
            volume_usd=80000,
        )
        calc = EVCalculator(config)
        estimates = [
            ModelEstimate(
                market_condition_id="multi_001",
                token_id="tok_a",
                outcome="A",
                probability=0.55,  # +25% edge
                confidence=0.80,
                model_name="test",
            ),
            ModelEstimate(
                market_condition_id="multi_001",
                token_id="tok_b",
                outcome="B",
                probability=0.65,  # +25% edge but smaller because price is higher
                confidence=0.80,
                model_name="test",
            ),
        ]
        signals = calc.compute_signals(market, estimates)
        if len(signals) >= 2:
            ev0 = abs(signals[0].metadata.get("ev_after_fees", 0))
            ev1 = abs(signals[1].metadata.get("ev_after_fees", 0))
            assert ev0 >= ev1

    def test_skips_illiquid_market(self, config: BotConfig):
        """Signals are not generated for markets below liquidity threshold."""
        illiquid_market = Market(
            condition_id="illiquid_001",
            question="Illiquid test",
            tokens=[Token(token_id="tok_il", outcome="Yes", price=0.30, book_depth_usd=100)],
            liquidity_usd=100,  # Way below 500 threshold
            volume_usd=200,
        )
        calc = EVCalculator(config)
        estimates = [
            ModelEstimate(
                market_condition_id="illiquid_001",
                token_id="tok_il",
                outcome="Yes",
                probability=0.70,
                confidence=0.90,
                model_name="test",
            )
        ]
        signals = calc.compute_signals(illiquid_market, estimates)
        assert len(signals) == 0


# -------------------------------------------------------- ArbitrageDetector

class TestArbitrageDetector:
    def test_detects_complement_arb(
        self, config: BotConfig, arb_market: Market, arb_order_books: dict[str, OrderBook]
    ):
        """Detects complement arb when YES_ask + NO_ask < 1.0."""
        detector = ArbitrageDetector(config)
        opp = detector.detect_complement_arb(arb_market, arb_order_books)
        assert opp is not None
        assert opp.arb_type == "complement"
        assert opp.net_profit_usd > 0
        assert len(opp.legs) == 2

    def test_no_arb_when_sum_exceeds_one(
        self, config: BotConfig, binary_market: Market, order_book_pair: dict[str, OrderBook]
    ):
        """No complement arb when YES_ask + NO_ask > 1.0."""
        # binary_market has 0.46 + 0.56 = 1.02 > 1.0
        detector = ArbitrageDetector(config)
        opp = detector.detect_complement_arb(binary_market, order_book_pair)
        assert opp is None

    def test_arb_scan_returns_signals(
        self, config: BotConfig, arb_market: Market, arb_order_books: dict[str, OrderBook]
    ):
        """scan() returns at least one signal for the arb market."""
        detector = ArbitrageDetector(config)
        signals = detector.scan([arb_market], arb_order_books)
        assert len(signals) >= 1
        assert signals[0].signal_type == SignalType.ARBITRAGE

    def test_mee_arb_skips_binary(self, config: BotConfig, arb_market: Market, arb_order_books: dict):
        """MEE arb skips binary markets (they use complement_arb)."""
        detector = ArbitrageDetector(config)
        opp = detector.detect_mee_arb(arb_market, arb_order_books)
        assert opp is None  # Binary market, skipped by MEE
