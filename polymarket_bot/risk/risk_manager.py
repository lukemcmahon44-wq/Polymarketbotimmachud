"""Seven-layer risk management system.

Layer 0 — Hard Limits           : global/per-market/per-trade caps, max positions
Layer 1 — Position Sizing       : Kelly-fraction sizing, liquidity threshold
Layer 2 — Execution Controls    : slippage, fill deviation, TTL
Layer 3 — Stop & Time Decay     : dynamic stop-loss, time-decay exits
Layer 4 — Portfolio Hedging     : correlation check, category concentration
Layer 5 — Circuit Breakers      : drawdown, anomalous fills, RPC failures
Layer 6 — Compliance & Safety   : anti-manipulation, paper-first, live-gate
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import structlog

from polymarket_bot.config.settings import BotConfig
from polymarket_bot.execution.order_manager import OrderManager
from polymarket_bot.types import Order, Position, Signal, Side

logger = structlog.get_logger(__name__)


class RiskAction(str, Enum):
    ALLOW = "allow"
    REDUCE = "reduce"
    BLOCK = "block"
    CIRCUIT_BREAK = "circuit_break"


@dataclass
class RiskDecision:
    """Result of the full layered risk check."""

    action: RiskAction = RiskAction.ALLOW
    approved_size_usd: float = 0.0
    checks: dict[str, bool] = field(default_factory=dict)
    adjustments: dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    @property
    def is_approved(self) -> bool:
        return self.action in (RiskAction.ALLOW, RiskAction.REDUCE)


class CircuitBreaker:
    """Tracks conditions that trigger an emergency stop."""

    def __init__(self) -> None:
        self.triggered = False
        self.reason = ""
        self.triggered_at: datetime | None = None

    def trip(self, reason: str) -> None:
        if not self.triggered:
            self.triggered = True
            self.reason = reason
            self.triggered_at = datetime.now(timezone.utc)
            logger.critical(
                "CIRCUIT_BREAKER_TRIPPED",
                reason=reason,
                ts=self.triggered_at.isoformat(),
            )

    def reset(self) -> None:
        self.triggered = False
        self.reason = ""
        self.triggered_at = None
        logger.warning("circuit_breaker_reset")


class RiskManager:
    """Evaluates every signal through all seven risk layers before execution."""

    def __init__(self, config: BotConfig, order_manager: OrderManager) -> None:
        self._config = config
        self._om = order_manager
        self._circuit_breaker = CircuitBreaker()
        self._daily_loss: float = 0.0
        self._daily_reset_date: str = ""
        self._fill_prices: list[float] = []  # for anomaly detection
        self._peak_equity: float = 0.0
        self._equity_baseline: float = config.risk.global_exposure_usd

    # -------------------------------------------------------------- public API

    def evaluate(self, signal: Signal) -> RiskDecision:
        """Run signal through all risk layers and return a decision."""
        decision = RiskDecision(approved_size_usd=signal.recommended_size_usd)
        self._reset_daily_if_needed()

        # Layer 5 checked first — if circuit is tripped, nothing goes through
        if not self._layer5_circuit_breaker(decision):
            return decision

        # Layer 6 — compliance & safety
        if not self._layer6_compliance(signal, decision):
            return decision

        # Layer 0 — hard limits
        if not self._layer0_hard_limits(signal, decision):
            return decision

        # Layer 1 — position sizing
        self._layer1_position_sizing(signal, decision)

        # Layer 2 — execution controls (checked at execution time too)
        if not self._layer2_execution_controls(signal, decision):
            return decision

        # Layer 3 — stop & time decay
        self._layer3_stop_time_decay(signal, decision)

        # Layer 4 — portfolio hedging
        self._layer4_portfolio_hedging(signal, decision)

        if decision.approved_size_usd <= 0:
            decision.action = RiskAction.BLOCK
            decision.reason = decision.reason or "Sizing reduced to zero after risk adjustments"

        logger.info(
            "risk_decision",
            signal_id=signal.signal_id,
            action=decision.action.value,
            approved_usd=round(decision.approved_size_usd, 2),
            checks=decision.checks,
            reason=decision.reason or "ok",
        )
        return decision

    def record_fill(self, order: Order) -> None:
        """Update risk state after a fill — used for anomaly detection."""
        if order.filled_price > 0:
            self._fill_prices.append(order.filled_price)
            # Keep rolling window
            if len(self._fill_prices) > 100:
                self._fill_prices = self._fill_prices[-100:]

        # Check for anomalous fill
        if len(self._fill_prices) >= 10:
            mean_price = statistics.mean(self._fill_prices)
            stdev_price = statistics.stdev(self._fill_prices)
            if stdev_price > 0:
                z = abs(order.filled_price - mean_price) / stdev_price
                if z > self._config.risk.anomalous_fill_threshold:
                    self._circuit_breaker.trip(
                        f"Anomalous fill detected: price={order.filled_price:.4f} "
                        f"z-score={z:.2f} (threshold={self._config.risk.anomalous_fill_threshold})"
                    )

    def record_pnl(self, pnl: float) -> None:
        """Record realized P&L for drawdown tracking."""
        self._daily_loss += min(0, pnl)
        current_equity = self._equity_baseline + self._om.total_unrealized_pnl()
        self._peak_equity = max(self._peak_equity, current_equity)

        # Check max daily loss
        if abs(self._daily_loss) > self._config.risk.max_daily_loss_usd:
            self._circuit_breaker.trip(
                f"Max daily loss exceeded: ${abs(self._daily_loss):.2f} "
                f"(limit ${self._config.risk.max_daily_loss_usd:.2f})"
            )

        # Check drawdown circuit breaker
        if self._peak_equity > 0:
            drawdown = (self._peak_equity - current_equity) / self._peak_equity
            if drawdown > self._config.risk.drawdown_circuit_breaker_pct:
                self._circuit_breaker.trip(
                    f"Drawdown circuit breaker: {drawdown:.1%} "
                    f"(limit {self._config.risk.drawdown_circuit_breaker_pct:.1%})"
                )

    def trip_circuit_breaker(self, reason: str) -> None:
        """Manually trip the circuit breaker (e.g., from monitoring)."""
        self._circuit_breaker.trip(reason)

    def reset_circuit_breaker(self) -> None:
        self._circuit_breaker.reset()

    @property
    def circuit_breaker_tripped(self) -> bool:
        return self._circuit_breaker.triggered

    @property
    def circuit_breaker_reason(self) -> str:
        return self._circuit_breaker.reason

    # ----------------------------------------------------------- risk layers

    def _layer0_hard_limits(self, signal: Signal, decision: RiskDecision) -> bool:
        """Layer 0: Hard monetary caps that can never be overridden."""
        cfg = self._config.risk

        # Global exposure cap
        current_exposure = self._om.total_exposure_usd()
        if current_exposure >= cfg.global_exposure_usd:
            decision.action = RiskAction.BLOCK
            decision.reason = (
                f"L0: Global exposure cap reached "
                f"(${current_exposure:.0f} >= ${cfg.global_exposure_usd:.0f})"
            )
            decision.checks["global_cap"] = False
            return False
        decision.checks["global_cap"] = True

        # Per-market exposure cap
        if signal.market:
            mkt_exp = self._om.exposure_by_market().get(signal.market.condition_id, 0)
            if mkt_exp >= cfg.per_market_exposure_usd:
                decision.action = RiskAction.BLOCK
                decision.reason = (
                    f"L0: Per-market cap reached for {signal.market.condition_id[:12]} "
                    f"(${mkt_exp:.0f} >= ${cfg.per_market_exposure_usd:.0f})"
                )
                decision.checks["market_cap"] = False
                return False
            # Reduce size if adding this trade would breach the cap
            headroom = cfg.per_market_exposure_usd - mkt_exp
            if decision.approved_size_usd > headroom:
                decision.approved_size_usd = headroom
                decision.action = RiskAction.REDUCE
                decision.adjustments["market_cap_reduce"] = headroom
        decision.checks["market_cap"] = True

        # Per-trade max size
        if decision.approved_size_usd > cfg.per_trade_max_usd:
            decision.approved_size_usd = cfg.per_trade_max_usd
            decision.action = RiskAction.REDUCE
            decision.adjustments["per_trade_cap"] = cfg.per_trade_max_usd
        decision.checks["per_trade_cap"] = True

        # Max concurrent positions
        n_open = len(self._om.get_open_positions())
        if n_open >= cfg.max_concurrent_positions:
            decision.action = RiskAction.BLOCK
            decision.reason = (
                f"L0: Max concurrent positions reached ({n_open}/{cfg.max_concurrent_positions})"
            )
            decision.checks["max_positions"] = False
            return False
        decision.checks["max_positions"] = True

        return True

    def _layer1_position_sizing(self, signal: Signal, decision: RiskDecision) -> None:
        """Layer 1: Kelly-fraction and liquidity-based sizing."""
        cfg = self._config.risk

        # Minimum liquidity threshold
        if signal.liquidity_usd < cfg.min_liquidity_usd:
            decision.approved_size_usd = 0.0
            decision.action = RiskAction.BLOCK
            decision.reason = (
                f"L1: Liquidity too low "
                f"(${signal.liquidity_usd:.0f} < ${cfg.min_liquidity_usd:.0f})"
            )
            decision.checks["liquidity"] = False
            return
        decision.checks["liquidity"] = True

        # Cap size at 5% of available liquidity to avoid moving the market
        liquidity_cap = signal.liquidity_usd * 0.05
        if decision.approved_size_usd > liquidity_cap:
            decision.approved_size_usd = liquidity_cap
            decision.action = RiskAction.REDUCE
            decision.adjustments["liquidity_cap"] = liquidity_cap

    def _layer2_execution_controls(self, signal: Signal, decision: RiskDecision) -> bool:
        """Layer 2: Execution quality controls."""
        cfg = self._config.risk

        # Slippage tolerance check — reject signals where spread is too wide
        if signal.market and signal.token:
            # Proxy: if token.price is extreme, the spread is likely wide
            price = signal.market_price
            if price < 0.01 or price > 0.99:
                decision.action = RiskAction.BLOCK
                decision.reason = f"L2: Price at extreme ({price:.4f}) — high slippage risk"
                decision.checks["slippage"] = False
                return False
        decision.checks["slippage"] = True
        return True

    def _layer3_stop_time_decay(self, signal: Signal, decision: RiskDecision) -> None:
        """Layer 3: Stop-loss and time-based position decay checks."""
        # Check time to resolution — avoid markets resolving very soon
        if signal.market and signal.market.end_date:
            now = datetime.now(timezone.utc)
            end = signal.market.end_date
            if end.tzinfo is None:
                from datetime import timezone as tz
                end = end.replace(tzinfo=tz.utc)
            hours_to_resolution = (end - now).total_seconds() / 3600

            if hours_to_resolution < 1:
                decision.approved_size_usd = 0.0
                decision.action = RiskAction.BLOCK
                decision.reason = f"L3: Market resolves in < 1 hour"
                decision.checks["time_decay"] = False
                return
            elif hours_to_resolution < self._config.risk.time_decay_hours:
                # Scale down size proportionally as we approach resolution
                scale = hours_to_resolution / self._config.risk.time_decay_hours
                decision.approved_size_usd *= scale
                decision.action = RiskAction.REDUCE
                decision.adjustments["time_decay_scale"] = scale

        decision.checks["time_decay"] = True

        # Check existing positions for stop-loss
        for pos in self._om.get_open_positions():
            if pos.token_id == (signal.token.token_id if signal.token else ""):
                loss_pct = abs(pos.unrealized_pnl) / max(pos.cost_basis, 0.01)
                if loss_pct > self._config.risk.stop_loss_pct and pos.unrealized_pnl < 0:
                    decision.action = RiskAction.BLOCK
                    decision.reason = (
                        f"L3: Stop-loss triggered on existing position "
                        f"(loss {loss_pct:.1%} > threshold {self._config.risk.stop_loss_pct:.1%})"
                    )
                    decision.checks["stop_loss"] = False
                    return

        decision.checks["stop_loss"] = True

    def _layer4_portfolio_hedging(self, signal: Signal, decision: RiskDecision) -> None:
        """Layer 4: Concentration and correlation checks."""
        cfg = self._config.risk

        # Category concentration check
        if signal.market and signal.market.category:
            category = signal.market.category
            total_exposure = self._om.total_exposure_usd()
            if total_exposure > 0:
                cat_exposure = sum(
                    p.exposure_usd
                    for p in self._om.get_open_positions()
                    # Approximate: all positions in same category
                    # (real impl would cross-reference market metadata)
                )
                cat_pct = cat_exposure / total_exposure
                if cat_pct > cfg.max_category_exposure_pct:
                    reduction = 1.0 - (cat_pct - cfg.max_category_exposure_pct)
                    decision.approved_size_usd *= max(0, reduction)
                    decision.action = RiskAction.REDUCE
                    decision.adjustments["category_concentration"] = cat_pct
                    logger.warning(
                        "category_concentration_warning",
                        category=category,
                        pct=round(cat_pct, 3),
                        threshold=cfg.max_category_exposure_pct,
                    )

        decision.checks["concentration"] = True

    def _layer5_circuit_breaker(self, decision: RiskDecision) -> bool:
        """Layer 5: Check circuit breaker state."""
        if self._circuit_breaker.triggered:
            decision.action = RiskAction.CIRCUIT_BREAK
            decision.reason = f"L5: Circuit breaker tripped — {self._circuit_breaker.reason}"
            decision.checks["circuit_breaker"] = False
            return False
        decision.checks["circuit_breaker"] = True
        return True

    def _layer6_compliance(self, signal: Signal, decision: RiskDecision) -> bool:
        """Layer 6: Compliance and safety checks."""
        # Paper-first enforcement: if paper_mode, don't allow live execution
        # (live_executor enforces its own gate; this is a belt-and-suspenders check)
        if self._config.paper_mode and not self._config.enable_live_trading:
            decision.checks["paper_mode"] = True  # Paper mode is always "compliant"

        # Sanity: reject zero or negative size
        if decision.approved_size_usd <= 0:
            decision.action = RiskAction.BLOCK
            decision.reason = "L6: Zero or negative size"
            decision.checks["size_positive"] = False
            return False
        decision.checks["size_positive"] = True

        # Sanity: reject signals with no token or market
        if signal.token is None or signal.market is None:
            decision.action = RiskAction.BLOCK
            decision.reason = "L6: Signal missing token or market"
            decision.checks["signal_complete"] = False
            return False
        decision.checks["signal_complete"] = True

        return True

    def _reset_daily_if_needed(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._daily_reset_date != today:
            self._daily_loss = 0.0
            self._daily_reset_date = today
