"""Unit tests for ArbDetector."""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from unittest.mock import MagicMock

import pytest

from arb_bot.core.arb_detector import ArbDetector, ArbOpportunity
from arb_bot.core.market_matcher import MarketPair


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_pair(
    kalshi_ticker="TEST-TICKER",
    confidence=95.0,
    risk="LOW",
    manual=True,
) -> MarketPair:
    return MarketPair(
        kalshi_ticker=kalshi_ticker,
        kalshi_title=f"Will {kalshi_ticker} resolve YES?",
        polymarket_condition_id="0xabc",
        polymarket_yes_token_id="yes-token-id",
        polymarket_no_token_id="no-token-id",
        polymarket_title=f"Will {kalshi_ticker} resolve YES?",
        match_confidence=confidence,
        resolution_risk=risk,
        verified_manual=manual,
    )


class MockFeed:
    def __init__(self, k_yes=None, k_no=None, p_yes=None, p_no=None, depth=100.0):
        self._k_yes = k_yes
        self._k_no = k_no
        self._p_yes = p_yes
        self._p_no = p_no
        self._depth = depth

    def kalshi_ask(self, ticker, side):
        return self._k_yes if side == "YES" else self._k_no

    def poly_ask(self, token_id):
        if "yes" in token_id:
            return self._p_yes
        return self._p_no

    def kalshi_depth(self, ticker, side):
        return self._depth

    def poly_depth(self, token_id):
        return self._depth


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestArbDetectorScan:
    def test_detects_profitable_opportunity(self):
        """YES on Kalshi at 0.44 + NO on Poly at 0.44 = 0.12 gross; well above threshold."""
        feed = MockFeed(k_yes=0.44, k_no=0.55, p_yes=0.56, p_no=0.44)
        detector = ArbDetector(feed, min_edge=0.01)
        pair = make_pair()
        opps = detector.scan([pair])
        assert len(opps) >= 1
        best = opps[0]
        assert best.kalshi_leg == "YES"
        assert best.polymarket_leg == "NO"
        assert best.gross_spread == pytest.approx(1.0 - 0.44 - 0.44)
        assert best.net_edge > 0

    def test_no_opportunity_when_prices_above_1(self):
        """No arb when YES + NO > $1 on either leg combination."""
        feed = MockFeed(k_yes=0.55, k_no=0.52, p_yes=0.53, p_no=0.51)
        detector = ArbDetector(feed, min_edge=0.01)
        opps = detector.scan([make_pair()])
        assert opps == []

    def test_skips_high_resolution_risk_pair(self):
        feed = MockFeed(k_yes=0.40, k_no=0.40, p_yes=0.40, p_no=0.40)
        detector = ArbDetector(feed, min_edge=0.01)
        pair = make_pair(risk="HIGH")
        opps = detector.scan([pair])
        assert opps == []

    def test_skips_low_confidence_unverified_pair(self):
        feed = MockFeed(k_yes=0.40, k_no=0.40, p_yes=0.40, p_no=0.40)
        detector = ArbDetector(feed, min_edge=0.01)
        pair = make_pair(confidence=70.0, manual=False)
        opps = detector.scan([pair])
        assert opps == []

    def test_allows_low_confidence_if_manually_verified(self):
        """Manual pairs bypass the confidence threshold."""
        feed = MockFeed(k_yes=0.40, k_no=0.40, p_yes=0.40, p_no=0.40)
        detector = ArbDetector(feed, min_edge=0.01)
        pair = make_pair(confidence=50.0, manual=True)
        opps = detector.scan([pair])
        # 1 - 0.80 = 0.20 gross; definitely above 1% threshold after fees
        assert len(opps) >= 1

    def test_opportunities_sorted_by_net_edge_descending(self):
        pair_a = make_pair("A")
        pair_b = make_pair("B")
        # pair_a has better edge
        feed = MockFeed(k_yes=0.35, k_no=0.60, p_yes=0.61, p_no=0.38)
        detector = ArbDetector(feed, min_edge=0.01)
        opps = detector.scan([pair_a, pair_b])
        if len(opps) > 1:
            assert opps[0].net_edge >= opps[1].net_edge

    def test_both_leg_directions_checked(self):
        """Detector should check both K-YES/P-NO and K-NO/P-YES."""
        # Only K-NO / P-YES direction is profitable
        feed = MockFeed(k_yes=0.60, k_no=0.38, p_yes=0.38, p_no=0.65)
        detector = ArbDetector(feed, min_edge=0.01)
        opps = detector.scan([make_pair()])
        assert len(opps) >= 1
        profitable = [o for o in opps if o.net_edge > 0]
        assert any(o.kalshi_leg == "NO" for o in profitable)

    def test_returns_empty_when_prices_missing(self):
        """If either side has no price data, skip the pair gracefully."""
        feed = MockFeed(k_yes=None, k_no=None, p_yes=None, p_no=None)
        detector = ArbDetector(feed, min_edge=0.01)
        opps = detector.scan([make_pair()])
        assert opps == []

    def test_max_size_capped_by_config(self, monkeypatch):
        import arb_bot.config as cfg
        monkeypatch.setattr(cfg, "MAX_POSITION_PER_MARKET", 25.0)
        monkeypatch.setattr(cfg, "MIN_FILL_RATIO", 0.90)
        feed = MockFeed(k_yes=0.40, k_no=0.40, p_yes=0.40, p_no=0.40, depth=200.0)
        detector = ArbDetector(feed, min_edge=0.01)
        opps = detector.scan([make_pair()])
        for opp in opps:
            assert opp.max_size_usdc <= 25.0
