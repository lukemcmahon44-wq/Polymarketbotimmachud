"""Synthetic market data generator for backtesting."""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone
from typing import Any

from polymarket_bot.types import Market, OrderBook, OrderBookLevel, Token


class SyntheticDataGenerator:
    """Generates realistic synthetic Polymarket-like market data.

    Produces time-series snapshots of markets with:
    - Random walks for price evolution
    - Varying liquidity and volume
    - Resolution events (market settles to 0 or 1)
    - Order book depth
    """

    def __init__(self, seed: int = 42) -> None:
        self.rng = random.Random(seed)
        self._market_counter = 0

    def generate_market(
        self,
        category: str = "crypto",
        initial_yes_price: float | None = None,
        hours_to_resolution: int = 168,
    ) -> Market:
        """Generate a single synthetic market."""
        self._market_counter += 1
        mid = f"market_{self._market_counter:04d}"

        if initial_yes_price is None:
            initial_yes_price = round(self.rng.uniform(0.10, 0.90), 3)

        end = datetime.now(timezone.utc) + timedelta(hours=hours_to_resolution)

        questions = {
            "crypto": [
                f"Will BTC exceed ${self.rng.randint(50, 150)}k by end of month?",
                f"Will ETH reach ${self.rng.randint(2, 8)}k this quarter?",
                f"Will SOL break ${self.rng.randint(100, 500)} this week?",
            ],
            "finance": [
                "Will the Fed cut rates at next meeting?",
                f"Will S&P 500 close above {self.rng.randint(5000, 7000)}?",
                "Will inflation fall below 3%?",
            ],
            "technology": [
                "Will a major tech company announce layoffs?",
                "Will GPT-5 be released this quarter?",
                "Will Apple announce a new product category?",
            ],
            "politics": [
                "Will the incumbent win the next election?",
                "Will a new trade deal be announced?",
                "Will a government shutdown occur?",
            ],
        }

        question = self.rng.choice(questions.get(category, questions["crypto"]))

        return Market(
            market_id=mid,
            condition_id=f"cond_{mid}",
            question=question,
            category=category,
            end_date=end,
            tokens=[
                Token(token_id=f"{mid}_yes", outcome="Yes", price=initial_yes_price),
                Token(token_id=f"{mid}_no", outcome="No", price=round(1 - initial_yes_price, 3)),
            ],
            volume_usd=self.rng.uniform(10_000, 500_000),
            liquidity_usd=self.rng.uniform(5_000, 200_000),
            active=True,
        )

    def generate_price_series(
        self,
        initial_price: float,
        steps: int = 100,
        volatility: float = 0.02,
        drift: float = 0.0,
        resolution: bool | None = None,
    ) -> list[float]:
        """Generate a price random walk, optionally resolving to 0 or 1.

        Uses geometric Brownian motion bounded to (0, 1).
        """
        prices = [initial_price]
        p = initial_price

        for i in range(steps - 1):
            # As we approach the end, price should converge if resolving
            progress = i / (steps - 1)

            if resolution is not None and progress > 0.7:
                target = 1.0 if resolution else 0.0
                pull = (target - p) * (progress - 0.7) / 0.3 * 0.1
            else:
                pull = 0

            noise = self.rng.gauss(0, volatility)
            dp = drift + noise + pull
            p = p + dp
            p = max(0.01, min(0.99, p))
            prices.append(round(p, 4))

        return prices

    def generate_order_book(
        self,
        token_id: str,
        mid_price: float,
        spread_bps: int = 100,
        depth_levels: int = 5,
        base_size: float = 100.0,
    ) -> OrderBook:
        """Generate a synthetic order book around a mid price."""
        spread = spread_bps / 10000
        half_spread = spread / 2

        bids: list[OrderBookLevel] = []
        asks: list[OrderBookLevel] = []

        for i in range(depth_levels):
            offset = half_spread + i * spread * 0.5
            bid_price = max(0.01, mid_price - offset)
            ask_price = min(0.99, mid_price + offset)
            # Size decreases away from mid
            size = base_size * (depth_levels - i) / depth_levels
            size *= self.rng.uniform(0.5, 1.5)

            bids.append(OrderBookLevel(price=round(bid_price, 4), size=round(size, 2)))
            asks.append(OrderBookLevel(price=round(ask_price, 4), size=round(size, 2)))

        bids.sort(key=lambda x: x.price, reverse=True)
        asks.sort(key=lambda x: x.price)

        return OrderBook(token_id=token_id, bids=bids, asks=asks)

    def generate_scenario(
        self,
        num_markets: int = 20,
        time_steps: int = 100,
        categories: list[str] | None = None,
    ) -> dict[str, Any]:
        """Generate a complete backtest scenario with multiple markets.

        Returns:
            {
                "markets": [Market, ...],
                "price_series": {market_id: {token_id: [prices]}},
                "order_books": {step: {token_id: OrderBook}},
                "resolutions": {market_id: bool | None},
            }
        """
        cats = categories or ["crypto", "finance", "technology"]
        markets: list[Market] = []
        price_series: dict[str, dict[str, list[float]]] = {}
        resolutions: dict[str, bool | None] = {}

        for i in range(num_markets):
            cat = cats[i % len(cats)]
            market = self.generate_market(category=cat)
            markets.append(market)

            # Decide resolution
            yes_price = market.tokens[0].price
            if yes_price > 0.7:
                resolve = self.rng.random() < 0.7  # Likely yes
            elif yes_price < 0.3:
                resolve = self.rng.random() < 0.3  # Likely no
            else:
                resolve = self.rng.random() < 0.5

            resolutions[market.market_id] = resolve

            yes_prices = self.generate_price_series(
                yes_price, time_steps,
                volatility=self.rng.uniform(0.01, 0.04),
                resolution=resolve,
            )
            no_prices = [round(1 - p, 4) for p in yes_prices]

            price_series[market.market_id] = {
                market.tokens[0].token_id: yes_prices,
                market.tokens[1].token_id: no_prices,
            }

        # Generate order books for each step
        order_books: dict[int, dict[str, OrderBook]] = {}
        for step in range(time_steps):
            step_books: dict[str, OrderBook] = {}
            for market in markets:
                for token in market.tokens:
                    prices = price_series[market.market_id][token.token_id]
                    mid = prices[step]
                    book = self.generate_order_book(
                        token.token_id, mid,
                        spread_bps=self.rng.randint(50, 200),
                    )
                    step_books[token.token_id] = book
            order_books[step] = step_books

        return {
            "markets": markets,
            "price_series": price_series,
            "order_books": order_books,
            "resolutions": resolutions,
            "time_steps": time_steps,
        }
