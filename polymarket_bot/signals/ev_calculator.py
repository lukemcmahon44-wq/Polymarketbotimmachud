"""Expected Value calculator and mispricing signal generation."""

from __future__ import annotations

import structlog

from polymarket_bot.config.settings import BotConfig
from polymarket_bot.models.base import ModelEstimate
from polymarket_bot.types import Market, OrderBook, Side, Signal, SignalType

logger = structlog.get_logger(__name__)

POLYMARKET_FEE_RATE = 0.02  # 2% taker fee


def kelly_fraction(
    prob: float,
    price: float,
    kelly_f: float = 0.25,
) -> float:
    """Compute fractional Kelly bet size as a fraction of bankroll.

    For a binary outcome paying $1 per share:
      edge = prob - price
      odds = (1 - price) / price   (payout per dollar risked)
      kelly = edge / (1 - price)   (simplified for binary)

    Args:
        prob: Model probability of winning.
        price: Current market price (cost per share).
        kelly_f: Kelly fraction to apply (0.25 = quarter-Kelly).

    Returns:
        Fraction of bankroll to bet (0.0 to 1.0).
    """
    if price <= 0 or price >= 1:
        return 0.0
    if prob <= price:
        return 0.0  # No edge

    edge = prob - price
    # For prediction markets: odds = (1 - price)/price
    odds = (1 - price) / price
    full_kelly = edge / (1 - price)  # Equivalent: edge * (1 + odds) / odds = edge / price * (1/odds)
    # More intuitive form: f* = (p * (1+b) - 1) / b  where b = (1-price)/price
    # Simplified: f* = (prob - price) / (1 - price)
    return max(0.0, min(1.0, full_kelly * kelly_f))


class EVCalculator:
    """Generates mispricing signals by comparing model probabilities to market prices.

    For each outcome token in a market:
      EV = model_probability - market_price
      ev_pct = EV / market_price

    A positive EV means the model thinks the event is more likely than
    the market implies — a BUY signal.
    A negative EV means the market overprices the outcome — a SELL signal
    (buying NO / the complement).
    """

    def __init__(self, config: BotConfig) -> None:
        self._config = config

    def compute_signals(
        self,
        market: Market,
        estimates: list[ModelEstimate],
        order_books: dict[str, OrderBook] | None = None,
    ) -> list[Signal]:
        """Compute EV signals for a market given model estimates.

        Args:
            market: The prediction market.
            estimates: Model probability estimates per token.
            order_books: Optional order book data per token_id.

        Returns:
            List of signals that pass the EV threshold and confidence filter.
        """
        signals: list[Signal] = []
        estimate_map = {e.token_id: e for e in estimates}

        for token in market.tokens:
            est = estimate_map.get(token.token_id)
            if est is None:
                continue

            market_price = token.price
            if market_price <= 0 or market_price >= 1:
                continue

            # Determine side and EV
            ev = est.probability - market_price

            # Apply fee drag: the fee makes the effective break-even lower
            ev_after_fees = ev - POLYMARKET_FEE_RATE * market_price

            # Filter by minimum EV and confidence
            if abs(ev_after_fees) < self._config.ev_threshold:
                continue
            if est.confidence < self._config.min_confidence:
                continue

            # Determine trade side
            side = Side.BUY if ev > 0 else Side.SELL

            # Check liquidity
            book = (order_books or {}).get(token.token_id)
            liquidity = token.book_depth_usd
            if book:
                liquidity = sum(l.price * l.size for l in book.bids[:5]) + sum(
                    l.price * l.size for l in book.asks[:5]
                )

            if liquidity < self._config.risk.min_liquidity_usd:
                logger.debug(
                    "signal_skip_illiquid",
                    token_id=token.token_id,
                    liquidity=liquidity,
                    threshold=self._config.risk.min_liquidity_usd,
                )
                continue

            # Kelly-sized position
            kelly_f = kelly_fraction(
                prob=est.probability,
                price=market_price,
                kelly_f=self._config.risk.kelly_fraction,
            )
            bankroll = self._config.risk.global_exposure_usd
            raw_size_usd = kelly_f * bankroll
            size_usd = min(raw_size_usd, self._config.risk.per_trade_max_usd)

            signal = Signal(
                signal_type=SignalType.MISPRICING,
                market=market,
                token=token,
                side=side,
                model_probability=est.probability,
                market_price=market_price,
                expected_value=ev,
                confidence=est.confidence,
                liquidity_usd=liquidity,
                recommended_size_usd=size_usd,
                metadata={
                    "ev_after_fees": ev_after_fees,
                    "kelly_fraction": kelly_f,
                    "model": est.model_name,
                    "reasoning": est.reasoning,
                },
            )

            signals.append(signal)
            logger.info(
                "signal_generated",
                signal_id=signal.signal_id,
                market=market.question[:60],
                outcome=token.outcome,
                side=side.value,
                ev=round(ev, 4),
                ev_after_fees=round(ev_after_fees, 4),
                model_prob=round(est.probability, 4),
                market_price=round(market_price, 4),
                confidence=round(est.confidence, 3),
                size_usd=round(size_usd, 2),
            )

        # Sort by absolute EV after fees, descending
        signals.sort(key=lambda s: abs(s.metadata.get("ev_after_fees", 0)), reverse=True)
        return signals
