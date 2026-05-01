"""Tests for the contract-count sizing formula in executor.execute_arb.

Regression net for the bug where `k_contracts = max(1, int(size))` treated the
dollar budget as contract count directly, leaving the two legs unbalanced.
The correct formula:  N = int(budget / (k_price + p_price))
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arb_bot.core.executor import execute_arb
from arb_bot.core.market_matcher import MarketPair


def _pair() -> MarketPair:
    return MarketPair(
        kalshi_ticker="NFLGAME-24-CHIEFS",
        kalshi_title="Will the Chiefs win?",
        polymarket_condition_id="0xabc",
        polymarket_yes_token_id="111",
        polymarket_no_token_id="222",
        polymarket_title="Chiefs to win?",
        match_confidence=95.0,
        resolution_risk="LOW",
        verified_manual=True,
    )


def _opp(k_price: float, p_price: float, budget: float) -> SimpleNamespace:
    """Mimics ArbOpportunity attribute surface used by execute_arb."""
    return SimpleNamespace(
        pair=_pair(),
        kalshi_price=k_price,
        polymarket_price=p_price,
        kalshi_leg="YES",
        polymarket_leg="NO",
        max_size_usdc=budget,
        net_edge=0.05,
        timestamp=datetime.utcnow(),  # fresh → passes staleness check
    )


def _risk() -> MagicMock:
    risk = MagicMock()
    risk.is_killed = False
    risk.pre_trade_check.return_value = (True, "")
    return risk


@pytest.mark.asyncio
async def test_contract_count_balances_legs():
    """k=0.65, p=0.30, budget=$100 → N=int(100/0.95)=105 contracts."""
    opp = _opp(k_price=0.65, p_price=0.30, budget=100.0)
    expected_n = int(100.0 / 0.95)  # 105

    kalshi = MagicMock()
    kalshi.place_order = AsyncMock(return_value={"order_id": "k1"})
    poly = MagicMock()
    poly.place_market_order = AsyncMock(return_value={"id": "p1"})

    with patch("arb_bot.config.MAX_POSITION_PER_MARKET", 500.0):
        result = await execute_arb(opp, kalshi, poly, _risk(), dry_run=False)

    assert result is not None
    assert result.k_contracts == expected_n
    # poly_spend = N * p_price, NOT the full budget
    assert abs(result.poly_spend - expected_n * 0.30) < 1e-6
    assert abs(result.kalshi_spend - expected_n * 0.65) < 1e-6
    # Total spend never exceeds the budget after rounding down
    assert result.kalshi_spend + result.poly_spend <= 100.0 + 1e-6


@pytest.mark.asyncio
async def test_poly_call_receives_poly_spend_not_full_budget():
    """place_market_order must get poly_spend = N*p_price, not budget."""
    opp = _opp(k_price=0.55, p_price=0.40, budget=200.0)
    expected_n = int(200.0 / 0.95)  # 210
    expected_poly_spend = expected_n * 0.40  # ~$84

    kalshi = MagicMock()
    kalshi.place_order = AsyncMock(return_value={"order_id": "k1"})
    poly = MagicMock()
    poly.place_market_order = AsyncMock(return_value={"id": "p1"})

    with patch("arb_bot.config.MAX_POSITION_PER_MARKET", 500.0):
        await execute_arb(opp, kalshi, poly, _risk(), dry_run=False)

    poly.place_market_order.assert_awaited_once()
    kwargs = poly.place_market_order.await_args.kwargs
    assert abs(kwargs["amount_usdc"] - expected_poly_spend) < 1e-6
    # Critical: must NOT be the full $200 budget
    assert kwargs["amount_usdc"] < 200.0


@pytest.mark.asyncio
async def test_kalshi_call_receives_contract_count():
    """Kalshi place_order's `count` is N contracts, not dollars."""
    opp = _opp(k_price=0.60, p_price=0.35, budget=95.0)
    expected_n = int(95.0 / 0.95)  # 100

    kalshi = MagicMock()
    kalshi.place_order = AsyncMock(return_value={"order_id": "k1"})
    poly = MagicMock()
    poly.place_market_order = AsyncMock(return_value={"id": "p1"})

    with patch("arb_bot.config.MAX_POSITION_PER_MARKET", 500.0):
        await execute_arb(opp, kalshi, poly, _risk(), dry_run=False)

    kalshi.place_order.assert_awaited_once()
    kwargs = kalshi.place_order.await_args.kwargs
    assert kwargs["count"] == expected_n


@pytest.mark.asyncio
async def test_minimum_one_contract_on_tiny_budget():
    """Even a $0.50 budget produces ≥ 1 contract (max(1, ...))."""
    opp = _opp(k_price=0.65, p_price=0.30, budget=0.50)

    kalshi = MagicMock()
    kalshi.place_order = AsyncMock(return_value={"order_id": "k1"})
    poly = MagicMock()
    poly.place_market_order = AsyncMock(return_value={"id": "p1"})

    with patch("arb_bot.config.MAX_POSITION_PER_MARKET", 500.0):
        result = await execute_arb(opp, kalshi, poly, _risk(), dry_run=True)

    assert result is not None
    assert result.k_contracts >= 1


@pytest.mark.asyncio
async def test_position_capped_at_max_per_market():
    """opp.max_size_usdc is capped by MAX_POSITION_PER_MARKET."""
    opp = _opp(k_price=0.50, p_price=0.45, budget=10_000.0)

    kalshi = MagicMock()
    kalshi.place_order = AsyncMock(return_value={"order_id": "k1"})
    poly = MagicMock()
    poly.place_market_order = AsyncMock(return_value={"id": "p1"})

    with patch("arb_bot.config.MAX_POSITION_PER_MARKET", 100.0):
        result = await execute_arb(opp, kalshi, poly, _risk(), dry_run=False)

    assert result is not None
    # Capped budget = $100, so N = int(100/0.95) = 105
    expected_n = int(100.0 / 0.95)
    assert result.k_contracts == expected_n
    assert result.kalshi_spend + result.poly_spend <= 100.0 + 1e-6


@pytest.mark.asyncio
async def test_stale_opportunity_skipped():
    """An opportunity older than _MAX_OPP_AGE_SECS returns None (no trade)."""
    from datetime import timedelta

    opp = _opp(k_price=0.65, p_price=0.30, budget=100.0)
    opp.timestamp = datetime.utcnow() - timedelta(seconds=10)  # stale

    kalshi = MagicMock()
    kalshi.place_order = AsyncMock()
    poly = MagicMock()
    poly.place_market_order = AsyncMock()

    result = await execute_arb(opp, kalshi, poly, _risk(), dry_run=False)

    assert result is None
    kalshi.place_order.assert_not_awaited()
    poly.place_market_order.assert_not_awaited()
