"""Crypto spot-price momentum strategy.

This is the highest-ROI strategy identified in research: exploit the lag between
confirmed spot price movements on CEXs (Binance, Coinbase) and Polymarket's
crypto price markets (e.g., "Will BTC be above $X in 15 minutes?").

Key insight from research:
- A bot turned $313 into $414,000 in one month using this strategy
- 98% win rate by exploiting a tiny window where Polymarket prices lag
  confirmed spot momentum on exchanges
- Average opportunity window: ~2.7 seconds for pure arb, but momentum
  signals can persist for 30-120 seconds

This module provides:
1. A mock spot price feed (replace with real Binance/Coinbase WebSocket)
2. Momentum detection (short-term trend confirmation)
3. Signal generation when momentum diverges from Polymarket price
"""

from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

import structlog

from polymarket_bot.models.base import ModelEstimate, ProbabilityModel
from polymarket_bot.types import Market

logger = structlog.get_logger()


@dataclass
class PricePoint:
    price: float
    timestamp: float  # monotonic time


class SpotPriceFeed(Protocol):
    """Interface for real-time spot price data."""

    async def get_price(self, symbol: str) -> float: ...
    async def get_price_history(self, symbol: str, lookback_seconds: int) -> list[PricePoint]: ...


class MockSpotFeed:
    """Simulated spot price feed for backtesting.

    In production, replace with:
    - Binance WebSocket: wss://stream.binance.com:9443/ws/btcusdt@trade
    - Coinbase WebSocket: wss://ws-feed.exchange.coinbase.com
    """

    def __init__(self, seed: int = 42) -> None:
        import random
        self.rng = random.Random(seed)
        self._prices: dict[str, deque[PricePoint]] = {}
        self._base_prices: dict[str, float] = {
            "BTC": 98_500.0,
            "ETH": 3_800.0,
            "SOL": 185.0,
            "MATIC": 0.85,
        }

    async def get_price(self, symbol: str) -> float:
        base = self._base_prices.get(symbol, 100.0)
        # Simulate small random walk
        noise = self.rng.gauss(0, base * 0.001)
        price = base + noise
        self._base_prices[symbol] = price

        history = self._prices.setdefault(symbol, deque(maxlen=500))
        history.append(PricePoint(price=price, timestamp=time.monotonic()))
        return price

    async def get_price_history(
        self, symbol: str, lookback_seconds: int = 300
    ) -> list[PricePoint]:
        history = self._prices.get(symbol, deque())
        cutoff = time.monotonic() - lookback_seconds
        return [p for p in history if p.timestamp >= cutoff]


@dataclass
class MomentumSignal:
    """A detected momentum signal from spot price analysis."""
    symbol: str
    direction: str  # "up" or "down"
    magnitude_pct: float  # How much the price moved
    confidence: float  # 0-1 based on signal strength
    lookback_seconds: int
    current_price: float
    start_price: float
    num_confirmations: int  # How many sub-windows confirm the trend


