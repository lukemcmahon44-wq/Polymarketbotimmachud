"""Scans matched market pairs for profitable cross-platform arbitrage.

An arb exists when buying YES on one platform and NO on the other costs
less than $1.00, because exactly one outcome must resolve TRUE, paying $1.00.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from arb_bot.core.market_matcher import MarketPair
from arb_bot.utils.fee_calculator import calc_kalshi_fee, calc_polymarket_fee
from arb_bot.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class ArbOpportunity:
    pair: MarketPair
    kalshi_leg: str               # "YES" | "NO"
    kalshi_price: float
    polymarket_leg: str           # "YES" | "NO"
    polymarket_price: float
    gross_spread: float           # 1.0 - (kalshi_price + poly_price)
    kalshi_fee: float
    polymarket_fee: float
    net_edge: float               # gross_spread - total_fees
    max_size_usdc: float          # limited by available liquidity
    timestamp: datetime

    def summary(self) -> str:
        return (
            f"{self.pair.kalshi_title[:50]:50s} | "
            f"K:{self.kalshi_leg}@{self.kalshi_price:.3f} "
            f"P:{self.polymarket_leg}@{self.polymarket_price:.3f} | "
            f"edge={self.net_edge:.2%} size=${self.max_size_usdc:.0f}"
        )


class ArbDetector:
    def __init__(self, price_feed, min_edge: Optional[float] = None):
        self._feed = price_feed
        if min_edge is None:
            from arb_bot.config import MIN_EDGE_AFTER_FEES
            min_edge = MIN_EDGE_AFTER_FEES
        self._min_edge = min_edge

    def scan(self, pairs: list[MarketPair]) -> list[ArbOpportunity]:
        """Return all opportunities above the minimum edge threshold, sorted best-first."""
        opportunities: list[ArbOpportunity] = []

        for pair in pairs:
            if not pair.is_tradeable():
                continue

            k_yes = self._feed.kalshi_ask(pair.kalshi_ticker, "YES")
            k_no = self._feed.kalshi_ask(pair.kalshi_ticker, "NO")
            p_yes = self._feed.poly_ask(pair.polymarket_yes_token_id)
            p_no = self._feed.poly_ask(pair.polymarket_no_token_id)

            # Two possible arb directions:
            #   A) Buy Kalshi-YES + Buy Poly-NO
            #   B) Buy Kalshi-NO  + Buy Poly-YES
            for k_leg, k_price, p_leg, p_price in [
                ("YES", k_yes, "NO", p_no),
                ("NO", k_no, "YES", p_yes),
            ]:
                if k_price is None or p_price is None:
                    continue

                gross = 1.0 - (k_price + p_price)
                if gross <= 0:
                    continue

                k_fee = calc_kalshi_fee(k_price, k_leg)
                p_fee = calc_polymarket_fee(p_price, pair)
                net = gross - k_fee - p_fee

                if net < self._min_edge:
                    continue

                size = self._available_size(pair, k_leg, p_leg)
                if size <= 0:
                    continue

                opportunities.append(
                    ArbOpportunity(
                        pair=pair,
                        kalshi_leg=k_leg,
                        kalshi_price=k_price,
                        polymarket_leg=p_leg,
                        polymarket_price=p_price,
                        gross_spread=gross,
                        kalshi_fee=k_fee,
                        polymarket_fee=p_fee,
                        net_edge=net,
                        max_size_usdc=size,
                        timestamp=datetime.utcnow(),
                    )
                )

        opportunities.sort(key=lambda o: o.net_edge, reverse=True)
        if opportunities:
            logger.info(f"Found {len(opportunities)} arb opportunities; best edge={opportunities[0].net_edge:.2%}")
        return opportunities

    def _available_size(
        self,
        pair: MarketPair,
        k_leg: str,
        p_leg: str,
    ) -> float:
        """Estimate max tradeable size from cached order-book depth."""
        from arb_bot.config import MAX_POSITION_PER_MARKET, MIN_FILL_RATIO

        k_depth = self._feed.kalshi_depth(pair.kalshi_ticker, k_leg)
        p_token = (
            pair.polymarket_yes_token_id if p_leg == "YES" else pair.polymarket_no_token_id
        )
        p_depth = self._feed.poly_depth(p_token)

        # Use the minimum available liquidity on either side
        min_depth = min(k_depth, p_depth)

        # Apply fill-ratio guard: only use what we're confident will fill
        usable = min_depth * MIN_FILL_RATIO

        return min(usable, MAX_POSITION_PER_MARKET)
