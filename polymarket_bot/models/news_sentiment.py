"""News sentiment probability model.

Uses keyword/headline analysis to adjust market probabilities based on
recent news. This is a rules-based scaffold — in production, plug in an
LLM (Claude API) or a fine-tuned NLP classifier for much higher accuracy.

Research context:
- Ensemble bots using news + social sentiment generated $2.2M in 2 months
- Speed of news processing is critical — first mover captures most of the edge
- Multiple independent signals (news, social, on-chain) combined via ensemble
  outperform any single signal source
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

import structlog

from polymarket_bot.models.base import ModelEstimate, ProbabilityModel
from polymarket_bot.types import Market

logger = structlog.get_logger()


@dataclass
class NewsItem:
    """A news headline or article summary."""
    headline: str
    source: str
    timestamp: datetime
    sentiment_score: float = 0.0  # -1.0 (bearish) to +1.0 (bullish)
    relevance_score: float = 0.0  # 0-1 how relevant to a given market
    url: str = ""


class NewsProvider(Protocol):
    """Interface for news data. Implement with real API in production."""

    async def get_recent_news(
        self, keywords: list[str], max_items: int = 20
    ) -> list[NewsItem]: ...


class MockNewsProvider:
    """Simulated news provider for testing and backtesting."""

    def __init__(self) -> None:
        self._headlines: list[NewsItem] = [
            NewsItem(
                headline="Bitcoin surges past $98,000 amid institutional buying",
                source="CoinDesk",
                timestamp=datetime.now(timezone.utc),
                sentiment_score=0.7,
            ),
            NewsItem(
                headline="Federal Reserve signals potential rate cut in upcoming meeting",
                source="Reuters",
                timestamp=datetime.now(timezone.utc),
                sentiment_score=0.5,
            ),
            NewsItem(
                headline="Ethereum network upgrade completed successfully",
                source="The Block",
                timestamp=datetime.now(timezone.utc),
                sentiment_score=0.6,
            ),
        ]

    async def get_recent_news(
        self, keywords: list[str], max_items: int = 20
    ) -> list[NewsItem]:
        # Filter by keyword relevance
        results: list[NewsItem] = []
        for item in self._headlines:
            headline_lower = item.headline.lower()
            for kw in keywords:
                if kw.lower() in headline_lower:
                    item.relevance_score = 0.8
                    results.append(item)
                    break
        return results[:max_items]


# Sentiment keyword dictionaries
BULLISH_KEYWORDS = {
    "surges", "soars", "rallies", "jumps", "breaks above", "all-time high",
    "institutional buying", "adoption", "approval", "upgrade", "success",
    "partnership", "growth", "record", "bullish", "positive", "gain",
    "rate cut", "easing", "stimulus", "recovery",
}
BEARISH_KEYWORDS = {
    "crashes", "plunges", "drops", "falls", "breaks below", "selloff",
    "hack", "exploit", "ban", "regulation", "crackdown", "lawsuit",
    "bankruptcy", "default", "bearish", "negative", "loss", "decline",
    "rate hike", "tightening", "recession", "inflation",
}


def _score_headline(headline: str) -> float:
    """Simple keyword-based sentiment scoring. Returns -1 to +1."""
    words = set(headline.lower().split())
    headline_lower = headline.lower()

    bullish_hits = sum(1 for kw in BULLISH_KEYWORDS if kw in headline_lower)
    bearish_hits = sum(1 for kw in BEARISH_KEYWORDS if kw in headline_lower)

    total = bullish_hits + bearish_hits
    if total == 0:
        return 0.0

    return (bullish_hits - bearish_hits) / total


def _extract_keywords(question: str) -> list[str]:
    """Extract searchable keywords from a market question."""
    # Remove common filler words
    stop_words = {
        "will", "the", "a", "an", "by", "in", "at", "to", "of", "for",
        "this", "that", "be", "is", "are", "was", "were", "has", "have",
        "end", "next", "before", "after",
    }
    words = re.findall(r'[A-Za-z]+', question)
    keywords = [w for w in words if w.lower() not in stop_words and len(w) > 2]
    return keywords[:8]  # Top 8 keywords


class NewsSentimentModel(ProbabilityModel):
    """Adjusts market probabilities based on recent news sentiment.

    Process:
    1. Extract keywords from market question
    2. Fetch recent news matching those keywords
    3. Score headline sentiment (bullish/bearish)
    4. Compute weighted sentiment adjustment
    5. Adjust market probability accordingly

    In production, replace _score_headline() with an LLM call:
      prompt = f"Given the market question '{question}' and headline
               '{headline}', estimate the probability shift (-1 to +1)"
    """

    def __init__(self, news_provider: NewsProvider | None = None) -> None:
        self._provider = news_provider or MockNewsProvider()

    @property
    def name(self) -> str:
        return "news_sentiment"

    async def estimate(self, market: Market) -> list[ModelEstimate]:
        keywords = _extract_keywords(market.question)
        news_items = await self._provider.get_recent_news(keywords)

        if not news_items:
            # No news — use market price with low confidence
            return [
                ModelEstimate(
                    model_name=self.name,
                    market_id=market.market_id,
                    token_id=t.token_id,
                    outcome=t.outcome,
                    probability=t.price,
                    confidence=0.15,
                    reasoning="No relevant news found",
                )
                for t in market.tokens
            ]

        # Score each headline
        for item in news_items:
            if item.sentiment_score == 0:
                item.sentiment_score = _score_headline(item.headline)

        # Weighted average sentiment (more relevant = more weight)
        total_weight = sum(max(0.1, item.relevance_score) for item in news_items)
        weighted_sentiment = sum(
            item.sentiment_score * max(0.1, item.relevance_score)
            for item in news_items
        ) / total_weight if total_weight > 0 else 0.0

        # Determine if sentiment is bullish for YES outcome
        # Positive sentiment → higher YES probability
        # Map sentiment (-1,+1) to probability adjustment (-0.15, +0.15)
        max_adjustment = 0.15
        adjustment = weighted_sentiment * max_adjustment

        # Confidence based on number of articles and consistency
        consistency = 1.0 - (
            sum(abs(item.sentiment_score - weighted_sentiment) for item in news_items)
            / max(1, len(news_items))
        )
        confidence = min(0.85, 0.3 + 0.3 * min(1.0, len(news_items) / 5) + 0.25 * consistency)

        estimates: list[ModelEstimate] = []
        for token in market.tokens:
            if token.outcome.lower() == "yes":
                model_prob = max(0.02, min(0.98, token.price + adjustment))
            else:
                model_prob = max(0.02, min(0.98, token.price - adjustment))

            estimates.append(ModelEstimate(
                model_name=self.name,
                market_id=market.market_id,
                token_id=token.token_id,
                outcome=token.outcome,
                probability=model_prob,
                confidence=confidence,
                reasoning=(
                    f"News sentiment: {weighted_sentiment:+.3f} from {len(news_items)} articles, "
                    f"adjustment={adjustment:+.3f}, consistency={consistency:.2f}"
                ),
                features={
                    "num_articles": len(news_items),
                    "weighted_sentiment": round(weighted_sentiment, 3),
                    "adjustment": round(adjustment, 4),
                    "headlines": [item.headline for item in news_items[:3]],
                },
            ))

        return estimates
