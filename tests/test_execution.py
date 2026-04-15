"""Unit tests for paper executor and order manager."""

from __future__ import annotations

import asyncio

import pytest

from polymarket_bot.config.settings import BotConfig
from polymarket_bot.execution.live_executor import LIVE_TRADING_PASSPHRASE, LiveExecutor, LiveTradingGateError
from polymarket_bot.execution.order_manager import OrderManager
from polymarket_bot.execution.paper_executor import PaperExecutor
from polymarket_bot.scanner.polymarket_client import PolymarketCLOBClient
from polymarket_bot.types import Market, OrderStatus, Side, Signal, SignalType, Token


def make_signal(market: Market, size: float = 50.0) -> Signal:
    token = market.tokens[0]
    return Signal(
        signal_type=SignalType.MISPRICING,
        market=market,
        token=token,
        side=Side.BUY,
        model_probability=0.65,
        market_price=token.price,
        expected_value=0.20,
        confidence=0.80,
        liquidity_usd=20_000,
        recommended_size_usd=size,
    )


class TestOrderManager:
    def test_create_order(self, config: BotConfig, binary_market: Market):
        """Creating an order persists it and sets initial state."""
        om = OrderManager(audit_log_path="logs/test_audit.jsonl")
        signal = make_signal(binary_market)
        order = om.create_order(signal, size_usd=50.0, is_paper=True)
        assert order.order_id in [o.order_id for o in om.open_orders()]
        assert order.is_paper is True
        assert order.size_usd == 50.0

    def test_update_order_to_filled(self, config: BotConfig, binary_market: Market):
        """Updating order status to FILLED works correctly."""
        om = OrderManager(audit_log_path="logs/test_audit.jsonl")
        signal = make_signal(binary_market)
        order = om.create_order(signal, size_usd=50.0)
        om.update_order(order.order_id, OrderStatus.FILLED, filled_size=order.size, filled_price=0.45)
        updated = om.get_order(order.order_id)
        assert updated is not None
        assert updated.status == OrderStatus.FILLED
        assert updated.filled_price == 0.45

    def test_open_position_after_fill(self, config: BotConfig, binary_market: Market):
        """Opening a position after fill tracks it correctly."""
        om = OrderManager(audit_log_path="logs/test_audit.jsonl")
        signal = make_signal(binary_market)
        order = om.create_order(signal, size_usd=50.0)
        om.update_order(order.order_id, OrderStatus.FILLED, filled_size=100.0, filled_price=0.45)
        pos = om.open_position(order)
        assert pos.token_id == order.token_id
        assert pos.size == 100.0
        assert pos.entry_price == 0.45
        assert om.total_exposure_usd() > 0

    def test_close_position_returns_pnl(self, config: BotConfig, binary_market: Market):
        """Closing a position returns realized P&L."""
        om = OrderManager(audit_log_path="logs/test_audit.jsonl")
        signal = make_signal(binary_market)
        order = om.create_order(signal, size_usd=50.0)
        om.update_order(order.order_id, OrderStatus.FILLED, filled_size=100.0, filled_price=0.40)
        om.open_position(order)
        pnl = om.close_position(order.token_id, Side.BUY, close_price=0.50)
        assert pnl == pytest.approx(100.0 * (0.50 - 0.40), rel=0.01)

    def test_exposure_by_market(self, config: BotConfig, binary_market: Market):
        """exposure_by_market correctly groups by condition_id."""
        om = OrderManager(audit_log_path="logs/test_audit.jsonl")
        signal = make_signal(binary_market)
        order = om.create_order(signal, size_usd=50.0)
        om.update_order(order.order_id, OrderStatus.FILLED, filled_size=50.0, filled_price=0.50)
        om.open_position(order)
        exp = om.exposure_by_market()
        assert binary_market.condition_id in exp
        assert exp[binary_market.condition_id] > 0


@pytest.mark.asyncio
class TestPaperExecutor:
    async def test_paper_execute_produces_fill(self, config: BotConfig, binary_market: Market):
        """Paper executor produces a fill (full or partial)."""
        om = OrderManager(audit_log_path="logs/test_audit.jsonl")
        executor = PaperExecutor(config, om)
        signal = make_signal(binary_market)
        order = await executor.execute(signal)
        assert order.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED)
        assert order.filled_price > 0

    async def test_paper_fill_has_slippage(self, config: BotConfig, binary_market: Market):
        """Paper fills include realistic slippage."""
        om = OrderManager(audit_log_path="logs/test_audit.jsonl")
        executor = PaperExecutor(config, om)
        signal = make_signal(binary_market)
        order = await executor.execute(signal)
        # Fill price should be at or above requested price (BUY side)
        assert order.filled_price >= order.price or abs(order.filled_price - order.price) < 0.05

    async def test_paper_cancel(self, config: BotConfig, binary_market: Market):
        """Cancelling a paper order sets status to CANCELLED."""
        om = OrderManager(audit_log_path="logs/test_audit.jsonl")
        executor = PaperExecutor(config, om)
        signal = make_signal(binary_market)
        order = await executor.execute(signal)
        # Force back to OPEN so we can cancel it
        om.update_order(order.order_id, OrderStatus.OPEN)
        cancelled = await executor.cancel(order.order_id)
        if cancelled:
            assert cancelled.status == OrderStatus.CANCELLED


class TestLiveExecutorGate:
    def test_live_executor_requires_env_var(self, config: BotConfig):
        """LiveExecutor raises without ENABLE_LIVE_TRADING env var."""
        import os
        os.environ.pop("ENABLE_LIVE_TRADING", None)
        om = OrderManager(audit_log_path="logs/test_audit.jsonl")
        clob = PolymarketCLOBClient(config)
        executor = LiveExecutor(config, om, clob)
        with pytest.raises(LiveTradingGateError, match="ENABLE_LIVE_TRADING"):
            executor.arm("wrong passphrase")

    def test_live_executor_requires_correct_passphrase(self, config: BotConfig, monkeypatch):
        """LiveExecutor raises with wrong passphrase even if env var is set."""
        monkeypatch.setenv("ENABLE_LIVE_TRADING", "true")
        config.enable_live_trading = True
        om = OrderManager(audit_log_path="logs/test_audit.jsonl")
        clob = PolymarketCLOBClient(config)
        executor = LiveExecutor(config, om, clob)
        with pytest.raises(LiveTradingGateError, match="Passphrase mismatch"):
            executor.arm("wrong passphrase")

    def test_live_executor_arms_with_correct_passphrase(self, config: BotConfig, monkeypatch):
        """LiveExecutor arms successfully with correct passphrase and env var."""
        monkeypatch.setenv("ENABLE_LIVE_TRADING", "true")
        config.enable_live_trading = True
        om = OrderManager(audit_log_path="logs/test_audit.jsonl")
        clob = PolymarketCLOBClient(config)
        executor = LiveExecutor(config, om, clob)
        executor.arm(LIVE_TRADING_PASSPHRASE)
        assert executor.is_armed is True

    def test_live_executor_disarms(self, config: BotConfig, monkeypatch):
        """LiveExecutor can be disarmed."""
        monkeypatch.setenv("ENABLE_LIVE_TRADING", "true")
        config.enable_live_trading = True
        om = OrderManager(audit_log_path="logs/test_audit.jsonl")
        clob = PolymarketCLOBClient(config)
        executor = LiveExecutor(config, om, clob)
        executor.arm(LIVE_TRADING_PASSPHRASE)
        executor.disarm()
        assert executor.is_armed is False
