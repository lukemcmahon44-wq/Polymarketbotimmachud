"""On-chain copy-trade detection using public blockchain data.

Ethics: Uses only public on-chain data. Does not attempt deanonymization
or privacy invasion. Monitors publicly visible transaction patterns only.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import structlog

from polymarket_bot.config import BotConfig
from polymarket_bot.types import CopyTradeAlert, DefensiveAction, Order

logger = structlog.get_logger()


@dataclass
class WatchedWallet:
    """A wallet being monitored for copy-trade behavior."""
    address: str
    similarity_score: float = 0.0
    matching_trades: int = 0
    last_seen: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    trade_history: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class OnChainTrade:
    """Represents a parsed on-chain trade from Polymarket contracts."""
    tx_hash: str
    address: str
    market_id: str
    token_id: str
    side: str  # "BUY" or "SELL"
    size: float
    price: float
    timestamp: datetime
    block_number: int = 0


class CopyTradeDetector:
    """Detects wallets that may be copy-trading our bot's trades.

    Heuristics:
    1. Same market, same side, within time window
    2. Timing correlation (trade follows ours within seconds/minutes)
    3. Size proportional to their wallet balance
    4. Pattern consistency across multiple trades

    Defensive actions:
    - reduce_size: Reduce order size by configured factor
    - randomize_timing: Add random delay to order submission
    - flag_only: Log alert but take no action
    """

    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self.ct_config = config.copy_trade_detection
        self.watched_wallets: dict[str, WatchedWallet] = {}
        self.our_trades: list[dict[str, Any]] = []
        self.alerts: list[CopyTradeAlert] = []

    def record_our_trade(self, order: Order) -> None:
        """Record one of our own trades for correlation analysis."""
        self.our_trades.append({
            "market_id": order.market_id,
            "token_id": order.token_id,
            "side": order.side.value,
            "size_usd": order.size_usd,
            "timestamp": order.created_at.timestamp(),
        })
        # Keep only recent trades
        cutoff = time.time() - self.ct_config.time_window_seconds * 2
        self.our_trades = [t for t in self.our_trades if t["timestamp"] > cutoff]

    def analyze_on_chain_trade(self, trade: OnChainTrade) -> CopyTradeAlert | None:
        """Analyze an on-chain trade for copy-trade signals.

        Returns a CopyTradeAlert if the trade matches our pattern.
        """
        if not self.ct_config.enabled:
            return None

        # Find our trades in the same market within the time window
        matching_our_trades = [
            t for t in self.our_trades
            if t["market_id"] == trade.market_id
            and t["side"] == trade.side
            and abs(trade.timestamp.timestamp() - t["timestamp"]) <= self.ct_config.time_window_seconds
        ]

        if not matching_our_trades:
            return None

        # Update or create watched wallet
        wallet = self.watched_wallets.get(trade.address)
        if wallet is None:
            wallet = WatchedWallet(address=trade.address)
            self.watched_wallets[trade.address] = wallet

        # Record this match
        wallet.matching_trades += 1
        wallet.last_seen = trade.timestamp
        wallet.trade_history.append({
            "market_id": trade.market_id,
            "side": trade.side,
            "size": trade.size,
            "timestamp": trade.timestamp.isoformat(),
            "our_trade_delay_seconds": min(
                abs(trade.timestamp.timestamp() - t["timestamp"])
                for t in matching_our_trades
            ),
        })

        # Compute similarity score
        wallet.similarity_score = self._compute_similarity(wallet)

        # Check thresholds
        if (
            wallet.similarity_score >= self.ct_config.similarity_threshold
            and wallet.matching_trades >= self.ct_config.min_matching_trades
        ):
            alert = CopyTradeAlert(
                suspect_address=trade.address,
                similarity_score=wallet.similarity_score,
                matching_trades=wallet.matching_trades,
                time_window_seconds=self.ct_config.time_window_seconds,
                details=wallet.trade_history[-5:],  # Last 5 matching trades
                recommended_action=DefensiveAction(self.ct_config.response),
            )
            self.alerts.append(alert)

            logger.warning(
                "copy_trade.alert",
                address=trade.address[:10] + "...",
                similarity=round(wallet.similarity_score, 3),
                matches=wallet.matching_trades,
                action=self.ct_config.response,
            )
            return alert

        return None

    def _compute_similarity(self, wallet: WatchedWallet) -> float:
        """Compute similarity score based on trade correlation patterns."""
        if not wallet.trade_history:
            return 0.0

        recent = wallet.trade_history[-20:]  # Last 20 matches
        if len(recent) < self.ct_config.min_matching_trades:
            return len(recent) / self.ct_config.min_matching_trades * 0.5

        # Factors:
        # 1. Timing consistency (low variance in delay = higher score)
        delays = [t.get("our_trade_delay_seconds", 999) for t in recent]
        avg_delay = sum(delays) / len(delays)
        delay_score = max(0, 1.0 - avg_delay / self.ct_config.time_window_seconds)

        # 2. Trade count relative to our trade count
        match_ratio = min(1.0, len(recent) / max(1, len(self.our_trades)))

        # 3. Frequency (more matches = higher score)
        freq_score = min(1.0, wallet.matching_trades / 10)

        # Weighted average
        similarity = 0.4 * delay_score + 0.3 * match_ratio + 0.3 * freq_score
        return round(min(1.0, similarity), 3)

    def apply_defensive_action(self, size_usd: float) -> tuple[float, float]:
        """Apply defensive measures if copy-trading detected.

        Returns (adjusted_size, delay_seconds).
        """
        if not self.alerts:
            return size_usd, 0.0

        # Check recent alerts
        recent_alerts = [
            a for a in self.alerts
            if (datetime.now(timezone.utc) - a.timestamp).total_seconds() < 3600
        ]

        if not recent_alerts:
            return size_usd, 0.0

        action = DefensiveAction(self.ct_config.response)

        if action == DefensiveAction.REDUCE_SIZE:
            adjusted = size_usd * self.ct_config.size_reduction_factor
            logger.info("copy_trade.defensive_reduce", original=size_usd, adjusted=adjusted)
            return adjusted, 0.0

        elif action == DefensiveAction.RANDOMIZE_TIMING:
            delay = random.uniform(1, self.ct_config.timing_randomize_seconds)
            logger.info("copy_trade.defensive_delay", delay_seconds=round(delay, 1))
            return size_usd, delay

        # flag_only
        return size_usd, 0.0

    def get_watchlist(self) -> list[dict[str, Any]]:
        """Return current watchlist for monitoring."""
        return [
            {
                "address": w.address[:10] + "...",
                "similarity": w.similarity_score,
                "matches": w.matching_trades,
                "last_seen": w.last_seen.isoformat(),
            }
            for w in sorted(
                self.watched_wallets.values(),
                key=lambda w: w.similarity_score,
                reverse=True,
            )[:20]
        ]

    def cleanup_stale(self, max_age_seconds: int = 7200) -> int:
        """Remove wallets not seen recently."""
        now = datetime.now(timezone.utc)
        stale = [
            addr for addr, w in self.watched_wallets.items()
            if (now - w.last_seen).total_seconds() > max_age_seconds
        ]
        for addr in stale:
            del self.watched_wallets[addr]
        return len(stale)
