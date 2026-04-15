"""Backtest replay engine — replays market snapshots through the full signal pipeline."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from polymarket_bot.backtest.data_generator import MarketSnapshot, SyntheticMarketGenerator
from polymarket_bot.backtest.metrics import BacktestMetrics
from polymarket_bot.config.settings import BotConfig
from polymarket_bot.execution.order_manager import OrderManager
from polymarket_bot.execution.paper_executor import PaperExecutor
from polymarket_bot.models.base import ModelEstimate, ProbabilityModel
from polymarket_bot.models.ensemble import EnsembleProbabilityModel
from polymarket_bot.risk.risk_manager import RiskAction, RiskManager
from polymarket_bot.signals.arbitrage import ArbitrageDetector
from polymarket_bot.signals.ev_calculator import EVCalculator
from polymarket_bot.types import BacktestResult, Market, OrderStatus, Signal, TradeLog


class _OracleModel(ProbabilityModel):
    """Backtest-only oracle model that uses the synthetic true probability.

    Uses the snapshot's ground-truth probability plus a configurable noise level
    to simulate a realistic (imperfect) information advantage.
    """

    def __init__(self, true_prob: float, noise: float = 0.05) -> None:
        import random
        self._true_prob = true_prob
        self._noise = noise
        self._rng = random.Random()

    @property
    def name(self) -> str:
        return "backtest_oracle"

    async def estimate(self, market: Market) -> list[ModelEstimate]:
        estimates = []
        for token in market.tokens:
            if token.outcome == "Yes":
                prob = max(0.05, min(0.95,
                    self._true_prob + self._rng.gauss(0, self._noise)))
            else:
                prob = max(0.05, min(0.95,
                    (1 - self._true_prob) + self._rng.gauss(0, self._noise)))
            estimates.append(ModelEstimate(
                market_condition_id=market.condition_id,
                token_id=token.token_id,
                outcome=token.outcome,
                probability=prob,
                confidence=0.75,
                model_name=self.name,
            ))
        return estimates

logger = structlog.get_logger(__name__)


class BacktestEngine:
    """Replays synthetic or historical market data through the full bot pipeline.

    Steps per time step:
    1. Feed market snapshot into signal pipeline
    2. Run risk check on each signal
    3. Simulate paper execution
    4. Update positions and P&L
    5. Record trade logs
    """

    def __init__(self, config: BotConfig, model: ProbabilityModel | None = None) -> None:
        self._config = config
        self._om = OrderManager(audit_log_path="logs/backtest_audit.jsonl")
        self._executor = PaperExecutor(config, self._om)
        self._risk = RiskManager(config, self._om)
        self._ev_calc = EVCalculator(config)
        self._arb = ArbitrageDetector(config)
        self._model: ProbabilityModel | None = model  # None → use oracle per snapshot

    async def run(
        self,
        n_markets: int = 20,
        n_steps: int = 500,
        step_seconds: int = 60,
        output_path: str = "logs/backtest_results.json",
    ) -> BacktestResult:
        """Run a full backtest and return results."""
        generator = SyntheticMarketGenerator(
            n_markets=n_markets,
            n_steps=n_steps,
            step_seconds=step_seconds,
        )

        pnl_series: list[float] = []
        equity_curve: list[float] = [self._config.risk.global_exposure_usd]
        trade_logs: list[TradeLog] = []
        total_signals = 0
        total_executed = 0
        total_blocked = 0

        logger.info(
            "backtest_starting",
            n_markets=n_markets,
            n_steps=n_steps,
            equity_start=self._config.risk.global_exposure_usd,
        )

        all_snapshots = list(generator.iter_steps())

        for step_idx, (ts, snapshots) in enumerate(all_snapshots):
            step_signals: list[Signal] = []

            for snap in snapshots:
                market = snap.market
                order_books = snap.order_books

                # Update token prices from snapshot
                for token in market.tokens:
                    if token.token_id in order_books:
                        token.price = order_books[token.token_id].mid_price

                # Use oracle model per snapshot (knows true probability + noise)
                model = self._model or _OracleModel(snap.true_probability, noise=0.04)
                estimates = await model.estimate(market)
                ev_signals = self._ev_calc.compute_signals(market, estimates, order_books)
                arb_signals = self._arb.scan([market], order_books)
                step_signals.extend(ev_signals)
                step_signals.extend(arb_signals)

                # Update existing position P&L
                for pos in self._om.get_open_positions():
                    if pos.token_id in order_books:
                        current_price = order_books[pos.token_id].mid_price
                        pos.update_pnl(current_price)

                # Close positions approaching resolution (time decay)
                await self._maybe_close_positions(snap, ts)

            total_signals += len(step_signals)

            # Execute top signals (by EV, capped per step)
            for signal in step_signals[:3]:  # Max 3 trades per time step
                decision = self._risk.evaluate(signal)

                if decision.action == RiskAction.CIRCUIT_BREAK:
                    logger.warning("backtest_circuit_break", step=step_idx)
                    break

                log = TradeLog(
                    signal=signal,
                    risk_checks=decision.checks,
                    risk_adjustments=decision.adjustments,
                )

                if decision.is_approved and decision.approved_size_usd > 0:
                    order = await self._executor.execute(signal, decision.approved_size_usd)
                    log.order = order
                    log.decision = "execute"
                    total_executed += 1

                    if order.status == OrderStatus.FILLED:
                        pnl_est = order.filled_size * (
                            signal.model_probability - order.filled_price
                        )
                        pnl_series.append(pnl_est)
                        self._risk.record_fill(order)
                        self._risk.record_pnl(pnl_est)
                else:
                    log.decision = "block"
                    log.reason = decision.reason
                    total_blocked += 1

                trade_logs.append(log)

            # Update equity curve
            unrealized = self._om.total_unrealized_pnl()
            realized = sum(pnl_series)
            current_equity = self._config.risk.global_exposure_usd + realized + unrealized
            equity_curve.append(current_equity)

            if step_idx % 50 == 0:
                logger.info(
                    "backtest_progress",
                    step=step_idx,
                    total=n_steps,
                    equity=round(current_equity, 2),
                    trades=total_executed,
                    pct=f"{step_idx/n_steps:.0%}",
                )

        # Compute metrics
        metrics = BacktestMetrics.compute(pnl_series, equity_curve)
        mc_metrics = BacktestMetrics.monte_carlo(pnl_series) if len(pnl_series) >= 20 else {}

        result = BacktestResult(
            total_trades=total_executed,
            winning_trades=int(metrics.get("winning_trades", 0)),
            losing_trades=int(metrics.get("losing_trades", 0)),
            total_pnl=metrics.get("total_pnl", 0),
            max_drawdown=metrics.get("max_drawdown", 0),
            sharpe_ratio=metrics.get("sharpe_ratio", 0),
            win_rate=metrics.get("win_rate", 0),
            avg_win=metrics.get("avg_win", 0),
            avg_loss=metrics.get("avg_loss", 0),
            equity_curve=equity_curve,
            trades=trade_logs,
            metadata={
                **metrics,
                **mc_metrics,
                "total_signals": total_signals,
                "blocked_trades": total_blocked,
                "n_markets": n_markets,
                "n_steps": n_steps,
            },
        )

        # Save results
        self._save_results(result, output_path)
        BacktestMetrics.print_report(metrics, mc_metrics if mc_metrics else None)

        logger.info(
            "backtest_complete",
            total_trades=total_executed,
            total_pnl=round(result.total_pnl, 2),
            sharpe=round(result.sharpe_ratio, 3),
            max_dd=round(result.max_drawdown, 3),
        )

        return result

    async def _maybe_close_positions(self, snap: MarketSnapshot, ts: datetime) -> None:
        """Close positions in markets nearing resolution."""
        if snap.market.end_date is None:
            return

        end = snap.market.end_date
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        now = ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts
        hours_left = (end - now).total_seconds() / 3600

        if hours_left < 1:
            for pos in self._om.get_open_positions():
                if pos.market_condition_id == snap.market.condition_id:
                    # Resolve at true probability
                    close_price = snap.true_probability
                    pnl = self._om.close_position(
                        pos.token_id, pos.side, close_price
                    )
                    self._risk.record_pnl(pnl)

    def _save_results(self, result: BacktestResult, path: str) -> None:
        """Persist backtest results to JSON."""
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "summary": {
                "total_trades": result.total_trades,
                "win_rate": round(result.win_rate, 4),
                "total_pnl": round(result.total_pnl, 4),
                "max_drawdown": round(result.max_drawdown, 4),
                "sharpe_ratio": round(result.sharpe_ratio, 4),
                "avg_win": round(result.avg_win, 4),
                "avg_loss": round(result.avg_loss, 4),
            },
            "metadata": {
                k: round(v, 6) if isinstance(v, float) else v
                for k, v in result.metadata.items()
            },
            "equity_curve": [round(e, 2) for e in result.equity_curve[-100:]],  # Last 100 points
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        logger.info("backtest_results_saved", path=path)
