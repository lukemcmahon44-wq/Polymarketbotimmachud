"""Dual-leg simultaneous execution via asyncio.gather.

Both orders fire within a single event-loop tick. If one leg fails,
the partial-fill handler immediately attempts to cancel/reverse the other
and triggers the kill switch if thresholds are exceeded.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from arb_bot.core.arb_detector import ArbOpportunity
from arb_bot.utils.alert import send_alert
from arb_bot.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class TradeResult:
    opportunity: ArbOpportunity
    kalshi_order: Optional[dict]
    poly_order: Optional[dict]
    size_usdc: float
    success: bool
    partial: bool
    error: Optional[str]
    executed_at: datetime


# In-memory trade log — append-only
_trade_log: list[TradeResult] = []


def get_trade_log() -> list[TradeResult]:
    return list(_trade_log)


async def execute_arb(
    opp: ArbOpportunity,
    kalshi_client,
    poly_client,
    risk_manager,
    dry_run: bool = True,
) -> Optional[TradeResult]:
    from arb_bot.config import MAX_POSITION_PER_MARKET
    from py_clob_client.order_builder.constants import BUY

    size = min(opp.max_size_usdc, MAX_POSITION_PER_MARKET)
    k_contracts = max(1, int(size))  # Kalshi contracts are integer-dollar-denominated

    if dry_run:
        logger.info(f"[DRY RUN] {opp.summary()}")
        result = TradeResult(
            opportunity=opp,
            kalshi_order=None,
            poly_order=None,
            size_usdc=size,
            success=True,
            partial=False,
            error=None,
            executed_at=datetime.utcnow(),
        )
        _trade_log.append(result)
        risk_manager.post_trade_update(opp, size, success=True)
        return result

    logger.info(f"[LIVE] Executing arb: {opp.summary()}")

    # --- Fire both legs concurrently ---
    p_token_id = (
        opp.pair.polymarket_yes_token_id
        if opp.polymarket_leg == "YES"
        else opp.pair.polymarket_no_token_id
    )

    kalshi_task = asyncio.create_task(
        kalshi_client.place_order(
            ticker=opp.pair.kalshi_ticker,
            side=opp.kalshi_leg.lower(),
            order_type="market",
            count=k_contracts,
        )
    )
    poly_task = asyncio.create_task(
        poly_client.place_market_order(
            token_id=p_token_id,
            amount_usdc=size,
            side=BUY,
        )
    )

    kalshi_result, poly_result = await asyncio.gather(
        kalshi_task, poly_task, return_exceptions=True
    )

    k_failed = isinstance(kalshi_result, BaseException)
    p_failed = isinstance(poly_result, BaseException)

    if k_failed or p_failed:
        error_msg = _format_errors(kalshi_result if k_failed else None, poly_result if p_failed else None)
        logger.error(f"[PARTIAL FILL] {error_msg}")
        await _handle_partial_fill(
            k_failed, p_failed,
            kalshi_result, poly_result,
            opp, kalshi_client, poly_client, risk_manager,
        )
        result = TradeResult(
            opportunity=opp,
            kalshi_order=None if k_failed else kalshi_result,
            poly_order=None if p_failed else poly_result,
            size_usdc=size,
            success=False,
            partial=True,
            error=error_msg,
            executed_at=datetime.utcnow(),
        )
        _trade_log.append(result)
        return result

    logger.info(f"[FILLED] Kalshi={kalshi_result.get('order_id', '?')} Poly={poly_result.get('orderID', '?')}")
    result = TradeResult(
        opportunity=opp,
        kalshi_order=kalshi_result,
        poly_order=poly_result,
        size_usdc=size,
        success=True,
        partial=False,
        error=None,
        executed_at=datetime.utcnow(),
    )
    _trade_log.append(result)
    risk_manager.post_trade_update(opp, size, success=True)
    return result


async def _handle_partial_fill(
    k_failed: bool,
    p_failed: bool,
    k_result,
    p_result,
    opp: ArbOpportunity,
    kalshi_client,
    poly_client,
    risk_manager,
) -> None:
    """Attempt to cancel the filled leg to avoid an unhedged directional position."""
    msg = (
        f"PARTIAL FILL on {opp.pair.kalshi_title[:40]} | "
        f"Kalshi {'FAILED' if k_failed else 'OK'} | Poly {'FAILED' if p_failed else 'OK'}"
    )
    await send_alert(f"🚨 {msg}")

    if not k_failed and isinstance(k_result, dict):
        order_id = k_result.get("order", {}).get("order_id") or k_result.get("order_id")
        if order_id:
            try:
                await kalshi_client.cancel_order(order_id)
                logger.info(f"Cancelled Kalshi order {order_id} after partial fill")
            except Exception as exc:
                logger.error(f"FAILED to cancel Kalshi order {order_id}: {exc}")
                logger.error("MANUAL REVIEW REQUIRED — open Kalshi position unhedged")

    # Polymarket market orders (FOK) either fully fill or cancel; no reversal needed
    # but log the state clearly
    if not p_failed:
        logger.warning("Poly order may have filled; Kalshi leg failed — check positions")

    risk_manager.on_partial_fill(opp)


def _format_errors(k_err, p_err) -> str:
    parts = []
    if k_err is not None:
        parts.append(f"Kalshi: {k_err}")
    if p_err is not None:
        parts.append(f"Poly: {p_err}")
    return " | ".join(parts)
