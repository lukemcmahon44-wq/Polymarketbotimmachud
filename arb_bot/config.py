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
# "international" | "us" — Polymarket US (CFTC-licensed, 2025) needs separate creds
POLYMARKET_VERSION: str = os.environ.get("POLYMARKET_VERSION", "international")

# ---------------------------------------------------------------------------
# Bot behaviour
# ---------------------------------------------------------------------------
DRY_RUN: bool = os.environ.get("DRY_RUN", "true").lower() == "true"

# Minimum net profit fraction required to execute a trade (after all fees)
MIN_EDGE_AFTER_FEES: float = float(os.environ.get("MIN_EDGE_AFTER_FEES", "0.035"))

# Maximum USDC deployed on a single arb pair
MAX_POSITION_PER_MARKET: float = float(os.environ.get("MAX_POSITION_PER_MARKET", "10"))

# Maximum total USDC deployed across all open arb positions simultaneously
MAX_GLOBAL_EXPOSURE: float = float(os.environ.get("MAX_GLOBAL_EXPOSURE", "100"))

# Kill switch: halt all trading when daily realised P&L drops below this (negative number)
MAX_DAILY_LOSS: float = float(os.environ.get("MAX_DAILY_LOSS", "10"))

# How often the main loop polls for new opportunities
SCAN_INTERVAL_SECONDS: float = float(os.environ.get("SCAN_INTERVAL_SECONDS", "2"))

# Minimum fraction of an order that must fill at the target price before executing
MIN_FILL_RATIO: float = float(os.environ.get("MIN_FILL_RATIO", "0.90"))

# Minimum fuzzy-match confidence (0-100) for auto-matched pairs
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
# Kalshi fee model (configurable)
# Default: ~7% of potential profit on the winning leg
# ---------------------------------------------------------------------------
KALSHI_FEE_RATE: float = float(os.environ.get("KALSHI_FEE_RATE", "0.07"))

# Polymarket taker fees by category
POLYMARKET_FEES: dict = {
    "crypto": 0.0180,
    "politics": 0.0100,
    "finance": 0.0100,
    "sports": 0.0075,
    "economics": 0.0150,
    "default": 0.0125,
}

# Path to manually curated market pairs
MARKET_MAP_PATH: Path = Path(__file__).parent / "data" / "market_map.json"
