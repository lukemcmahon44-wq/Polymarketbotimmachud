"""Built-in probability models and ensemble combiner."""

from __future__ import annotations

import math
import random
from typing import Any

import structlog

from polymarket_bot.models.base import ModelEstimate, ProbabilityModel
from polymarket_bot.types import Market

logger = structlog.get_logger()


class NaiveModel(ProbabilityModel):
    """Baseline: uses market price with slight mean-reversion toward 0.5.

    This is intentionally simple — it exists to test the pipeline.
    Replace with a real model for production use.
    """

    @property
    def name(self) -> str:
        return "naive_mean_reversion"

    async def estimate(self, market: Market) -> list[ModelEstimate]:
        estimates: list[ModelEstimate] = []
        for token in market.tokens:
            # Slight pull toward 0.5 (mean reversion heuristic)
            reversion_strength = 0.05
            model_prob = token.price + reversion_strength * (0.5 - token.price)
            model_prob = max(0.01, min(0.99, model_prob))

            estimates.append(ModelEstimate(
                model_name=self.name,
                market_id=market.market_id,
                token_id=token.token_id,
                outcome=token.outcome,
                probability=model_prob,
                confidence=0.3,  # Low confidence — it's naive
                reasoning="Mean-reversion toward 0.5",
            ))
        return estimates


class MeanReversionModel(ProbabilityModel):
    """Stronger mean-reversion model with volume and time-to-resolution factors."""

    @property
    def name(self) -> str:
        return "mean_reversion_v2"

    async def estimate(self, market: Market) -> list[ModelEstimate]:
        from datetime import datetime, timezone

        estimates: list[ModelEstimate] = []
        hours_to_end = max(
            1.0,
            (market.end_date - datetime.now(timezone.utc)).total_seconds() / 3600,
        )

        for token in market.tokens:
            # Stronger mean reversion for far-out markets, weaker for near-term
            time_factor = min(1.0, hours_to_end / (24 * 30))  # Normalize to ~30 days
            reversion = 0.10 * time_factor
            model_prob = token.price + reversion * (0.5 - token.price)

            # Volume adjustment: high volume = price is more informative
            volume_confidence = min(1.0, market.volume_usd / 100_000)
            model_prob = volume_confidence * token.price + (1 - volume_confidence) * model_prob
            model_prob = max(0.01, min(0.99, model_prob))

            confidence = 0.4 + 0.2 * volume_confidence
            estimates.append(ModelEstimate(
                model_name=self.name,
                market_id=market.market_id,
                token_id=token.token_id,
                outcome=token.outcome,
                probability=model_prob,
                confidence=confidence,
                reasoning=f"Mean-reversion with time_factor={time_factor:.2f}, vol_conf={volume_confidence:.2f}",
            ))
        return estimates


class LiquidityAdjustedModel(ProbabilityModel):
    """Adjusts prices based on order book depth and spread."""

    @property
    def name(self) -> str:
        return "liquidity_adjusted"

    async def estimate(self, market: Market) -> list[ModelEstimate]:
        estimates: list[ModelEstimate] = []
        for token in market.tokens:
            # Wide spreads suggest uncertainty — push toward 0.5
            spread_proxy = 0.05  # Default assumed spread
            spread_adjustment = spread_proxy * (0.5 - token.price)
            model_prob = token.price + spread_adjustment
            model_prob = max(0.01, min(0.99, model_prob))

            # Liquidity-based confidence
            liq_score = min(1.0, market.liquidity_usd / 50_000)
            confidence = 0.35 + 0.25 * liq_score

            estimates.append(ModelEstimate(
                model_name=self.name,
                market_id=market.market_id,
                token_id=token.token_id,
                outcome=token.outcome,
                probability=model_prob,
                confidence=confidence,
                reasoning=f"Liquidity-adjusted, liq_score={liq_score:.2f}",
            ))
        return estimates


class EnsembleModel(ProbabilityModel):
    """Combines multiple models via weighted average.

    Weights are proportional to each model's confidence.
    """

    def __init__(self, models: list[ProbabilityModel] | None = None) -> None:
        if models is not None:
            self._models = models
        else:
            from polymarket_bot.models.crypto_momentum import CryptoMomentumModel
            from polymarket_bot.models.news_sentiment import NewsSentimentModel
            self._models = [
                NaiveModel(),
                MeanReversionModel(),
                LiquidityAdjustedModel(),
                CryptoMomentumModel(),
                NewsSentimentModel(),
            ]

    @property
    def name(self) -> str:
        return "ensemble"

    async def estimate(self, market: Market) -> list[ModelEstimate]:
        all_estimates: list[list[ModelEstimate]] = []
        for model in self._models:
            try:
                est = await model.estimate(market)
                all_estimates.append(est)
            except Exception as exc:
                logger.warning("ensemble.model_error", model=model.name, error=str(exc))

        if not all_estimates:
            # Fallback: use market prices
            return [
                ModelEstimate(
                    model_name=self.name,
                    market_id=market.market_id,
                    token_id=t.token_id,
                    outcome=t.outcome,
                    probability=t.price,
                    confidence=0.1,
                    reasoning="Fallback to market price (all models failed)",
                )
                for t in market.tokens
            ]

        # Group by token_id and compute weighted average
        token_estimates: dict[str, list[ModelEstimate]] = {}
        for estimates in all_estimates:
            for est in estimates:
                token_estimates.setdefault(est.token_id, []).append(est)

        combined: list[ModelEstimate] = []
        for token_id, ests in token_estimates.items():
            total_weight = sum(e.confidence for e in ests)
            if total_weight == 0:
                avg_prob = sum(e.probability for e in ests) / len(ests)
                avg_conf = 0.1
            else:
                avg_prob = sum(e.probability * e.confidence for e in ests) / total_weight
                avg_conf = total_weight / len(ests)

            avg_prob = max(0.01, min(0.99, avg_prob))
            model_names = [e.model_name for e in ests]

            combined.append(ModelEstimate(
                model_name=self.name,
                market_id=ests[0].market_id,
                token_id=token_id,
                outcome=ests[0].outcome,
                probability=avg_prob,
                confidence=min(0.95, avg_conf),
                reasoning=f"Ensemble of {model_names}, weighted by confidence",
                features={"sub_estimates": [
                    {"model": e.model_name, "prob": round(e.probability, 4), "conf": round(e.confidence, 3)}
                    for e in ests
                ]},
            ))

        # Normalize probabilities to sum to ~1.0
        total = sum(e.probability for e in combined)
        if total > 0 and len(combined) > 1:
            for e in combined:
                e.probability = e.probability / total

        return combined
