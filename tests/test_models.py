"""Tests for probability estimation models."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from polymarket_bot.models.base import ModelEstimate
from polymarket_bot.models.ensemble import EnsembleModel, NaiveModel, MeanReversionModel, LiquidityAdjustedModel
from polymarket_bot.models.crypto_momentum import (
    CryptoMomentumModel,
    MockSpotFeed,
    MomentumSignal,
)
from polymarket_bot.models.news_sentiment import (
    NewsSentimentModel,
    MockNewsProvider,
    _score_headline,
    _extract_keywords,
)
from polymarket_bot.types import Market, Token


def _crypto_market(question: str = "Will BTC exceed $100k by end of month?") -> Market:
    return Market(
        market_id="mkt_btc",
        condition_id="cond_btc",
        question=question,
        category="crypto",
        end_date=datetime.now(timezone.utc) + timedelta(days=7),
        tokens=[
            Token(token_id="btc_yes", outcome="Yes", price=0.55),
            Token(token_id="btc_no", outcome="No", price=0.45),
        ],
        volume_usd=200_000,
        liquidity_usd=80_000,
    )


def _generic_market() -> Market:
    return Market(
        market_id="mkt_gen",
        condition_id="cond_gen",
        question="Will Apple announce a new product?",
        category="technology",
        end_date=datetime.now(timezone.utc) + timedelta(days=14),
        tokens=[
            Token(token_id="gen_yes", outcome="Yes", price=0.40),
            Token(token_id="gen_no", outcome="No", price=0.60),
        ],
        volume_usd=100_000,
        liquidity_usd=50_000,
    )


class TestNaiveModel:
    @pytest.mark.asyncio
    async def test_produces_estimates(self) -> None:
        model = NaiveModel()
        market = _generic_market()
        estimates = await model.estimate(market)
        assert len(estimates) == 2
        assert all(0 < e.probability < 1 for e in estimates)
        assert all(e.model_name == "naive_mean_reversion" for e in estimates)

    @pytest.mark.asyncio
    async def test_mean_reversion(self) -> None:
        model = NaiveModel()
        market = _crypto_market()
        # Price at 0.55 should be pulled slightly toward 0.50
        estimates = await model.estimate(market)
        yes_est = next(e for e in estimates if e.outcome == "Yes")
        assert yes_est.probability < 0.55  # Pulled toward 0.50


class TestMeanReversionModel:
    @pytest.mark.asyncio
    async def test_time_factor(self) -> None:
        model = MeanReversionModel()
        # Far-out market with low volume: stronger reversion
        far_market = _crypto_market()
        far_market.end_date = datetime.now(timezone.utc) + timedelta(days=60)
        far_market.volume_usd = 10_000  # Low volume → model trusts reversion more
        far_est = await model.estimate(far_market)

        # Near-term market with high volume: weaker reversion (trusts price)
        near_market = _crypto_market()
        near_market.end_date = datetime.now(timezone.utc) + timedelta(hours=6)
        near_market.volume_usd = 500_000  # High volume → model trusts market price
        near_est = await model.estimate(near_market)

        far_yes = next(e for e in far_est if e.outcome == "Yes")
        near_yes = next(e for e in near_est if e.outcome == "Yes")

        # Far-out + low-volume should be pulled more toward 0.50 than near-term + high-volume
        assert abs(far_yes.probability - 0.50) < abs(near_yes.probability - 0.50)


class TestCryptoMomentumModel:
    @pytest.mark.asyncio
    async def test_parses_btc_question(self) -> None:
        model = CryptoMomentumModel()
        sym, thresh = model._extract_symbol_and_threshold("Will BTC exceed $100k by end of month?")
        assert sym == "BTC"
        assert thresh == 100_000

    @pytest.mark.asyncio
    async def test_parses_eth_question(self) -> None:
        model = CryptoMomentumModel()
        sym, thresh = model._extract_symbol_and_threshold("Will ETH reach $5k this quarter?")
        assert sym == "ETH"
        assert thresh == 5_000

    @pytest.mark.asyncio
    async def test_non_crypto_returns_low_confidence(self) -> None:
        model = CryptoMomentumModel()
        market = _generic_market()
        estimates = await model.estimate(market)
        assert len(estimates) == 2
        assert all(e.confidence <= 0.2 for e in estimates)

    @pytest.mark.asyncio
    async def test_crypto_market_estimate(self) -> None:
        feed = MockSpotFeed(seed=42)
        model = CryptoMomentumModel(spot_feed=feed, confirmation_required=1)
        market = _crypto_market()

        # Prime the feed with some price history
        for _ in range(10):
            await feed.get_price("BTC")

        estimates = await model.estimate(market)
        assert len(estimates) == 2
        assert all(0 < e.probability < 1 for e in estimates)
        # Should sum to ~1
        total = sum(e.probability for e in estimates)
        assert 0.95 <= total <= 1.05


class TestNewsSentimentModel:
    def test_score_headline_bullish(self) -> None:
        score = _score_headline("Bitcoin surges to all-time high amid institutional buying")
        assert score > 0

    def test_score_headline_bearish(self) -> None:
        score = _score_headline("Crypto market crashes as regulation crackdown intensifies")
        assert score < 0

    def test_score_headline_neutral(self) -> None:
        score = _score_headline("The weather is nice today")
        assert score == 0.0

    def test_extract_keywords(self) -> None:
        keywords = _extract_keywords("Will BTC exceed $100k by end of month?")
        assert "BTC" in keywords
        assert "exceed" in keywords
        assert "will" not in [k.lower() for k in keywords]

    @pytest.mark.asyncio
    async def test_produces_estimates(self) -> None:
        model = NewsSentimentModel()
        market = _crypto_market()
        estimates = await model.estimate(market)
        assert len(estimates) == 2
        assert all(0 < e.probability < 1 for e in estimates)
        assert all(e.model_name == "news_sentiment" for e in estimates)

    @pytest.mark.asyncio
    async def test_no_news_low_confidence(self) -> None:
        model = NewsSentimentModel()
        # Use a question unlikely to match mock headlines
        market = Market(
            market_id="mkt_obscure",
            condition_id="cond_obscure",
            question="Will penguins colonize Mars?",
            category="science",
            end_date=datetime.now(timezone.utc) + timedelta(days=30),
            tokens=[
                Token(token_id="obs_yes", outcome="Yes", price=0.10),
                Token(token_id="obs_no", outcome="No", price=0.90),
            ],
            volume_usd=1000,
            liquidity_usd=500,
        )
        estimates = await model.estimate(market)
        assert all(e.confidence <= 0.2 for e in estimates)


class TestEnsembleWithNewModels:
    @pytest.mark.asyncio
    async def test_ensemble_includes_all_models(self) -> None:
        model = EnsembleModel()
        assert len(model._models) == 5  # 3 original + momentum + sentiment

    @pytest.mark.asyncio
    async def test_ensemble_produces_valid_output(self) -> None:
        model = EnsembleModel()
        market = _crypto_market()
        estimates = await model.estimate(market)
        assert len(estimates) == 2
        total = sum(e.probability for e in estimates)
        assert 0.9 <= total <= 1.1
        assert all(e.model_name == "ensemble" for e in estimates)

    @pytest.mark.asyncio
    async def test_ensemble_sub_estimates_in_features(self) -> None:
        model = EnsembleModel()
        market = _crypto_market()
        estimates = await model.estimate(market)
        for est in estimates:
            assert est.features is not None
            assert "sub_estimates" in est.features
            # Should have estimates from each sub-model
            assert len(est.features["sub_estimates"]) >= 3
