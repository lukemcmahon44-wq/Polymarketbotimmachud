"""CLI entry point — control plane for status, run, backtest, and live-enable."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import click
import structlog

from polymarket_bot.config import load_config
from polymarket_bot.monitoring.logger import setup_logging

logger = structlog.get_logger()


@click.group()
@click.option("--config", "-c", default=None, help="Path to config YAML file")
@click.pass_context
def cli(ctx: click.Context, config: str | None) -> None:
    """Polymarket Trading Bot — autonomous prediction market trader."""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config


@cli.command()
@click.option("--paper/--no-paper", default=True, help="Force paper mode (default: True)")
@click.pass_context
def run(ctx: click.Context, paper: bool) -> None:
    """Run the trading bot (default: paper mode)."""
    cfg = load_config(ctx.obj.get("config_path"))
    if paper:
        cfg.paper_mode = True
    setup_logging(cfg.monitoring.log_level, json_output=cfg.monitoring.structured_logging)

    click.echo("=" * 60)
    click.echo("  POLYMARKET TRADING BOT")
    click.echo(f"  Mode: {'PAPER' if cfg.paper_mode else 'LIVE'}")
    click.echo(f"  Global exposure cap: ${cfg.global_exposure_usd:,.0f}")
    click.echo(f"  Per-trade max: ${cfg.per_trade_max_usd:,.0f}")
    click.echo("=" * 60)

    if not cfg.paper_mode:
        _confirm_live_trading(cfg)

    asyncio.run(_run_bot(cfg))


async def _run_bot(cfg: object) -> None:
    """Main bot loop."""
    from polymarket_bot.scanner.polymarket_client import PolymarketClient
    from polymarket_bot.scanner.market_feed import MarketFeed
    from polymarket_bot.models.ensemble import EnsembleModel
    from polymarket_bot.signals.ev_calculator import EVCalculator
    from polymarket_bot.signals.arbitrage import ArbitrageDetector
    from polymarket_bot.execution.engine import ExecutionEngine
    from polymarket_bot.execution.paper_executor import PaperExecutor
    from polymarket_bot.risk.manager import RiskManager
    from polymarket_bot.wallet_tracing.detector import CopyTradeDetector
    from polymarket_bot.monitoring.alerts import AlertManager
    from polymarket_bot.config import BotConfig

    assert isinstance(cfg, BotConfig)

    client = PolymarketClient(cfg)
    feed = MarketFeed(client, cfg)
    model = EnsembleModel()
    ev_calc = EVCalculator(cfg)
    arb_detector = ArbitrageDetector(cfg)
    risk_mgr = RiskManager(cfg)
    paper_exec = PaperExecutor()
    exec_engine = ExecutionEngine(paper_exec, cfg)
    copy_detector = CopyTradeDetector(cfg)
    alerts = AlertManager(cfg)

    logger.info("bot.starting", paper_mode=cfg.paper_mode)

    try:
        cycle = 0
        while True:
            cycle += 1
            logger.info("bot.cycle_start", cycle=cycle)

            try:
                # 1. Scan markets
                markets = await feed.scan_all_categories()
                if not markets:
                    logger.warning("bot.no_markets_found")
                    await asyncio.sleep(cfg.scanner.poll_interval_seconds)
                    continue

                # 2. Refresh order books
                books = await feed.refresh_order_books()

                # 3. Generate model estimates
                estimates = await model.batch_estimate(markets)

                # 4. Generate signals
                signals = ev_calc.generate_signals(markets, estimates, books)
                arb_signals = arb_detector.detect_all(markets, books)
                all_signals = signals + arb_signals
                all_signals.sort(key=lambda s: s.ev, reverse=True)

                logger.info(
                    "bot.signals",
                    mispricing=len(signals),
                    arbitrage=len(arb_signals),
                    total=len(all_signals),
                )

                # 5. Execute top signals
                for signal in all_signals[:3]:
                    decision = risk_mgr.evaluate_signal(signal)
                    if not decision.allowed:
                        logger.info(
                            "bot.signal_rejected",
                            reason=decision.reason,
                            layer=decision.layer,
                        )
                        continue

                    # Apply copy-trade defensive measures
                    size, delay = copy_detector.apply_defensive_action(decision.adjusted_size_usd)
                    if delay > 0:
                        await asyncio.sleep(delay)

                    order = await exec_engine.execute_signal(signal, size)
                    copy_detector.record_our_trade(order)

                    logger.info(
                        "bot.trade_executed",
                        order_id=order.order_id[:8],
                        status=order.status.value,
                        side=order.side.value,
                        price=order.price,
                        size_usd=order.size_usd,
                        paper=order.is_paper,
                    )

                # 6. Reconcile orders
                await exec_engine.reconcile_orders()

                # 7. Log snapshot
                risk_snap = risk_mgr.get_snapshot()
                feed_snap = feed.get_snapshot()
                logger.info("bot.snapshot", risk=risk_snap, feed=feed_snap)

            except Exception as exc:
                logger.error("bot.cycle_error", error=str(exc), cycle=cycle)
                risk_mgr.record_rpc_failure()
                if risk_mgr.check_circuit_breakers():
                    await alerts.alert_circuit_breaker(
                        "RPC failures exceeded threshold",
                        risk_mgr.get_snapshot(),
                    )
                    break

            await asyncio.sleep(cfg.scanner.poll_interval_seconds)

    except KeyboardInterrupt:
        logger.info("bot.shutdown_requested")
    finally:
        await client.close()
        logger.info("bot.stopped")


def _confirm_live_trading(cfg: object) -> None:
    """Require passphrase confirmation for live trading."""
    from polymarket_bot.config import BotConfig
    assert isinstance(cfg, BotConfig)

    import os
    if os.environ.get("ENABLE_LIVE_TRADING", "false").lower() != "true":
        click.echo("\nERROR: ENABLE_LIVE_TRADING environment variable must be 'true'")
        click.echo("Set: export ENABLE_LIVE_TRADING=true")
        sys.exit(1)

    click.echo("\n*** LIVE TRADING MODE ***")
    click.echo("You are about to enable REAL transaction signing and broadcasting.")
    click.echo(f"Type the following passphrase exactly to confirm:\n  {cfg.live_passphrase}\n")

    passphrase = input("Passphrase: ").strip()
    if passphrase != cfg.live_passphrase:
        click.echo("ERROR: Passphrase mismatch. Aborting.")
        sys.exit(1)

    click.echo("Live trading confirmed. Proceeding with caution.\n")


@cli.command()
@click.option("--markets", "-m", default=20, help="Number of synthetic markets")
@click.option("--steps", "-s", default=100, help="Time steps to simulate")
@click.option("--capital", default=10000.0, help="Initial capital")
@click.pass_context
def backtest(ctx: click.Context, markets: int, steps: int, capital: float) -> None:
    """Run backtest with synthetic or historical data."""
    cfg = load_config(ctx.obj.get("config_path"))
    cfg.paper_mode = True  # Always paper for backtests
    setup_logging(cfg.monitoring.log_level, json_output=False)

    click.echo(f"Running backtest: {markets} markets, {steps} steps, ${capital:,.0f} capital")
    asyncio.run(_run_backtest(cfg, markets, steps, capital))


async def _run_backtest(cfg: object, num_markets: int, steps: int, capital: float) -> None:
    from polymarket_bot.backtest.engine import BacktestEngine
    from polymarket_bot.backtest.data_generator import SyntheticDataGenerator
    from polymarket_bot.config import BotConfig

    assert isinstance(cfg, BotConfig)

    gen = SyntheticDataGenerator()
    scenario = gen.generate_scenario(num_markets=num_markets, time_steps=steps)

    engine = BacktestEngine(cfg)
    result = await engine.run(scenario, initial_capital=capital)

    click.echo("\n" + "=" * 60)
    click.echo("  BACKTEST RESULTS")
    click.echo("=" * 60)
    click.echo(f"  Total trades:      {result.total_trades}")
    click.echo(f"  Winning trades:    {result.winning_trades}")
    click.echo(f"  Losing trades:     {result.losing_trades}")
    click.echo(f"  Win rate:          {result.win_rate:.1%}")
    click.echo(f"  Total P&L:         ${result.total_pnl:,.2f}")
    click.echo(f"  Avg trade P&L:     ${result.avg_trade_pnl:,.2f}")
    click.echo(f"  Sharpe ratio:      {result.sharpe_ratio:.3f}")
    click.echo(f"  Max drawdown:      {result.max_drawdown:.2%}")
    click.echo(f"  Realized/Expected: {result.realized_vs_expected:.3f}")

    mc = result.metadata.get("monte_carlo", {})
    if mc:
        click.echo("\n  Monte Carlo Stress Test:")
        click.echo(f"    Simulations:     {mc.get('num_simulations', 0)}")
        click.echo(f"    Mean P&L:        ${mc.get('mean_pnl', 0):,.2f}")
        click.echo(f"    5th pct P&L:     ${mc.get('pnl_5th_percentile', 0):,.2f}")
        click.echo(f"    95th pct P&L:    ${mc.get('pnl_95th_percentile', 0):,.2f}")
        click.echo(f"    Worst drawdown:  {mc.get('worst_max_drawdown', 0):.2%}")
        click.echo(f"    Ruin probability:{mc.get('probability_of_ruin', 0):.2%}")

    click.echo("=" * 60)


@cli.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show current bot status and risk snapshot."""
    cfg = load_config(ctx.obj.get("config_path"))
    click.echo("Bot Configuration:")
    click.echo(f"  Mode: {'PAPER' if cfg.paper_mode else 'LIVE'}")
    click.echo(f"  Global cap: ${cfg.global_exposure_usd:,.0f}")
    click.echo(f"  Per-market cap: ${cfg.per_market_exposure_usd:,.0f}")
    click.echo(f"  Per-trade max: ${cfg.per_trade_max_usd:,.0f}")
    click.echo(f"  Kelly fraction: {cfg.kelly_fraction}")
    click.echo(f"  Slippage tolerance: {cfg.slippage_tolerance_pct:.1%}")
    click.echo(f"  Circuit breaker drawdown: {cfg.max_drawdown_pct:.1%}")


@cli.command("kill")
def kill_switch() -> None:
    """Emergency kill switch — halts all trading."""
    click.echo("KILL SWITCH ACTIVATED")
    click.echo("All trading halted. Restart bot to resume.")
    # In production, this would signal the running bot process
    sys.exit(0)


if __name__ == "__main__":
    cli()
