"""Arbitrage detection across correlated and mutually exclusive markets."""

from __future__ import annotations

from itertools import combinations

import structlog

from polymarket_bot.config import BotConfig
from polymarket_bot.types import Market, OrderBook, Side, Signal, SignalType

logger = structlog.get_logger()


class ArbitrageDetector:
    """Detects arbitrage opportunities across Polymarket markets.

    Types of arbitrage detected:
    1. Direct: YES + NO prices in same market sum to != 1.0
    2. Cross-market: mutually exclusive outcomes across related markets
    3. Hedged: correlated markets where combined position reduces risk
    """

    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self.fee_rate = config.backtest.fee_rate

    def detect_direct_arbitrage(self, markets: list[Market]) -> list[Signal]:
        """Detect when YES + NO prices don't sum to 1.0 within a market.

        If YES = 0.45 and NO = 0.50, total cost = 0.95, guaranteed profit = 0.05.
        After fees: profit = 0.05 - fee_rate.
        """
        signals: list[Signal] = []

        for market in markets:
            if len(market.tokens) < 2:
                continue

            # Sum all outcome prices
            total_price = sum(t.price for t in market.tokens)
            spread = 1.0 - total_price  # Positive = underpriced (buy all), Negative = overpriced

            # Account for fees
            profit_after_fees = abs(spread) - self.fee_rate

            if profit_after_fees <= self.config.min_ev_threshold:
                continue

            if spread > 0:
                # All outcomes are cheap — buy all, guaranteed $1 payout
                for token in market.tokens:
                    signals.append(Signal(
                        signal_type=SignalType.ARBITRAGE,
                        market_id=market.market_id,
                        token_id=token.token_id,
                        side=Side.BUY,
                        model_prob=token.price,
                        market_price=token.price,
                        ev=profit_after_fees / len(market.tokens),
                        confidence=0.95,
                        liquidity_usd=market.liquidity_usd,
                        metadata={
                            "arb_type": "direct_underpriced",
                            "total_price": total_price,
                            "spread": spread,
                            "profit_after_fees": profit_after_fees,
                            "question": market.question,
                        },
                    ))
            elif spread < -self.fee_rate:
                # All outcomes are expensive — sell all (or buy the cheapest NO)
                cheapest = min(market.tokens, key=lambda t: t.price)
                signals.append(Signal(
                    signal_type=SignalType.ARBITRAGE,
                    market_id=market.market_id,
                    token_id=cheapest.token_id,
                    side=Side.SELL,
                    model_prob=1.0 - cheapest.price,
                    market_price=cheapest.price,
                    ev=profit_after_fees,
                    confidence=0.90,
                    liquidity_usd=market.liquidity_usd,
                    metadata={
                        "arb_type": "direct_overpriced",
                        "total_price": total_price,
                        "spread": spread,
                        "profit_after_fees": profit_after_fees,
                        "question": market.question,
                    },
                ))

        logger.info("arbitrage.direct_scan", opportunities=len(signals))
        return signals

    def detect_cross_market_arbitrage(
        self,
        markets: list[Market],
        correlation_groups: dict[str, list[str]] | None = None,
    ) -> list[Signal]:
        """Detect arbitrage across mutually exclusive markets.

        Example: If Market A = "Will BTC > 100k by June?" and
                 Market B = "Will BTC > 90k by June?", then
                 P(A) should be <= P(B). If not, arbitrage exists.
        """
        signals: list[Signal] = []

        if not correlation_groups:
            # Auto-detect by category grouping
            by_category: dict[str, list[Market]] = {}
            for m in markets:
                by_category.setdefault(m.category, []).append(m)
            correlation_groups = {
                cat: [m.market_id for m in ms]
                for cat, ms in by_category.items()
                if len(ms) >= 2
            }

        market_map = {m.market_id: m for m in markets}

        for group_name, market_ids in correlation_groups.items():
            group_markets = [market_map[mid] for mid in market_ids if mid in market_map]
            if len(group_markets) < 2:
                continue

            # Check pairs for inconsistent pricing
            for m1, m2 in combinations(group_markets, 2):
                if not m1.tokens or not m2.tokens:
                    continue

                yes1 = m1.tokens[0].price
                yes2 = m2.tokens[0].price

                # Simple heuristic: if same-category markets have YES prices
                # that sum to > 1.0 and they're mutually exclusive
                combined = yes1 + yes2
                if combined > 1.0 + self.fee_rate:
                    profit = combined - 1.0 - self.fee_rate
                    if profit > self.config.min_ev_threshold:
                        # Sell YES on both (or buy NO on both)
                        for m, price in [(m1, yes1), (m2, yes2)]:
                            signals.append(Signal(
                                signal_type=SignalType.HEDGED_ARBITRAGE,
                                market_id=m.market_id,
                                token_id=m.tokens[0].token_id,
                                side=Side.SELL,
                                model_prob=1.0 - price,
                                market_price=price,
                                ev=profit / 2,
                                confidence=0.80,
                                liquidity_usd=min(m1.liquidity_usd, m2.liquidity_usd),
                                metadata={
                                    "arb_type": "cross_market",
                                    "group": group_name,
                                    "pair": [m1.market_id, m2.market_id],
                                    "combined_price": combined,
                                    "profit_after_fees": profit,
                                },
                            ))

        logger.info("arbitrage.cross_market_scan", opportunities=len(signals))
        return signals

    def detect_all(
        self,
        markets: list[Market],
        order_books: dict[str, OrderBook] | None = None,
    ) -> list[Signal]:
        """Run all arbitrage detection strategies."""
        signals: list[Signal] = []
        signals.extend(self.detect_direct_arbitrage(markets))
        signals.extend(self.detect_cross_market_arbitrage(markets))
        # Sort by EV
        signals.sort(key=lambda s: s.ev, reverse=True)
        return signals
