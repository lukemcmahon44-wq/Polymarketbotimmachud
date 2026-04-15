"""Tests for copy-trade detection."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from polymarket_bot.config import BotConfig
from polymarket_bot.types import Order, Side
from polymarket_bot.wallet_tracing.detector import CopyTradeDetector, OnChainTrade


class TestCopyTradeDetector:
    def test_no_alert_without_our_trades(self, config: BotConfig) -> None:
        detector = CopyTradeDetector(config)
        trade = OnChainTrade(
            tx_hash="0xabc",
            address="0x1234567890",
            market_id="mkt_1",
            token_id="tok_1",
            side="BUY",
            size=100,
            price=0.55,
            timestamp=datetime.now(timezone.utc),
        )
        alert = detector.analyze_on_chain_trade(trade)
        assert alert is None

    def test_alert_on_matching_trade(self, config: BotConfig) -> None:
        config.copy_trade_detection.min_matching_trades = 1
        config.copy_trade_detection.similarity_threshold = 0.0
        detector = CopyTradeDetector(config)

        # Record our trade
        our_order = Order(
            market_id="mkt_1", token_id="tok_1", side=Side.BUY, size_usd=100,
        )
        detector.record_our_trade(our_order)

        # Simulate on-chain trade from another wallet
        trade = OnChainTrade(
            tx_hash="0xdef",
            address="0xSUSPECT",
            market_id="mkt_1",
            token_id="tok_1",
            side="BUY",
            size=50,
            price=0.56,
            timestamp=datetime.now(timezone.utc),
        )
        alert = detector.analyze_on_chain_trade(trade)
        assert alert is not None
        assert alert.suspect_address == "0xSUSPECT"

    def test_defensive_reduce_size(self, config: BotConfig) -> None:
        config.copy_trade_detection.response = "reduce_size"
        config.copy_trade_detection.size_reduction_factor = 0.5
        config.copy_trade_detection.min_matching_trades = 1
        config.copy_trade_detection.similarity_threshold = 0.0
        detector = CopyTradeDetector(config)

        # Create an alert
        our_order = Order(market_id="mkt_1", side=Side.BUY, size_usd=100)
        detector.record_our_trade(our_order)
        trade = OnChainTrade(
            tx_hash="0x1", address="0xSUSP", market_id="mkt_1",
            token_id="tok_1", side="BUY", size=50, price=0.5,
            timestamp=datetime.now(timezone.utc),
        )
        detector.analyze_on_chain_trade(trade)

        # Apply defensive action
        size, delay = detector.apply_defensive_action(200.0)
        assert size == 100.0  # 50% reduction
        assert delay == 0.0

    def test_watchlist(self, config: BotConfig) -> None:
        config.copy_trade_detection.min_matching_trades = 1
        config.copy_trade_detection.similarity_threshold = 0.0
        detector = CopyTradeDetector(config)

        our_order = Order(market_id="mkt_1", side=Side.BUY, size_usd=100)
        detector.record_our_trade(our_order)

        for i in range(3):
            trade = OnChainTrade(
                tx_hash=f"0x{i}", address="0xWATCHED",
                market_id="mkt_1", token_id="tok_1",
                side="BUY", size=50, price=0.5,
                timestamp=datetime.now(timezone.utc),
            )
            detector.analyze_on_chain_trade(trade)

        watchlist = detector.get_watchlist()
        assert len(watchlist) >= 1
        assert watchlist[0]["matches"] >= 3

    def test_disabled_detection(self, config: BotConfig) -> None:
        config.copy_trade_detection.enabled = False
        detector = CopyTradeDetector(config)
        our_order = Order(market_id="mkt_1", side=Side.BUY, size_usd=100)
        detector.record_our_trade(our_order)
        trade = OnChainTrade(
            tx_hash="0x1", address="0xADDR", market_id="mkt_1",
            token_id="tok_1", side="BUY", size=50, price=0.5,
            timestamp=datetime.now(timezone.utc),
        )
        assert detector.analyze_on_chain_trade(trade) is None
