"""Performance metrics for backtest results."""

from __future__ import annotations

import math
import statistics
from typing import Sequence

import structlog

from polymarket_bot.types import BacktestResult

logger = structlog.get_logger(__name__)


class BacktestMetrics:
    """Compute performance metrics from a list of P&L values and equity curve."""

    @staticmethod
    def compute(
        pnl_series: list[float],
        equity_curve: list[float],
        risk_free_rate_annual: float = 0.05,
    ) -> dict[str, float]:
        """Compute full performance metrics.

        Args:
            pnl_series: P&L for each closed trade.
            equity_curve: Portfolio equity at each time step.
            risk_free_rate_annual: Annual risk-free rate for Sharpe ratio.

        Returns:
            Dict of metric_name → float value.
        """
        if not pnl_series:
            return {"error": -1.0}

        wins = [p for p in pnl_series if p > 0]
        losses = [p for p in pnl_series if p <= 0]

        total_pnl = sum(pnl_series)
        win_rate = len(wins) / len(pnl_series) if pnl_series else 0.0
        avg_win = statistics.mean(wins) if wins else 0.0
        avg_loss = statistics.mean(losses) if losses else 0.0
        profit_factor = abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else float("inf")

        # Sharpe Ratio (annualized, assumes daily steps)
        if len(pnl_series) >= 2:
            try:
                mean_return = statistics.mean(pnl_series)
                std_return = statistics.stdev(pnl_series)
                steps_per_year = 365 * 24 * 60  # if steps are 1-minute
                # Estimate based on number of data points
                annualization = math.sqrt(max(1, len(pnl_series)))
                daily_rf = risk_free_rate_annual / 365
                sharpe = (mean_return - daily_rf) / std_return * annualization if std_return > 0 else 0.0
            except Exception:
                sharpe = 0.0
        else:
            sharpe = 0.0

        # Max drawdown
        max_dd = BacktestMetrics.max_drawdown(equity_curve)

        # Sortino ratio (only downside deviation)
        down_pnl = [p for p in pnl_series if p < 0]
        if down_pnl and len(down_pnl) >= 2:
            try:
                down_std = statistics.stdev(down_pnl)
                mean_r = statistics.mean(pnl_series)
                sortino = mean_r / down_std * math.sqrt(len(pnl_series)) if down_std > 0 else 0.0
            except Exception:
                sortino = 0.0
        else:
            sortino = sharpe  # No downside deviation = use Sharpe

        # Calmar ratio
        calmar = total_pnl / abs(max_dd) if max_dd != 0 else float("inf")

        return {
            "total_trades": float(len(pnl_series)),
            "winning_trades": float(len(wins)),
            "losing_trades": float(len(losses)),
            "total_pnl": total_pnl,
            "win_rate": win_rate,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "profit_factor": profit_factor,
            "sharpe_ratio": sharpe,
            "sortino_ratio": sortino,
            "max_drawdown": max_dd,
            "calmar_ratio": calmar,
        }

    @staticmethod
    def max_drawdown(equity_curve: Sequence[float]) -> float:
        """Compute max drawdown from an equity curve."""
        if len(equity_curve) < 2:
            return 0.0
        peak = equity_curve[0]
        max_dd = 0.0
        for value in equity_curve:
            if value > peak:
                peak = value
            drawdown = (peak - value) / peak if peak > 0 else 0.0
            max_dd = max(max_dd, drawdown)
        return max_dd

    @staticmethod
    def monte_carlo(
        pnl_series: list[float],
        n_simulations: int = 1000,
        n_periods: int | None = None,
        seed: int = 42,
    ) -> dict[str, float]:
        """Run Monte Carlo simulation by resampling trade P&L.

        Returns distribution statistics for total P&L and max drawdown.
        """
        import random

        rng = random.Random(seed)
        n = n_periods or len(pnl_series)

        sim_totals: list[float] = []
        sim_drawdowns: list[float] = []

        for _ in range(n_simulations):
            sample = [rng.choice(pnl_series) for _ in range(n)]
            sim_totals.append(sum(sample))

            # Build equity curve for this simulation
            equity = [10_000.0]
            for pnl in sample:
                equity.append(equity[-1] + pnl)
            sim_drawdowns.append(BacktestMetrics.max_drawdown(equity))

        sim_totals.sort()
        sim_drawdowns.sort()

        def percentile(data: list[float], p: float) -> float:
            idx = int(p / 100 * len(data))
            return data[min(idx, len(data) - 1)]

        return {
            "mc_pnl_mean": statistics.mean(sim_totals),
            "mc_pnl_p5": percentile(sim_totals, 5),
            "mc_pnl_p25": percentile(sim_totals, 25),
            "mc_pnl_p50": percentile(sim_totals, 50),
            "mc_pnl_p75": percentile(sim_totals, 75),
            "mc_pnl_p95": percentile(sim_totals, 95),
            "mc_dd_mean": statistics.mean(sim_drawdowns),
            "mc_dd_p95": percentile(sim_drawdowns, 95),
            "mc_dd_p99": percentile(sim_drawdowns, 99),
        }

    @staticmethod
    def print_report(metrics: dict[str, float], mc_metrics: dict[str, float] | None = None) -> None:
        """Print a formatted performance report."""
        print("\n" + "=" * 60)
        print("  BACKTEST PERFORMANCE REPORT")
        print("=" * 60)
        print(f"  Total Trades       : {int(metrics.get('total_trades', 0))}")
        print(f"  Win Rate           : {metrics.get('win_rate', 0):.1%}")
        print(f"  Total P&L          : ${metrics.get('total_pnl', 0):.2f}")
        print(f"  Avg Win            : ${metrics.get('avg_win', 0):.2f}")
        print(f"  Avg Loss           : ${metrics.get('avg_loss', 0):.2f}")
        print(f"  Profit Factor      : {metrics.get('profit_factor', 0):.2f}")
        print(f"  Sharpe Ratio       : {metrics.get('sharpe_ratio', 0):.3f}")
        print(f"  Sortino Ratio      : {metrics.get('sortino_ratio', 0):.3f}")
        print(f"  Max Drawdown       : {metrics.get('max_drawdown', 0):.1%}")
        print(f"  Calmar Ratio       : {metrics.get('calmar_ratio', 0):.2f}")

        if mc_metrics:
            print("\n  --- Monte Carlo (1000 simulations) ---")
            print(f"  P&L P5  / P50 / P95: "
                  f"${mc_metrics.get('mc_pnl_p5', 0):.0f} / "
                  f"${mc_metrics.get('mc_pnl_p50', 0):.0f} / "
                  f"${mc_metrics.get('mc_pnl_p95', 0):.0f}")
            print(f"  DD  P50 / P95 / P99: "
                  f"{mc_metrics.get('mc_dd_mean', 0):.1%} / "
                  f"{mc_metrics.get('mc_dd_p95', 0):.1%} / "
                  f"{mc_metrics.get('mc_dd_p99', 0):.1%}")
        print("=" * 60 + "\n")
