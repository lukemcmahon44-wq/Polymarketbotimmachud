"""Fee-adjusted edge computation for Kalshi and Polymarket.

Always use taker fees — under time pressure you cross the spread and
will never earn maker rebates.

With MARKET_CATEGORIES=sports the Polymarket taker fee is 0.75%, which
is the lowest of all categories and is used as the default here.
"""

from typing import Optional


def calc_kalshi_fee(
    price: float,
    side: str,
    fee_rate: Optional[float] = None,
) -> float:
    """
    Kalshi fee on the winning side.

    fee = fee_rate * (1 - price)   [per $1 notional contract]

    fee_rate defaults to config.KALSHI_FEE_RATE (~7%).
    """
    if fee_rate is None:
        from arb_bot.config import KALSHI_FEE_RATE
        fee_rate = KALSHI_FEE_RATE
    return fee_rate * (1.0 - price)


def calc_polymarket_fee(
    price: float,
    pair=None,
    category: Optional[str] = None,
    fee_rate: Optional[float] = None,
) -> float:
    """
    Polymarket taker fee as a fraction of USDC spent.

    Current rates (March 2026):
        Sports    0.75%  <-- default when MARKET_CATEGORIES=sports
        Politics  1.00%
        Finance   1.00%
        Economics 1.50%
        Crypto    1.80%
        Default   1.25%

    Fee is on the USDC notional (price), not on the $1 payout.
    """
    if fee_rate is not None:
        rate = fee_rate
    else:
        from arb_bot.config import POLYMARKET_FEES, POLYMARKET_DEFAULT_FEE
        rate = POLYMARKET_DEFAULT_FEE  # already resolved to sports rate if configured

        if category is not None:
            rate = POLYMARKET_FEES.get(category.lower(), POLYMARKET_DEFAULT_FEE)
        elif pair is not None:
            title = (pair.polymarket_title + " " + pair.kalshi_title).lower()
            for cat in ("crypto", "politics", "sports", "finance", "economics"):
                if cat in title:
                    rate = POLYMARKET_FEES[cat]
                    break

    return rate * price


def net_edge_estimate(
    kalshi_price: float,
    poly_price: float,
    kalshi_side: str,
    poly_pair=None,
    poly_category: Optional[str] = None,
) -> dict:
    """Full breakdown; useful for logging and tests."""
    gross = 1.0 - (kalshi_price + poly_price)
    k_fee = calc_kalshi_fee(kalshi_price, kalshi_side)
    p_fee = calc_polymarket_fee(poly_price, pair=poly_pair, category=poly_category)
    net = gross - k_fee - p_fee
    return {
        "gross_spread": gross,
        "kalshi_fee": k_fee,
        "polymarket_fee": p_fee,
        "total_fees": k_fee + p_fee,
        "net_edge": net,
        "profitable": net > 0,
    }
