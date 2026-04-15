"""Abstract base for probability estimation models."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from polymarket_bot.types import Market


@dataclass
class ModelEstimate:
    """A probability estimate with confidence."""

    market_condition_id: str
    token_id: str
    outcome: str
    probability: float  # 0.0 to 1.0
    confidence: float  # 0.0 to 1.0
    model_name: str = ""
    reasoning: str = ""

    def __post_init__(self) -> None:
        self.probability = max(0.0, min(1.0, self.probability))
        self.confidence = max(0.0, min(1.0, self.confidence))


class ProbabilityModel(ABC):
    """Interface for probability estimation models.

    Implementations can range from simple heuristics to full ML pipelines.
    The bot uses dependency injection so models can be swapped easily.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable model name."""
        ...

    @abstractmethod
    async def estimate(self, market: Market) -> list[ModelEstimate]:
        """Estimate probabilities for each outcome in a market.

        Returns one ModelEstimate per token/outcome.
        Probabilities should sum to ~1.0 for a complete market.
        """
        ...

    async def batch_estimate(self, markets: list[Market]) -> dict[str, list[ModelEstimate]]:
        """Estimate probabilities for multiple markets. Override for efficiency."""
        results: dict[str, list[ModelEstimate]] = {}
        for market in markets:
            results[market.condition_id] = await self.estimate(market)
        return results
