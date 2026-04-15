"""Gamma Markets API client for market discovery and metadata."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx
import structlog

from polymarket_bot.config.settings import BotConfig
from polymarket_bot.types import Market, Token

logger = structlog.get_logger(__name__)

GAMMA_API_URL = "https://gamma-api.polymarket.com"


class GammaClient:
    """Client for the Polymarket Gamma API (market discovery and metadata)."""

    def __init__(self, config: BotConfig) -> None:
        self._config = config
        self._http = httpx.AsyncClient(
            base_url=GAMMA_API_URL,
            timeout=30.0,
            headers={"Accept": "application/json"},
        )

    async def get_active_markets(
        self,
        limit: int = 100,
        offset: int = 0,
        category: str | None = None,
    ) -> list[Market]:
        """Fetch active markets from the Gamma API."""
        params: dict[str, Any] = {
            "limit": limit,
            "offset": offset,
            "active": True,
            "closed": False,
        }
        if category:
            params["tag"] = category

        resp = await self._http.get("/markets", params=params)
        resp.raise_for_status()
        raw_markets = resp.json()

        markets: list[Market] = []
        for m in raw_markets:
            try:
                market = self._parse_market(m)
                if self._passes_filters(market):
                    markets.append(market)
            except (KeyError, ValueError) as e:
                logger.debug("market_parse_skip", market_id=m.get("id", "?"), error=str(e))
                continue

        logger.info("gamma_markets_fetched", count=len(markets), category=category or "all")
        return markets

    async def get_market_by_id(self, condition_id: str) -> Market | None:
        """Fetch a single market by its condition ID."""
        resp = await self._http.get(f"/markets/{condition_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return self._parse_market(resp.json())

    def _parse_market(self, data: dict[str, Any]) -> Market:
        """Parse raw Gamma API response into a Market object."""
        tokens: list[Token] = []
        for t in data.get("tokens", []):
            tokens.append(
                Token(
                    token_id=str(t.get("token_id", "")),
                    outcome=t.get("outcome", ""),
                    price=float(t.get("price", 0.0)),
                )
            )

        end_date = None
        if data.get("end_date_iso"):
            try:
                end_date = datetime.fromisoformat(data["end_date_iso"].replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass

        return Market(
            condition_id=str(data.get("condition_id", data.get("id", ""))),
            question=data.get("question", ""),
            tokens=tokens,
            category=data.get("tag", data.get("category", "")),
            end_date=end_date,
            active=data.get("active", True),
            liquidity_usd=float(data.get("liquidity", 0.0)),
            volume_usd=float(data.get("volume", 0.0)),
            description=data.get("description", ""),
            slug=data.get("slug", ""),
        )

    def _passes_filters(self, market: Market) -> bool:
        """Check if a market passes minimum thresholds."""
        cfg = self._config.scanner
        if market.volume_usd < cfg.min_volume_usd:
            return False
        if market.liquidity_usd < cfg.min_liquidity_usd:
            return False
        if not market.tokens:
            return False
        return True

    async def close(self) -> None:
        await self._http.aclose()
