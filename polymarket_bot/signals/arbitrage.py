"""Arbitrage detection: complement, triangular, and cross-market hedged arb."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog

from polymarket_bot.config.settings import BotConfig
from polymarket_bot.types import Market, OrderBook, Side, Signal, SignalType

logger = structlog.get_logger(__name__)

POLYMARKET_FEE_RATE = 0.02


@dataclass
class ArbitrageOpportunity:
    """A detected arbitrage opportunity."""

    arb_type: str  # "complement", "triangular", "hedged"
    legs: list[dict[str, Any]] = field(default_factory=list)
    gross_profit_usd: float = 0.0
    net_profit_usd: float = 0.0  # After fees
    required_capital_usd: float = 0.0
    roi_pct: float = 0.0
    description: str = ""


class ArbitrageDetector:
    """Detects arbitrage opportunities across Polymarket markets.

    Strategy 1 — Complement Arbitrage:
      For a binary YES/NO market, YES + NO must = $1.00 at resolution.
      If YES_price + NO_price < 1.0, buy both sides to lock in profit.
      If YES_price + NO_price > 1.0, sell both (or buy the cheaper combo).

    Strategy 2 — Mutually Exclusive Exhaustive (MEE) Arbitrage:
      For markets with multiple outcomes that must sum to exactly one winner,
      if the sum of all cheapest prices < $1.00 there is a direct arb.

    Strategy 3 — Hedged Cross-Market Arbitrage:
      Two correlated markets that should move together but have diverged.
      E.g., "BTC > $100k by Dec" on PM vs a similar contract elsewhere.
    """

    def __init__(self, config: BotConfig) -> None:
        self._config = config

    def detect_complement_arb(
        self,
        market: Market,
        order_books: dict[str, OrderBook],
    ) -> ArbitrageOpportunity | None:
        """Detect complement arbitrage in a binary YES/NO market."""
        if not market.is_binary or len(market.tokens) != 2:
            return None

        yes_token = market.tokens[0]
        no_token = market.tokens[1]

        yes_book = order_books.get(yes_token.token_id)
        no_book = order_books.get(no_token.token_id)

        if yes_book is None or no_book is None:
            return None

        # Best ask = cheapest price to buy
        yes_ask = yes_book.best_ask
        no_ask = no_book.best_ask

        if yes_ask <= 0 or no_ask <= 0:
            return None

        combined_cost = yes_ask + no_ask

        if combined_cost < 1.0:
            gross_profit = 1.0 - combined_cost
            fee_cost = (yes_ask + no_ask) * POLYMARKET_FEE_RATE
            net_profit = gross_profit - fee_cost

            if net_profit <= 0:
                return None

            # Size by minimum of available liquidity on each side
            yes_size = min(l.size for l in yes_book.asks[:3]) if yes_book.asks else 0
            no_size = min(l.size for l in no_book.asks[:3]) if no_book.asks else 0
            max_shares = min(yes_size, no_size)
            if max_shares <= 0:
                return None

            capital = combined_cost * max_shares
            # Cap at per_trade_max
            capital = min(capital, self._config.risk.per_trade_max_usd)
            shares = capital / combined_cost
            total_net = net_profit * shares

            opp = ArbitrageOpportunity(
                arb_type="complement",
                gross_profit_usd=gross_profit * shares,
                net_profit_usd=total_net,
                required_capital_usd=capital,
                roi_pct=(total_net / capital * 100) if capital > 0 else 0,
                description=(
                    f"Buy YES@{yes_ask:.3f} + NO@{no_ask:.3f} = {combined_cost:.3f} "
                    f"(profit {gross_profit:.3f}/share)"
                ),
                legs=[
                    {
                        "token_id": yes_token.token_id,
                        "outcome": yes_token.outcome,
                        "side": Side.BUY,
                        "price": yes_ask,
                        "size_shares": shares,
                        "size_usd": yes_ask * shares,
                    },
                    {
                        "token_id": no_token.token_id,
                        "outcome": no_token.outcome,
                        "side": Side.BUY,
                        "price": no_ask,
                        "size_shares": shares,
                        "size_usd": no_ask * shares,
                    },
                ],
            )

            logger.info(
                "complement_arb_found",
                market=market.question[:60],
                yes_ask=yes_ask,
                no_ask=no_ask,
                combined=combined_cost,
                net_profit_per_share=net_profit,
                roi_pct=round(opp.roi_pct, 2),
            )
            return opp

        return None

    def detect_mee_arb(
        self,
        market: Market,
        order_books: dict[str, OrderBook],
    ) -> ArbitrageOpportunity | None:
        """Detect mutually exclusive exhaustive arbitrage (multi-outcome markets).

        If the sum of cheapest prices across all outcomes < 1.0 AND those
        outcomes are mutually exclusive and exhaustive, buying all is risk-free.
        """
        if len(market.tokens) < 3:
            return None  # Use complement_arb for binary markets

        asks: list[tuple[str, str, float, float]] = []
        for token in market.tokens:
            book = order_books.get(token.token_id)
            if book and book.asks:
                asks.append((token.token_id, token.outcome, book.best_ask,
                              book.asks[0].size if book.asks else 0))
            else:
                return None  # Can't compute without all books

        total_cost = sum(a[2] for a in asks)
        if total_cost >= 1.0:
            return None

        gross_profit = 1.0 - total_cost
        fee_cost = total_cost * POLYMARKET_FEE_RATE
        net_profit = gross_profit - fee_cost

        if net_profit <= 0:
            return None

        min_size = min(a[3] for a in asks)
        capital = total_cost * min_size
        capital = min(capital, self._config.risk.per_trade_max_usd)
        shares = capital / total_cost if total_cost > 0 else 0

        return ArbitrageOpportunity(
            arb_type="mee",
            gross_profit_usd=gross_profit * shares,
            net_profit_usd=net_profit * shares,
            required_capital_usd=capital,
            roi_pct=(net_profit / total_cost * 100),
            description=f"Buy all {len(asks)} outcomes @ total={total_cost:.3f}",
            legs=[
                {
                    "token_id": tid,
                    "outcome": outcome,
                    "side": Side.BUY,
                    "price": ask,
                    "size_shares": shares,
                    "size_usd": ask * shares,
                }
                for tid, outcome, ask, _ in asks
            ],
        )

    def detect_correlated_hedged_arb(
        self,
        markets: list[Market],
        order_books: dict[str, OrderBook],
    ) -> list[ArbitrageOpportunity]:
        """Detect hedged arbitrage between correlated markets.

        Looks for pairs of markets where:
        - They cover the same underlying event (question similarity)
        - Their implied probabilities have diverged beyond a threshold
        """
        opportunities: list[ArbitrageOpportunity] = []

        for i, m1 in enumerate(markets):
            for m2 in markets[i + 1:]:
                if not (m1.is_binary and m2.is_binary):
                    continue
                if not m1.tokens or not m2.tokens:
                    continue

                # Simple heuristic: same category + similar probability range
                if m1.category != m2.category:
                    continue

                yes1_price = m1.tokens[0].price
                yes2_price = m2.tokens[0].price

                divergence = abs(yes1_price - yes2_price)
                if divergence < 0.05:  # Less than 5% divergence, not interesting
                    continue

                # One market says higher probability — simple signal, not pure arb
                # Real cross-market arb would require correlated settlement
                if divergence > 0.15:
                    logger.debug(
                        "correlated_divergence",
                        m1=m1.question[:40],
                        m2=m2.question[:40],
                        divergence=round(divergence, 3),
                    )

        return opportunities

    def scan(
        self,
        markets: list[Market],
        order_books: dict[str, OrderBook],
    ) -> list[Signal]:
        """Scan all markets for arbitrage and return as signals."""
        signals: list[Signal] = []

        for market in markets:
            # Complement arb
            opp = self.detect_complement_arb(market, order_books)
            if opp and opp.net_profit_usd > 0:
                signal = self._arb_to_signal(market, opp)
                if signal:
                    signals.append(signal)

            # MEE arb
            mee_opp = self.detect_mee_arb(market, order_books)
            if mee_opp and mee_opp.net_profit_usd > 0:
                signal = self._arb_to_signal(market, mee_opp)
                if signal:
                    signals.append(signal)

        # Correlated / hedged arb
        self.detect_correlated_hedged_arb(markets, order_books)

        return signals

    def _arb_to_signal(
        self,
        market: Market,
        opp: ArbitrageOpportunity,
    ) -> Signal | None:
        """Convert an ArbitrageOpportunity into a Signal for the execution pipeline."""
        if not opp.legs:
            return None

        first_leg = opp.legs[0]
        token = next(
            (t for t in market.tokens if t.token_id == first_leg["token_id"]), None
        )
        if token is None:
            return None

        return Signal(
            signal_type=SignalType.ARBITRAGE,
            market=market,
            token=token,
            side=first_leg["side"],
            model_probability=1.0 - opp.required_capital_usd,  # pseudo
            market_price=first_leg["price"],
            expected_value=opp.net_profit_usd,
            confidence=0.9,  # High confidence for pure arb
            liquidity_usd=market.liquidity_usd,
            recommended_size_usd=opp.required_capital_usd,
            metadata={
                "arb_type": opp.arb_type,
                "legs": opp.legs,
                "roi_pct": opp.roi_pct,
                "description": opp.description,
                "ev_after_fees": opp.net_profit_usd,
            },
        )
