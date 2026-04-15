"""Synthetic Polymarket-like market data generator for backtesting."""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterator

from polymarket_bot.types import Market, OrderBook, PriceLevel, Token


@dataclass
class MarketSnapshot:
    """A point-in-time snapshot of market state."""

    market: Market
    order_books: dict[str, OrderBook]
    timestamp: datetime
    true_probability: float  # Ground truth for evaluation


class SyntheticMarketGenerator:
    """Generates realistic Polymarket-like market snapshots for backtesting.

    Models:
    - Binary YES/NO markets with Brownian-motion price evolution
    - Mispricing events: periodic 2–8% price dislocations
    - Liquidity variation: realistic book depth
    - Resolution: deterministic based on true probability
    """

    def __init__(
        self,
        n_markets: int = 20,
        n_steps: int = 500,
        step_seconds: int = 60,
        mispricing_freq: float = 0.05,  # 5% of steps have a mispricing
        seed: int = 42,
    ) -> None:
        self._n_markets = n_markets
        self._n_steps = n_steps
        self._step_seconds = step_seconds
        self._mispricing_freq = mispricing_freq
        self._rng = random.Random(seed)

    def generate(self) -> list[list[MarketSnapshot]]:
        """Generate n_markets × n_steps grid of market snapshots."""
        all_snapshots: list[list[MarketSnapshot]] = []

        for i in range(self._n_markets):
            true_prob = self._rng.uniform(0.15, 0.85)
            start_price = true_prob + self._rng.uniform(-0.10, 0.10)
            start_price = max(0.05, min(0.95, start_price))
            liquidity = self._rng.uniform(5_000, 100_000)
            volume = liquidity * self._rng.uniform(0.5, 3.0)

            market = self._make_market(i, true_prob, liquidity, volume)
            snapshots = self._simulate_market(market, start_price, true_prob)
            all_snapshots.append(snapshots)

        return all_snapshots

    def iter_steps(self) -> Iterator[tuple[datetime, list[MarketSnapshot]]]:
        """Yield each time step with all market snapshots at that step."""
        all_snapshots = self.generate()
        n_steps = len(all_snapshots[0]) if all_snapshots else 0

        for step in range(n_steps):
            ts = all_snapshots[0][step].timestamp
            yield ts, [market_snaps[step] for market_snaps in all_snapshots]

    def _make_market(
        self, idx: int, true_prob: float, liquidity: float, volume: float
    ) -> Market:
        condition_id = f"synthetic_{idx:04d}"
        yes_id = f"token_yes_{idx:04d}"
        no_id = f"token_no_{idx:04d}"

        return Market(
            condition_id=condition_id,
            question=f"Synthetic market #{idx} — will outcome A occur?",
            tokens=[
                Token(token_id=yes_id, outcome="Yes", price=true_prob),
                Token(token_id=no_id, outcome="No", price=1 - true_prob),
            ],
            category="synthetic",
            end_date=datetime.now(timezone.utc) + timedelta(days=7),
            active=True,
            liquidity_usd=liquidity,
            volume_usd=volume,
        )

    def _simulate_market(
        self, market: Market, start_price: float, true_prob: float
    ) -> list[MarketSnapshot]:
        snapshots: list[MarketSnapshot] = []
        price = start_price
        base_ts = datetime.now(timezone.utc)
        yes_id = market.tokens[0].token_id
        no_id = market.tokens[1].token_id

        for step in range(self._n_steps):
            ts = base_ts + timedelta(seconds=step * self._step_seconds)

            # Brownian motion with mean reversion toward true_prob
            drift = 0.001 * (true_prob - price)  # Mean reversion
            shock = self._rng.gauss(0, 0.008)
            price = max(0.02, min(0.98, price + drift + shock))

            # Inject mispricing
            has_mispricing = self._rng.random() < self._mispricing_freq
            if has_mispricing:
                direction = 1 if self._rng.random() > 0.5 else -1
                magnitude = self._rng.uniform(0.02, 0.08)
                misprice = max(0.02, min(0.98, price + direction * magnitude))
            else:
                misprice = price

            # Build order book
            spread = self._rng.uniform(0.003, 0.015)
            yes_mid = misprice
            no_mid = 1 - misprice
            depth = market.liquidity_usd / 10

            yes_book = self._make_book(yes_id, yes_mid, spread, depth)
            no_book = self._make_book(no_id, no_mid, spread, depth)

            # Update token prices
            import copy
            snap_market = copy.deepcopy(market)
            snap_market.tokens[0].price = yes_mid
            snap_market.tokens[1].price = no_mid

            snapshots.append(
                MarketSnapshot(
                    market=snap_market,
                    order_books={yes_id: yes_book, no_id: no_book},
                    timestamp=ts,
                    true_probability=true_prob,
                )
            )

        return snapshots

    def _make_book(
        self, token_id: str, mid: float, spread: float, depth_usd: float
    ) -> OrderBook:
        half = spread / 2
        best_bid = max(0.01, mid - half)
        best_ask = min(0.99, mid + half)
        size_per_level = depth_usd / (5 * mid) if mid > 0 else 100

        bids = [
            PriceLevel(price=round(best_bid - i * 0.005, 4), size=size_per_level)
            for i in range(5)
        ]
        asks = [
            PriceLevel(price=round(best_ask + i * 0.005, 4), size=size_per_level)
            for i in range(5)
        ]

        return OrderBook(token_id=token_id, bids=bids, asks=asks, timestamp=datetime.now(timezone.utc))
