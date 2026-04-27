"""Central configuration — all values loaded from environment / .env file."""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Kalshi
# ---------------------------------------------------------------------------
KALSHI_API_KEY_ID: str = os.environ.get("KALSHI_API_KEY_ID", "")
KALSHI_PRIVATE_KEY_PATH: str = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "./kalshi_private_key.pem")
KALSHI_BASE_URL: str = os.environ.get("KALSHI_BASE_URL", "https://api.kalshi.com/trade-api/v2")
KALSHI_WS_URL: str = os.environ.get("KALSHI_WS_URL", "wss://api.kalshi.com/trade-api/ws/v2")
KALSHI_DEMO_MODE: bool = os.environ.get("KALSHI_DEMO_MODE", "false").lower() == "true"

# ---------------------------------------------------------------------------
# Polymarket
# ---------------------------------------------------------------------------
POLYMARKET_PRIVATE_KEY: str = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
POLYMARKET_FUNDER_ADDRESS: str = os.environ.get("POLYMARKET_FUNDER_ADDRESS", "")
POLYMARKET_CLOB_HOST: str = os.environ.get("POLYMARKET_CLOB_HOST", "https://clob.polymarket.com")
POLYMARKET_GAMMA_HOST: str = os.environ.get("POLYMARKET_GAMMA_HOST", "https://gamma-api.polymarket.com")
POLYMARKET_CHAIN_ID: int = int(os.environ.get("POLYMARKET_CHAIN_ID", "137"))
POLYMARKET_VERSION: str = os.environ.get("POLYMARKET_VERSION", "international")

# ---------------------------------------------------------------------------
# Market category filter
# Comma-separated list of categories to trade. Default: sports only.
# Kalshi category names: Sports, Football, Basketball, Baseball, Hockey, Soccer, Tennis, Golf
# Polymarket category name: Sports
# ---------------------------------------------------------------------------
MARKET_CATEGORIES: list[str] = [
    c.strip().lower()
    for c in os.environ.get("MARKET_CATEGORIES", "sports").split(",")
    if c.strip()
]

# ---------------------------------------------------------------------------
# VPN / connectivity check
# ---------------------------------------------------------------------------
# Verify Polymarket is reachable before starting. Set false only if you are
# certain your network routes Polymarket traffic correctly without a check.
POLYMARKET_CONNECTIVITY_CHECK: bool = (
    os.environ.get("POLYMARKET_CONNECTIVITY_CHECK", "true").lower() == "true"
)
CONNECTIVITY_TIMEOUT: float = float(os.environ.get("CONNECTIVITY_TIMEOUT", "10"))

# ---------------------------------------------------------------------------
# Bot behaviour
# ---------------------------------------------------------------------------
DRY_RUN: bool = os.environ.get("DRY_RUN", "true").lower() == "true"
MIN_EDGE_AFTER_FEES: float = float(os.environ.get("MIN_EDGE_AFTER_FEES", "0.035"))
MAX_POSITION_PER_MARKET: float = float(os.environ.get("MAX_POSITION_PER_MARKET", "10"))
MAX_GLOBAL_EXPOSURE: float = float(os.environ.get("MAX_GLOBAL_EXPOSURE", "100"))
MAX_DAILY_LOSS: float = float(os.environ.get("MAX_DAILY_LOSS", "10"))
SCAN_INTERVAL_SECONDS: float = float(os.environ.get("SCAN_INTERVAL_SECONDS", "2"))
MIN_FILL_RATIO: float = float(os.environ.get("MIN_FILL_RATIO", "0.90"))
MIN_MATCH_CONFIDENCE: float = float(os.environ.get("MIN_MATCH_CONFIDENCE", "85"))

# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------
ALERT_WEBHOOK_URL: str = os.environ.get("ALERT_WEBHOOK_URL", "")

# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
DASHBOARD_HOST: str = os.environ.get("DASHBOARD_HOST", "localhost")
DASHBOARD_PORT: int = int(os.environ.get("DASHBOARD_PORT", "8000"))

# ---------------------------------------------------------------------------
# Fee model
# ---------------------------------------------------------------------------
KALSHI_FEE_RATE: float = float(os.environ.get("KALSHI_FEE_RATE", "0.07"))

POLYMARKET_FEES: dict = {
    "crypto": 0.0180,
    "politics": 0.0100,
    "finance": 0.0100,
    "sports": 0.0075,
    "economics": 0.0150,
    "default": 0.0125,
}

# Default Polymarket taker fee derived from the active category filter.
# With MARKET_CATEGORIES=sports this resolves to 0.0075 (0.75%).
POLYMARKET_DEFAULT_FEE: float = (
    POLYMARKET_FEES.get(MARKET_CATEGORIES[0], POLYMARKET_FEES["default"])
    if len(MARKET_CATEGORIES) == 1
    else POLYMARKET_FEES["default"]
)

# Path to manually curated market pairs
MARKET_MAP_PATH: Path = Path(__file__).parent / "data" / "market_map.json"
