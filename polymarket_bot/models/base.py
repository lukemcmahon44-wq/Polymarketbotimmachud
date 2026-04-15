"""Base interface for probability estimation models."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from polymarket_bot.types import Market


@dataclass
class ModelEstimate:
    """A probability estimate from a model."""
    model_name: str
    market_id: str
    token_id: str
    outcome: str
    probability: float        # 0.0–1.0
    confidence: float         # 0.0–1.0 (how sure the model is)
    reasoning: str = ""
    features: dict[str, Any] | None = None


class ProbabilityModel(ABC):
    """Abstract base for probability estimation.

    Implement this interface to plug in any model — from simple heuristics
    to LLM-based reasoning or ML classifiers.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable model name."""

    @abstractmethod
    async def estimate(self, market: Market) -> list[ModelEstimate]:
        """Produce probability estimates for each outcome in a market.

        Returns one ModelEstimate per token/outcome in the market.
        Probabilities across outcomes should sum to ~1.0.
        """

    async def batch_estimate(self, markets: list[Market]) -> list[list[ModelEstimate]]:
        """Estimate for multiple markets. Override for batch-optimized models."""
        results: list[list[ModelEstimate]] = []
        for market in markets:
            results.append(await self.estimate(market))
        return results
