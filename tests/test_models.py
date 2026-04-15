"""Unit tests for probability estimation models."""

from __future__ import annotations

import pytest

from polymarket_bot.models.ensemble import (
    ComplementArbitrageModel,
    EnsembleProbabilityModel,
    MarketImpliedModel,
    VolumeWeightedModel,
)
from polymarket_bot.types import Market, Token


@pytest.fixture
def balanced_binary() -> Market:
    return Market(
        condition_id="bal_001",
        question="Will A happen?",
        tokens=[
            Token("t_yes", "Yes", price=0.50),
            Token("t_no", "No", price=0.50),
        ],
        liquidity_usd=10_000,
        volume_usd=5_000,
    )


@pytest.fixture
def mispriced_binary() -> Market:
    return Market(
        condition_id="mis_001",
        question="Will B happen?",
        tokens=[
            Token("t_yes2", "Yes", price=0.40),
            Token("t_no2", "No", price=0.40),  # YES + NO = 0.80 < 1.0
        ],
        liquidity_usd=10_000,
        volume_usd=5_000,
    )


@pytest.mark.asyncio
class TestMarketImpliedModel:
    async def test_returns_market_price_as_probability(self, balanced_binary):
        model = MarketImpliedModel()
        estimates = await model.estimate(balanced_binary)
        assert len(estimates) == 2
        yes_est = next(e for e in estimates if e.outcome == "Yes")
        assert yes_est.probability == pytest.approx(0.50)

    async def test_name(self):
        assert MarketImpliedModel().name == "market_implied"


@pytest.mark.asyncio
class TestComplementArbitrageModel:
    async def test_normalizes_probabilities(self, mispriced_binary):
        model = ComplementArbitrageModel()
        estimates = await model.estimate(mispriced_binary)
        assert len(estimates) == 2
        total = sum(e.probability for e in estimates)
        assert total == pytest.approx(1.0, abs=0.001)

    async def test_higher_confidence_for_larger_overround(self, mispriced_binary):
        model = ComplementArbitrageModel()
        estimates = await model.estimate(mispriced_binary)
        # 0.80 total → 0.20 overround → high confidence
        assert estimates[0].confidence > 0.5

    async def test_skips_non_binary(self):
        multi = Market(
            condition_id="multi",
            question="?",
            tokens=[Token("a", "A", 0.33), Token("b", "B", 0.33), Token("c", "C", 0.33)],
            liquidity_usd=1000,
            volume_usd=1000,
        )
        model = ComplementArbitrageModel()
        estimates = await model.estimate(multi)
        assert len(estimates) == 0


@pytest.mark.asyncio
class TestEnsembleModel:
    async def test_returns_one_estimate_per_token(self, balanced_binary):
        model = EnsembleProbabilityModel()
        estimates = await model.estimate(balanced_binary)
        assert len(estimates) == len(balanced_binary.tokens)

    async def test_probabilities_are_bounded(self, balanced_binary):
        model = EnsembleProbabilityModel()
        estimates = await model.estimate(balanced_binary)
        for e in estimates:
            assert 0 < e.probability < 1

    async def test_confidence_is_bounded(self, balanced_binary):
        model = EnsembleProbabilityModel()
        estimates = await model.estimate(balanced_binary)
        for e in estimates:
            assert 0 < e.confidence <= 1

    async def test_model_name(self):
        assert EnsembleProbabilityModel().name == "ensemble"
