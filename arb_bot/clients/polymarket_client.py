"""Polymarket CLOB + Gamma API client.

Wraps py-clob-client for order management and uses httpx for Gamma market data.
Supports both Polymarket International and Polymarket US (CFTC) via config flag.
"""

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY, SELL
except ImportError:  # pragma: no cover
    ClobClient = None  # type: ignore
    OrderArgs = None  # type: ignore
    OrderType = None  # type: ignore
    BUY = "BUY"
    SELL = "SELL"

try:
    import websockets
except ImportError:  # pragma: no cover
    websockets = None  # type: ignore

from arb_bot.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class PolyPrice:
    token_id: str
    best_ask: Optional[float] = None
    best_bid: Optional[float] = None
    updated_at: float = field(default_factory=time.time)


class PolymarketClient:
    _WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

    def __init__(
        self,
        private_key: str,
        funder_address: str,
        clob_host: str,
        gamma_host: str,
        chain_id: int = 137,
        version: str = "international",
    ):
        self.private_key = private_key
        self.funder_address = funder_address
        self.clob_host = clob_host
        self.gamma_host = gamma_host.rstrip("/")
        self.chain_id = chain_id
        self.version = version
        self.price_cache: dict[str, PolyPrice] = {}
        self._ws_running = False
        self._http = httpx.AsyncClient(timeout=15.0)

        if ClobClient is None:
            logger.warning("py-clob-client not installed — order placement disabled")
            self._clob: Optional[ClobClient] = None
        else:
            self._clob = ClobClient(
                clob_host,
                key=private_key,
                chain_id=chain_id,
                signature_type=1,  # EIP-712
                funder=funder_address,
            )
            try:
                self._clob.set_api_creds(self._clob.create_or_derive_api_creds())
            except Exception as exc:
                logger.warning(f"Could not derive Polymarket API creds: {exc}")

    @classmethod
    def from_env(cls) -> "PolymarketClient":
        from arb_bot.config import (
            POLYMARKET_PRIVATE_KEY,
            POLYMARKET_FUNDER_ADDRESS,
            POLYMARKET_CLOB_HOST,
            POLYMARKET_GAMMA_HOST,
            POLYMARKET_CHAIN_ID,
            POLYMARKET_VERSION,
        )
        return cls(
            private_key=POLYMARKET_PRIVATE_KEY,
            funder_address=POLYMARKET_FUNDER_ADDRESS,
            clob_host=POLYMARKET_CLOB_HOST,
            gamma_host=POLYMARKET_GAMMA_HOST,
            chain_id=POLYMARKET_CHAIN_ID,
            version=POLYMARKET_VERSION,
        )

    # ------------------------------------------------------------------
    # Market data (Gamma API — no auth required)
    # ------------------------------------------------------------------

    async def get_markets(
        self,
        active: bool = True,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """Fetch markets from Gamma API with basic pagination."""
        all_markets: list[dict] = []
        params = {
            "active": str(active).lower(),
            "limit": limit,
            "offset": offset,
            "closed": "false",
        }
        while True:
            resp = await self._http.get(f"{self.gamma_host}/markets", params=params)
            resp.raise_for_status()
            batch = resp.json()
            if isinstance(batch, dict):
                batch = batch.get("markets", [])
            if not batch:
                break
            all_markets.extend(batch)
            if len(batch) < limit:
                break
            params["offset"] = params["offset"] + limit  # type: ignore[operator]
        return all_markets

    async def get_orderbook(self, token_id: str) -> dict:
        """Fetch CLOB order book for a YES or NO token."""
        resp = await self._http.get(
            f"{self.clob_host}/book",
            params={"token_id": token_id},
        )
        resp.raise_for_status()
        return resp.json()

    async def get_price(self, token_id: str, side: str = "BUY") -> Optional[float]:
        """Best ask (for BUY) or best bid (for SELL) from the CLOB."""
        try:
            resp = await self._http.get(
                f"{self.clob_host}/price",
                params={"token_id": token_id, "side": side.upper()},
            )
            resp.raise_for_status()
            data = resp.json()
            raw = data.get("price")
            return float(raw) if raw is not None else None
        except Exception as exc:
            logger.debug(f"get_price({token_id}) failed: {exc}")
            return None

    async def get_balance(self) -> float:
        """USDC balance on Polygon for the configured funder address."""
        if self._clob is None:
            return 0.0
        try:
            balance = self._clob.get_balance()
            return float(balance)
        except Exception as exc:
            logger.warning(f"Polymarket get_balance failed: {exc}")
            return 0.0

    # ------------------------------------------------------------------
    # Order placement
    # ------------------------------------------------------------------

    async def place_market_order(
        self,
        token_id: str,
        amount_usdc: float,
        side: str = "BUY",
    ) -> dict:
        """Fill-or-Kill market order spending `amount_usdc` of USDC."""
        if self._clob is None:
            raise RuntimeError("py-clob-client not available")
        order_args = OrderArgs(
            price=1.0,  # ignored for market orders; use amount
            size=amount_usdc,
            side=BUY if side.upper() == "BUY" else SELL,
            token_id=token_id,
        )
        order = self._clob.create_market_order(order_args)
        return self._clob.post_order(order, OrderType.FOK)

    async def place_limit_order(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str = "BUY",
    ) -> dict:
        """GTC limit order."""
        if self._clob is None:
            raise RuntimeError("py-clob-client not available")
        order_args = OrderArgs(
            price=price,
            size=size,
            side=BUY if side.upper() == "BUY" else SELL,
            token_id=token_id,
        )
        order = self._clob.create_limit_order(order_args)
        return self._clob.post_order(order, OrderType.GTC)

    async def get_positions(self) -> list[dict]:
        if self._clob is None:
            return []
        try:
            return self._clob.get_positions()  # type: ignore[return-value]
        except Exception as exc:
            logger.warning(f"Polymarket get_positions failed: {exc}")
            return []

    # ------------------------------------------------------------------
    # WebSocket price feed
    # ------------------------------------------------------------------

    async def subscribe_tokens(self, token_ids: list[str]) -> None:
        """Open WebSocket and maintain live price cache. Runs until cancelled."""
        if websockets is None:
            raise RuntimeError("websockets package not installed")

        self._ws_running = True
        while self._ws_running:
            try:
                async with websockets.connect(self._WS_URL) as ws:
                    sub = {
                        "auth": {},  # public channel, no auth needed
                        "type": "market",
                        "assets_ids": token_ids,
                    }
                    await ws.send(json.dumps(sub))
                    logger.info(f"Polymarket WS subscribed to {len(token_ids)} tokens")

                    async for raw in ws:
                        self._handle_ws_message(json.loads(raw))

            except Exception as exc:
                logger.warning(f"Polymarket WS disconnected ({exc}), reconnecting in 3s")
                await asyncio.sleep(3)

    def _handle_ws_message(self, msg: dict) -> None:
        event_type = msg.get("event_type") or msg.get("type")
        if event_type not in ("price_change", "book", "tick"):
            return

        token_id = msg.get("asset_id") or msg.get("token_id")
        if not token_id:
            return

        cached = self.price_cache.setdefault(token_id, PolyPrice(token_id=token_id))

        if "best_ask" in msg:
            try:
                cached.best_ask = float(msg["best_ask"])
            except (TypeError, ValueError):
                pass
        if "best_bid" in msg:
            try:
                cached.best_bid = float(msg["best_bid"])
            except (TypeError, ValueError):
                pass

        cached.updated_at = time.time()

    def stop_ws(self) -> None:
        self._ws_running = False

    async def close(self) -> None:
        self.stop_ws()
        await self._http.aclose()
