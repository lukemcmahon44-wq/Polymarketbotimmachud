#!/usr/bin/env python3
"""Example: single scan → signal → simulated execution cycle with structured logs.

Run with:  python run_example.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone

# Setup logging first
from polymarket_bot.monitoring.logger import setup_logging
setup_logging(level="INFO", json_output=False)

from polymarket_bot.backtest.data_generator import SyntheticDataGenerator
from polymarket_bot.config import load_config
from polymarket_bot.execution.engine import ExecutionEngine
from polymarket_bot.execution.paper_executor import PaperExecutor
from polymarket_bot.models.ensemble import EnsembleModel
from polymarket_bot.risk.manager import RiskManager
from polymarket_bot.signals.arbitrage import ArbitrageDetector
from polymarket_bot.signals.ev_calculator import EVCalculator
from polymarket_bot.types import OrderStatus, Position


async def main() -> None:
    print("=" * 70)
    print("  POLYMARKET BOT — EXAMPLE TRADE CYCLE (PAPER MODE)")
    print("=" * 70)
    print()

    # 1. Load config
    config = load_config()
    config.paper_mode = True
    config.min_liquidity_usd = 0  # Allow synthetic data
    config.min_ev_threshold = 0.005
    config.confidence_floor = 0.2
    print(f"[CONFIG] Paper mode: {config.paper_mode}")
    print(f"[CONFIG] Global exposure cap: ${config.global_exposure_usd:,.0f}")
    print(f"[CONFIG] Per-trade max: ${config.per_trade_max_usd:,.0f}")
    print(f"[CONFIG] Kelly fraction: {config.kelly_fraction}")
    print()

    # 2. Generate synthetic market data
    gen = SyntheticDataGenerator(seed=42)
    markets = [
        gen.generate_market(category="crypto"),
        gen.generate_market(category="finance"),
        gen.generate_market(category="technology"),
        gen.generate_market(category="crypto"),
        gen.generate_market(category="politics"),
    ]
    print(f"[SCAN] Scanned {len(markets)} markets:")
    for m in markets:
        yes_price = m.tokens[0].price
        no_price = m.tokens[1].price
        print(f"  • {m.question}")
        print(f"    YES={yes_price:.3f} NO={no_price:.3f} | Vol=${m.volume_usd:,.0f} Liq=${m.liquidity_usd:,.0f}")
    print()

    # 3. Generate model estimates
    model = EnsembleModel()
    estimates = await model.batch_estimate(markets)
    print(f"[MODEL] Ensemble estimates for {len(estimates)} markets:")
    for market, ests in zip(markets, estimates):
        for est in ests:
            diff = est.probability - next(
                t.price for t in market.tokens if t.token_id == est.token_id
            )
            print(f"  • {est.outcome}: model={est.probability:.4f} market={est.probability - diff:.4f} "
                  f"Δ={diff:+.4f} conf={est.confidence:.3f}")
    print()

    # 4. Generate signals (EV + arbitrage)
    ev_calc = EVCalculator(config)
    signals = ev_calc.generate_signals(markets, estimates)

    arb_detector = ArbitrageDetector(config)
    arb_signals = arb_detector.detect_all(markets)

    all_signals = signals + arb_signals
    all_signals.sort(key=lambda s: s.ev, reverse=True)

    print(f"[SIGNALS] Generated {len(signals)} mispricing + {len(arb_signals)} arbitrage signals")
    for i, sig in enumerate(all_signals[:10]):
        print(f"  #{i+1} [{sig.signal_type.value}] {sig.side.value} | "
              f"EV={sig.ev:+.4f} | model={sig.model_prob:.3f} mkt={sig.market_price:.3f} "
              f"conf={sig.confidence:.3f}")
    print()

    # 5. Risk check and execute
    risk_mgr = RiskManager(config)
    executor = PaperExecutor(fill_probability=0.9, base_slippage_bps=50)
    engine = ExecutionEngine(executor, config)

    print("[EXECUTION] Processing top signals through risk manager:")
    for signal in all_signals[:5]:
        decision = risk_mgr.evaluate_signal(signal)
        if not decision.allowed:
            print(f"  ✗ REJECTED: {decision.reason} (Layer: {decision.layer})")
            continue

        order = await engine.execute_signal(signal, decision.adjusted_size_usd)
        status_icon = "✓" if order.status == OrderStatus.FILLED else "◐" if order.status == OrderStatus.PARTIALLY_FILLED else "○"
        print(f"  {status_icon} {order.side.value} ${order.size_usd:.2f} @ {order.price:.4f} "
              f"→ {order.status.value} | fill={order.fill_price:.4f} "
              f"slip={order.slippage:.4f} gas=${order.gas_cost_usd:.3f}")

        if order.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
            pos = Position(
                market_id=order.market_id,
                token_id=order.token_id,
                side=order.side,
                entry_price=order.fill_price,
                current_price=order.fill_price,
                size_usd=order.size_usd,
                size_shares=order.filled_size,
            )
            risk_mgr.add_position(pos)
    print()

    # 6. Risk snapshot
    snap = risk_mgr.get_snapshot()
    print("[RISK SNAPSHOT]")
    for key, val in snap.items():
        print(f"  {key}: {val}")
    print()

    # 7. Audit log summary
    print(f"[AUDIT] {len(engine.audit_log)} events logged:")
    for event in engine.audit_log[:10]:
        print(f"  [{event['timestamp'][:19]}] {event['event']} | "
              f"order={event['order_id'][:8]} {event['side']} ${event['size_usd']:.2f} "
              f"status={event['status']}")
    print()

    print("=" * 70)
    print("  CYCLE COMPLETE — All trades simulated (paper mode)")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
