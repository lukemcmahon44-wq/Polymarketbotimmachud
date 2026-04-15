"""HTTP + WebSocket client for Polymarket CLOB and Gamma APIs."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Protocol

import httpx
import structlog

from polymarket_bot.config import BotConfig
from polymarket_bot.scanner.rate_limiter import RateLimiter
from polymarket_bot.types import Market, OrderBook, OrderBookLevel, Token

logger = structlog.get_logger()


class MarketDataProvider(Protocol):
    """Interface for market data — enables dependency injection and mocking."""

    async def get_markets(self, category: str | None = None) -> list[Market]: ...
    async def get_order_book(self, token_id: str) -> OrderBook: ...
    async def get_price(self, token_id: str) -> float: ...


class PolymarketClient:
    """Production client for Polymarket CLOB + Gamma APIs."""

    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self.clob_url = config.polymarket_clob_url
        self.gamma_url = config.polymarket_gamma_url
        self._http: httpx.AsyncClient | None = None
        self._rate_limiter = RateLimiter(
            max_requests=config.rate_limits.rest_requests_per_10s,
            window_seconds=10.0,
            backoff_base=config.rate_limits.backoff_base_seconds,
            backoff_max=config.rate_limits.backoff_max_seconds,
            backoff_multiplier=config.rate_limits.backoff_multiplier,
        )

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(30.0),
                headers={"Accept": "application/json"},
            )
        return self._http

    async def close(self) -> None:
        if self._http and not self._http.is_closed:
            await self._http.aclose()

    async def _get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        """GET with rate limiting and retry."""
        client = await self._client()
        for attempt in range(4):
            await self._rate_limiter.acquire()
            try:
                resp = await client.get(url, params=params)
                if resp.status_code == 429:
                    await self._rate_limiter.backoff_on_429()
                    continue
                resp.raise_for_status()
                self._rate_limiter.reset_backoff()
                return resp.json()
            except httpx.HTTPStatusError as exc:
                logger.error("api.http_error", status=exc.response.status_code, url=url)
                if attempt == 3:
                    raise
                await asyncio.sleep(2 ** attempt)
            except httpx.RequestError as exc:
                logger.error("api.request_error", error=str(exc), url=url)
                if attempt == 3:
                    raise
                await asyncio.sleep(2 ** attempt)
        return None

    async def get_markets(self, category: str | None = None) -> list[Market]:
        """Fetch active markets from Gamma API."""
        params: dict[str, Any] = {"active": "true", "closed": "false", "limit": 100}
        if category:
            params["tag"] = category

        data = await self._get(f"{self.gamma_url}/markets", params=params)
        if not data:
            return []

        markets: list[Market] = []
        for item in data:
            try:
                tokens = []
                for t in item.get("tokens", []):
                    tokens.append(Token(
                        token_id=str(t.get("token_id", "")),
                        outcome=t.get("outcome", "Unknown"),
                        price=float(t.get("price", 0.5)),
                    ))

                end_str = item.get("end_date_iso", "") or item.get("end_date", "")
                try:
                    end_date = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    end_date = datetime.now(timezone.utc)

                markets.append(Market(
                    market_id=str(item.get("condition_id", item.get("id", ""))),
                    condition_id=str(item.get("condition_id", "")),
                    question=item.get("question", ""),
                    category=item.get("category", category or ""),
                    end_date=end_date,
                    tokens=tokens,
                    volume_usd=float(item.get("volume", 0)),
                    liquidity_usd=float(item.get("liquidity", 0)),
                    active=item.get("active", True),
                ))
            except (KeyError, ValueError, TypeError) as exc:
                logger.warning("scanner.parse_market_error", error=str(exc))
                continue

        logger.info("scanner.fetched_markets", count=len(markets), category=category)
        return markets

    async def get_order_book(self, token_id: str) -> OrderBook:
        """Fetch order book from CLOB API."""
        data = await self._get(f"{self.clob_url}/book", params={"token_id": token_id})
        if not data:
            return OrderBook(token_id=token_id)

        bids = [
            OrderBookLevel(price=float(b["price"]), size=float(b["size"]))
            for b in data.get("bids", [])
        ]
        asks = [
            OrderBookLevel(price=float(a["price"]), size=float(a["size"]))
            for a in data.get("asks", [])
        ]
        # Sort: bids descending, asks ascending
        bids.sort(key=lambda x: x.price, reverse=True)
        asks.sort(key=lambda x: x.price)

        return OrderBook(token_id=token_id, bids=bids, asks=asks)

    async def get_price(self, token_id: str) -> float:
        """Fetch mid-price for a token."""
        data = await self._get(f"{self.clob_url}/price", params={"token_id": token_id})
        if data and "price" in data:
            return float(data["price"])
        # Fall back to order book mid
        book = await self.get_order_book(token_id)
        return book.mid_price

    async def stream_prices(self, token_ids: list[str]) -> AsyncIterator[dict[str, float]]:
        """Stream price updates via WebSocket (with REST fallback)."""
        # WebSocket streaming — graceful fallback to polling if unavailable
        try:
            import websockets  # type: ignore[import-untyped]
            ws_url = self.clob_url.replace("https://", "wss://").replace("http://", "ws://")
            async with websockets.connect(f"{ws_url}/ws/prices") as ws:
                # Subscribe to token IDs
                import json
                await ws.send(json.dumps({"type": "subscribe", "token_ids": token_ids}))
                async for msg in ws:
                    data = json.loads(msg)
                    if "prices" in data:
                        yield {k: float(v) for k, v in data["prices"].items()}
        except Exception as exc:
            logger.warning("scanner.ws_fallback", error=str(exc))
            # REST polling fallback
            while True:
                prices: dict[str, float] = {}
                for tid in token_ids:
                    try:
                        prices[tid] = await self.get_price(tid)
                    except Exception:
                        pass
                if prices:
                    yield prices
                await asyncio.sleep(self.config.scanner.poll_interval_seconds)
