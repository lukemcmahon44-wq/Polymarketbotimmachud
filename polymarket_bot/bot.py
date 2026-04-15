"""Main trading bot orchestrator — wires all modules together."""

from __future__ import annotations

import asyncio
import os
import signal
from datetime import datetime, timezone
from typing import Any

import structlog

from polymarket_bot.backtest.engine import BacktestEngine
from polymarket_bot.config.settings import BotConfig
from polymarket_bot.execution.live_executor import LiveExecutor, LiveTradingGateError
from polymarket_bot.execution.order_manager import OrderManager
from polymarket_bot.execution.paper_executor import PaperExecutor
from polymarket_bot.models.ensemble import EnsembleProbabilityModel
from polymarket_bot.monitoring.alerting import Alerter
from polymarket_bot.risk.risk_manager import RiskAction, RiskManager
from polymarket_bot.scanner.gamma_client import GammaClient
from polymarket_bot.scanner.market_feed import MarketFeed
from polymarket_bot.scanner.polymarket_client import PolymarketCLOBClient
from polymarket_bot.signals.arbitrage import ArbitrageDetector
from polymarket_bot.signals.ev_calculator import EVCalculator
from polymarket_bot.wallet_tracing.copy_trade_detector import CopyTradeDetector

logger = structlog.get_logger(__name__)


class TradingBot:
    """Autonomous Polymarket trading bot.

    Wires together scanning → modelling → signal generation →
    risk management → execution → monitoring in a continuous loop.

    Paper mode (default): All executions are simulated.
    Live mode: Requires explicit arming via CLI + passphrase.
    """

    def __init__(self, config: BotConfig) -> None:
        self._config = config
        self._running = False

        # Core components
        self._om = OrderManager()
        self._clob = PolymarketCLOBClient(config)
        self._gamma = GammaClient(config)
        self._feed = MarketFeed(config, self._gamma, self._clob)
        self._model = EnsembleProbabilityModel()
        self._ev_calc = EVCalculator(config)
        self._arb = ArbitrageDetector(config)
        self._risk = RiskManager(config, self._om)
        self._alerter = Alerter(config)
        self._copy_trade = CopyTradeDetector(config, self._om)

        # Executors — always create paper executor; live is optional
        self._paper_exec = PaperExecutor(config, self._om)
        self._live_exec: LiveExecutor | None = None

        if config.enable_live_trading:
            self._live_exec = LiveExecutor(config, self._om, self._clob)

    async def run(self) -> None:
        """Main event loop."""
        self._running = True
        self._setup_signal_handlers()

        logger.info(
            "bot_starting",
            mode="PAPER" if self._config.paper_mode else "LIVE",
            ev_threshold=self._config.ev_threshold,
            global_cap=self._config.risk.global_exposure_usd,
        )

        # Start subsystems
        await self._feed.start()
        await self._copy_trade.start()

        # Register market update callback
        self._feed.on_update(self._on_market_update)

        try:
            while self._running:
                await asyncio.sleep(1)
                await self._maintenance_loop()
        except asyncio.CancelledError:
            pass
        finally:
            await self._shutdown()

    async def _on_market_update(
        self, condition_id: str, market: Any, order_book: Any
    ) -> None:
        """Called when market data is updated. Run signal pipeline."""
        if not self._running:
            return
        if self._risk.circuit_breaker_tripped:
            return

        try:
            estimates = await self._model.estimate(market)
            order_books = self._feed.order_books

            ev_signals = self._ev_calc.compute_signals(market, estimates, order_books)
            arb_signals = self._arb.scan([market], order_books)
            all_signals = ev_signals + arb_signals

            for signal in all_signals[:2]:  # Process top 2 signals per market update
                await self._process_signal(signal)

        except Exception as e:
            logger.error("market_update_error", condition_id=condition_id[:12], error=str(e))

    async def _process_signal(self, signal: Any) -> None:
        """Run risk checks and execute a signal."""
        decision = self._risk.evaluate(signal)

        if decision.action == RiskAction.CIRCUIT_BREAK:
            await self._alerter.circuit_breaker_alert(self._risk.circuit_breaker_reason)
            return

        if not decision.is_approved:
            logger.debug(
                "signal_blocked",
                signal_id=signal.signal_id,
                reason=decision.reason,
            )
            return

        try:
            if self._config.paper_mode:
                order = await self._paper_exec.execute(signal, decision.approved_size_usd)
            elif self._live_exec and self._live_exec.is_armed:
                order = await self._live_exec.execute(signal, decision.approved_size_usd)
                self._copy_trade.record_our_order(order)
            else:
                logger.warning("live_exec_not_armed", signal_id=signal.signal_id)
                return

            self._risk.record_fill(order)
            await self._alerter.large_fill_alert(order.order_id, order.size_usd)

        except LiveTradingGateError as e:
            logger.error("live_gate_error", error=str(e))
        except Exception as e:
            logger.error("execution_error", signal_id=signal.signal_id, error=str(e))

    async def _maintenance_loop(self) -> None:
        """Periodic maintenance: cancel stale orders, check copy trades."""
        cancelled = await self._paper_exec.cancel_stale_orders()
        if cancelled:
            logger.info("stale_orders_cancelled", count=len(cancelled))

        # Check for copy trade alerts
        alerts = self._copy_trade.get_alerts()
        for alert in alerts[-5:]:  # Process last 5 alerts
            if alert.similarity_score >= self._config.copy_trade.similarity_threshold:
                await self._alerter.copy_trade_alert(
                    alert.suspect_wallet,
                    alert.similarity_score,
                    alert.recommended_action.value,
                )

    async def _shutdown(self) -> None:
        """Graceful shutdown."""
        logger.info("bot_shutting_down")
        await self._feed.stop()
        await self._copy_trade.stop()
        await self._clob.close()
        await self._gamma.close()
        logger.info("bot_stopped")

    def _setup_signal_handlers(self) -> None:
        """Register OS signal handlers for graceful shutdown."""
        loop = asyncio.get_event_loop()

        def handle_stop() -> None:
            logger.warning("shutdown_signal_received")
            self._running = False

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, handle_stop)
            except (NotImplementedError, RuntimeError):
                pass  # Windows / non-main-thread
