"""Ensemble probability model combining multiple signal sources."""

from __future__ import annotations

import structlog

from polymarket_bot.models.base import ModelEstimate, ProbabilityModel
from polymarket_bot.types import Market

logger = structlog.get_logger(__name__)


class MarketImpliedModel(ProbabilityModel):
    """Uses the market price itself as a baseline probability.

    This is the simplest model: it treats the current market price as the
    implied probability and adds no alpha. Used as a baseline/anchor.
    """

    @property
    def name(self) -> str:
        return "market_implied"

    async def estimate(self, market: Market) -> list[ModelEstimate]:
        estimates: list[ModelEstimate] = []
        for token in market.tokens:
            estimates.append(
                ModelEstimate(
                    market_condition_id=market.condition_id,
                    token_id=token.token_id,
                    outcome=token.outcome,
                    probability=token.price,
                    confidence=0.5,  # Low confidence: just the market price
                    model_name=self.name,
                )
            )
        return estimates


class ComplementArbitrageModel(ProbabilityModel):
    """Detects mispricings via complement constraint (YES + NO should = 1.0).

    For binary markets, if YES + NO != 1.0 there is a direct arbitrage
    opportunity. This model flags the side that offers edge.
    """

    @property
    def name(self) -> str:
        return "complement_arb"

    async def estimate(self, market: Market) -> list[ModelEstimate]:
        estimates: list[ModelEstimate] = []
        if not market.is_binary or len(market.tokens) != 2:
            return estimates

        yes_price = market.tokens[0].price
        no_price = market.tokens[1].price
        total = yes_price + no_price

        if total == 0:
            return estimates

        # Normalize: the "true" probability from the complement constraint
        yes_prob = yes_price / total
        no_prob = no_price / total
        overround = abs(total - 1.0)

        # Confidence is higher when the overround (mispricing) is larger
        confidence = min(1.0, overround * 10)

        estimates.append(
            ModelEstimate(
                market_condition_id=market.condition_id,
                token_id=market.tokens[0].token_id,
                outcome=market.tokens[0].outcome,
                probability=yes_prob,
                confidence=confidence,
                model_name=self.name,
                reasoning=f"complement_total={total:.4f} overround={overround:.4f}",
            )
        )
        estimates.append(
            ModelEstimate(
                market_condition_id=market.condition_id,
                token_id=market.tokens[1].token_id,
                outcome=market.tokens[1].outcome,
                probability=no_prob,
                confidence=confidence,
                model_name=self.name,
                reasoning=f"complement_total={total:.4f} overround={overround:.4f}",
            )
        )
        return estimates


class VolumeWeightedModel(ProbabilityModel):
    """Adjusts probabilities based on volume and liquidity signals.

    Higher volume relative to liquidity suggests more informed trading,
    which means the market price may be more accurate. Lower volume in
    high-liquidity markets may indicate stale pricing and opportunity.
    """

    @property
    def name(self) -> str:
        return "volume_weighted"

    async def estimate(self, market: Market) -> list[ModelEstimate]:
        estimates: list[ModelEstimate] = []
        if not market.tokens:
            return estimates

        # Volume/liquidity ratio as a proxy for price discovery quality
        vl_ratio = (
            market.volume_usd / market.liquidity_usd if market.liquidity_usd > 0 else 0.0
        )

        # High V/L = good price discovery = price is likely accurate
        # Low V/L = poor price discovery = more alpha opportunity
        confidence = max(0.2, min(0.9, 1.0 - (vl_ratio * 0.1)))

        for token in market.tokens:
            # Slightly mean-revert extreme prices (they tend to overshoot)
            price = token.price
            if price > 0.90:
                adj_prob = price - 0.01 * (1.0 - confidence)
            elif price < 0.10:
                adj_prob = price + 0.01 * (1.0 - confidence)
            else:
                adj_prob = price

            estimates.append(
                ModelEstimate(
                    market_condition_id=market.condition_id,
                    token_id=token.token_id,
                    outcome=token.outcome,
                    probability=max(0.01, min(0.99, adj_prob)),
                    confidence=confidence,
                    model_name=self.name,
                    reasoning=f"vl_ratio={vl_ratio:.3f}",
                )
            )
        return estimates


class EnsembleProbabilityModel(ProbabilityModel):
    """Combines multiple models with weighted averaging.

    The ensemble approach reduces model-specific risk and provides
    more robust probability estimates.
    """

    def __init__(self, models: list[ProbabilityModel] | None = None) -> None:
        self._models = models or [
            MarketImpliedModel(),
            ComplementArbitrageModel(),
            VolumeWeightedModel(),
        ]

    @property
    def name(self) -> str:
        return "ensemble"

    async def estimate(self, market: Market) -> list[ModelEstimate]:
        all_estimates: list[list[ModelEstimate]] = []
        for model in self._models:
            try:
                estimates = await model.estimate(market)
                if estimates:
                    all_estimates.append(estimates)
            except Exception as e:
                logger.warning("model_error", model=model.name, error=str(e))

        if not all_estimates:
            return []

        # Merge by token_id using confidence-weighted average
        token_estimates: dict[str, list[ModelEstimate]] = {}
        for estimates in all_estimates:
            for est in estimates:
                token_estimates.setdefault(est.token_id, []).append(est)

        merged: list[ModelEstimate] = []
        for token_id, estimates in token_estimates.items():
            total_weight = sum(e.confidence for e in estimates)
            if total_weight == 0:
                continue

            weighted_prob = sum(e.probability * e.confidence for e in estimates) / total_weight
            avg_confidence = total_weight / len(estimates)
            model_names = ", ".join(e.model_name for e in estimates)

            merged.append(
                ModelEstimate(
                    market_condition_id=estimates[0].market_condition_id,
                    token_id=token_id,
                    outcome=estimates[0].outcome,
                    probability=weighted_prob,
                    confidence=avg_confidence,
                    model_name=self.name,
                    reasoning=f"ensemble({model_names}) n={len(estimates)}",
                )
            )

        return merged
