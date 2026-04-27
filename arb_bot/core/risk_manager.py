"""Position limits, daily loss cap, kill switch."""

import threading
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from arb_bot.core.arb_detector import ArbOpportunity
from arb_bot.utils.logger import get_logger

logger = get_logger(__name__)

_MAX_PARTIAL_FILLS = 3  # Kill switch after N partial fills in one session


@dataclass
class RiskState:
    daily_pnl: float = 0.0
    global_exposure: float = 0.0
    positions: dict = field(default_factory=dict)  # ticker -> size
    partial_fill_count: int = 0
    killed: bool = False
    kill_reason: str = ""
    session_date: date = field(default_factory=date.today)


class RiskManager:
    def __init__(
        self,
        max_daily_loss: Optional[float] = None,
        max_global_exposure: Optional[float] = None,
        max_position_per_market: Optional[float] = None,
        min_match_confidence: Optional[float] = None,
    ):
        from arb_bot.config import (
            MAX_DAILY_LOSS,
            MAX_GLOBAL_EXPOSURE,
            MAX_POSITION_PER_MARKET,
            MIN_MATCH_CONFIDENCE,
        )
        self._max_daily_loss = max_daily_loss if max_daily_loss is not None else MAX_DAILY_LOSS
        self._max_exposure = max_global_exposure if max_global_exposure is not None else MAX_GLOBAL_EXPOSURE
        self._max_per_market = max_position_per_market if max_position_per_market is not None else MAX_POSITION_PER_MARKET
        self._min_confidence = min_match_confidence if min_match_confidence is not None else MIN_MATCH_CONFIDENCE
        self._state = RiskState()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def pre_trade_check(self, opp: ArbOpportunity, size: float) -> tuple[bool, str]:
        """Return (allowed, reason). Call before every trade."""
        with self._lock:
            self._roll_daily_pnl_if_needed()

            if self._state.killed:
                return False, f"Kill switch active: {self._state.kill_reason}"

            if self._state.daily_pnl <= -self._max_daily_loss:
                self._kill(f"Daily loss limit hit: ${self._state.daily_pnl:.2f}")
                return False, self._state.kill_reason

            if self._state.global_exposure + size > self._max_exposure:
                return False, (
                    f"Exposure limit: ${self._state.global_exposure:.2f} + ${size:.2f} "
                    f"> ${self._max_exposure:.2f}"
                )

            if size > self._max_per_market:
                return False, f"Position too large: ${size:.2f} > ${self._max_per_market:.2f}"

            if opp.pair.resolution_risk == "HIGH":
                return False, "Resolution risk HIGH"

            if not opp.pair.verified_manual and opp.pair.match_confidence < self._min_confidence:
                return False, f"Low match confidence: {opp.pair.match_confidence:.0f}"

            return True, "OK"

    def post_trade_update(self, opp: ArbOpportunity, size: float, success: bool) -> None:
        with self._lock:
            if success:
                self._state.global_exposure += size
                self._state.positions[opp.pair.kalshi_ticker] = (
                    self._state.positions.get(opp.pair.kalshi_ticker, 0.0) + size
                )

    def on_settlement(self, kalshi_ticker: str, pnl: float) -> None:
        """Call when a position settles and is removed from open exposure."""
        with self._lock:
            self._state.daily_pnl += pnl
            settled_size = self._state.positions.pop(kalshi_ticker, 0.0)
            self._state.global_exposure = max(0.0, self._state.global_exposure - settled_size)
            logger.info(f"Settled {kalshi_ticker}: pnl=${pnl:.2f}, daily_pnl=${self._state.daily_pnl:.2f}")

    def on_partial_fill(self, opp: ArbOpportunity) -> None:
        with self._lock:
            self._state.partial_fill_count += 1
            logger.warning(f"Partial fill #{self._state.partial_fill_count} on {opp.pair.kalshi_ticker}")
            if self._state.partial_fill_count >= _MAX_PARTIAL_FILLS:
                self._kill(f"{_MAX_PARTIAL_FILLS} partial fills in session")

    def kill(self, reason: str) -> None:
        with self._lock:
            self._kill(reason)

    # ------------------------------------------------------------------
    # Properties for dashboard
    # ------------------------------------------------------------------

    @property
    def state(self) -> RiskState:
        return self._state

    @property
    def is_killed(self) -> bool:
        return self._state.killed

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _kill(self, reason: str) -> None:
        self._state.killed = True
        self._state.kill_reason = reason
        logger.error(f"[KILL SWITCH ACTIVATED] {reason}")

    def _roll_daily_pnl_if_needed(self) -> None:
        today = date.today()
        if self._state.session_date != today:
            logger.info(f"New trading day — resetting daily P&L (was ${self._state.daily_pnl:.2f})")
            self._state.daily_pnl = 0.0
            self._state.session_date = today
            # Do NOT reset kill switch; require manual intervention
