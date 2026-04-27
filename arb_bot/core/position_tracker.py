"""Persistent position tracker: survives process restarts via atomic JSON writes."""

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Lock
from typing import Optional

from arb_bot.utils.logger import get_logger

logger = get_logger(__name__)

_DEFAULT_PATH = Path("positions.json")


@dataclass
class Position:
    kalshi_ticker: str
    polymarket_condition_id: str
    k_contracts: int
    kalshi_spend: float
    poly_spend: float
    kalshi_price: float
    polymarket_price: float
    opened_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.opened_at:
            self.opened_at = time.time()

    @property
    def total_cost(self) -> float:
        return self.kalshi_spend + self.poly_spend

    @property
    def max_payout(self) -> float:
        """Each side pays $1/contract if it resolves YES; only one side wins."""
        return float(self.k_contracts)

    @property
    def locked_edge(self) -> float:
        """Guaranteed profit = payout - cost (already locked in at entry)."""
        return self.max_payout - self.total_cost


class PositionTracker:
    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or _DEFAULT_PATH
        self._lock = Lock()
        self._positions: dict[str, Position] = {}
        self._load()

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def add(self, position: Position) -> None:
        with self._lock:
            self._positions[position.kalshi_ticker] = position
            self._save()

    def remove(self, kalshi_ticker: str) -> Optional[Position]:
        with self._lock:
            pos = self._positions.pop(kalshi_ticker, None)
            if pos is not None:
                self._save()
            return pos

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get(self, kalshi_ticker: str) -> Optional[Position]:
        with self._lock:
            return self._positions.get(kalshi_ticker)

    def all(self) -> list[Position]:
        with self._lock:
            return list(self._positions.values())

    def total_exposure(self) -> float:
        with self._lock:
            return sum(p.total_cost for p in self._positions.values())

    def locked_profit(self) -> float:
        with self._lock:
            return sum(p.locked_edge for p in self._positions.values())

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            with open(self._path) as fh:
                raw = json.load(fh)
            for entry in raw.get("positions", []):
                pos = Position(**entry)
                self._positions[pos.kalshi_ticker] = pos
            logger.info(f"Loaded {len(self._positions)} open position(s) from {self._path}")
        except Exception as exc:
            logger.warning(f"Could not load positions file ({self._path}): {exc}")

    def _save(self) -> None:
        """Atomic write via temp file to avoid corruption on crash."""
        try:
            data = {"positions": [asdict(p) for p in self._positions.values()]}
            tmp = self._path.with_suffix(".tmp")
            with open(tmp, "w") as fh:
                json.dump(data, fh, indent=2)
            tmp.replace(self._path)
        except Exception as exc:
            logger.error(f"Failed to save positions to {self._path}: {exc}")
