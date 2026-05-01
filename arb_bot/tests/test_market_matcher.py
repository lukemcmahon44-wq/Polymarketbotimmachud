"""Tests for resolution-risk classification in market_matcher.

These tests are the regression net for the critical bug where r"official"
in _HIGH_RISK_PATTERNS blocked every sports market ("official NFL game",
"official score", "official stats"). Sports-context "official" must be LOW
risk; only true oracle/governance language should be HIGH.
"""

import pytest

from arb_bot.core.market_matcher import MarketMatcher


def _risk(
    k_title: str,
    p_title: str,
    k_close: str = "2024-10-01",
    p_close: str = "2024-10-01",
) -> str:
    return MarketMatcher._assess_resolution_risk(k_title, p_title, k_close, p_close)


class TestHighRiskPatterns:
    """Genuine oracle/governance risk — these MUST block trading."""

    def test_uma_oracle_blocked(self):
        assert _risk("Will the Chiefs win? Resolves per UMA oracle.", "") == "HIGH"

    def test_uma_word_blocked(self):
        assert _risk("NFL winner UMA vote", "") == "HIGH"

    def test_community_resolution_blocked(self):
        assert _risk("", "Will Lakers win? Community resolution applies.") == "HIGH"

    def test_admin_resolution_blocked(self):
        assert _risk("Admin resolution if disputed", "") == "HIGH"

    def test_administrator_resolution_blocked(self):
        assert _risk("Administrator resolution at platform discretion", "") == "HIGH"

    def test_cftc_resolves_per_blocked(self):
        assert _risk("Resolves per CFTC guidelines", "") == "HIGH"

    def test_government_resolves_blocked(self):
        assert _risk("Resolves per government data", "") == "HIGH"


class TestSportsTitlesNotBlocked:
    """Sports-context "official" language MUST stay LOW (regression test)."""

    def test_official_nfl_game_low(self):
        assert _risk(
            "Will the Chiefs win the official NFL game?",
            "Chiefs vs Eagles official result",
        ) == "LOW"

    def test_official_score_low(self):
        assert _risk("Chiefs official score > 21", "") == "LOW"

    def test_official_stats_low(self):
        assert _risk("NBA official stats: LeBron > 25 points", "") == "LOW"

    def test_plain_world_series_low(self):
        assert _risk(
            "Will the Yankees win the World Series?",
            "Yankees World Series winner",
        ) == "LOW"

    def test_afc_championship_low(self):
        assert _risk(
            "NFL AFC Championship winner",
            "Who wins the AFC Championship?",
        ) == "LOW"

    def test_super_bowl_low(self):
        assert _risk(
            "Will Eagles win Super Bowl 59?",
            "Eagles to win Super Bowl LIX",
        ) == "LOW"


class TestMediumRiskPatterns:
    def test_postponed_medium(self):
        assert _risk("Game postponed due to weather", "") == "MEDIUM"

    def test_cancelled_medium(self):
        assert _risk("Match cancelled", "") == "MEDIUM"

    def test_canceled_us_spelling_medium(self):
        assert _risk("Match canceled", "") == "MEDIUM"

    def test_rain_delay_medium(self):
        assert _risk("", "Rain delay — resolves when game resumes") == "MEDIUM"

    def test_expiry_mismatch_medium(self):
        assert _risk(
            "Chiefs win?", "Chiefs win?", "2024-10-01", "2024-10-02"
        ) == "MEDIUM"

    def test_eod_language_medium(self):
        assert _risk("Resolves by end of day", "") == "MEDIUM"

    def test_close_of_business_medium(self):
        assert _risk("Resolves by close of business", "") == "MEDIUM"


class TestCategoryFilter:
    """_is_allowed_category should accept sports markets and reject non-sports."""

    def test_kalshi_sports_category_passes(self):
        assert MarketMatcher._is_allowed_category("sports", "", ["sports"]) is True
        assert MarketMatcher._is_allowed_category("NFL", "", ["sports"]) is True
        assert MarketMatcher._is_allowed_category("NBA", "", ["sports"]) is True

    def test_title_keyword_fallback_passes(self):
        assert (
            MarketMatcher._is_allowed_category(
                "", "Super Bowl 59 winner", ["sports"]
            )
            is True
        )
        assert (
            MarketMatcher._is_allowed_category(
                "", "NCAA basketball championship", ["sports"]
            )
            is True
        )

    def test_politics_blocked_when_sports_only(self):
        assert (
            MarketMatcher._is_allowed_category(
                "politics", "Will Trump win 2028?", ["sports"]
            )
            is False
        )

    def test_crypto_blocked_when_sports_only(self):
        assert (
            MarketMatcher._is_allowed_category(
                "crypto", "BTC > $100k by EOY", ["sports"]
            )
            is False
        )
