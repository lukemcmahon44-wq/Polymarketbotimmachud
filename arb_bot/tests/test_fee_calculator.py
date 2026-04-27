"""Unit tests for fee_calculator."""

import pytest

from arb_bot.utils.fee_calculator import (
    calc_kalshi_fee,
    calc_polymarket_fee,
    net_edge_estimate,
)


class TestKalshiFee:
    def test_zero_price(self):
        # Buying at $0 (impossible in practice, but math should hold)
        fee = calc_kalshi_fee(0.0, "YES", fee_rate=0.07)
        assert fee == pytest.approx(0.07)

    def test_full_price(self):
        # Buying at $1.00 means zero potential profit — fee should be zero
        fee = calc_kalshi_fee(1.0, "YES", fee_rate=0.07)
        assert fee == pytest.approx(0.0)

    def test_midpoint_price(self):
        # At $0.50, fee = 0.07 * 0.50 = 0.035
        fee = calc_kalshi_fee(0.50, "YES", fee_rate=0.07)
        assert fee == pytest.approx(0.035)

    def test_typical_arb_price(self):
        # Typical: buying YES at 0.48, fee_rate 7%
        fee = calc_kalshi_fee(0.48, "YES", fee_rate=0.07)
        assert fee == pytest.approx(0.07 * (1.0 - 0.48))


class TestPolymarketFee:
    def test_default_rate(self):
        fee = calc_polymarket_fee(0.50, fee_rate=0.0125)
        assert fee == pytest.approx(0.0125 * 0.50)

    def test_politics_rate(self):
        fee = calc_polymarket_fee(0.60, category="politics")
        assert fee == pytest.approx(0.0100 * 0.60)

    def test_crypto_rate(self):
        fee = calc_polymarket_fee(0.60, category="crypto")
        assert fee == pytest.approx(0.0180 * 0.60)

    def test_sports_rate(self):
        fee = calc_polymarket_fee(0.40, category="sports")
        assert fee == pytest.approx(0.0075 * 0.40)

    def test_unknown_category_uses_default(self):
        fee_unknown = calc_polymarket_fee(0.50, category="unknown_cat")
        fee_default = calc_polymarket_fee(0.50, fee_rate=0.0125)
        assert fee_unknown == pytest.approx(fee_default)


class TestNetEdgeEstimate:
    def test_profitable_arb(self):
        # Combined prices: 0.45 + 0.48 = 0.93 → gross = 0.07
        result = net_edge_estimate(
            kalshi_price=0.45,
            poly_price=0.48,
            kalshi_side="YES",
            poly_category="politics",
        )
        assert result["gross_spread"] == pytest.approx(0.07)
        assert result["profitable"] is True
        assert result["net_edge"] < result["gross_spread"]  # fees reduce the edge
        assert result["net_edge"] > 0  # still profitable after fees

    def test_unprofitable_arb(self):
        # Combined prices: 0.51 + 0.50 = 1.01 → gross = -0.01
        result = net_edge_estimate(
            kalshi_price=0.51,
            poly_price=0.50,
            kalshi_side="YES",
        )
        assert result["gross_spread"] == pytest.approx(-0.01)
        assert result["profitable"] is False
        assert result["net_edge"] < 0

    def test_break_even_after_fees(self):
        # At ~0.01 gross, fees should eat all the edge
        result = net_edge_estimate(
            kalshi_price=0.495,
            poly_price=0.495,
            kalshi_side="YES",
            poly_category="politics",
        )
        assert result["gross_spread"] == pytest.approx(0.01)
        # Total fees > 0.01 for typical fee rates
        assert result["net_edge"] < 0

    def test_fee_components_sum(self):
        result = net_edge_estimate(
            kalshi_price=0.40,
            poly_price=0.42,
            kalshi_side="NO",
            poly_category="crypto",
        )
        assert result["total_fees"] == pytest.approx(
            result["kalshi_fee"] + result["polymarket_fee"]
        )
        assert result["net_edge"] == pytest.approx(
            result["gross_spread"] - result["total_fees"]
        )
