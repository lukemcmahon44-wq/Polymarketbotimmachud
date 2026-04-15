"""Shared domain types used across all modules."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class SignalType(str, Enum):
    MISPRICING = "MISPRICING"
    ARBITRAGE = "ARBITRAGE"
    HEDGED_ARBITRAGE = "HEDGED_ARBITRAGE"


class DefensiveAction(str, Enum):
    REDUCE_SIZE = "reduce_size"
    RANDOMIZE_TIMING = "randomize_timing"
    FLAG_ONLY = "flag_only"


@dataclass
class Market:
    """Represents a Polymarket market."""
    market_id: str
    condition_id: str
    question: str
    category: str
    end_date: datetime
    tokens: list[Token]
    volume_usd: float = 0.0
    liquidity_usd: float = 0.0
    active: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Token:
    """A token in a binary/multi-outcome market (YES/NO)."""
    token_id: str
    outcome: str  # "Yes", "No", etc.
    price: float  # 0.0 to 1.0
    winner: bool | None = None


@dataclass
class OrderBook:
    """Snapshot of an order book for a token."""
    token_id: str
    bids: list[OrderBookLevel] = field(default_factory=list)
    asks: list[OrderBookLevel] = field(default_factory=list)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def best_bid(self) -> float:
        return self.bids[0].price if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0].price if self.asks else 1.0

    @property
    def spread(self) -> float:
        return self.best_ask - self.best_bid

    @property
    def mid_price(self) -> float:
        return (self.best_bid + self.best_ask) / 2


@dataclass
class OrderBookLevel:
    price: float
    size: float


@dataclass
class Signal:
    """A trading signal produced by the signal engine."""
    signal_id: str = field(default_factory=lambda: str(uuid.uuid4())[:12])
    signal_type: SignalType = SignalType.MISPRICING
    market_id: str = ""
    token_id: str = ""
    side: Side = Side.BUY
    model_prob: float = 0.0
    market_price: float = 0.0
    ev: float = 0.0
    confidence: float = 0.0
    liquidity_usd: float = 0.0
    suggested_size_usd: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def ev_after_fees(self) -> float:
        return self.ev - 0.02  # 2% fee approximation


@dataclass
class Order:
    """An order in the system."""
    order_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    signal_id: str = ""
    market_id: str = ""
    token_id: str = ""
    side: Side = Side.BUY
    order_type: OrderType = OrderType.LIMIT
    price: float = 0.0
    size_usd: float = 0.0
    size_shares: float = 0.0
    status: OrderStatus = OrderStatus.PENDING
    filled_size: float = 0.0
    fill_price: float = 0.0
    slippage: float = 0.0
    gas_cost_usd: float = 0.0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime | None = None
    is_paper: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Position:
    """A tracked position."""
    position_id: str = field(default_factory=lambda: str(uuid.uuid4())[:12])
    market_id: str = ""
    token_id: str = ""
    side: Side = Side.BUY
    entry_price: float = 0.0
    current_price: float = 0.0
    size_usd: float = 0.0
    size_shares: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    category: str = ""
    stop_loss_price: float | None = None

    @property
    def pnl_pct(self) -> float:
        if self.entry_price == 0:
            return 0.0
        if self.side == Side.BUY:
            return (self.current_price - self.entry_price) / self.entry_price
        return (self.entry_price - self.current_price) / self.entry_price


@dataclass
class CopyTradeAlert:
    """Alert for suspected copy-trading activity."""
    alert_id: str = field(default_factory=lambda: str(uuid.uuid4())[:12])
    suspect_address: str = ""
    similarity_score: float = 0.0
    matching_trades: int = 0
    time_window_seconds: int = 600
    details: list[dict[str, Any]] = field(default_factory=list)
    recommended_action: DefensiveAction = DefensiveAction.FLAG_ONLY
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class BacktestResult:
    """Results from a backtest run."""
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl: float = 0.0
    max_drawdown: float = 0.0
    sharpe_ratio: float = 0.0
    win_rate: float = 0.0
    avg_trade_pnl: float = 0.0
    realized_vs_expected: float = 0.0
    trades: list[Order] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
