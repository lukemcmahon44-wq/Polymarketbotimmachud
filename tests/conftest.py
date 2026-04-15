"""Shared fixtures for all tests."""

from __future__ import annotations

import pytest

from polymarket_bot.config.settings import BotConfig, RiskConfig, ScannerConfig
from polymarket_bot.types import Market, OrderBook, PriceLevel, Token


@pytest.fixture
def config() -> BotConfig:
    """Conservative test config with small limits."""
    cfg = BotConfig()
    cfg.paper_mode = True
    cfg.enable_live_trading = False
    cfg.ev_threshold = 0.02
    cfg.min_confidence = 0.5
    cfg.risk = RiskConfig(
        global_exposure_usd=1000.0,
        per_market_exposure_usd=200.0,
        per_trade_max_usd=100.0,
        max_concurrent_positions=5,
        kelly_fraction=0.25,
        min_liquidity_usd=500.0,
        slippage_tolerance_pct=0.02,
        stop_loss_pct=0.15,
        drawdown_circuit_breaker_pct=0.20,
        max_daily_loss_usd=500.0,
    )
    cfg.scanner = ScannerConfig(min_volume_usd=100.0, min_liquidity_usd=500.0)
    return cfg


@pytest.fixture
def binary_market() -> Market:
    """A simple binary YES/NO market."""
    return Market(
        condition_id="test_market_001",
        question="Will BTC exceed $100k by end of 2025?",
        tokens=[
            Token(token_id="yes_001", outcome="Yes", price=0.45, book_depth_usd=10_000),
            Token(token_id="no_001", outcome="No", price=0.55, book_depth_usd=10_000),
        ],
        category="crypto",
        liquidity_usd=50_000,
        volume_usd=100_000,
    )


@pytest.fixture
def mispriced_market() -> Market:
    """A binary market with detectable mispricing (YES underpriced)."""
    return Market(
        condition_id="test_market_002",
        question="Will ETH 2.0 staking yield > 5%?",
        tokens=[
            Token(token_id="yes_002", outcome="Yes", price=0.30, book_depth_usd=15_000),
            Token(token_id="no_002", outcome="No", price=0.70, book_depth_usd=15_000),
        ],
        category="crypto",
        liquidity_usd=30_000,
        volume_usd=60_000,
    )


@pytest.fixture
def arb_market() -> Market:
    """A market where YES + NO < 1.0 (complement arbitrage opportunity)."""
    return Market(
        condition_id="test_market_003",
        question="Will inflation drop below 3%?",
        tokens=[
            Token(token_id="yes_003", outcome="Yes", price=0.42, book_depth_usd=8_000),
            Token(token_id="no_003", outcome="No", price=0.52, book_depth_usd=8_000),
        ],
        category="finance",
        liquidity_usd=20_000,
        volume_usd=40_000,
    )


@pytest.fixture
def order_book_pair(binary_market: Market) -> dict[str, OrderBook]:
    """Order books for the binary market fixture."""
    yes_token = binary_market.tokens[0]
    no_token = binary_market.tokens[1]

    yes_book = OrderBook(
        token_id=yes_token.token_id,
        bids=[PriceLevel(0.44, 500), PriceLevel(0.43, 1000), PriceLevel(0.42, 2000)],
        asks=[PriceLevel(0.46, 500), PriceLevel(0.47, 1000), PriceLevel(0.48, 2000)],
    )
    no_book = OrderBook(
        token_id=no_token.token_id,
        bids=[PriceLevel(0.54, 500), PriceLevel(0.53, 1000), PriceLevel(0.52, 2000)],
        asks=[PriceLevel(0.56, 500), PriceLevel(0.57, 1000), PriceLevel(0.58, 2000)],
    )
    return {yes_token.token_id: yes_book, no_token.token_id: no_book}


@pytest.fixture
def arb_order_books(arb_market: Market) -> dict[str, OrderBook]:
    """Order books for the arb market (YES + NO best ask = 0.94 < 1.0)."""
    yes_token = arb_market.tokens[0]
    no_token = arb_market.tokens[1]

    yes_book = OrderBook(
        token_id=yes_token.token_id,
        bids=[PriceLevel(0.41, 300)],
        asks=[PriceLevel(0.42, 300)],
    )
    no_book = OrderBook(
        token_id=no_token.token_id,
        bids=[PriceLevel(0.51, 300)],
        asks=[PriceLevel(0.52, 300)],
    )
    return {yes_token.token_id: yes_book, no_token.token_id: no_book}
