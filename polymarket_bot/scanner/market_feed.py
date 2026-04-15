"""Unified market feed combining REST polling and WebSocket streaming."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any, Callable

import structlog

from polymarket_bot.config.settings import BotConfig
from polymarket_bot.scanner.gamma_client import GammaClient
from polymarket_bot.scanner.polymarket_client import PolymarketCLOBClient
from polymarket_bot.types import Market, OrderBook

logger = structlog.get_logger(__name__)

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class MarketFeed:
    """Aggregates market data from REST and WebSocket sources.

    Maintains a local cache of markets and order books, updated by either
    periodic REST polling or real-time WebSocket events.
    """

    def __init__(
        self,
        config: BotConfig,
        gamma_client: GammaClient,
        clob_client: PolymarketCLOBClient,
    ) -> None:
        self._config = config
        self._gamma = gamma_client
        self._clob = clob_client
        self._markets: dict[str, Market] = {}
        self._order_books: dict[str, OrderBook] = {}
        self._running = False
        self._callbacks: list[Callable[[str, Market, OrderBook | None], Any]] = []
        self._ws_task: asyncio.Task[None] | None = None
        self._poll_task: asyncio.Task[None] | None = None

    @property
    def markets(self) -> dict[str, Market]:
        return dict(self._markets)

    @property
    def order_books(self) -> dict[str, OrderBook]:
        return dict(self._order_books)

    def on_update(self, callback: Callable[[str, Market, OrderBook | None], Any]) -> None:
        """Register a callback for market updates."""
        self._callbacks.append(callback)

    async def start(self) -> None:
        """Start the market feed (polling + optional WebSocket)."""
        self._running = True
        logger.info("market_feed_starting")

        # Initial market load
        await self._poll_markets()

        # Start background polling
        self._poll_task = asyncio.create_task(self._poll_loop())

        # Start WebSocket if enabled
        if self._config.scanner.websocket_enabled:
            self._ws_task = asyncio.create_task(self._ws_loop())

        logger.info("market_feed_started", markets=len(self._markets))

    async def stop(self) -> None:
        """Stop the market feed."""
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
        if self._ws_task:
            self._ws_task.cancel()
        logger.info("market_feed_stopped")

    async def _poll_markets(self) -> None:
        """Fetch markets from Gamma API and order books from CLOB."""
        try:
            all_markets: list[Market] = []
            for category in self._config.scanner.categories:
                markets = await self._gamma.get_active_markets(
                    limit=self._config.scanner.max_markets,
                    category=category,
                )
                all_markets.extend(markets)

            # Deduplicate by condition_id
            seen: set[str] = set()
            for m in all_markets:
                if m.condition_id not in seen:
                    seen.add(m.condition_id)
                    self._markets[m.condition_id] = m

            # Fetch order books for top markets (by liquidity)
            sorted_markets = sorted(
                self._markets.values(), key=lambda x: x.liquidity_usd, reverse=True
            )[: self._config.scanner.max_markets]

            for market in sorted_markets:
                for token in market.tokens:
                    try:
                        book = await self._clob.get_order_book(token.token_id)
                        self._order_books[token.token_id] = book
                        token.price = book.mid_price
                        token.book_depth_usd = sum(
                            l.price * l.size for l in book.bids[:5]
                        ) + sum(l.price * l.size for l in book.asks[:5])
                    except Exception as e:
                        logger.debug("orderbook_fetch_error", token_id=token.token_id, error=str(e))

            logger.info(
                "poll_complete",
                markets=len(self._markets),
                books=len(self._order_books),
            )

        except Exception as e:
            logger.error("poll_markets_error", error=str(e))

    async def _poll_loop(self) -> None:
        """Background loop for periodic REST polling."""
        while self._running:
            await asyncio.sleep(self._config.scanner.poll_interval_seconds)
            await self._poll_markets()
            for cid, market in self._markets.items():
                for cb in self._callbacks:
                    try:
                        book = None
                        if market.tokens:
                            book = self._order_books.get(market.tokens[0].token_id)
                        cb(cid, market, book)
                    except Exception as e:
                        logger.error("callback_error", error=str(e))

    async def _ws_loop(self) -> None:
        """WebSocket connection loop with auto-reconnect."""
        backoff = 1.0
        max_backoff = 60.0

        while self._running:
            try:
                import websockets

                async with websockets.connect(WS_URL) as ws:
                    logger.info("ws_connected")
                    backoff = 1.0  # Reset on successful connect

                    # Subscribe to market updates
                    token_ids = list(self._order_books.keys())[:50]  # Limit subscriptions
                    if token_ids:
                        subscribe_msg = json.dumps({
                            "type": "subscribe",
                            "channel": "market",
                            "assets_ids": token_ids,
                        })
                        await ws.send(subscribe_msg)

                    async for message in ws:
                        if not self._running:
                            break
                        try:
                            data = json.loads(message)
                            self._handle_ws_message(data)
                        except json.JSONDecodeError:
                            continue

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("ws_error", error=str(e), reconnect_in=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)

    def _handle_ws_message(self, data: dict[str, Any]) -> None:
        """Process a WebSocket market update message."""
        event_type = data.get("event_type", data.get("type", ""))
        if event_type in ("book", "price_change", "trade"):
            token_id = data.get("asset_id", "")
            if token_id and token_id in self._order_books:
                # Update price from WS
                if "price" in data:
                    for market in self._markets.values():
                        for token in market.tokens:
                            if token.token_id == token_id:
                                token.price = float(data["price"])
                                break

    async def refresh_market(self, condition_id: str) -> Market | None:
        """Force-refresh a single market and its order books."""
        market = await self._gamma.get_market_by_id(condition_id)
        if market:
            self._markets[condition_id] = market
            for token in market.tokens:
                try:
                    book = await self._clob.get_order_book(token.token_id)
                    self._order_books[token.token_id] = book
                    token.price = book.mid_price
                except Exception as e:
                    logger.debug("refresh_book_error", token_id=token.token_id, error=str(e))
        return market

    def get_book_for_token(self, token_id: str) -> OrderBook | None:
        return self._order_books.get(token_id)