class CryptoMomentumModel(ProbabilityModel):
    """Detects crypto spot price momentum and translates to prediction market signals.

    Strategy:
    1. Monitor spot prices over multiple timeframes (15s, 60s, 300s)
    2. Detect confirmed momentum (price moving consistently in one direction)
    3. Compare to Polymarket's crypto price markets
    4. If Polymarket hasn't adjusted, generate a high-confidence signal

    Example:
    - BTC spot rises 0.5% in 60s on Binance (confirmed uptrend)
    - Polymarket "Will BTC > $99k?" still at 0.55
    - Model estimates true probability at 0.72 based on momentum
    - Signal: BUY YES with EV = 0.72 - 0.55 = 0.17
    """

    def __init__(
        self,
        spot_feed: SpotPriceFeed | None = None,
        momentum_threshold_pct: float = 0.002,  # 0.2% minimum momentum
        lookback_windows: list[int] | None = None,
        confirmation_required: int = 2,  # Minimum windows confirming trend
    ) -> None:
        self.spot_feed = spot_feed or MockSpotFeed()
        self.momentum_threshold = momentum_threshold_pct
        self.lookback_windows = lookback_windows or [15, 60, 300]
        self.confirmation_required = confirmation_required
        self._momentum_cache: dict[str, MomentumSignal] = {}

    @property
    def name(self) -> str:
        return "crypto_momentum"

    def _extract_symbol_and_threshold(self, question: str) -> tuple[str | None, float | None]:
        """Parse market question to extract crypto symbol and price threshold.

        Examples:
        - "Will BTC exceed $100k by end of month?" → ("BTC", 100000)
        - "Will ETH reach $5k this quarter?" → ("ETH", 5000)
        - "Will SOL break $200 this week?" → ("SOL", 200)
        """
        question_upper = question.upper()
        symbols = ["BTC", "ETH", "SOL", "MATIC", "BITCOIN", "ETHEREUM", "SOLANA"]
        found_symbol: str | None = None

        for sym in symbols:
            if sym in question_upper:
                # Normalize aliases
                if sym == "BITCOIN":
                    found_symbol = "BTC"
                elif sym == "ETHEREUM":
                    found_symbol = "ETH"
                elif sym == "SOLANA":
                    found_symbol = "SOL"
                else:
                    found_symbol = sym
                break

        if not found_symbol:
            return None, None

        # Extract dollar amount
        import re
        # Match patterns like $100k, $100,000, $5k, $200
        patterns = [
            r'\$(\d+(?:,\d{3})*(?:\.\d+)?)\s*k\b',  # $100k
            r'\$(\d+(?:,\d{3})*(?:\.\d+)?)\b',        # $100,000 or $200
        ]
        for pattern in patterns:
            match = re.search(pattern, question, re.IGNORECASE)
            if match:
                value_str = match.group(1).replace(",", "")
                value = float(value_str)
                if "k" in question[match.end() - 1:match.end() + 1].lower():
                    value *= 1000
                elif value < 1000 and found_symbol in ("BTC", "ETH"):
                    # Likely shorthand: $100 for BTC means $100k
                    value *= 1000
                return found_symbol, value

        return found_symbol, None

    async def _detect_momentum(self, symbol: str) -> MomentumSignal | None:
        """Analyze spot price across multiple timeframes for momentum."""
        current_price = await self.spot_feed.get_price(symbol)
        confirmations = 0
        total_magnitude = 0.0

        for window in self.lookback_windows:
            history = await self.spot_feed.get_price_history(symbol, window)
            if len(history) < 2:
                continue

            start_price = history[0].price
            pct_change = (current_price - start_price) / start_price

            if abs(pct_change) >= self.momentum_threshold:
                confirmations += 1
                total_magnitude += pct_change

        if confirmations < self.confirmation_required:
            return None

        avg_magnitude = total_magnitude / confirmations
        direction = "up" if avg_magnitude > 0 else "down"

        # Confidence scales with number of confirming windows and magnitude
        conf_from_windows = min(1.0, confirmations / len(self.lookback_windows))
        conf_from_magnitude = min(1.0, abs(avg_magnitude) / 0.01)  # 1% = max confidence
        confidence = 0.5 * conf_from_windows + 0.5 * conf_from_magnitude
        confidence = min(0.95, max(0.3, confidence))

        history_all = await self.spot_feed.get_price_history(symbol, max(self.lookback_windows))
        start = history_all[0].price if history_all else current_price

        signal = MomentumSignal(
            symbol=symbol,
            direction=direction,
            magnitude_pct=abs(avg_magnitude),
            confidence=confidence,
            lookback_seconds=max(self.lookback_windows),
            current_price=current_price,
            start_price=start,
            num_confirmations=confirmations,
        )
        self._momentum_cache[symbol] = signal
        return signal

    async def estimate(self, market: Market) -> list[ModelEstimate]:
        """Estimate probability based on crypto spot price momentum."""
        estimates: list[ModelEstimate] = []

        # Check if this is a crypto price market
        symbol, threshold = self._extract_symbol_and_threshold(market.question)
        if not symbol or not threshold:
            # Not a crypto price market — fall back to market price
            for token in market.tokens:
                estimates.append(ModelEstimate(
                    model_name=self.name,
                    market_id=market.market_id,
                    token_id=token.token_id,
                    outcome=token.outcome,
                    probability=token.price,
                    confidence=0.1,
                    reasoning="Not a crypto price market — no momentum signal",
                ))
            return estimates

        # Detect momentum
        momentum = await self._detect_momentum(symbol)

        if not momentum:
            # No momentum detected — use market price
            for token in market.tokens:
                estimates.append(ModelEstimate(
                    model_name=self.name,
                    market_id=market.market_id,
                    token_id=token.token_id,
                    outcome=token.outcome,
                    probability=token.price,
                    confidence=0.2,
                    reasoning=f"No confirmed momentum for {symbol}",
                ))
            return estimates

        # Translate momentum to probability adjustment
        # If price is moving toward the threshold, YES probability increases
        distance_to_threshold = (threshold - momentum.current_price) / threshold
        momentum_toward_threshold = (
            (momentum.direction == "up" and distance_to_threshold > 0)
            or (momentum.direction == "down" and distance_to_threshold < 0)
        )

        yes_token = next((t for t in market.tokens if t.outcome.lower() == "yes"), None)
        no_token = next((t for t in market.tokens if t.outcome.lower() == "no"), None)

        if not yes_token or not no_token:
            return estimates

        # Base probability from market
        base_yes = yes_token.price

        # Adjustment based on momentum
        if momentum_toward_threshold:
            # Price moving toward threshold — increase YES
            adjustment = momentum.magnitude_pct * 5  # Amplify small movements
            adjustment = min(0.25, adjustment)  # Cap at 25% adjustment
            model_yes = min(0.98, base_yes + adjustment)
        else:
            # Price moving away from threshold — decrease YES
            adjustment = momentum.magnitude_pct * 5
            adjustment = min(0.25, adjustment)
            model_yes = max(0.02, base_yes - adjustment)

        model_no = 1.0 - model_yes

        reasoning = (
            f"Momentum {momentum.direction} {momentum.magnitude_pct:.3%} "
            f"({momentum.num_confirmations} windows), "
            f"spot=${momentum.current_price:,.0f}, threshold=${threshold:,.0f}, "
            f"{'toward' if momentum_toward_threshold else 'away from'} threshold"
        )

        estimates.append(ModelEstimate(
            model_name=self.name,
            market_id=market.market_id,
            token_id=yes_token.token_id,
            outcome="Yes",
            probability=model_yes,
            confidence=momentum.confidence,
            reasoning=reasoning,
            features={
                "symbol": symbol,
                "threshold": threshold,
                "spot_price": momentum.current_price,
                "momentum_direction": momentum.direction,
                "momentum_magnitude": momentum.magnitude_pct,
                "confirmations": momentum.num_confirmations,
            },
        ))
        estimates.append(ModelEstimate(
            model_name=self.name,
            market_id=market.market_id,
            token_id=no_token.token_id,
            outcome="No",
            probability=model_no,
            confidence=momentum.confidence,
            reasoning=reasoning,
        ))

        logger.info(
            "crypto_momentum.signal",
            symbol=symbol,
            direction=momentum.direction,
            magnitude=f"{momentum.magnitude_pct:.3%}",
            model_yes=f"{model_yes:.3f}",
            market_yes=f"{base_yes:.3f}",
            edge=f"{model_yes - base_yes:+.3f}",
        )

        return estimates
