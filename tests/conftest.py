"""Shared test fixtures."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from polymarket_bot.config import BotConfig, load_config
from polymarket_bot.types import Market, OrderBook, OrderBookLevel, Token


@pytest.fixture
def config() -> BotConfig:
    """Load default config for testing."""
    cfg = load_config()
    cfg.paper_mode = True
    cfg.min_liquidity_usd = 0  # Allow all markets in tests
    cfg.min_ev_threshold = 0.001
    cfg.confidence_floor = 0.1
    return cfg


@pytest.fixture
def sample_market() -> Market:
    """A sample binary market."""
    return Market(
        market_id="test_market_001",
        condition_id="cond_test_001",
        question="Will BTC exceed $100k by end of month?",
        category="crypto",
        end_date=datetime.now(timezone.utc) + timedelta(days=7),
        tokens=[
            Token(token_id="tok_yes_001", outcome="Yes", price=0.60),
            Token(token_id="tok_no_001", outcome="No", price=0.40),
        ],
        volume_usd=100_000,
        liquidity_usd=50_000,
        active=True,
    )


@pytest.fixture
def sample_markets() -> list[Market]:
    """Multiple sample markets for testing."""
    now = datetime.now(timezone.utc)
    return [
        Market(
            market_id="mkt_btc",
            condition_id="cond_btc",
            question="Will BTC > $100k?",
            category="crypto",
            end_date=now + timedelta(days=7),
            tokens=[
                Token(token_id="btc_yes", outcome="Yes", price=0.65),
                Token(token_id="btc_no", outcome="No", price=0.35),
            ],
            volume_usd=200_000,
            liquidity_usd=80_000,
        ),
        Market(
            market_id="mkt_eth",
            condition_id="cond_eth",
            question="Will ETH > $5k?",
            category="crypto",
            end_date=now + timedelta(days=14),
            tokens=[
                Token(token_id="eth_yes", outcome="Yes", price=0.45),
                Token(token_id="eth_no", outcome="No", price=0.55),
            ],
            volume_usd=150_000,
            liquidity_usd=60_000,
        ),
        # Mispriced market (YES + NO != 1.0)
        Market(
            market_id="mkt_arb",
            condition_id="cond_arb",
            question="Will a mispriced event occur?",
            category="finance",
            end_date=now + timedelta(days=3),
            tokens=[
                Token(token_id="arb_yes", outcome="Yes", price=0.45),
                Token(token_id="arb_no", outcome="No", price=0.50),
            ],
            volume_usd=100_000,
            liquidity_usd=40_000,
        ),
    ]


@pytest.fixture
def sample_order_book() -> OrderBook:
    return OrderBook(
        token_id="tok_yes_001",
        bids=[
            OrderBookLevel(price=0.59, size=500),
            OrderBookLevel(price=0.58, size=1000),
            OrderBookLevel(price=0.57, size=1500),
        ],
        asks=[
            OrderBookLevel(price=0.61, size=500),
            OrderBookLevel(price=0.62, size=1000),
            OrderBookLevel(price=0.63, size=1500),
        ],
    )
