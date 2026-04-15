#!/usr/bin/env python3
"""Example: single scan → signal → risk check → paper execution cycle.

Demonstrates the full pipeline in paper mode using synthetic data.
No API keys or real funds are needed.

Two demo modes:
  1. EV mispricing signals (using a demo model with synthetic alpha)
  2. Complement arbitrage signals (injected 2-5% underround in books)
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

Path("logs").mkdir(exist_ok=True)

from polymarket_bot.backtest.data_generator import SyntheticMarketGenerator
from polymarket_bot.config.settings import load_config
from polymarket_bot.execution.order_manager import OrderManager
from polymarket_bot.execution.paper_executor import PaperExecutor
from polymarket_bot.models.base import ModelEstimate, ProbabilityModel
from polymarket_bot.monitoring.logger import configure_logging
from polymarket_bot.risk.risk_manager import RiskAction, RiskManager
from polymarket_bot.signals.arbitrage import ArbitrageDetector
from polymarket_bot.signals.ev_calculator import EVCalculator
from polymarket_bot.types import (
    Market, OrderBook, OrderStatus, PriceLevel, Token,
)


class DemoAlphaModel(ProbabilityModel):
    """Demo model that injects synthetic alpha for illustration.

    In production, replace with:
    - News sentiment scoring
    - Prediction aggregator consensus (Metaculus/Manifold)
    - Calibrated superforecaster models
    - Latency arb vs. crypto exchange prices
    """

    def __init__(self, alpha_map: dict[str, float]) -> None:
        """alpha_map: token_id → true probability (from demo oracle)"""
        self._alpha = alpha_map

    @property
    def name(self) -> str:
        return "demo_alpha"

    async def estimate(self, market: Market) -> list[ModelEstimate]:
        estimates = []
        for token in market.tokens:
            if token.token_id in self._alpha:
                prob = self._alpha[token.token_id]
                estimates.append(ModelEstimate(
                    market_condition_id=market.condition_id,
                    token_id=token.token_id,
                    outcome=token.outcome,
                    probability=prob,
                    confidence=0.80,
                    model_name=self.name,
                    reasoning="demo oracle: true_probability from synthetic generator",
                ))
        return estimates


def make_demo_markets() -> list[tuple[Market, dict[str, OrderBook], dict[str, float]]]:
    """Create hand-crafted demo markets that clearly show signals."""
    demos = []

    # ── Market 1: EV mispricing (YES underpriced by 12%) ──────────────
    m1 = Market(
        condition_id="demo_001",
        question="Will the Fed cut rates before June 2026?",
        tokens=[
            Token("d1_yes", "Yes", price=0.38),
            Token("d1_no",  "No",  price=0.62),
        ],
        category="finance",
        liquidity_usd=45_000,
        volume_usd=120_000,
    )
    books1 = {
        "d1_yes": OrderBook("d1_yes",
            bids=[PriceLevel(0.375, 8000), PriceLevel(0.37, 12000)],
            asks=[PriceLevel(0.385, 8000), PriceLevel(0.39, 12000)]),
        "d1_no":  OrderBook("d1_no",
            bids=[PriceLevel(0.615, 8000), PriceLevel(0.61, 12000)],
            asks=[PriceLevel(0.625, 8000), PriceLevel(0.63, 12000)]),
    }
    # Model says YES is 50% — market prices it at 38% → +12% EV
    alpha1 = {"d1_yes": 0.50, "d1_no": 0.50}
    demos.append((m1, books1, alpha1))

    # ── Market 2: Complement arbitrage (YES_ask + NO_ask = 0.93 < 1.0) ─
    m2 = Market(
        condition_id="demo_002",
        question="Will BTC exceed $120,000 before July 2026?",
        tokens=[
            Token("d2_yes", "Yes", price=0.44),
            Token("d2_no",  "No",  price=0.49),
        ],
        category="crypto",
        liquidity_usd=80_000,
        volume_usd=250_000,
    )
    books2 = {
        "d2_yes": OrderBook("d2_yes",
            bids=[PriceLevel(0.435, 8000), PriceLevel(0.43, 12000)],
            asks=[PriceLevel(0.44,  8000), PriceLevel(0.45, 12000)]),   # ask=0.44
        "d2_no":  OrderBook("d2_no",
            bids=[PriceLevel(0.485, 8000), PriceLevel(0.48, 12000)],
            asks=[PriceLevel(0.49,  8000), PriceLevel(0.50, 12000)]),   # ask=0.49
    }
    # YES_ask + NO_ask = 0.44 + 0.49 = 0.93 < 1.0 → complement arb!
    alpha2 = {"d2_yes": 0.50, "d2_no": 0.50}
    demos.append((m2, books2, alpha2))

    # ── Market 3: Large EV signal with high confidence ─────────────────
    m3 = Market(
        condition_id="demo_003",
        question="Will the S&P 500 close above 5,500 in Q2 2026?",
        tokens=[
            Token("d3_yes", "Yes", price=0.55),
            Token("d3_no",  "No",  price=0.45),
        ],
        category="finance",
        liquidity_usd=35_000,
        volume_usd=90_000,
    )
    books3 = {
        "d3_yes": OrderBook("d3_yes",
            bids=[PriceLevel(0.545, 10000), PriceLevel(0.54, 15000)],
            asks=[PriceLevel(0.555, 10000), PriceLevel(0.56, 15000)]),
        "d3_no":  OrderBook("d3_no",
            bids=[PriceLevel(0.445, 10000), PriceLevel(0.44, 15000)],
            asks=[PriceLevel(0.455, 10000), PriceLevel(0.46, 15000)]),
    }
    # Model says NO is 60% (market prices NO at only 45%) → +15% EV on NO
    alpha3 = {"d3_yes": 0.40, "d3_no": 0.60}
    demos.append((m3, books3, alpha3))

    return demos


async def main() -> None:
    configure_logging(log_level="INFO")
    config = load_config("config/default.yaml")

    print("\n" + "=" * 65)
    print("  POLYMARKET BOT — EXAMPLE RUN (PAPER MODE)")
    print(f"  {datetime.now(timezone.utc).isoformat()}")
    print("=" * 65)
    print(f"  Mode          : {'PAPER ✓' if config.paper_mode else 'LIVE ⚠️'}")
    print(f"  EV threshold  : {config.ev_threshold:.1%}")
    print(f"  Kelly fraction: {config.risk.kelly_fraction:.2f}× (quarter-Kelly)")
    print(f"  Max trade size: ${config.risk.per_trade_max_usd:.0f}")
    print(f"  Global cap    : ${config.risk.global_exposure_usd:,.0f}")
    print("=" * 65 + "\n")

    om       = OrderManager(audit_log_path="logs/example_audit.jsonl")
    paper    = PaperExecutor(config, om)
    risk_mgr = RiskManager(config, om)
    ev_calc  = EVCalculator(config)
    arb_det  = ArbitrageDetector(config)

    executed: list[dict] = []
    blocked:  list[dict] = []

    print("Processing demo markets...\n")

    for market, order_books, alpha_map in make_demo_markets():
        # Update token prices from order book mid-prices
        for token in market.tokens:
            if token.token_id in order_books:
                token.price = order_books[token.token_id].mid_price
                token.book_depth_usd = sum(
                    l.price * l.size for l in order_books[token.token_id].bids
                ) + sum(l.price * l.size for l in order_books[token.token_id].asks)

        model = DemoAlphaModel(alpha_map)
        estimates = await model.estimate(market)

        ev_signals  = ev_calc.compute_signals(market, estimates, order_books)
        arb_signals = arb_det.scan([market], order_books)
        all_signals = ev_signals + arb_signals

        if not all_signals:
            print(f"  ○ No signals  | {market.question[:55]}\n")
            continue

        for signal in all_signals:
            decision = risk_mgr.evaluate(signal)

            if decision.action == RiskAction.CIRCUIT_BREAK:
                print(f"  ⛔ CIRCUIT BREAKER: {risk_mgr.circuit_breaker_reason}")
                break

            if decision.is_approved and decision.approved_size_usd > 0:
                order = await paper.execute(signal, decision.approved_size_usd)
                ev_fees = signal.metadata.get("ev_after_fees", signal.expected_value)
                entry = {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "signal_id": signal.signal_id,
                    "market": market.question,
                    "outcome": signal.token.outcome if signal.token else "?",
                    "side": signal.side.value,
                    "signal_type": signal.signal_type.value,
                    "model_prob": round(signal.model_probability, 4),
                    "market_price": round(signal.market_price, 4),
                    "ev": round(signal.expected_value, 4),
                    "ev_after_fees": round(ev_fees, 4),
                    "confidence": round(signal.confidence, 3),
                    "approved_size_usd": round(decision.approved_size_usd, 2),
                    "fill_status": order.status.value,
                    "fill_price": round(order.filled_price, 4),
                    "fill_size_shares": round(order.filled_size, 4),
                    "fill_usd": round(order.filled_size * order.filled_price, 2),
                    "slippage": round(order.slippage, 5),
                    "risk_checks": decision.checks,
                    "risk_action": decision.action.value,
                    "is_paper": order.is_paper,
                }
                executed.append(entry)

                icon = "✅" if order.status == OrderStatus.FILLED else "⚡"
                print(
                    f"  {icon} {signal.signal_type.value:<12} | {market.question[:50]}\n"
                    f"    Side      : {signal.side.value} {signal.token.outcome if signal.token else ''}\n"
                    f"    Market px : {signal.market_price:.4f}   Model prob: {signal.model_probability:.4f}\n"
                    f"    EV        : {signal.expected_value:+.4f}  After fees: {ev_fees:+.4f}\n"
                    f"    Size USD  : ${decision.approved_size_usd:.2f}\n"
                    f"    Fill      : {order.filled_price:.4f} × {order.filled_size:.2f}"
                    f" = ${order.filled_size * order.filled_price:.2f}"
                    f"  slippage={order.slippage:+.5f}\n"
                    f"    Status    : {order.status.value}"
                    f"  Risk: {decision.action.value}  Checks: {list(decision.checks.keys())}\n"
                )
            else:
                blocked.append({"market": market.question[:50], "reason": decision.reason})
                print(f"  🚫 BLOCKED    | {market.question[:50]}\n"
                      f"    Reason    : {decision.reason}\n")

    # ── Summary ───────────────────────────────────────────────────────
    print("=" * 65)
    print("  RUN SUMMARY")
    print("=" * 65)
    print(f"  Executed trades  : {len(executed)}")
    print(f"  Blocked signals  : {len(blocked)}")
    print(f"  Open positions   : {len(om.get_open_positions())}")
    print(f"  Total exposure   : ${om.total_exposure_usd():.2f}")
    print(f"  Unrealized P&L   : ${om.total_unrealized_pnl():.4f}")
    print(f"  Circuit breaker  : {'TRIPPED ⛔' if risk_mgr.circuit_breaker_tripped else 'OK ✓'}")
    print("=" * 65)

    # Save structured log
    log = {
        "run_ts": datetime.now(timezone.utc).isoformat(),
        "mode": "paper",
        "executed": executed,
        "blocked": blocked,
        "summary": {
            "executed": len(executed),
            "blocked": len(blocked),
            "open_positions": len(om.get_open_positions()),
            "total_exposure_usd": round(om.total_exposure_usd(), 2),
            "unrealized_pnl": round(om.total_unrealized_pnl(), 4),
        },
    }
    with open("logs/example_run.json", "w") as f:
        json.dump(log, f, indent=2)
    print(f"\n  Full structured log : logs/example_run.json")
    print(f"  Audit log           : logs/example_audit.jsonl\n")


if __name__ == "__main__":
    asyncio.run(main())
