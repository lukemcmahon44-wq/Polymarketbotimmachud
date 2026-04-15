"""Performance metrics calculation for backtests."""

from __future__ import annotations

import math
from typing import Any

from polymarket_bot.types import BacktestResult, Order, OrderStatus


def compute_metrics(
    trades: list[Order],
    equity_curve: list[float],
    initial_capital: float = 10_000.0,
) -> BacktestResult:
    """Compute comprehensive backtest performance metrics.

    Metrics:
    - Total P&L, win rate, average trade P&L
    - Sharpe ratio (annualized, assuming 365 trading days)
    - Maximum drawdown
    - Realized vs expected P&L ratio
    """
    filled = [t for t in trades if t.status == OrderStatus.FILLED]

    if not filled:
        return BacktestResult(trades=trades, equity_curve=equity_curve)

    # Compute per-trade P&L (simplified: fill_price vs market resolution)
    pnls: list[float] = []
    expected_pnls: list[float] = []

    for trade in filled:
        # Simulated P&L from metadata or price movement
        pnl = trade.metadata.get("realized_pnl", 0.0)
        expected = trade.metadata.get("expected_pnl", 0.0)
        pnls.append(pnl)
        expected_pnls.append(expected)

    total_pnl = sum(pnls)
    winners = [p for p in pnls if p > 0]
    losers = [p for p in pnls if p <= 0]

    # Sharpe ratio
    if len(pnls) > 1:
        mean_return = sum(pnls) / len(pnls)
        variance = sum((p - mean_return) ** 2 for p in pnls) / (len(pnls) - 1)
        std_return = math.sqrt(variance) if variance > 0 else 1e-10
        sharpe = (mean_return / std_return) * math.sqrt(365)
    else:
        sharpe = 0.0

    # Max drawdown from equity curve
    max_drawdown = compute_max_drawdown(equity_curve) if equity_curve else 0.0

    # Win rate
    win_rate = len(winners) / len(filled) if filled else 0.0

    # Realized vs expected
    total_expected = sum(expected_pnls)
    rv_ratio = total_pnl / total_expected if total_expected != 0 else 0.0

    return BacktestResult(
        total_trades=len(filled),
        winning_trades=len(winners),
        losing_trades=len(losers),
        total_pnl=round(total_pnl, 2),
        max_drawdown=round(max_drawdown, 4),
        sharpe_ratio=round(sharpe, 3),
        win_rate=round(win_rate, 4),
        avg_trade_pnl=round(total_pnl / len(filled), 2) if filled else 0,
        realized_vs_expected=round(rv_ratio, 3),
        trades=trades,
        equity_curve=equity_curve,
    )


def compute_max_drawdown(equity_curve: list[float]) -> float:
    """Compute maximum drawdown from an equity curve."""
    if not equity_curve or len(equity_curve) < 2:
        return 0.0

    peak = equity_curve[0]
    max_dd = 0.0

    for val in equity_curve:
        peak = max(peak, val)
        if peak > 0:
            dd = (peak - val) / peak
            max_dd = max(max_dd, dd)

    return max_dd


def monte_carlo_stress_test(
    trades: list[Order],
    initial_capital: float = 10_000.0,
    num_simulations: int = 1000,
    seed: int = 42,
) -> dict[str, Any]:
    """Monte Carlo stress test by reshuffling trade order.

    Simulates different orderings of the same trades to estimate
    the distribution of outcomes (drawdown, final P&L, etc.).
    """
    import random
    rng = random.Random(seed)

    pnls = [t.metadata.get("realized_pnl", 0.0) for t in trades if t.status == OrderStatus.FILLED]

    if not pnls:
        return {"error": "No filled trades for Monte Carlo"}

    final_pnls: list[float] = []
    max_drawdowns: list[float] = []
    ruin_count = 0

    for _ in range(num_simulations):
        shuffled = pnls.copy()
        rng.shuffle(shuffled)

        # Build equity curve
        equity = [initial_capital]
        for pnl in shuffled:
            equity.append(equity[-1] + pnl)

        final_pnls.append(equity[-1] - initial_capital)
        max_drawdowns.append(compute_max_drawdown(equity))

        if equity[-1] <= 0:
            ruin_count += 1

    return {
        "num_simulations": num_simulations,
        "mean_pnl": round(sum(final_pnls) / len(final_pnls), 2),
        "median_pnl": round(sorted(final_pnls)[len(final_pnls) // 2], 2),
        "pnl_5th_percentile": round(sorted(final_pnls)[int(0.05 * len(final_pnls))], 2),
        "pnl_95th_percentile": round(sorted(final_pnls)[int(0.95 * len(final_pnls))], 2),
        "mean_max_drawdown": round(sum(max_drawdowns) / len(max_drawdowns), 4),
        "worst_max_drawdown": round(max(max_drawdowns), 4),
        "probability_of_ruin": round(ruin_count / num_simulations, 4),
    }
