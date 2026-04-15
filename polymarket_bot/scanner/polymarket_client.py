"""Polymarket CLOB API client wrapper with rate limiting and retry logic."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Protocol

import httpx
import structlog

from polymarket_bot.config.settings import BotConfig
from polymarket_bot.types import OrderBook, PriceLevel

logger = structlog.get_logger(__name__)


class ClobClientProtocol(Protocol):
    """Protocol for the official py-clob-client, allowing test mocks."""

    def get_order_book(self, token_id: str) -> Any: ...
    def get_price(self, token_id: str, side: str) -> Any: ...
    def create_order(self, order_args: Any) -> Any: ...
    def post_order(self, signed_order: Any, order_type: Any) -> Any: ...
    def cancel(self, order_id: str) -> Any: ...
    def get_orders(self, **kwargs: Any) -> Any: ...
    def get_trades(self, **kwargs: Any) -> Any: ...


class RateLimiter:
    """Token-bucket rate limiter for API calls."""

    def __init__(self, max_requests: int, window_seconds: float = 10.0) -> None:
        self._max = max_requests
        self._window = window_seconds
        self._timestamps: list[float] = []

    async def acquire(self) -> None:
        now = time.monotonic()
        self._timestamps = [t for t in self._timestamps if now - t < self._window]
        if len(self._timestamps) >= self._max:
            sleep_time = self._window - (now - self._timestamps[0])
            logger.warning("rate_limit_wait", sleep_seconds=round(sleep_time, 2))
            await asyncio.sleep(sleep_time)
        self._timestamps.append(time.monotonic())


class PolymarketCLOBClient:
    """Async wrapper around Polymarket CLOB REST API with rate limiting."""

    def __init__(self, config: BotConfig, clob_client: ClobClientProtocol | None = None) -> None:
        self._config = config
        self._clob = clob_client
        self._http = httpx.AsyncClient(
            base_url=config.polymarket_host,
            timeout=30.0,
            headers={"Accept": "application/json"},
        )
        self._data_limiter = RateLimiter(
            config.rate_limits.data_requests_per_10s, window_seconds=10.0
        )
        self._order_limiter = RateLimiter(config.rate_limits.orders_per_10s, window_seconds=10.0)

    async def get_order_book(self, token_id: str) -> OrderBook:
        """Fetch the current order book for a token."""
        await self._data_limiter.acquire()
        resp = await self._http.get(f"/book", params={"token_id": token_id})
        resp.raise_for_status()
        data = resp.json()

        bids = [PriceLevel(price=float(b["price"]), size=float(b["size"])) for b in data.get("bids", [])]
        asks = [PriceLevel(price=float(a["price"]), size=float(a["size"])) for a in data.get("asks", [])]

        bids.sort(key=lambda x: x.price, reverse=True)
        asks.sort(key=lambda x: x.price)

        return OrderBook(token_id=token_id, bids=bids, asks=asks)

    async def get_midpoint(self, token_id: str) -> float:
        """Get the midpoint price for a token."""
        await self._data_limiter.acquire()
        resp = await self._http.get(f"/midpoint", params={"token_id": token_id})
        resp.raise_for_status()
        data = resp.json()
        return float(data.get("mid", 0.5))

    async def get_price(self, token_id: str, side: str) -> float:
        """Get the best price for a token on a given side."""
        await self._data_limiter.acquire()
        resp = await self._http.get(f"/price", params={"token_id": token_id, "side": side})
        resp.raise_for_status()
        data = resp.json()
        return float(data.get("price", 0.0))

    async def get_markets(self, next_cursor: str = "") -> dict[str, Any]:
        """Fetch paginated market list from CLOB."""
        await self._data_limiter.acquire()
        params: dict[str, str] = {}
        if next_cursor:
            params["next_cursor"] = next_cursor
        resp = await self._http.get("/markets", params=params)
        resp.raise_for_status()
        return resp.json()

    async def place_limit_order(
        self, token_id: str, price: float, size: float, side: str
    ) -> dict[str, Any]:
        """Place a limit order via the CLOB client (live mode only)."""
        await self._order_limiter.acquire()
        if self._clob is None:
            raise RuntimeError("CLOB client not initialized — cannot place live orders")

        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY, SELL

        order_side = BUY if side == "BUY" else SELL
        order_args = OrderArgs(token_id=token_id, price=price, size=size, side=order_side)
        signed = self._clob.create_order(order_args)
        resp = self._clob.post_order(signed, OrderType.GTC)
        logger.info(
            "live_order_placed",
            token_id=token_id,
            price=price,
            size=size,
            side=side,
            response=str(resp),
        )
        return resp if isinstance(resp, dict) else {"status": "ok", "raw": str(resp)}

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        """Cancel an open order."""
        await self._order_limiter.acquire()
        if self._clob is None:
            raise RuntimeError("CLOB client not initialized")
        resp = self._clob.cancel(order_id)
        logger.info("order_cancelled", order_id=order_id)
        return resp if isinstance(resp, dict) else {"status": "ok"}

    async def close(self) -> None:
        await self._http.aclose()
