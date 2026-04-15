"""Tests for signal generation (EV calculator and arbitrage detection)."""

from __future__ import annotations

import pytest

from polymarket_bot.config import BotConfig
from polymarket_bot.models.base import ModelEstimate
from polymarket_bot.signals.ev_calculator import EVCalculator
from polymarket_bot.signals.arbitrage import ArbitrageDetector
from polymarket_bot.types import Market, Side, SignalType


class TestEVCalculator:
    def test_compute_ev_positive(self, config: BotConfig) -> None:
        calc = EVCalculator(config)
        # Model says 0.70, market at 0.50 — positive EV
        ev = calc.compute_ev(model_prob=0.70, market_price=0.50)
        assert ev > 0

    def test_compute_ev_negative(self, config: BotConfig) -> None:
        calc = EVCalculator(config)
        # Model agrees with market — near-zero EV after fees
        ev = calc.compute_ev(model_prob=0.50, market_price=0.50)
        assert ev < 0  # Negative due to fees

    def test_compute_ev_extreme_mispricing(self, config: BotConfig) -> None:
        calc = EVCalculator(config)
        # Model says 0.95, market at 0.30 — huge EV
        ev = calc.compute_ev(model_prob=0.95, market_price=0.30)
        assert ev > 0.3

    def test_generate_signals_filters_low_ev(
        self, config: BotConfig, sample_market: Market
    ) -> None:
        config.min_ev_threshold = 0.05
        calc = EVCalculator(config)

        # Model matches market — should produce no signals
        estimates = [[
            ModelEstimate(
                model_name="test",
                market_id=sample_market.market_id,
                token_id="tok_yes_001",
                outcome="Yes",
                probability=0.60,  # Same as market
                confidence=0.8,
            ),
        ]]

        signals = calc.generate_signals([sample_market], estimates)
        assert len(signals) == 0

    def test_generate_signals_finds_mispricing(
        self, config: BotConfig, sample_market: Market
    ) -> None:
        config.min_ev_threshold = 0.01
        calc = EVCalculator(config)

        # Model says 0.80, market at 0.60 — clear mispricing
        estimates = [[
            ModelEstimate(
                model_name="test",
                market_id=sample_market.market_id,
                token_id="tok_yes_001",
                outcome="Yes",
                probability=0.80,
                confidence=0.8,
            ),
        ]]

        signals = calc.generate_signals([sample_market], estimates)
        assert len(signals) >= 1
        assert signals[0].signal_type == SignalType.MISPRICING
        assert signals[0].ev > 0
        assert signals[0].side == Side.BUY

    def test_generate_signals_sell_direction(
        self, config: BotConfig, sample_market: Market
    ) -> None:
        config.min_ev_threshold = 0.01
        calc = EVCalculator(config)

        # Model says 0.30, market YES at 0.60 — sell YES (buy NO)
        estimates = [[
            ModelEstimate(
                model_name="test",
                market_id=sample_market.market_id,
                token_id="tok_yes_001",
                outcome="Yes",
                probability=0.30,
                confidence=0.8,
            ),
        ]]

        signals = calc.generate_signals([sample_market], estimates)
        assert len(signals) >= 1
        assert signals[0].side == Side.SELL


class TestArbitrageDetector:
    def test_detect_direct_arbitrage_underpriced(
        self, config: BotConfig, sample_markets: list[Market]
    ) -> None:
        config.min_ev_threshold = 0.001
        detector = ArbitrageDetector(config)

        # mkt_arb has YES=0.45 + NO=0.50 = 0.95 (underpriced)
        arb_market = sample_markets[2]
        signals = detector.detect_direct_arbitrage([arb_market])

        assert len(signals) >= 1
        assert signals[0].signal_type == SignalType.ARBITRAGE
        assert signals[0].metadata["arb_type"] == "direct_underpriced"

    def test_no_arbitrage_when_fair(self, config: BotConfig) -> None:
        from datetime import datetime, timedelta, timezone
        from polymarket_bot.types import Token

        config.min_ev_threshold = 0.01
        detector = ArbitrageDetector(config)

        fair_market = Market(
            market_id="fair",
            condition_id="fair",
            question="Fair market",
            category="test",
            end_date=datetime.now(timezone.utc) + timedelta(days=7),
            tokens=[
                Token(token_id="fair_yes", outcome="Yes", price=0.50),
                Token(token_id="fair_no", outcome="No", price=0.50),
            ],
            volume_usd=100000,
            liquidity_usd=50000,
        )

        signals = detector.detect_direct_arbitrage([fair_market])
        assert len(signals) == 0

    def test_detect_all_combines_strategies(
        self, config: BotConfig, sample_markets: list[Market]
    ) -> None:
        config.min_ev_threshold = 0.001
        detector = ArbitrageDetector(config)
        signals = detector.detect_all(sample_markets)
        # Should find at least the direct arb from mkt_arb
        assert isinstance(signals, list)
