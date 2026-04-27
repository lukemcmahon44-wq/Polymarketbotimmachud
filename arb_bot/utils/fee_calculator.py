"""Fee-adjusted edge computation for Kalshi and Polymarket.

Always use taker fees for arb sizing — under time pressure you are always
crossing the spread and will never get maker rebates.
"""

from typing import Optional


def calc_kalshi_fee(
    price: float,
    side: str,
    fee_rate: Optional[float] = None,
) -> float:
    """
    Kalshi charges a fee on the winning side.

    Approximation (as of 2024):
        fee = fee_rate * (1 - price) * notional

    For a $1 notional contract bought at `price`:
        winning_payout = $1
        fee ≈ fee_rate * (1 - price)

    fee_rate defaults to config.KALSHI_FEE_RATE (~7%).
    """
    if fee_rate is None:
        from arb_bot.config import KALSHI_FEE_RATE
        fee_rate = KALSHI_FEE_RATE

    # Fee is applied to the potential profit on each $1 contract
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
        Crypto    1.80%
        Politics  1.00%
        Finance   1.00%
        Sports    0.75%
        Economics 1.50%
        Default   1.25%

    For a position bought at `price`, spending $1 of USDC, fee = rate * 1.0
    (i.e. fee is on the USDC notional, not the potential profit).
    """
    if fee_rate is not None:
        rate = fee_rate
    else:
        from arb_bot.config import POLYMARKET_FEES
        rate = POLYMARKET_FEES["default"]

        if category is not None:
            rate = POLYMARKET_FEES.get(category.lower(), POLYMARKET_FEES["default"])
        elif pair is not None:
            # Try to infer category from the market titles
            title = (pair.polymarket_title + " " + pair.kalshi_title).lower()
            for cat in ("crypto", "politics", "sports", "finance", "economics"):
                if cat in title:
                    rate = POLYMARKET_FEES[cat]
                    break

    # Fee is on the USDC spent (price), not on the potential $1 payout
    return rate * price


def net_edge_estimate(
    kalshi_price: float,
    poly_price: float,
    kalshi_side: str,
    poly_pair=None,
    poly_category: Optional[str] = None,
) -> dict:
    """Full edge calculation; returns a dict with all fee components."""
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
