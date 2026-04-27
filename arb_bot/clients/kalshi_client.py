"""Kalshi REST v2 + WebSocket client.

Authentication: RSA-PSS signed headers (March 2026 format).
Prices are dollar strings (e.g. "0.6500") — never integer cents.
"""

import asyncio
import base64
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

try:
    import websockets
except ImportError:  # pragma: no cover
    websockets = None  # type: ignore

from arb_bot.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class MarketPrice:
    ticker: str
    yes_ask: Optional[float] = None
    yes_bid: Optional[float] = None
    no_ask: Optional[float] = None
    no_bid: Optional[float] = None
    last_price: Optional[float] = None
    updated_at: float = field(default_factory=time.time)


class KalshiClient:
    def __init__(self, api_key_id: str, private_key_path: str, base_url: str, ws_url: str):
        self.api_key_id = api_key_id
        self.base_url = base_url.rstrip("/")
        self.ws_url = ws_url
        self._private_key = self._load_private_key(private_key_path)
        self._http = httpx.AsyncClient(timeout=10.0)
        self.price_cache: dict[str, MarketPrice] = {}
        self._ws_running = False

    @classmethod
    def from_env(cls) -> "KalshiClient":
        from arb_bot.config import (
            KALSHI_API_KEY_ID,
            KALSHI_PRIVATE_KEY_PATH,
            KALSHI_BASE_URL,
            KALSHI_WS_URL,
        )
        return cls(
            api_key_id=KALSHI_API_KEY_ID,
            private_key_path=KALSHI_PRIVATE_KEY_PATH,
            base_url=KALSHI_BASE_URL,
            ws_url=KALSHI_WS_URL,
        )

    # ------------------------------------------------------------------
    # Auth helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _load_private_key(path: str):
        key_path = Path(path)
        if not key_path.exists():
            raise FileNotFoundError(f"Kalshi private key not found: {path}")
        with open(key_path, "rb") as fh:
            return serialization.load_pem_private_key(fh.read(), password=None, backend=default_backend())

    def _sign(self, timestamp: str, method: str, path: str, body: str = "") -> str:
        message = f"{timestamp}{method}{path}{body}".encode()
        signature = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode()

    def _auth_headers(self, method: str, path: str, body: str = "") -> dict:
        ts = str(int(time.time() * 1000))
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": self._sign(ts, method.upper(), path, body),
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # REST helpers
    # ------------------------------------------------------------------

    async def _get(self, endpoint: str, params: Optional[dict] = None) -> Any:
        url = f"{self.base_url}{endpoint}"
        # Build the path including query string for signature (Kalshi requires full path)
        path_for_sig = endpoint
        headers = self._auth_headers("GET", path_for_sig)
        resp = await self._http.get(url, headers=headers, params=params)
        resp.raise_for_status()
        return resp.json()

    async def _post(self, endpoint: str, payload: dict) -> Any:
        body = json.dumps(payload)
        headers = self._auth_headers("POST", endpoint, body)
        url = f"{self.base_url}{endpoint}"
        resp = await self._http.post(url, headers=headers, content=body)
        resp.raise_for_status()
        return resp.json()

    async def _delete(self, endpoint: str) -> Any:
        headers = self._auth_headers("DELETE", endpoint)
        url = f"{self.base_url}{endpoint}"
        resp = await self._http.delete(url, headers=headers)
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Public REST methods
    # ------------------------------------------------------------------

    async def get_markets(self, limit: int = 200, cursor: Optional[str] = None, status: str = "open") -> list[dict]:
        """Paginate through all active markets."""
        all_markets: list[dict] = []
        params: dict = {"limit": limit, "status": status}
        while True:
            if cursor:
                params["cursor"] = cursor
            data = await self._get("/markets", params=params)
            markets = data.get("markets", [])
            all_markets.extend(markets)
            cursor = data.get("cursor")
            if not cursor or len(markets) < limit:
                break
        return all_markets

    async def get_market(self, ticker: str) -> dict:
        data = await self._get(f"/markets/{ticker}")
        return data.get("market", data)

    async def get_orderbook(self, ticker: str, depth: int = 5) -> dict:
        return await self._get(f"/markets/{ticker}/orderbook", params={"depth": depth})

    async def get_balance(self) -> float:
        data = await self._get("/portfolio/balance")
        # Returns balance in cents historically, but verify against live API
        return float(data.get("balance", 0))

    async def place_order(
        self,
        ticker: str,
        side: str,          # "yes" | "no"
        order_type: str,    # "market" | "limit"
        count: int,
        yes_price: Optional[float] = None,
        no_price: Optional[float] = None,
    ) -> dict:
        """
        Place a buy order.
        count = number of contracts (each contract settles at $1).
        Prices are fractional dollars (0.0 – 1.0).
        """
        payload: dict = {
            "ticker": ticker,
            "action": "buy",
            "side": side.lower(),
            "type": order_type.lower(),
            "count": count,
        }
        if yes_price is not None:
            # API expects dollar string with up to 4 decimal places
            payload["yes_price"] = f"{yes_price:.4f}"
        if no_price is not None:
            payload["no_price"] = f"{no_price:.4f}"
        return await self._post("/portfolio/orders", payload)

    async def cancel_order(self, order_id: str) -> dict:
        return await self._delete(f"/portfolio/orders/{order_id}")

    async def get_positions(self) -> list[dict]:
        data = await self._get("/portfolio/positions")
        return data.get("market_positions", [])

    # ------------------------------------------------------------------
    # WebSocket price feed
    # ------------------------------------------------------------------

    async def subscribe_tickers(self, tickers: list[str]) -> None:
        """Open WebSocket and maintain live price cache. Runs until cancelled."""
        if websockets is None:
            raise RuntimeError("websockets package not installed")

        self._ws_running = True
        ts = str(int(time.time() * 1000))
        sig = self._sign(ts, "GET", "/trade-api/ws/v2")
        extra_headers = {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": sig,
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }

        while self._ws_running:
            try:
                async with websockets.connect(self.ws_url, extra_headers=extra_headers) as ws:
                    # Subscribe to orderbook_delta for each ticker
                    sub_msg = {
                        "id": 1,
                        "cmd": "subscribe",
                        "params": {
                            "channels": ["ticker"],
                            "market_tickers": tickers,
                        },
                    }
                    await ws.send(json.dumps(sub_msg))
                    logger.info(f"Kalshi WS subscribed to {len(tickers)} tickers")

                    async for raw in ws:
                        self._handle_ws_message(json.loads(raw))

            except Exception as exc:
                logger.warning(f"Kalshi WS disconnected ({exc}), reconnecting in 3s")
                await asyncio.sleep(3)

    def _handle_ws_message(self, msg: dict) -> None:
        msg_type = msg.get("type")
        if msg_type != "ticker":
            return
        data = msg.get("msg", {})
        ticker = data.get("market_ticker")
        if not ticker:
            return

        cached = self.price_cache.setdefault(ticker, MarketPrice(ticker=ticker))

        def _parse(val) -> Optional[float]:
            if val is None:
                return None
            return float(val)

        cached.yes_ask = _parse(data.get("yes_ask"))
        cached.yes_bid = _parse(data.get("yes_bid"))
        cached.no_ask = _parse(data.get("no_ask"))
        cached.no_bid = _parse(data.get("no_bid"))
        cached.last_price = _parse(data.get("last_price"))
        cached.updated_at = time.time()

    def stop_ws(self) -> None:
        self._ws_running = False

    async def close(self) -> None:
        self.stop_ws()
        await self._http.aclose()
