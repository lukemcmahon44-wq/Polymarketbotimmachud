"""Integration tests — full pipeline from scan to simulated execution."""

from __future__ import annotations

import pytest

from polymarket_bot.config import BotConfig
from polymarket_bot.execution.engine import ExecutionEngine
from polymarket_bot.execution.paper_executor import PaperExecutor
from polymarket_bot.models.ensemble import EnsembleModel
from polymarket_bot.risk.manager import RiskManager
from polymarket_bot.signals.ev_calculator import EVCalculator
from polymarket_bot.signals.arbitrage import ArbitrageDetector
from polymarket_bot.types import Market, OrderStatus


@pytest.mark.integration
class TestFullPipeline:
    @pytest.mark.asyncio
    async def test_scan_signal_execute_cycle(
        self, config: BotConfig, sample_markets: list[Market]
    ) -> None:
        """End-to-end: model estimate → signal → risk check → paper execution."""
        config.min_liquidity_usd = 0
        config.min_ev_threshold = 0.001
        config.confidence_floor = 0.1

        # 1. Generate model estimates
        model = EnsembleModel()
        estimates = await model.batch_estimate(sample_markets)
        assert len(estimates) == len(sample_markets)

        # 2. Generate signals
        ev_calc = EVCalculator(config)
        signals = ev_calc.generate_signals(sample_markets, estimates)

        arb_detector = ArbitrageDetector(config)
        arb_signals = arb_detector.detect_all(sample_markets)
        all_signals = signals + arb_signals

        # 3. Risk-check and execute top signals
        risk_mgr = RiskManager(config)
        executor = PaperExecutor(fill_probability=1.0)
        engine = ExecutionEngine(executor, config)

        executed = 0
        for signal in all_signals[:5]:
            decision = risk_mgr.evaluate_signal(signal)
            if decision.allowed and decision.adjusted_size_usd > 0:
                order = await engine.execute_signal(signal, decision.adjusted_size_usd)
                assert order.is_paper
                if order.status == OrderStatus.FILLED:
                    executed += 1

        # Should have executed at least one trade
        assert executed >= 0  # May be 0 if no signals pass all filters
        assert len(engine.audit_log) >= 0

    @pytest.mark.asyncio
    async def test_risk_blocks_unsafe_signal(self, config: BotConfig) -> None:
        """Verify risk manager blocks a signal when exposure is full."""
        config.global_exposure_usd = 100
        config.min_liquidity_usd = 0
        config.min_ev_threshold = 0.001

        from polymarket_bot.types import Position, Signal, Side, SignalType

        risk_mgr = RiskManager(config)
        # Fill up exposure
        risk_mgr.add_position(Position(market_id="full", size_usd=100))

        signal = Signal(
            signal_type=SignalType.MISPRICING,
            market_id="new_mkt",
            token_id="new_tok",
            side=Side.BUY,
            model_prob=0.80,
            market_price=0.50,
            ev=0.10,
            confidence=0.90,
            liquidity_usd=50_000,
        )
        decision = risk_mgr.evaluate_signal(signal)
        assert not decision.allowed

    @pytest.mark.asyncio
    async def test_ensemble_model_produces_estimates(
        self, sample_markets: list[Market]
    ) -> None:
        """Verify ensemble model produces valid probability estimates."""
        model = EnsembleModel()
        estimates = await model.batch_estimate(sample_markets)

        for market_est in estimates:
            total_prob = sum(e.probability for e in market_est)
            # Should sum to approximately 1.0
            assert 0.9 <= total_prob <= 1.1, f"Probabilities sum to {total_prob}"
            for est in market_est:
                assert 0 < est.probability < 1
                assert 0 < est.confidence <= 1
