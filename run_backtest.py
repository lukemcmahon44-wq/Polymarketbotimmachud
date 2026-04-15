#!/usr/bin/env python3
"""Run a full backtest with Monte Carlo stress testing.

Usage:
    python run_backtest.py                        # Default: 20 markets, 500 steps
    python run_backtest.py --markets 50 --steps 1000
    python run_backtest.py --output results.json
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path


async def main(markets: int, steps: int, output: str) -> None:
    Path("logs").mkdir(exist_ok=True)

    from polymarket_bot.backtest.engine import BacktestEngine
    from polymarket_bot.config.settings import load_config
    from polymarket_bot.monitoring.logger import configure_logging

    configure_logging(log_level="INFO")
    config = load_config("config/default.yaml")

    print(f"\nRunning backtest: {markets} markets × {steps} steps...")
    print(f"Config: ev_threshold={config.ev_threshold:.1%}  "
          f"kelly={config.risk.kelly_fraction:.2f}x  "
          f"global_cap=${config.risk.global_exposure_usd:,.0f}\n")

    engine = BacktestEngine(config)
    result = await engine.run(
        n_markets=markets,
        n_steps=steps,
        output_path=output,
    )

    print(f"\nResults saved to: {output}")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run Polymarket bot backtest")
    parser.add_argument("--markets", type=int, default=20, help="Number of synthetic markets")
    parser.add_argument("--steps", type=int, default=500, help="Number of time steps")
    parser.add_argument("--output", default="logs/backtest_results.json", help="Output file")
    args = parser.parse_args()

    asyncio.run(main(args.markets, args.steps, args.output))
