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
        self._changed: Optional[asyncio.Event] = None

    async def start(self) -> None:
        """Launch both WebSocket feeds concurrently. Runs indefinitely."""
        # Event is created here so it belongs to the running event loop
        self._changed = asyncio.Event()

        # Wire price-update callbacks so every incoming WS tick sets the event
        def _notify() -> None:
            if self._changed is not None:
                self._changed.set()

        self._k.on_price_update = _notify
        self._p.on_price_update = _notify

        k_tickers = [pair.kalshi_ticker for pair in self._pairs]
        p_tokens = []
        for pair in self._pairs:
            if pair.polymarket_yes_token_id:
                p_tokens.append(pair.polymarket_yes_token_id)
            if pair.polymarket_no_token_id:
                p_tokens.append(pair.polymarket_no_token_id)

        p_tokens = list(dict.fromkeys(p_tokens))

        logger.info(f"PriceFeed starting: {len(k_tickers)} Kalshi tickers, {len(p_tokens)} Poly tokens")

        await asyncio.gather(
            self._k.subscribe_tickers(k_tickers),
            self._p.subscribe_tokens(p_tokens),
        )

    async def wait_for_price_change(self, timeout: float = 2.0) -> bool:
        """Block until any WS price update arrives, or timeout expires.

        Returns True if a price change occurred, False on timeout.
        Reduces scan latency from ~2000ms polling to ~10ms event-driven.
        """
        if self._changed is None:
            await asyncio.sleep(timeout)
            return False
        self._changed.clear()
        try:
            await asyncio.wait_for(self._changed.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # ------------------------------------------------------------------
    # Price age helpers
    # ------------------------------------------------------------------

    def kalshi_price_age(self, ticker: str) -> float:
        """Seconds since the last Kalshi price update for this ticker."""
        cached = self._k.price_cache.get(ticker)
        if cached is None:
            return float("inf")
        return time.time() - cached.updated_at

    def poly_price_age(self, token_id: str) -> float:
        """Seconds since the last Polymarket price update for this token."""
        cached = self._p.price_cache.get(token_id)
        if cached is None:
            return float("inf")
        return time.time() - cached.updated_at

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
