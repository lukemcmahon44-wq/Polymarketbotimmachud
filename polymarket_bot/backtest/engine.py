"""Backtest replay engine — processes synthetic or historical market snapshots."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import structlog

from polymarket_bot.backtest.data_generator import SyntheticDataGenerator
from polymarket_bot.backtest.metrics import compute_metrics, monte_carlo_stress_test
from polymarket_bot.config import BotConfig
from polymarket_bot.execution.paper_executor import PaperExecutor
from polymarket_bot.execution.engine import ExecutionEngine
from polymarket_bot.models.ensemble import EnsembleModel
from polymarket_bot.risk.manager import RiskManager
from polymarket_bot.signals.arbitrage import ArbitrageDetector
from polymarket_bot.signals.ev_calculator import EVCalculator
from polymarket_bot.types import (
    BacktestResult, Market, OrderBook, OrderStatus, Position, Side, Token,
)

logger = structlog.get_logger()


class BacktestEngine:
    """Replays market scenarios through the full trading pipeline.

    Simulates:
    - Price evolution over time steps
    - Signal generation at each step
    - Risk-checked order execution
    - Position tracking and P&L
    - Slippage, latency, and partial fills
    """

    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self.model = EnsembleModel()
        self.ev_calc = EVCalculator(config)
        self.arb_detector = ArbitrageDetector(config)
        self.risk_mgr = RiskManager(config)
        self.paper_executor = PaperExecutor(
            base_slippage_bps=config.backtest.default_slippage_bps,
        )
        self.exec_engine = ExecutionEngine(self.paper_executor, config)

    async def run(
        self,
        scenario: dict[str, Any] | None = None,
        initial_capital: float = 10_000.0,
    ) -> BacktestResult:
        """Run a complete backtest.

        Args:
            scenario: Output from SyntheticDataGenerator.generate_scenario().
                      If None, generates a default scenario.
            initial_capital: Starting capital in USD.
        """
        if scenario is None:
            gen = SyntheticDataGenerator()
            scenario = gen.generate_scenario(num_markets=20, time_steps=100)

        markets: list[Market] = scenario["markets"]
        price_series: dict[str, dict[str, list[float]]] = scenario["price_series"]
        order_books_by_step: dict[int, dict[str, OrderBook]] = scenario["order_books"]
        resolutions: dict[str, bool | None] = scenario["resolutions"]
        time_steps: int = scenario["time_steps"]

        equity = initial_capital
        equity_curve: list[float] = [equity]
        self.risk_mgr._current_equity = equity
        self.risk_mgr._peak_equity = equity

        logger.info("backtest.starting", markets=len(markets), steps=time_steps)

        for step in range(time_steps):
            # Update market prices at this step
            step_markets = self._update_prices(markets, price_series, step)
            step_books = order_books_by_step.get(step, {})

            # Update existing positions
            for pos in list(self.risk_mgr.positions.values()):
                token_prices = price_series.get(pos.market_id, {})
                token_series = token_prices.get(pos.token_id, [])
                if step < len(token_series):
                    pos.current_price = token_series[step]
                    if pos.side == Side.BUY:
                        pos.unrealized_pnl = (pos.current_price - pos.entry_price) * pos.size_shares
                    else:
                        pos.unrealized_pnl = (pos.entry_price - pos.current_price) * pos.size_shares

            # Check stops (Layer 3)
            exits = self.risk_mgr.check_stops()
            for pos in exits:
                pnl = pos.unrealized_pnl
                equity += pnl
                self.risk_mgr.record_trade_result(pnl)
                self.risk_mgr.remove_position(pos.position_id)

            # Check circuit breakers (Layer 5)
            if self.risk_mgr.check_circuit_breakers():
                logger.warning("backtest.circuit_breaker_halt", step=step)
                equity_curve.append(equity)
                continue

            # Generate model estimates
            estimates = await self.model.batch_estimate(step_markets)

            # Generate EV signals
            signals = self.ev_calc.generate_signals(step_markets, estimates, step_books)

            # Also check arbitrage
            arb_signals = self.arb_detector.detect_all(step_markets, step_books)
            signals.extend(arb_signals)

            # Process top signals through risk manager
            for signal in signals[:5]:  # Max 5 trades per step
                decision = self.risk_mgr.evaluate_signal(signal)
                if not decision.allowed:
                    continue

                # Execute
                order = await self.exec_engine.execute_signal(signal, decision.adjusted_size_usd)

                if order.status == OrderStatus.FILLED:
                    # Create position
                    pos = Position(
                        market_id=order.market_id,
                        token_id=order.token_id,
                        side=order.side,
                        entry_price=order.fill_price,
                        current_price=order.fill_price,
                        size_usd=order.size_usd,
                        size_shares=order.filled_size,
                        category=signal.metadata.get("category", ""),
                    )
                    self.risk_mgr.add_position(pos)

                    # Record expected P&L for metrics
                    order.metadata["expected_pnl"] = signal.ev * order.size_usd

            # Resolve markets at final step
            if step == time_steps - 1:
                for pos in list(self.risk_mgr.positions.values()):
                    resolution = resolutions.get(pos.market_id)
                    if resolution is not None:
                        if pos.side == Side.BUY:
                            final_price = 1.0 if resolution else 0.0
                        else:
                            final_price = 0.0 if resolution else 1.0
                        pnl = (final_price - pos.entry_price) * pos.size_shares
                        equity += pnl
                        self.risk_mgr.record_trade_result(pnl)

                        # Annotate the original order
                        for order in self.exec_engine.orders.values():
                            if order.token_id == pos.token_id:
                                order.metadata["realized_pnl"] = pnl
                                break

                        self.risk_mgr.remove_position(pos.position_id)

            equity_curve.append(equity)

        # Compute final metrics
        all_orders = list(self.exec_engine.orders.values())
        result = compute_metrics(all_orders, equity_curve, initial_capital)

        # Monte Carlo stress test
        mc_results = monte_carlo_stress_test(all_orders, initial_capital, self.config.backtest.monte_carlo_runs)
        result.metadata["monte_carlo"] = mc_results

        logger.info(
            "backtest.complete",
            total_trades=result.total_trades,
            total_pnl=result.total_pnl,
            sharpe=result.sharpe_ratio,
            max_drawdown=result.max_drawdown,
            win_rate=result.win_rate,
        )
        return result

    def _update_prices(
        self,
        markets: list[Market],
        price_series: dict[str, dict[str, list[float]]],
        step: int,
    ) -> list[Market]:
        """Update market token prices for the current step."""
        for market in markets:
            series = price_series.get(market.market_id, {})
            for token in market.tokens:
                token_prices = series.get(token.token_id, [])
                if step < len(token_prices):
                    token.price = token_prices[step]
        return markets
