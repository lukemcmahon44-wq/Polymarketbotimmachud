"""Tests for the execution engine and paper executor."""

from __future__ import annotations

import os

import pytest

from polymarket_bot.config import BotConfig
from polymarket_bot.execution.engine import ExecutionEngine
from polymarket_bot.execution.paper_executor import PaperExecutor
from polymarket_bot.execution.live_executor import LiveExecutor, LiveTradingNotEnabled
from polymarket_bot.types import Order, OrderStatus, Side, Signal, SignalType


def _make_signal(ev: float = 0.05) -> Signal:
    return Signal(
        signal_type=SignalType.MISPRICING,
        market_id="mkt_test",
        token_id="tok_test",
        side=Side.BUY,
        model_prob=0.70,
        market_price=0.55,
        ev=ev,
        confidence=0.80,
        liquidity_usd=50_000,
        metadata={"effective_price": 0.56},
    )


class TestPaperExecutor:
    @pytest.mark.asyncio
    async def test_place_order_fills(self) -> None:
        executor = PaperExecutor(fill_probability=1.0)
        order = Order(
            market_id="mkt_test",
            token_id="tok_test",
            side=Side.BUY,
            price=0.55,
            size_usd=100,
            size_shares=181.82,
        )
        result = await executor.place_order(order)
        assert result.status == OrderStatus.FILLED
        assert result.is_paper
        assert result.fill_price > 0
        assert result.gas_cost_usd > 0

    @pytest.mark.asyncio
    async def test_cancel_order(self) -> None:
        executor = PaperExecutor(fill_probability=0.0)
        order = Order(
            market_id="mkt_test", token_id="tok_test",
            side=Side.BUY, price=0.55, size_usd=100, size_shares=100,
        )
        result = await executor.place_order(order)
        success = await executor.cancel_order(result.order_id)
        assert success
        updated = await executor.get_order_status(result.order_id)
        assert updated is not None
        assert updated.status == OrderStatus.CANCELLED


class TestExecutionEngine:
    @pytest.mark.asyncio
    async def test_signal_to_order(self, config: BotConfig) -> None:
        executor = PaperExecutor()
        engine = ExecutionEngine(executor, config)
        signal = _make_signal()
        order = engine.signal_to_order(signal, 100.0)
        assert order.market_id == "mkt_test"
        assert order.side == Side.BUY
        assert order.size_usd == 100.0
        assert order.is_paper == config.paper_mode

    @pytest.mark.asyncio
    async def test_execute_signal(self, config: BotConfig) -> None:
        executor = PaperExecutor(fill_probability=1.0)
        engine = ExecutionEngine(executor, config)
        signal = _make_signal()
        order = await engine.execute_signal(signal, 100.0)
        assert order.status == OrderStatus.FILLED
        assert len(engine.audit_log) >= 2  # created + submitted

    @pytest.mark.asyncio
    async def test_audit_log_recorded(self, config: BotConfig) -> None:
        executor = PaperExecutor(fill_probability=1.0)
        engine = ExecutionEngine(executor, config)
        signal = _make_signal()
        await engine.execute_signal(signal, 50.0)
        assert len(engine.audit_log) > 0
        assert engine.audit_log[0]["event"] == "order_created"


class TestLiveExecutor:
    @pytest.mark.asyncio
    async def test_live_blocked_paper_mode(self, config: BotConfig) -> None:
        config.paper_mode = True
        executor = LiveExecutor(config)
        order = Order(side=Side.BUY, price=0.55, size_usd=100)
        with pytest.raises(LiveTradingNotEnabled, match="paper_mode"):
            await executor.place_order(order)

    @pytest.mark.asyncio
    async def test_live_blocked_no_env(self, config: BotConfig) -> None:
        config.paper_mode = False
        os.environ.pop("ENABLE_LIVE_TRADING", None)
        executor = LiveExecutor(config)
        executor.confirm_passphrase(config.live_passphrase)
        order = Order(side=Side.BUY, price=0.55, size_usd=100)
        with pytest.raises(LiveTradingNotEnabled, match="ENABLE_LIVE_TRADING"):
            await executor.place_order(order)

    @pytest.mark.asyncio
    async def test_live_blocked_no_passphrase(self, config: BotConfig) -> None:
        config.paper_mode = False
        os.environ["ENABLE_LIVE_TRADING"] = "true"
        executor = LiveExecutor(config)
        # Do NOT confirm passphrase
        order = Order(side=Side.BUY, price=0.55, size_usd=100)
        with pytest.raises(LiveTradingNotEnabled, match="passphrase"):
            await executor.place_order(order)
        os.environ.pop("ENABLE_LIVE_TRADING", None)

    def test_passphrase_confirmation(self, config: BotConfig) -> None:
        executor = LiveExecutor(config)
        assert not executor.confirm_passphrase("wrong passphrase")
        assert executor.confirm_passphrase("I UNDERSTAND AND ENABLE LIVE TRADING")
