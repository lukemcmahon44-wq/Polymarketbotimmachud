"""Market feed aggregator — maintains ranked candidate list."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import structlog

from polymarket_bot.config import BotConfig
from polymarket_bot.scanner.polymarket_client import PolymarketClient
from polymarket_bot.types import Market, OrderBook

logger = structlog.get_logger()


class MarketFeed:
    """Continuously scans and maintains a ranked list of tradable markets."""

    def __init__(self, client: PolymarketClient, config: BotConfig) -> None:
        self.client = client
        self.config = config
        self.markets: dict[str, Market] = {}
        self.order_books: dict[str, OrderBook] = {}
        self._running = False

    async def scan_all_categories(self) -> list[Market]:
        """Fetch markets across all configured categories."""
        all_markets: list[Market] = []
        tasks = [
            self.client.get_markets(category=cat)
            for cat in self.config.scanner.categories
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, Exception):
                logger.error("market_feed.scan_error", error=str(result))
                continue
            all_markets.extend(result)

        # Deduplicate by market_id
        seen: set[str] = set()
        unique: list[Market] = []
        for m in all_markets:
            if m.market_id not in seen:
                seen.add(m.market_id)
                unique.append(m)

        # Filter: active, sufficient liquidity, not expired
        now = datetime.now(timezone.utc)
        filtered = [
            m for m in unique
            if m.active
            and m.liquidity_usd >= self.config.min_liquidity_usd
            and m.end_date > now
        ]

        # Rank by volume * liquidity (proxy for opportunity)
        filtered.sort(key=lambda m: m.volume_usd * m.liquidity_usd, reverse=True)

        # Cap tracked markets
        filtered = filtered[: self.config.scanner.max_markets_to_track]

        self.markets = {m.market_id: m for m in filtered}
        logger.info("market_feed.scan_complete", tracked=len(filtered))
        return filtered

    async def refresh_order_books(self) -> dict[str, OrderBook]:
        """Refresh order books for all tracked market tokens."""
        token_ids: list[str] = []
        for market in self.markets.values():
            for token in market.tokens:
                token_ids.append(token.token_id)

        tasks = [self.client.get_order_book(tid) for tid in token_ids]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for tid, result in zip(token_ids, results):
            if isinstance(result, Exception):
                logger.warning("market_feed.book_error", token_id=tid, error=str(result))
                continue
            self.order_books[tid] = result

        logger.info("market_feed.books_refreshed", count=len(self.order_books))
        return self.order_books

    async def run_continuous(self) -> None:
        """Main loop: scan markets, refresh books, sleep, repeat."""
        self._running = True
        logger.info("market_feed.starting")

        while self._running:
            try:
                await self.scan_all_categories()
                await self.refresh_order_books()
            except Exception as exc:
                logger.error("market_feed.loop_error", error=str(exc))

            await asyncio.sleep(self.config.scanner.poll_interval_seconds)

    def stop(self) -> None:
        self._running = False

    def get_snapshot(self) -> dict[str, Any]:
        """Current state snapshot for monitoring."""
        return {
            "tracked_markets": len(self.markets),
            "order_books": len(self.order_books),
            "categories": list({m.category for m in self.markets.values()}),
        }
