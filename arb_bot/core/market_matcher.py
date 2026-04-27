"""Two-layer market matching: curated JSON override + fuzzy text matching.

Resolution divergence is the #1 risk in cross-platform arb. Only markets
matching MARKET_CATEGORIES (default: sports) are considered.

HIGH-RISK patterns are deliberately narrow: only genuine oracle-governance
risks where the *platform* can resolve against the obvious game result.
"Official" language is NOT flagged — in sports it always means the final
game/league result, not an ambiguous government or regulatory source.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    from rapidfuzz import fuzz
except ImportError:  # pragma: no cover
    fuzz = None  # type: ignore

from arb_bot.utils.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Resolution-risk patterns
# HIGH  -> never trade (oracle/governance risk where platform overrides result)
# MEDIUM -> trade only if manual-verified
# ---------------------------------------------------------------------------
_HIGH_RISK_PATTERNS = [
    # UMA oracle / community governance — can vote against the obvious outcome
    r"\bUMA\b",
    r"UMA\s+oracle",
    r"community\s+resolution",
    r"admin(?:istrator)?\s+resolution",
    # Explicit third-party resolution with named non-sports arbiter
    r"resolves?\s+per\s+(?:CFTC|SEC|Fed(?:eral)?|government|court)",
]

_MEDIUM_RISK_PATTERNS = [
    # Game scheduling risk — postponement/cancellation handled differently per platform
    r"postpone[d]?",
    r"cancel(?:le?d)?",
    r"(?:rain|weather)\s*delay",
    # Timing-dependent language that can differ in interpretation
    r"by\s+(?:end\s+of|close\s+of|eod)\b",
    r"before\s+(?:end|close)\s+of",
]

# Kalshi category strings that map to sports
_KALSHI_SPORTS_CATEGORIES = {
    "sports", "football", "basketball", "baseball", "hockey",
    "soccer", "tennis", "golf", "mma", "boxing", "racing",
}

# Title keyword fallback when category field is absent
_SPORTS_TITLE_KEYWORDS = {
    "nfl", "nba", "mlb", "nhl", "mls", "ufc", "fifa", "ncaa", "wnba", "pga",
    "super bowl", "world series", "stanley cup", "nba finals", "march madness",
    "premier league", "champions league", "world cup",
    "playoffs", "playoff",
}


@dataclass
class MarketPair:
    kalshi_ticker: str
    kalshi_title: str
    polymarket_condition_id: str
    polymarket_yes_token_id: str
    polymarket_no_token_id: str
    polymarket_title: str
    match_confidence: float          # 0-100
    resolution_risk: str             # "LOW" | "MEDIUM" | "HIGH"
    verified_manual: bool

    def is_tradeable(self, min_confidence: float = 85.0) -> bool:
        if self.resolution_risk == "HIGH":
            return False
        if not self.verified_manual and self.match_confidence < min_confidence:
            return False
        return True


class MarketMatcher:
    def __init__(self, market_map_path: Optional[Path] = None):
        if market_map_path is None:
            from arb_bot.config import MARKET_MAP_PATH
            market_map_path = MARKET_MAP_PATH
        self._manual_map: list[dict] = self._load_manual_map(market_map_path)

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    async def build_pairs(
        self,
        kalshi_client,
        poly_client,
        fuzzy_threshold: int = 80,
    ) -> list[MarketPair]:
        from arb_bot.config import MARKET_CATEGORIES

        logger.info(f"Fetching Kalshi markets (filter: {MARKET_CATEGORIES})...")
        all_kalshi = await kalshi_client.get_markets(limit=200, status="open")
        kalshi_markets = [
            m for m in all_kalshi
            if self._is_allowed_category(
                m.get("category", ""), m.get("title", ""), MARKET_CATEGORIES
            )
        ]
        logger.info(
            f"  Kalshi: {len(kalshi_markets)} sports markets "
            f"(of {len(all_kalshi)} total)"
        )

        logger.info(f"Fetching Polymarket markets (filter: {MARKET_CATEGORIES})...")
        all_poly = await poly_client.get_markets(
            active=True, limit=500, categories=MARKET_CATEGORIES
        )
        poly_markets = [
            m for m in all_poly
            if self._is_allowed_category(
                m.get("category", ""), m.get("question", ""), MARKET_CATEGORIES
            )
        ]
        logger.info(
            f"  Polymarket: {len(poly_markets)} sports markets "
            f"(of {len(all_poly)} total)"
        )

        pairs = self._apply_manual_map(kalshi_markets, poly_markets)
        manual_tickers = {p.kalshi_ticker for p in pairs}

        if fuzz is None:
            logger.warning("rapidfuzz not installed; skipping fuzzy matching")
        else:
            pairs.extend(
                self._fuzzy_match(
                    kalshi_markets, poly_markets, fuzzy_threshold, manual_tickers
                )
            )

        logger.info(
            f"Matched {len(pairs)} pairs "
            f"({sum(p.verified_manual for p in pairs)} manual, "
            f"{sum(not p.verified_manual for p in pairs)} fuzzy)"
        )
        return pairs

    # ------------------------------------------------------------------
    # Category filtering
    # ------------------------------------------------------------------

    @staticmethod
    def _is_allowed_category(category: str, title: str, allowed: list[str]) -> bool:
        if not allowed:
            return True
        cat_lower = category.strip().lower()
        for c in allowed:
            if c == "sports":
                if cat_lower in _KALSHI_SPORTS_CATEGORIES:
                    return True
                if any(kw in title.lower() for kw in _SPORTS_TITLE_KEYWORDS):
                    return True
            elif c in cat_lower:
                return True
        return False

    # ------------------------------------------------------------------
    # Manual map
    # ------------------------------------------------------------------

    @staticmethod
    def _load_manual_map(path: Path) -> list[dict]:
        if not path.exists():
            return []
        try:
            with open(path) as fh:
                return json.load(fh).get("pairs", [])
        except Exception as exc:
            logger.warning(f"Could not load market_map.json: {exc}")
            return []

    def _apply_manual_map(
        self, kalshi_markets: list[dict], poly_markets: list[dict]
    ) -> list[MarketPair]:
        k_by_ticker    = {m["ticker"]: m for m in kalshi_markets if "ticker" in m}
        p_by_condition = {m.get("conditionId", ""): m for m in poly_markets}

        pairs: list[MarketPair] = []
        for entry in self._manual_map:
            km = k_by_ticker.get(entry.get("kalshi_ticker", ""))
            pm = p_by_condition.get(entry.get("polymarket_condition_id", ""))
            if km is None or pm is None:
                continue
            pairs.append(self._build_pair(km, pm, confidence=100.0, manual=True))
        return pairs

    # ------------------------------------------------------------------
    # Fuzzy matching
    # ------------------------------------------------------------------

    def _fuzzy_match(
        self,
        kalshi_markets: list[dict],
        poly_markets: list[dict],
        threshold: int,
        skip_tickers: set,
    ) -> list[MarketPair]:
        pairs: list[MarketPair] = []
        for km in kalshi_markets:
            ticker = km.get("ticker", "")
            if ticker in skip_tickers:
                continue
            k_title = self._normalize(km.get("title", ""))
            if not k_title:
                continue

            best_pm, best_score = None, 0
            for pm in poly_markets:
                p_title = self._normalize(pm.get("question", ""))
                if not p_title:
                    continue
                score = fuzz.token_sort_ratio(k_title, p_title)
                if score > best_score:
                    best_score, best_pm = score, pm

            if best_pm is not None and best_score >= threshold:
                pairs.append(
                    self._build_pair(km, best_pm, confidence=float(best_score), manual=False)
                )
        return pairs

    # ------------------------------------------------------------------
    # Pair construction
    # ------------------------------------------------------------------

    def _build_pair(
        self, km: dict, pm: dict, confidence: float, manual: bool
    ) -> MarketPair:
        yes_token_id = no_token_id = ""
        for t in pm.get("tokens", []):
            outcome = (t.get("outcome") or "").upper()
            if outcome == "YES":
                yes_token_id = t.get("token_id", "")
            elif outcome == "NO":
                no_token_id = t.get("token_id", "")

        risk = self._assess_resolution_risk(
            km.get("title", ""),
            pm.get("question", ""),
            km.get("close_time", ""),
            pm.get("endDate", ""),
        )
        return MarketPair(
            kalshi_ticker=km.get("ticker", ""),
            kalshi_title=km.get("title", ""),
            polymarket_condition_id=pm.get("conditionId", ""),
            polymarket_yes_token_id=yes_token_id,
            polymarket_no_token_id=no_token_id,
            polymarket_title=pm.get("question", ""),
            match_confidence=confidence,
            resolution_risk=risk,
            verified_manual=manual,
        )

    # ------------------------------------------------------------------
    # Resolution risk assessment
    # ------------------------------------------------------------------

    @staticmethod
    def _assess_resolution_risk(
        kalshi_title: str, poly_title: str, k_close: str, p_close: str
    ) -> str:
        combined = f"{kalshi_title} {poly_title}".lower()

        for pat in _HIGH_RISK_PATTERNS:
            if re.search(pat, combined, re.IGNORECASE):
                return "HIGH"

        # Expiry mismatch > 0 days → MEDIUM (different events or rolling dates)
        if k_close and p_close and k_close[:10] != p_close[:10]:
            return "MEDIUM"

        for pat in _MEDIUM_RISK_PATTERNS:
            if re.search(pat, combined, re.IGNORECASE):
                return "MEDIUM"

        return "LOW"

    @staticmethod
    def _normalize(title: str) -> str:
        title = title.lower()
        title = re.sub(r"^(will|does|is|are|did|has|have|can|when|what|who)\s+", "", title)
        title = re.sub(r"[^a-z0-9\s]", " ", title)
        return re.sub(r"\s+", " ", title).strip()
