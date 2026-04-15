#!/usr/bin/env python3
"""Run a complete backtest with detailed reporting and file output.

Usage:
    python run_backtest.py
    python run_backtest.py --markets 50 --steps 200 --capital 25000
    python run_backtest.py --output backtest_results/
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from polymarket_bot.monitoring.logger import setup_logging

setup_logging(level="WARNING", json_output=False)

from polymarket_bot.backtest.data_generator import SyntheticDataGenerator
from polymarket_bot.backtest.engine import BacktestEngine
from polymarket_bot.backtest.metrics import monte_carlo_stress_test
from polymarket_bot.config import load_config


async def run_backtest(
    num_markets: int = 30,
    time_steps: int = 150,
    initial_capital: float = 10_000.0,
    output_dir: str = "backtest_results",
    seed: int = 42,
) -> None:
    cfg = load_config()
    cfg.paper_mode = True
    cfg.min_liquidity_usd = 0
    cfg.min_ev_threshold = 0.003
    cfg.confidence_floor = 0.15
    cfg.backtest.monte_carlo_runs = 500

    print("=" * 70)
    print("  POLYMARKET BOT — BACKTEST REPORT")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 70)
    print()
    print(f"  Markets:          {num_markets}")
    print(f"  Time steps:       {time_steps}")
    print(f"  Initial capital:  ${initial_capital:,.0f}")
    print(f"  Seed:             {seed}")
    print(f"  Kelly fraction:   {cfg.kelly_fraction}")
    print(f"  Slippage (bps):   {cfg.backtest.default_slippage_bps}")
    print(f"  Fee rate:         {cfg.backtest.fee_rate:.1%}")
    print()

    # Generate scenario
    print("[1/4] Generating synthetic market data...")
    gen = SyntheticDataGenerator(seed=seed)
    scenario = gen.generate_scenario(
        num_markets=num_markets,
        time_steps=time_steps,
        categories=["crypto", "finance", "technology", "politics"],
    )

    markets = scenario["markets"]
    resolutions = scenario["resolutions"]
    print(f"       {len(markets)} markets generated across {len(set(m.category for m in markets))} categories")
    print(f"       Resolutions: {sum(1 for r in resolutions.values() if r)} YES / "
          f"{sum(1 for r in resolutions.values() if not r)} NO")
    print()

    # Run backtest
    print("[2/4] Running backtest engine...")
    engine = BacktestEngine(cfg)
    result = await engine.run(scenario, initial_capital=initial_capital)
    print(f"       Complete — {result.total_trades} trades executed")
    print()

    # Print results
    print("[3/4] Performance Summary")
    print("-" * 50)
    print(f"  Total trades:        {result.total_trades}")
    print(f"  Winning trades:      {result.winning_trades}")
    print(f"  Losing trades:       {result.losing_trades}")
    print(f"  Win rate:            {result.win_rate:.1%}")
    print()
    print(f"  Total P&L:           ${result.total_pnl:,.2f}")
    print(f"  Avg trade P&L:       ${result.avg_trade_pnl:,.2f}")
    print(f"  Final equity:        ${result.equity_curve[-1]:,.2f}" if result.equity_curve else "")
    print(f"  Return:              {((result.equity_curve[-1] - initial_capital) / initial_capital):.2%}" if result.equity_curve else "")
    print()
    print(f"  Sharpe ratio:        {result.sharpe_ratio:.3f}")
    print(f"  Max drawdown:        {result.max_drawdown:.2%}")
    print(f"  Realized/Expected:   {result.realized_vs_expected:.3f}")
    print()

    # Monte Carlo
    mc = result.metadata.get("monte_carlo", {})
    if mc:
        print("  Monte Carlo Stress Test ({} simulations)".format(mc.get("num_simulations", 0)))
        print("  " + "-" * 48)
        print(f"    Mean P&L:          ${mc.get('mean_pnl', 0):,.2f}")
        print(f"    Median P&L:        ${mc.get('median_pnl', 0):,.2f}")
        print(f"    5th percentile:    ${mc.get('pnl_5th_percentile', 0):,.2f}")
        print(f"    95th percentile:   ${mc.get('pnl_95th_percentile', 0):,.2f}")
        print(f"    Mean max drawdown: {mc.get('mean_max_drawdown', 0):.2%}")
        print(f"    Worst drawdown:    {mc.get('worst_max_drawdown', 0):.2%}")
        print(f"    Ruin probability:  {mc.get('probability_of_ruin', 0):.2%}")
    print()

    # Trade log
    print("  Trade Log (first 20 trades):")
    print("  " + "-" * 48)
    from polymarket_bot.types import OrderStatus
    filled = [t for t in result.trades if t.status == OrderStatus.FILLED]
    for i, trade in enumerate(filled[:20]):
        pnl = trade.metadata.get("realized_pnl", 0)
        pnl_str = f"${pnl:+.2f}" if pnl != 0 else "open"
        print(f"    #{i+1:3d}  {trade.side.value:4s}  ${trade.size_usd:7.2f}  "
              f"@{trade.fill_price:.4f}  slip={trade.slippage:.4f}  "
              f"P&L={pnl_str}")
    if len(filled) > 20:
        print(f"    ... and {len(filled) - 20} more trades")
    print()

    # Save results
    print("[4/4] Saving results...")
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    # Save summary JSON
    summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "config": {
            "num_markets": num_markets,
            "time_steps": time_steps,
            "initial_capital": initial_capital,
            "seed": seed,
            "kelly_fraction": cfg.kelly_fraction,
            "slippage_bps": cfg.backtest.default_slippage_bps,
            "fee_rate": cfg.backtest.fee_rate,
        },
        "results": {
            "total_trades": result.total_trades,
            "winning_trades": result.winning_trades,
            "losing_trades": result.losing_trades,
            "win_rate": result.win_rate,
            "total_pnl": result.total_pnl,
            "avg_trade_pnl": result.avg_trade_pnl,
            "sharpe_ratio": result.sharpe_ratio,
            "max_drawdown": result.max_drawdown,
            "realized_vs_expected": result.realized_vs_expected,
            "final_equity": result.equity_curve[-1] if result.equity_curve else initial_capital,
        },
        "monte_carlo": mc,
    }
    with open(out_path / "backtest_summary.json", "w") as f:
        json.dump(summary, f, indent=2)

    # Save equity curve
    with open(out_path / "equity_curve.csv", "w") as f:
        f.write("step,equity\n")
        for i, eq in enumerate(result.equity_curve):
            f.write(f"{i},{eq:.2f}\n")

    # Save trade log
    trades_data = []
    for t in filled:
        trades_data.append({
            "order_id": t.order_id[:12],
            "market_id": t.market_id,
            "side": t.side.value,
            "price": t.price,
            "fill_price": t.fill_price,
            "size_usd": t.size_usd,
            "slippage": t.slippage,
            "realized_pnl": t.metadata.get("realized_pnl", 0),
            "expected_pnl": t.metadata.get("expected_pnl", 0),
        })
    with open(out_path / "trades.json", "w") as f:
        json.dump(trades_data, f, indent=2)

    print(f"       Saved to {out_path}/")
    print(f"       - backtest_summary.json")
    print(f"       - equity_curve.csv ({len(result.equity_curve)} points)")
    print(f"       - trades.json ({len(trades_data)} trades)")
    print()
    print("=" * 70)
    print("  BACKTEST COMPLETE")
    print("=" * 70)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Polymarket bot backtest")
    parser.add_argument("--markets", "-m", type=int, default=30, help="Number of markets")
    parser.add_argument("--steps", "-s", type=int, default=150, help="Time steps")
    parser.add_argument("--capital", "-c", type=float, default=10_000, help="Initial capital")
    parser.add_argument("--output", "-o", type=str, default="backtest_results", help="Output dir")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    args = parser.parse_args()

    asyncio.run(run_backtest(
        num_markets=args.markets,
        time_steps=args.steps,
        initial_capital=args.capital,
        output_dir=args.output,
        seed=args.seed,
    ))


if __name__ == "__main__":
    main()
