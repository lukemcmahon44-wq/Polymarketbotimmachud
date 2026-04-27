"""Entry point for the Kalshi x Polymarket arbitrage bot.

Usage:
    python -m arb_bot.main                  # dry-run, with dashboard
    python -m arb_bot.main --live           # live trading (requires DRY_RUN=false in .env)
    python -m arb_bot.main --no-dashboard   # suppress the web dashboard
"""

import argparse
import asyncio
import sys

import httpx

from arb_bot.utils.logger import get_logger

logger = get_logger(__name__)


async def _check_polymarket_connectivity(clob_host: str, timeout: float) -> bool:
    """Return True if the Polymarket CLOB API responds.

    This is the first thing that fails when a VPN is needed but not active.
    We hit the root endpoint which requires no auth and returns quickly.
    """
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{clob_host}/")
            return resp.status_code < 500
    except Exception:
        return False


async def main(dry_run: bool, dashboard: bool) -> None:
    from arb_bot.clients.kalshi_client import KalshiClient
    from arb_bot.clients.polymarket_client import PolymarketClient
    from arb_bot.config import (
        CONNECTIVITY_TIMEOUT,
        DASHBOARD_HOST,
        DASHBOARD_PORT,
        DRY_RUN as ENV_DRY_RUN,
        MARKET_CATEGORIES,
        POLYMARKET_CLOB_HOST,
        POLYMARKET_CONNECTIVITY_CHECK,
        SCAN_INTERVAL_SECONDS,
    )
    from arb_bot.core.arb_detector import ArbDetector
    from arb_bot.core.executor import execute_arb
    from arb_bot.core.market_matcher import MarketMatcher
    from arb_bot.core.risk_manager import RiskManager
    from arb_bot.data.price_feed import PriceFeed

    # Require explicit double opt-in for live trading
    if not dry_run and ENV_DRY_RUN:
        logger.error(
            "Live trading requested (--live) but DRY_RUN=true in .env. "
            "Set DRY_RUN=false in your .env to enable live trades."
        )
        sys.exit(1)

    mode = "DRY RUN" if dry_run else "LIVE TRADING"
    logger.info(f"Starting arb bot | mode={mode} | categories={MARKET_CATEGORIES}")

    # ------------------------------------------------------------------
    # VPN / connectivity check
    # ------------------------------------------------------------------
    if POLYMARKET_CONNECTIVITY_CHECK:
        logger.info(
            f"Checking Polymarket connectivity at {POLYMARKET_CLOB_HOST} "
            f"(VPN required if geo-restricted)..."
        )
        reachable = await _check_polymarket_connectivity(POLYMARKET_CLOB_HOST, CONNECTIVITY_TIMEOUT)
        if not reachable:
            logger.error(
                "Polymarket is NOT reachable.\n"
                "  -> If you are in a geo-restricted region, start your VPN first,\n"
                "     then re-run the bot.\n"
                "  -> To skip this check, set POLYMARKET_CONNECTIVITY_CHECK=false in .env."
            )
            sys.exit(1)
        logger.info("Polymarket connectivity OK")
    else:
        logger.warning(
            "Polymarket connectivity check skipped (POLYMARKET_CONNECTIVITY_CHECK=false). "
            "Ensure your VPN is active if Polymarket is geo-restricted in your region."
        )

    # ------------------------------------------------------------------
    # Initialise clients
    # ------------------------------------------------------------------
    try:
        kalshi = KalshiClient.from_env()
    except FileNotFoundError as exc:
        logger.error(f"Kalshi client init failed: {exc}")
        sys.exit(1)

    poly = PolymarketClient.from_env()
    risk = RiskManager()

    # ------------------------------------------------------------------
    # Build initial market pairs
    # ------------------------------------------------------------------
    matcher = MarketMatcher()
    pairs = await matcher.build_pairs(kalshi, poly)
    logger.info(f"[INIT] {len(pairs)} matched sports market pairs")

    if not pairs:
        logger.warning(
            "No market pairs found. This is normal when sports seasons are off-peak. "
            "The bot will retry on the next refresh cycle."
        )

    # ------------------------------------------------------------------
    # Start real-time price feeds
    # ------------------------------------------------------------------
    feed = PriceFeed(kalshi, poly, pairs)
    asyncio.create_task(feed.start())

    # ------------------------------------------------------------------
    # Optionally start dashboard
    # ------------------------------------------------------------------
    if dashboard:
        from arb_bot.dashboard.server import start_dashboard
        asyncio.create_task(
            start_dashboard(feed, risk, host=DASHBOARD_HOST, port=DASHBOARD_PORT)
        )
        logger.info(f"Dashboard: http://{DASHBOARD_HOST}:{DASHBOARD_PORT}")

    # Allow WebSocket connections to establish before first scan
    await asyncio.sleep(2)

    # ------------------------------------------------------------------
    # Main scan loop
    # ------------------------------------------------------------------
    detector = ArbDetector(feed)
    pair_refresh_counter = 0
    PAIR_REFRESH_INTERVAL = int(1800 / SCAN_INTERVAL_SECONDS)  # ~30 min

    logger.info("[BOT] Scan loop started")
    while True:
        pair_refresh_counter += 1
        if pair_refresh_counter >= PAIR_REFRESH_INTERVAL:
            logger.info("Refreshing market pairs...")
            try:
                pairs = await matcher.build_pairs(kalshi, poly)
                logger.info(f"Refreshed: {len(pairs)} pairs")
            except Exception as exc:
                logger.warning(f"Pair refresh failed (will retry next cycle): {exc}")
            pair_refresh_counter = 0

        opportunities = detector.scan(pairs)

        if dashboard:
            from arb_bot.dashboard.server import update_opportunities
            update_opportunities(opportunities)

        for opp in opportunities:
            if risk.is_killed:
                logger.warning("Kill switch active — halting all trades")
                break

            ok, reason = risk.pre_trade_check(opp, opp.max_size_usdc)
            if not ok:
                logger.debug(f"Skipped ({reason}): {opp.pair.kalshi_ticker}")
                continue

            logger.info(
                f"[ARB] {opp.pair.kalshi_title[:50]} | "
                f"edge={opp.net_edge:.2%} size=${opp.max_size_usdc:.0f}"
            )
            await execute_arb(opp, kalshi, poly, risk, dry_run=dry_run)

        await asyncio.sleep(SCAN_INTERVAL_SECONDS)


def cli() -> None:
    parser = argparse.ArgumentParser(
        description="Kalshi x Polymarket cross-platform arbitrage bot (sports only)"
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Execute real trades. Also requires DRY_RUN=false in .env.",
    )
    parser.add_argument(
        "--no-dashboard",
        action="store_true",
        help="Disable the web dashboard (default: localhost:8000).",
    )
    args = parser.parse_args()

    try:
        asyncio.run(main(dry_run=not args.live, dashboard=not args.no_dashboard))
    except KeyboardInterrupt:
        logger.info("Shutting down (KeyboardInterrupt)")


if __name__ == "__main__":
    cli()
