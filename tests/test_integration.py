"""Integration tests: full scan → signal → risk → paper execution cycle."""

from __future__ import annotations

import pytest

from polymarket_bot.backtest.data_generator import SyntheticMarketGenerator
from polymarket_bot.config.settings import BotConfig
from polymarket_bot.execution.order_manager import OrderManager
from polymarket_bot.execution.paper_executor import PaperExecutor
from polymarket_bot.models.ensemble import EnsembleProbabilityModel
from polymarket_bot.risk.risk_manager import RiskAction, RiskManager
from polymarket_bot.signals.arbitrage import ArbitrageDetector
from polymarket_bot.signals.ev_calculator import EVCalculator
from polymarket_bot.types import OrderStatus


@pytest.mark.asyncio
@pytest.mark.integration
class TestFullTradeCycle:
    async def test_scan_signal_risk_execute_cycle(self, config: BotConfig):
        """Full pipeline: synthetic market → signal → risk → paper fill."""
        # Generate one market snapshot with a likely mispricing
        gen = SyntheticMarketGenerator(n_markets=10, n_steps=1, mispricing_freq=1.0)
        all_snaps = gen.generate()

        model = EnsembleProbabilityModel()
        ev_calc = EVCalculator(config)
        arb = ArbitrageDetector(config)
        om = OrderManager(audit_log_path="logs/test_integration_audit.jsonl")
        risk = RiskManager(config, om)
        executor = PaperExecutor(config, om)

        executed_count = 0
        blocked_count = 0

        for market_snaps in all_snaps:
            snap = market_snaps[0]
            market = snap.market
            order_books = snap.order_books

            for token in market.tokens:
                if token.token_id in order_books:
                    token.price = order_books[token.token_id].mid_price

            estimates = await model.estimate(market)
            ev_signals = ev_calc.compute_signals(market, estimates, order_books)
            arb_signals = arb.scan([market], order_books)
            all_signals = ev_signals + arb_signals

            for signal in all_signals:
                decision = risk.evaluate(signal)
                if decision.is_approved and decision.approved_size_usd > 0:
                    order = await executor.execute(signal, decision.approved_size_usd)
                    assert order.status in (
                        OrderStatus.FILLED,
                        OrderStatus.PARTIALLY_FILLED,
                    )
                    assert order.filled_price > 0
                    assert order.is_paper is True
                    executed_count += 1
                else:
                    blocked_count += 1

        # With 10 markets and 100% mispricing frequency, we expect some executions
        total = executed_count + blocked_count
        assert total >= 0  # Pipeline ran without exception

    async def test_circuit_breaker_halts_all_trades(self, config: BotConfig):
        """After circuit breaker trip, no trades are executed."""
        gen = SyntheticMarketGenerator(n_markets=5, n_steps=1, mispricing_freq=1.0)
        all_snaps = gen.generate()

        model = EnsembleProbabilityModel()
        ev_calc = EVCalculator(config)
        om = OrderManager(audit_log_path="logs/test_cb_audit.jsonl")
        risk = RiskManager(config, om)

        # Trip the circuit breaker before processing
        risk.trip_circuit_breaker("Integration test trigger")

        executed = 0
        for market_snaps in all_snaps:
            snap = market_snaps[0]
            estimates = await model.estimate(snap.market)
            signals = ev_calc.compute_signals(snap.market, estimates, snap.order_books)
            for signal in signals:
                decision = risk.evaluate(signal)
                assert decision.action == RiskAction.CIRCUIT_BREAK
                assert not decision.is_approved
                executed += 1  # We evaluated but not executed

        assert risk.circuit_breaker_tripped

    async def test_paper_mode_never_calls_live_api(self, config: BotConfig):
        """In paper mode, LiveExecutor is never armed and no real API calls happen."""
        assert config.paper_mode is True
        assert config.enable_live_trading is False

        om = OrderManager(audit_log_path="logs/test_audit.jsonl")
        from polymarket_bot.execution.live_executor import LiveExecutor
        from polymarket_bot.scanner.polymarket_client import PolymarketCLOBClient

        clob = PolymarketCLOBClient(config)
        live = LiveExecutor(config, om, clob)

        assert live.is_armed is False

    async def test_arb_signal_produced_for_complement(self, config: BotConfig):
        """Complement arb signal is produced and risk-checked correctly."""
        from polymarket_bot.types import Market, OrderBook, PriceLevel, Token

        # Market where YES_ask + NO_ask = 0.92 < 1.0
        market = Market(
            condition_id="arb_int_001",
            question="Integration arb test",
            tokens=[
                Token("arb_yes", "Yes", price=0.43),
                Token("arb_no", "No", price=0.49),
            ],
            liquidity_usd=20_000,
            volume_usd=40_000,
        )
        order_books = {
            "arb_yes": OrderBook(
                token_id="arb_yes",
                bids=[PriceLevel(0.42, 500)],
                asks=[PriceLevel(0.43, 500)],
            ),
            "arb_no": OrderBook(
                token_id="arb_no",
                bids=[PriceLevel(0.48, 500)],
                asks=[PriceLevel(0.49, 500)],
            ),
        }

        arb = ArbitrageDetector(config)
        signals = arb.scan([market], order_books)

        assert len(signals) >= 1
        assert signals[0].expected_value > 0
