"""Dual-leg simultaneous execution via asyncio.gather.

Correct position sizing for binary arb:
  Each Kalshi contract costs k_price dollars and pays $1 on resolution.
  Each Polymarket token costs p_price dollars and pays $1 on resolution.
  Buying N of each guarantees $N regardless of outcome.

  N = total_budget / (k_price + p_price)
  kalshi_spend = N * k_price
  poly_spend   = N * p_price
  total_spend  = N * (k_price + p_price) = total_budget  (exact, before rounding)

Half-fill recovery:
  If only one leg fills, we are unhedged and exposed to the full underlying
  outcome. The recovery path attempts (in order):
    1. cancel_order — succeeds for resting/unfilled limit orders.
    2. counter market order — actively flattens a filled position by selling
       the same count back to the book. Caps the loss to ~spread × count.
  If both fail, an alert is raised for manual review.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from arb_bot.core.arb_detector import ArbOpportunity
from arb_bot.utils.alert import send_alert
from arb_bot.utils.logger import get_logger

logger = get_logger(__name__)

# Skip execution if the opportunity is older than this (prices may have moved)
_MAX_OPP_AGE_SECS = 1.5


@dataclass
class TradeResult:
    opportunity: ArbOpportunity
    k_contracts: int
    kalshi_spend: float
    poly_spend: float
    kalshi_order: Optional[dict]
    poly_order: Optional[dict]
    success: bool
    partial: bool
    error: Optional[str]
    executed_at: datetime


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

    opp_age = (datetime.utcnow() - opp.timestamp).total_seconds()
    if opp_age > _MAX_OPP_AGE_SECS:
        logger.debug(f"Opportunity stale ({opp_age:.2f}s old) — skipping {opp.pair.kalshi_ticker}")
        return None

    # ------------------------------------------------------------------ #
    # Position sizing                                                    #
    # Each contract pair (1 Kalshi + 1 Poly) costs (k_price + p_price)   #
    # and guarantees a $1 payout. Round down to whole contracts.         #
    # ------------------------------------------------------------------ #
    total_budget = min(opp.max_size_usdc, MAX_POSITION_PER_MARKET)
    k_contracts = max(1, int(total_budget / (opp.kalshi_price + opp.polymarket_price)))
    kalshi_spend = k_contracts * opp.kalshi_price
    poly_spend = k_contracts * opp.polymarket_price

    p_token_id = (
        opp.pair.polymarket_yes_token_id
        if opp.polymarket_leg == "YES"
        else opp.pair.polymarket_no_token_id
    )

    if dry_run:
        logger.info(
            f"[DRY RUN] {opp.pair.kalshi_title[:45]} | "
            f"K:{opp.kalshi_leg}×{k_contracts}@{opp.kalshi_price:.3f}=${kalshi_spend:.2f} "
            f"P:{opp.polymarket_leg}×{k_contracts}@{opp.polymarket_price:.3f}=${poly_spend:.2f} "
            f"net={opp.net_edge:.2%}"
        )
        result = TradeResult(
            opportunity=opp,
            k_contracts=k_contracts,
            kalshi_spend=kalshi_spend,
            poly_spend=poly_spend,
            kalshi_order=None,
            poly_order=None,
            success=True,
            partial=False,
            error=None,
            executed_at=datetime.utcnow(),
        )
        _trade_log.append(result)
        risk_manager.post_trade_update(opp, kalshi_spend + poly_spend, success=True)
        return result

    logger.info(
        f"[LIVE] {opp.pair.kalshi_title[:45]} | "
        f"K:{opp.kalshi_leg}×{k_contracts}@{opp.kalshi_price:.3f} "
        f"P:{opp.polymarket_leg}×{k_contracts}@{opp.polymarket_price:.3f} "
        f"total=${kalshi_spend + poly_spend:.2f} net={opp.net_edge:.2%}"
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
            amount_usdc=poly_spend,
        )
    )

    kalshi_result, poly_result = await asyncio.gather(
        kalshi_task, poly_task, return_exceptions=True
    )

    k_failed = isinstance(kalshi_result, BaseException)
    p_failed = isinstance(poly_result, BaseException)

    if k_failed or p_failed:
        err = _fmt_errors(
            kalshi_result if k_failed else None,
            poly_result if p_failed else None,
        )
        logger.error(f"[PARTIAL FILL] {err}")
        await _handle_partial_fill(
            k_failed,
            p_failed,
            kalshi_result,
            poly_result,
            opp,
            k_contracts,
            kalshi_client,
            poly_client,
            risk_manager,
        )
        result = TradeResult(
            opportunity=opp,
            k_contracts=k_contracts,
            kalshi_spend=kalshi_spend,
            poly_spend=poly_spend,
            kalshi_order=None if k_failed else kalshi_result,
            poly_order=None if p_failed else poly_result,
            success=False,
            partial=True,
            error=err,
            executed_at=datetime.utcnow(),
        )
        _trade_log.append(result)
        return result

    k_id = _order_id(kalshi_result)
    p_id = _order_id(poly_result)
    logger.info(
        f"[FILLED] Kalshi={k_id} Poly={p_id} "
        f"total=${kalshi_spend + poly_spend:.2f}"
    )
    result = TradeResult(
        opportunity=opp,
        k_contracts=k_contracts,
        kalshi_spend=kalshi_spend,
        poly_spend=poly_spend,
        kalshi_order=kalshi_result,
        poly_order=poly_result,
        success=True,
        partial=False,
        error=None,
        executed_at=datetime.utcnow(),
    )
    _trade_log.append(result)
    risk_manager.post_trade_update(opp, kalshi_spend + poly_spend, success=True)
    return result


async def _handle_partial_fill(
    k_failed: bool,
    p_failed: bool,
    k_result,
    p_result,
    opp: ArbOpportunity,
    k_contracts: int,
    kalshi_client,
    poly_client,
    risk_manager,
) -> None:
    msg = (
        f"PARTIAL FILL: {opp.pair.kalshi_title[:40]} | "
        f"Kalshi={'FAILED' if k_failed else 'OK'} "
        f"Poly={'FAILED' if p_failed else 'OK'}"
    )
    await send_alert(f"\U0001f6a8 {msg}")

    if not k_failed and isinstance(k_result, dict):
        await _flatten_kalshi(k_result, opp, k_contracts, kalshi_client)

    if not p_failed and isinstance(p_result, dict):
        await _flatten_polymarket(p_result, opp, k_contracts, poly_client)

    risk_manager.on_partial_fill(opp)


async def _flatten_kalshi(
    order_result: dict,
    opp: ArbOpportunity,
    k_contracts: int,
    kalshi_client,
) -> None:
    """Cancel a resting order; if already filled, sell the position back."""
    oid = _order_id(order_result)

    # Step 1: try cancel (works for resting orders only)
    if oid != "?":
        try:
            await kalshi_client.cancel_order(oid)
            logger.info(f"Cancelled Kalshi order {oid}")
            return
        except Exception as exc:
            logger.warning(f"Kalshi cancel {oid} failed ({exc}); attempting active flatten")

    # Step 2: market sell to flatten an already-filled position
    try:
        await kalshi_client.place_order(
            ticker=opp.pair.kalshi_ticker,
            side=opp.kalshi_leg.lower(),
            order_type="market",
            count=k_contracts,
            action="sell",
        )
        logger.info(
            f"Flattened Kalshi position via SELL {k_contracts}×{opp.kalshi_leg} "
            f"on {opp.pair.kalshi_ticker}"
        )
    except Exception as exc:
        logger.error(f"CANNOT flatten Kalshi position on {opp.pair.kalshi_ticker}: {exc}")
        await send_alert(
            f"⛔ MANUAL: Kalshi {k_contracts}×{opp.kalshi_leg} on "
            f"{opp.pair.kalshi_ticker} UNHEDGED — review immediately"
        )


async def _flatten_polymarket(
    order_result: dict,
    opp: ArbOpportunity,
    k_contracts: int,
    poly_client,
) -> None:
    """Polymarket FOK either fills fully or self-cancels. If it filled, sell back."""
    status = (order_result.get("status") or "").lower()
    if status in ("cancelled", "canceled", "unfilled", "rejected", ""):
        return  # nothing to flatten

    p_token_id = (
        opp.pair.polymarket_yes_token_id
        if opp.polymarket_leg == "YES"
        else opp.pair.polymarket_no_token_id
    )
    try:
        await poly_client.place_market_order(
            token_id=p_token_id,
            amount_usdc=float(k_contracts),  # selling N tokens, expect ~N*price proceeds
            side="SELL",
        )
        logger.info(
            f"Flattened Polymarket position via SELL {k_contracts}×{opp.polymarket_leg}"
        )
    except Exception as exc:
        logger.error(f"CANNOT flatten Polymarket position on {p_token_id}: {exc}")
        await send_alert(
            f"⛔ MANUAL: Polymarket {k_contracts}×{opp.polymarket_leg} "
            f"on {p_token_id[:8]}… UNHEDGED — review immediately"
        )


def _order_id(result) -> str:
    if not isinstance(result, dict):
        return "?"
    return (
        result.get("order", {}).get("order_id")
        or result.get("order_id")
        or result.get("orderID")
        or "?"
    )


def _fmt_errors(k_err, p_err) -> str:
    parts = []
    if k_err is not None:
        parts.append(f"Kalshi: {k_err}")
    if p_err is not None:
        parts.append(f"Poly: {p_err}")
    return " | ".join(parts)
