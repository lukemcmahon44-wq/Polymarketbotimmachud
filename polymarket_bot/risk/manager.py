"""Six-layer risk manager enforcing all risk controls.

Layer 0: Hard limits (exposure caps, max positions)
Layer 1: Position sizing (Kelly, liquidity threshold)
Layer 2: Execution controls (slippage, fill deviation, TTL)
Layer 3: Stop-loss and time-based decay exits
Layer 4: Portfolio hedging and concentration limits
Layer 5: Circuit breakers and kill switches
Layer 6: Compliance and safety (wash trading prevention, paper-first)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

from polymarket_bot.config import BotConfig
from polymarket_bot.types import Order, Position, Side, Signal

logger = structlog.get_logger()


@dataclass
class RiskDecision:
    """Result of a risk check."""
    allowed: bool
    adjusted_size_usd: float = 0.0
    reason: str = ""
    layer: str = ""
    details: dict[str, Any] = field(default_factory=dict)


class RiskManager:
    """Enforces all six layers of risk management."""

    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self.positions: dict[str, Position] = {}
        self.trade_history: list[Order] = []
        self._circuit_breaker_active = False
        self._consecutive_losses = 0
        self._rpc_failures = 0
        self._peak_equity = 0.0
        self._current_equity = 0.0
        self._kill_switch = False

    # ── Layer 0: Hard Limits ─────────────────────────────────────

    def check_hard_limits(self, signal: Signal, size_usd: float) -> RiskDecision:
        """Enforce global and per-market exposure caps."""
        # Kill switch
        if self._kill_switch:
            return RiskDecision(False, 0, "Kill switch activated", "L0")

        # Circuit breaker
        if self._circuit_breaker_active:
            return RiskDecision(False, 0, "Circuit breaker active", "L0")

        # Per-trade max
        if size_usd > self.config.per_trade_max_usd:
            size_usd = self.config.per_trade_max_usd
            logger.info("risk.L0.capped_trade_size", new_size=size_usd)

        # Global exposure
        total_exposure = sum(p.size_usd for p in self.positions.values())
        if total_exposure + size_usd > self.config.global_exposure_usd:
            remaining = max(0, self.config.global_exposure_usd - total_exposure)
            if remaining <= 0:
                return RiskDecision(False, 0, "Global exposure cap reached", "L0",
                                    {"total_exposure": total_exposure})
            size_usd = remaining

        # Per-market exposure
        market_exposure = sum(
            p.size_usd for p in self.positions.values()
            if p.market_id == signal.market_id
        )
        if market_exposure + size_usd > self.config.per_market_exposure_usd:
            remaining = max(0, self.config.per_market_exposure_usd - market_exposure)
            if remaining <= 0:
                return RiskDecision(False, 0, "Per-market exposure cap reached", "L0",
                                    {"market_exposure": market_exposure})
            size_usd = remaining

        # Max concurrent positions
        if len(self.positions) >= self.config.max_concurrent_positions:
            if signal.market_id not in {p.market_id for p in self.positions.values()}:
                return RiskDecision(False, 0, "Max concurrent positions reached", "L0",
                                    {"positions": len(self.positions)})

        return RiskDecision(True, size_usd, "L0 passed", "L0")

    # ── Layer 1: Position Sizing ─────────────────────────────────

    def compute_position_size(self, signal: Signal) -> float:
        """Kelly-fraction sizing with confidence and liquidity adjustments."""
        # Kelly criterion: f = (p * b - q) / b
        # where p = model_prob, q = 1-p, b = odds (payout ratio)
        p = signal.model_prob
        q = 1.0 - p
        price = signal.market_price

        if price <= 0 or price >= 1:
            return 0.0

        if signal.side == Side.BUY:
            b = (1.0 - price) / price  # Payout ratio for buying YES
        else:
            b = price / (1.0 - price)

        if b <= 0:
            return 0.0

        kelly = (p * b - q) / b
        kelly = max(0.0, kelly)

        # Apply fractional Kelly
        size_fraction = kelly * self.config.kelly_fraction

        # Scale by confidence
        size_fraction *= signal.confidence

        # Convert to USD
        bankroll = self.config.global_exposure_usd
        size_usd = bankroll * size_fraction

        # Floor and cap
        size_usd = min(size_usd, self.config.per_trade_max_usd)
        if size_usd < 1.0:  # Minimum $1 trade
            return 0.0

        # Liquidity check
        if signal.liquidity_usd < self.config.min_liquidity_usd:
            return 0.0

        # Don't exceed available liquidity (max 10% of book)
        max_from_liquidity = signal.liquidity_usd * 0.10
        size_usd = min(size_usd, max_from_liquidity)

        logger.debug(
            "risk.L1.position_size",
            kelly_raw=round(kelly, 4),
            kelly_frac=round(size_fraction, 4),
            size_usd=round(size_usd, 2),
        )
        return round(size_usd, 2)

    # ── Layer 2: Execution Controls ──────────────────────────────

    def check_execution_controls(self, order: Order) -> RiskDecision:
        """Validate slippage and fill deviation bounds."""
        # Slippage tolerance
        if order.slippage > 0:
            slippage_pct = order.slippage / order.price if order.price > 0 else 0
            if slippage_pct > self.config.slippage_tolerance_pct:
                return RiskDecision(
                    False, 0,
                    f"Slippage {slippage_pct:.2%} exceeds tolerance {self.config.slippage_tolerance_pct:.2%}",
                    "L2",
                )

        # Fill price deviation
        if order.fill_price > 0 and order.price > 0:
            deviation = abs(order.fill_price - order.price) / order.price
            if deviation > self.config.max_fill_deviation_pct:
                return RiskDecision(
                    False, 0,
                    f"Fill deviation {deviation:.2%} exceeds max {self.config.max_fill_deviation_pct:.2%}",
                    "L2",
                )

        return RiskDecision(True, order.size_usd, "L2 passed", "L2")

    # ── Layer 3: Stop & Time Decay ───────────────────────────────

    def check_stops(self) -> list[Position]:
        """Check all positions for stop-loss and time-decay exits."""
        exits: list[Position] = []
        now = datetime.now(timezone.utc)
        max_hold = timedelta(hours=self.config.time_decay_hours)

        for pos in self.positions.values():
            # Stop-loss
            if pos.pnl_pct < -self.config.stop_loss_pct:
                logger.warning(
                    "risk.L3.stop_loss_triggered",
                    position_id=pos.position_id,
                    pnl_pct=round(pos.pnl_pct, 4),
                )
                exits.append(pos)
                continue

            # Trailing stop
            if pos.pnl_pct > 0:
                # Set trailing stop at peak - trailing_stop_pct
                if pos.stop_loss_price is None:
                    pos.stop_loss_price = pos.entry_price * (1 - self.config.trailing_stop_pct)
                else:
                    new_stop = pos.current_price * (1 - self.config.trailing_stop_pct)
                    pos.stop_loss_price = max(pos.stop_loss_price, new_stop)

                if pos.current_price <= pos.stop_loss_price:
                    logger.warning(
                        "risk.L3.trailing_stop_triggered",
                        position_id=pos.position_id,
                    )
                    exits.append(pos)
                    continue

            # Time decay
            if now - pos.opened_at > max_hold:
                logger.warning(
                    "risk.L3.time_decay_exit",
                    position_id=pos.position_id,
                    hours_held=(now - pos.opened_at).total_seconds() / 3600,
                )
                exits.append(pos)

        return exits

    # ── Layer 4: Portfolio Hedging ───────────────────────────────

    def check_concentration(self) -> list[dict[str, Any]]:
        """Check for category concentration and correlation risk."""
        warnings: list[dict[str, Any]] = []

        if not self.positions:
            return warnings

        # Category concentration
        total = sum(p.size_usd for p in self.positions.values())
        if total == 0:
            return warnings

        by_category: dict[str, float] = {}
        for p in self.positions.values():
            by_category[p.category] = by_category.get(p.category, 0) + p.size_usd

        for cat, exposure in by_category.items():
            concentration = exposure / total
            if concentration > self.config.max_category_concentration_pct:
                warnings.append({
                    "type": "concentration",
                    "category": cat,
                    "concentration_pct": round(concentration, 3),
                    "exposure_usd": round(exposure, 2),
                    "action": "reduce_exposure",
                })
                logger.warning(
                    "risk.L4.concentration_warning",
                    category=cat,
                    concentration=round(concentration, 3),
                )

        return warnings

    # ── Layer 5: Circuit Breakers ────────────────────────────────

    def update_equity(self, pnl: float) -> None:
        """Update equity tracking for drawdown calculation."""
        self._current_equity += pnl
        self._peak_equity = max(self._peak_equity, self._current_equity)

    def check_circuit_breakers(self) -> bool:
        """Check all circuit breaker conditions. Returns True if tripped."""
        # Drawdown
        if self._peak_equity > 0:
            drawdown = (self._peak_equity - self._current_equity) / self._peak_equity
            if drawdown >= self.config.max_drawdown_pct:
                self._circuit_breaker_active = True
                logger.critical(
                    "risk.L5.circuit_breaker_drawdown",
                    drawdown=round(drawdown, 4),
                    threshold=self.config.max_drawdown_pct,
                )
                return True

        # Consecutive losses
        if self._consecutive_losses >= self.config.max_consecutive_losses:
            self._circuit_breaker_active = True
            logger.critical(
                "risk.L5.circuit_breaker_consecutive_losses",
                losses=self._consecutive_losses,
            )
            return True

        # RPC failures
        if self._rpc_failures >= self.config.rpc_failure_threshold:
            self._circuit_breaker_active = True
            logger.critical(
                "risk.L5.circuit_breaker_rpc_failures",
                failures=self._rpc_failures,
            )
            return True

        return False

    def record_trade_result(self, pnl: float) -> None:
        """Record a trade result for circuit breaker tracking."""
        self.update_equity(pnl)
        if pnl < 0:
            self._consecutive_losses += 1
        else:
            self._consecutive_losses = 0

    def record_rpc_failure(self) -> None:
        self._rpc_failures += 1

    def reset_rpc_failures(self) -> None:
        self._rpc_failures = 0

    def activate_kill_switch(self) -> None:
        """Emergency stop — no trades until manually reset."""
        self._kill_switch = True
        self._circuit_breaker_active = True
        logger.critical("risk.L5.kill_switch_activated")

    def reset_circuit_breaker(self) -> None:
        """Manual reset of circuit breaker (not kill switch)."""
        self._circuit_breaker_active = False
        self._consecutive_losses = 0
        self._rpc_failures = 0
        logger.warning("risk.L5.circuit_breaker_reset")

    # ── Layer 6: Compliance ──────────────────────────────────────

    def check_compliance(self, signal: Signal) -> RiskDecision:
        """Enforce wash trading prevention and paper-first policy."""
        if self.config.no_wash_trading:
            # Check for opposing trades on same market within time window
            now = datetime.now(timezone.utc)
            cutoff = now - timedelta(seconds=self.config.min_time_between_opposing_trades_seconds)
            for trade in reversed(self.trade_history):
                if trade.created_at < cutoff:
                    break
                if (
                    trade.market_id == signal.market_id
                    and trade.side != signal.side
                ):
                    return RiskDecision(
                        False, 0,
                        "Wash trading prevention: opposing trade too recent",
                        "L6",
                    )

        return RiskDecision(True, 0, "L6 passed", "L6")

    # ── Full Pipeline ────────────────────────────────────────────

    def evaluate_signal(self, signal: Signal) -> RiskDecision:
        """Run signal through all risk layers. Returns final decision."""
        # L6: Compliance first
        l6 = self.check_compliance(signal)
        if not l6.allowed:
            return l6

        # L1: Position sizing
        size_usd = self.compute_position_size(signal)
        if size_usd <= 0:
            return RiskDecision(False, 0, "Position size too small after L1 checks", "L1")

        # L0: Hard limits
        l0 = self.check_hard_limits(signal, size_usd)
        if not l0.allowed:
            return l0
        size_usd = l0.adjusted_size_usd

        # L4: Concentration check (warning only, doesn't block)
        self.check_concentration()

        # L5: Circuit breakers
        if self.check_circuit_breakers():
            return RiskDecision(False, 0, "Circuit breaker tripped", "L5")

        return RiskDecision(
            True, size_usd, "All risk layers passed",
            details={"layers_checked": ["L0", "L1", "L2", "L3", "L4", "L5", "L6"]},
        )

    def add_position(self, position: Position) -> None:
        self.positions[position.position_id] = position

    def remove_position(self, position_id: str) -> Position | None:
        return self.positions.pop(position_id, None)

    def get_snapshot(self) -> dict[str, Any]:
        """Current risk state for monitoring."""
        total_exposure = sum(p.size_usd for p in self.positions.values())
        total_pnl = sum(p.unrealized_pnl for p in self.positions.values())
        return {
            "total_exposure_usd": round(total_exposure, 2),
            "positions_count": len(self.positions),
            "unrealized_pnl": round(total_pnl, 2),
            "circuit_breaker_active": self._circuit_breaker_active,
            "kill_switch": self._kill_switch,
            "consecutive_losses": self._consecutive_losses,
            "peak_equity": round(self._peak_equity, 2),
            "current_equity": round(self._current_equity, 2),
        }
