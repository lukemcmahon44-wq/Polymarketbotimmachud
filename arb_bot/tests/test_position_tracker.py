"""Tests for the persistent JSON-backed PositionTracker."""

import json
from pathlib import Path

import pytest

from arb_bot.core.position_tracker import Position, PositionTracker


def _pos(ticker: str = "NFL-CHIEFS-24", **overrides) -> Position:
    defaults = dict(
        kalshi_ticker=ticker,
        polymarket_condition_id="0xabc",
        k_contracts=100,
        kalshi_spend=65.0,
        poly_spend=30.0,
        kalshi_price=0.65,
        polymarket_price=0.30,
    )
    defaults.update(overrides)
    return Position(**defaults)


class TestPositionMath:
    def test_total_cost(self):
        p = _pos()
        assert p.total_cost == pytest.approx(95.0)

    def test_max_payout_equals_contracts(self):
        p = _pos(k_contracts=210)
        assert p.max_payout == 210.0

    def test_locked_edge_is_payout_minus_cost(self):
        p = _pos(k_contracts=100, kalshi_spend=65.0, poly_spend=30.0)
        assert p.locked_edge == pytest.approx(5.0)

    def test_opened_at_auto_timestamps(self):
        p = _pos()
        assert p.opened_at > 0


class TestPersistence:
    def test_save_and_reload(self, tmp_path: Path):
        path = tmp_path / "positions.json"
        t = PositionTracker(path=path)
        t.add(_pos("AAA"))
        t.add(_pos("BBB", k_contracts=50, kalshi_spend=30.0, poly_spend=15.0))

        # Reload from disk via a fresh tracker
        t2 = PositionTracker(path=path)
        all_pos = sorted(t2.all(), key=lambda p: p.kalshi_ticker)
        assert len(all_pos) == 2
        assert all_pos[0].kalshi_ticker == "AAA"
        assert all_pos[1].kalshi_ticker == "BBB"
        assert all_pos[1].k_contracts == 50

    def test_remove(self, tmp_path: Path):
        path = tmp_path / "positions.json"
        t = PositionTracker(path=path)
        t.add(_pos("AAA"))
        t.add(_pos("BBB"))
        removed = t.remove("AAA")
        assert removed is not None and removed.kalshi_ticker == "AAA"
        assert t.get("AAA") is None
        assert t.get("BBB") is not None

    def test_remove_nonexistent_returns_none(self, tmp_path: Path):
        path = tmp_path / "positions.json"
        t = PositionTracker(path=path)
        assert t.remove("DOES_NOT_EXIST") is None

    def test_total_exposure(self, tmp_path: Path):
        path = tmp_path / "positions.json"
        t = PositionTracker(path=path)
        t.add(_pos("AAA", kalshi_spend=65.0, poly_spend=30.0))   # $95
        t.add(_pos("BBB", kalshi_spend=40.0, poly_spend=55.0))   # $95
        assert t.total_exposure() == pytest.approx(190.0)

    def test_locked_profit_aggregate(self, tmp_path: Path):
        path = tmp_path / "positions.json"
        t = PositionTracker(path=path)
        t.add(_pos("AAA", k_contracts=100, kalshi_spend=65.0, poly_spend=30.0))  # +5
        t.add(_pos("BBB", k_contracts=100, kalshi_spend=55.0, poly_spend=40.0))  # +5
        assert t.locked_profit() == pytest.approx(10.0)

    def test_atomic_write_creates_no_tmp_file(self, tmp_path: Path):
        """_save uses .tmp + replace; final state should not leave a .tmp behind."""
        path = tmp_path / "positions.json"
        t = PositionTracker(path=path)
        t.add(_pos("AAA"))
        assert path.exists()
        assert not path.with_suffix(".tmp").exists()

    def test_corrupt_file_does_not_crash(self, tmp_path: Path):
        path = tmp_path / "positions.json"
        path.write_text("{not valid json")
        t = PositionTracker(path=path)  # should log warning, not raise
        assert t.all() == []

    def test_missing_file_starts_empty(self, tmp_path: Path):
        path = tmp_path / "nonexistent.json"
        t = PositionTracker(path=path)
        assert t.all() == []

    def test_json_format_is_pretty(self, tmp_path: Path):
        path = tmp_path / "positions.json"
        t = PositionTracker(path=path)
        t.add(_pos("AAA"))
        raw = path.read_text()
        # Indent=2 means newlines should be present
        assert "\n" in raw
        data = json.loads(raw)
        assert "positions" in data
        assert len(data["positions"]) == 1
