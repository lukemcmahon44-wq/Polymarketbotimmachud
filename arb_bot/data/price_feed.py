"""Unified real-time price cache fed by WebSocket streams from both platforms.

The PriceFeed starts both WebSocket connections concurrently and exposes a
simple synchronous interface to the ArbDetector so the hot path never awaits.
"""

import asyncio
import time
from typing import Optional

from arb_bot.core.market_matcher import MarketPair
from arb_bot.utils.logger import get_logger

logger = get_logger(__name__)

# How old a price can be before we consider it stale (seconds)
_STALE_THRESHOLD = 30.0

# Assumed book depth (USDC) when we have no order-book data yet
_DEFAULT_DEPTH = 50.0


class PriceFeed:
    def __init__(self, kalshi_client, poly_client, pairs: list[MarketPair]):
        self._k = kalshi_client
        self._p = poly_client
        self._pairs = pairs

    async def start(self) -> None:
        """Launch both WebSocket feeds concurrently. Runs indefinitely."""
        k_tickers = [pair.kalshi_ticker for pair in self._pairs]
        p_tokens = []
        for pair in self._pairs:
            if pair.polymarket_yes_token_id:
                p_tokens.append(pair.polymarket_yes_token_id)
            if pair.polymarket_no_token_id:
                p_tokens.append(pair.polymarket_no_token_id)

        # Remove duplicates while preserving order
        p_tokens = list(dict.fromkeys(p_tokens))

        logger.info(f"PriceFeed starting: {len(k_tickers)} Kalshi tickers, {len(p_tokens)} Poly tokens")

        await asyncio.gather(
            self._k.subscribe_tickers(k_tickers),
            self._p.subscribe_tokens(p_tokens),
        )

    # ------------------------------------------------------------------
    # Synchronous accessors used by ArbDetector (no await needed)
    # ------------------------------------------------------------------

    def kalshi_ask(self, ticker: str, side: str) -> Optional[float]:
        """Best ask price for buying YES or NO on Kalshi."""
        cached = self._k.price_cache.get(ticker)
        if cached is None:
            return None
        if time.time() - cached.updated_at > _STALE_THRESHOLD:
            return None
        return cached.yes_ask if side.upper() == "YES" else cached.no_ask

    def poly_ask(self, token_id: str) -> Optional[float]:
        """Best ask price for buying this token on Polymarket."""
        if not token_id:
            return None
        cached = self._p.price_cache.get(token_id)
        if cached is None:
            return None
        if time.time() - cached.updated_at > _STALE_THRESHOLD:
            return None
        return cached.best_ask

    def kalshi_depth(self, ticker: str, side: str) -> float:
        """Approximate USDC available at best ask on Kalshi (fallback: default)."""
        # Full order-book depth requires a separate REST call; we approximate from
        # the WebSocket ticker. If not available fall back to a conservative default.
        return _DEFAULT_DEPTH

    def poly_depth(self, token_id: str) -> float:
        """Approximate USDC available at best ask on Polymarket."""
        return _DEFAULT_DEPTH

    def snapshot(self) -> dict:
        """Return a JSON-serialisable snapshot of all cached prices for the dashboard."""
        kalshi_data = [
            {
                "ticker": ticker,
                "yes_ask": p.yes_ask,
                "no_ask": p.no_ask,
                "updated_at": p.updated_at,
            }
            for ticker, p in self._k.price_cache.items()
        ]
        poly_data = [
            {
                "token_id": token_id,
                "best_ask": p.best_ask,
                "best_bid": p.best_bid,
                "updated_at": p.updated_at,
            }
            for token_id, p in self._p.price_cache.items()
        ]
        return {"kalshi": kalshi_data, "polymarket": poly_data}
