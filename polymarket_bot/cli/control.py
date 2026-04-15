"""CLI control plane for the Polymarket trading bot.

Commands:
  polybot run        — Start the bot (paper mode by default)
  polybot backtest   — Run backtests with synthetic data
  polybot status     — Print current positions and metrics
  polybot stop       — Graceful shutdown
  polybot enable-live — Arm live trading (requires passphrase)
"""

from __future__ import annotations

import asyncio
import os
import sys

import click
import structlog

from polymarket_bot.config.settings import load_config
from polymarket_bot.monitoring.logger import configure_logging

logger = structlog.get_logger(__name__)


@click.group()
@click.option("--config", "config_path", default=None, help="Path to config YAML file.")
@click.option("--log-level", default="INFO", help="Logging level.")
@click.option("--json-logs", is_flag=True, help="Output JSON structured logs.")
@click.pass_context
def main(ctx: click.Context, config_path: str | None, log_level: str, json_logs: bool) -> None:
    """Polymarket autonomous trading bot."""
    configure_logging(log_level=log_level, json_output=json_logs)
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path
    ctx.obj["config"] = load_config(config_path)


@main.command()
@click.option("--paper", is_flag=True, default=True, help="Run in paper trading mode (default).")
@click.pass_context
def run(ctx: click.Context, paper: bool) -> None:
    """Start the trading bot."""
    from polymarket_bot.bot import TradingBot

    config = ctx.obj["config"]

    if not config.paper_mode and config.enable_live_trading:
        click.echo("\n⚠️  Live trading config detected.")
        click.echo("To arm live trading, use: polybot enable-live\n")

    bot = TradingBot(config)
    click.echo(
        f"Starting bot in {'PAPER' if config.paper_mode else 'LIVE'} mode..."
    )
    asyncio.run(bot.run())


@main.command()
@click.option("--markets", default=20, help="Number of synthetic markets.")
@click.option("--steps", default=500, help="Number of time steps.")
@click.option("--output", default="logs/backtest_results.json", help="Output file path.")
@click.pass_context
def backtest(ctx: click.Context, markets: int, steps: int, output: str) -> None:
    """Run backtest on synthetic market data."""
    from polymarket_bot.backtest.engine import BacktestEngine

    config = ctx.obj["config"]
    engine = BacktestEngine(config)
    click.echo(f"Running backtest: {markets} markets × {steps} steps...")
    asyncio.run(engine.run(n_markets=markets, n_steps=steps, output_path=output))


@main.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Print current bot status (positions, P&L, circuit breakers)."""
    from polymarket_bot.execution.order_manager import OrderManager
    from polymarket_bot.risk.risk_manager import RiskManager

    config = ctx.obj["config"]
    om = OrderManager()
    risk = RiskManager(config, om)

    click.echo("\n--- Bot Status ---")
    click.echo(f"Mode         : {'PAPER' if config.paper_mode else 'LIVE'}")
    click.echo(f"Open positions: {len(om.get_open_positions())}")
    click.echo(f"Total exposure: ${om.total_exposure_usd():.2f}")
    click.echo(f"Unrealized P&L: ${om.total_unrealized_pnl():.2f}")
    click.echo(f"Circuit breaker: {'TRIPPED ⛔' if risk.circuit_breaker_tripped else 'OK ✓'}")
    if risk.circuit_breaker_tripped:
        click.echo(f"  Reason: {risk.circuit_breaker_reason}")
    click.echo("")


@main.command("enable-live")
@click.pass_context
def enable_live(ctx: click.Context) -> None:
    """Arm live trading with passphrase confirmation.

    Requires ENABLE_LIVE_TRADING=true environment variable AND the exact passphrase.
    """
    from polymarket_bot.execution.live_executor import LIVE_TRADING_PASSPHRASE, LiveExecutor, LiveTradingGateError
    from polymarket_bot.execution.order_manager import OrderManager
    from polymarket_bot.scanner.polymarket_client import PolymarketCLOBClient

    config = ctx.obj["config"]

    click.echo("\n" + "=" * 65)
    click.echo("  LIVE TRADING ENABLEMENT — READ CAREFULLY")
    click.echo("=" * 65)
    click.echo(
        "\n⚠️  You are about to enable LIVE trading with REAL funds.\n"
        "    Polymarket trading involves significant financial risk.\n"
        "    Ensure you have read and accepted all terms of service.\n"
        "    Only proceed if you fully understand the risks.\n"
    )

    if os.environ.get("ENABLE_LIVE_TRADING", "").lower() != "true":
        click.echo("❌ ENABLE_LIVE_TRADING env var is not set to 'true'.")
        click.echo("   Run: export ENABLE_LIVE_TRADING=true\n")
        sys.exit(1)

    passphrase = click.prompt(
        f"Type exactly: {LIVE_TRADING_PASSPHRASE}\n> "
    )

    om = OrderManager()
    clob_client = PolymarketCLOBClient(config)
    executor = LiveExecutor(config, om, clob_client)

    try:
        executor.arm(passphrase)
        click.echo("\n✅ Live trading armed. The bot will now place real orders.")
        click.echo(
            f"   First {config.first_live_trades_manual_review} trades require manual approval.\n"
        )
    except LiveTradingGateError as e:
        click.echo(f"\n❌ Live trading gate check failed:\n   {e}\n")
        sys.exit(1)


@main.command("emergency-stop")
@click.pass_context
def emergency_stop(ctx: click.Context) -> None:
    """Trip the circuit breaker and cancel all open orders (emergency use)."""
    from polymarket_bot.execution.order_manager import OrderManager
    from polymarket_bot.risk.risk_manager import RiskManager

    config = ctx.obj["config"]
    om = OrderManager()
    risk = RiskManager(config, om)

    if click.confirm("⚠️  This will trip the circuit breaker and cancel all orders. Continue?"):
        risk.trip_circuit_breaker("Manual emergency stop via CLI")
        click.echo("⛔ Circuit breaker tripped. All future signals blocked.")
        click.echo("   Run 'polybot status' to confirm. Restart bot to resume after review.")
