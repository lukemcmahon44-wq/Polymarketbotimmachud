"""Expected-value signal detection from model vs market mispricing."""

from __future__ import annotations

import structlog

from polymarket_bot.config import BotConfig
from polymarket_bot.models.base import ModelEstimate
from polymarket_bot.types import Market, OrderBook, Side, Signal, SignalType

logger = structlog.get_logger()

# Polymarket fee rate (2% on winnings)
DEFAULT_FEE_RATE = 0.02


class EVCalculator:
    """Detects mispricing signals by comparing model probability to market price.

    EV calculation:
      - For BUY YES at price p with model probability q:
          EV = q * (1 - p) * (1 - fee) - (1 - q) * p
      - Simplified: EV ≈ model_prob - market_price (for quick screening)
      - Full: accounts for fees, slippage, and order book depth.
    """

    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self.fee_rate = config.backtest.fee_rate

    def compute_ev(
        self,
        model_prob: float,
        market_price: float,
        fee_rate: float | None = None,
    ) -> float:
        """Compute full expected value for buying YES at market_price.

        EV = prob * (1 - price) * (1 - fee) - (1 - prob) * price
        """
        fee = fee_rate if fee_rate is not None else self.fee_rate
        ev = model_prob * (1.0 - market_price) * (1.0 - fee) - (1.0 - model_prob) * market_price
        return ev

    def generate_signals(
        self,
        markets: list[Market],
        estimates: list[list[ModelEstimate]],
        order_books: dict[str, OrderBook] | None = None,
    ) -> list[Signal]:
        """Generate ranked signals from model estimates vs market prices.

        Returns signals sorted by EV descending, filtered by thresholds.
        """
        signals: list[Signal] = []

        for market, market_estimates in zip(markets, estimates):
            for est in market_estimates:
                # Find corresponding token
                token = next(
                    (t for t in market.tokens if t.token_id == est.token_id), None
                )
                if not token:
                    continue

                market_price = token.price

                # Check both directions: buy YES and buy NO
                # Buy YES: profitable if model_prob > market_price
                ev_yes = self.compute_ev(est.probability, market_price)
                # Buy NO: profitable if (1 - model_prob) > (1 - market_price)
                ev_no = self.compute_ev(1.0 - est.probability, 1.0 - market_price)

                # Determine best direction
                if ev_yes > ev_no and ev_yes > self.config.min_ev_threshold:
                    side = Side.BUY
                    ev = ev_yes
                    effective_price = market_price
                elif ev_no > self.config.min_ev_threshold:
                    side = Side.SELL
                    ev = ev_no
                    effective_price = 1.0 - market_price
                else:
                    continue

                # Confidence filter
                if est.confidence < self.config.confidence_floor:
                    continue

                # Liquidity filter
                if market.liquidity_usd < self.config.min_liquidity_usd:
                    continue

                # Adjust EV for order book depth if available
                book_liquidity = market.liquidity_usd
                if order_books and est.token_id in order_books:
                    book = order_books[est.token_id]
                    if side == Side.BUY and book.asks:
                        effective_price = book.best_ask
                        book_liquidity = sum(a.size for a in book.asks[:5])
                    elif side == Side.SELL and book.bids:
                        effective_price = book.best_bid
                        book_liquidity = sum(b.size for b in book.bids[:5])

                signals.append(Signal(
                    signal_type=SignalType.MISPRICING,
                    market_id=market.market_id,
                    token_id=est.token_id,
                    side=side,
                    model_prob=est.probability,
                    market_price=market_price,
                    ev=ev,
                    confidence=est.confidence,
                    liquidity_usd=book_liquidity,
                    metadata={
                        "outcome": est.outcome,
                        "question": market.question,
                        "model": est.model_name,
                        "reasoning": est.reasoning,
                        "effective_price": effective_price,
                    },
                ))

        # Sort by EV descending
        signals.sort(key=lambda s: s.ev, reverse=True)

        logger.info(
            "ev_calculator.signals_generated",
            total_signals=len(signals),
            top_ev=round(signals[0].ev, 4) if signals else 0,
        )
        return signals
