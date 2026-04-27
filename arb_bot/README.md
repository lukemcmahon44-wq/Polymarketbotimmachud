# Kalshi × Polymarket Cross-Platform Arbitrage Bot

A production-ready Python bot that monitors Kalshi and Polymarket simultaneously,
detects mispriced event contracts, and executes simultaneous cross-platform trades
to lock in guaranteed profit regardless of outcome.

**Core mechanic:** If `YES_price_A + NO_price_B < $1.00` (after fees), buying both
legs simultaneously guarantees a $1.00 payout whichever way the event resolves.
The spread is pure profit.

---

## ⚠️ CRITICAL: Start in Dry-Run Mode

The bot defaults to `DRY_RUN=true`. It will **refuse** to place live trades unless
both `--live` (CLI flag) **and** `DRY_RUN=false` (in `.env`) are set simultaneously.

Run at least 50 dry-run cycles before switching to live. Verify the logged edge is
consistently positive before deploying real capital.

---

## Setup

### Prerequisites
- Python 3.11+
- A funded Kalshi account with API access
- A funded Polymarket account with a Polygon (MATIC) wallet

### Installation

```bash
git clone <repo>
cd arb_bot
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### Configure environment

```bash
cp .env.example .env
# Edit .env with your credentials
```

---

## Getting API Credentials

### Kalshi

1. Create an account at [kalshi.com](https://kalshi.com)
2. Generate an RSA-2048 key pair:
   ```bash
   openssl genrsa -out kalshi_private_key.pem 2048
   openssl rsa -in kalshi_private_key.pem -pubout -out kalshi_public_key.pem
   ```
3. Upload `kalshi_public_key.pem` in the Kalshi web console under **Account → API Settings**.
4. Copy the displayed **Key ID** into `.env` as `KALSHI_API_KEY_ID`.
5. Set `KALSHI_PRIVATE_KEY_PATH=./kalshi_private_key.pem` in `.env`.

> **Keep `kalshi_private_key.pem` out of version control.** It is already in `.gitignore`.

### Polymarket

1. Create a Polymarket account at [polymarket.com](https://polymarket.com)
2. Export your wallet private key from MetaMask: **Account Details → Export Private Key**
3. Your wallet address is the **Funder Address**.
4. Fund your wallet with USDC on Polygon (bridge from Ethereum or buy directly on Polygon).
5. Set `POLYMARKET_PRIVATE_KEY` and `POLYMARKET_FUNDER_ADDRESS` in `.env`.

> **Polymarket US** (CFTC-licensed, launched 2025) requires separate credentials.
> Set `POLYMARKET_VERSION=us` if you are using the US product.

### Funding accounts

- **Kalshi:** Deposit USD via ACH or wire on the Kalshi website.
- **Polymarket:** Deposit USDC on Polygon. Use the [Polygon Bridge](https://portal.polygon.technology/bridge) or buy USDC directly on a Polygon-compatible exchange.

---

## Running the Bot

```bash
# Dry-run with dashboard (recommended first step)
python -m arb_bot.main

# Dry-run without dashboard
python -m arb_bot.main --no-dashboard

# Live trading (requires DRY_RUN=false in .env)
python -m arb_bot.main --live
```

The dashboard is available at `http://localhost:8000` while the bot is running.

---

## Configuration Reference

| Variable | Default | Description |
|---|---|---|
| `DRY_RUN` | `true` | Simulate trades without executing them |
| `MIN_EDGE_AFTER_FEES` | `0.035` | Minimum net profit fraction to execute (3.5%) |
| `MAX_POSITION_PER_MARKET` | `10` | Max USDC per single arb trade |
| `MAX_GLOBAL_EXPOSURE` | `100` | Max total USDC deployed across all positions |
| `MAX_DAILY_LOSS` | `10` | Kill switch: halt if daily P&L drops below `-$N` |
| `SCAN_INTERVAL_SECONDS` | `2` | Main loop polling interval |
| `MIN_FILL_RATIO` | `0.90` | Required order-book fill ratio at target price |
| `MIN_MATCH_CONFIDENCE` | `85` | Minimum fuzzy-match score (0–100) for auto-pairs |
| `KALSHI_FEE_RATE` | `0.07` | Kalshi fee as fraction of potential winnings |
| `ALERT_WEBHOOK_URL` | *(empty)* | Discord or Telegram webhook for alerts |

### Recommended first-week settings

```
DRY_RUN=true
MIN_EDGE_AFTER_FEES=0.035
MAX_POSITION_PER_MARKET=10
MAX_GLOBAL_EXPOSURE=100
MAX_DAILY_LOSS=10
```

Once 50+ dry-run cycles log consistent positive edge, switch to live with `$10` max
position before scaling up.

---

## Adding Manual Market Pairs

Manually verified pairs take priority over fuzzy-matched ones and bypass the
confidence threshold. Edit `arb_bot/data/market_map.json`:

```json
{
  "pairs": [
    {
      "kalshi_ticker": "PRES-2026-DJT",
      "polymarket_condition_id": "0xabc123...",
      "notes": "Both resolve on AP call; same deadline; verified 2026-01-15"
    }
  ]
}
```

Find the Polymarket `conditionId` from the Gamma API:
```bash
curl "https://gamma-api.polymarket.com/markets?active=true&limit=20" | python -m json.tool | grep conditionId
```

---

## Resolution Risk — The #1 Danger

The **2024 government shutdown** case is the canonical failure mode:
- Polymarket resolved **YES** (no shutdown occurred)
- Kalshi resolved **NO** (shutdown did technically happen)

This caused a total loss on both legs simultaneously instead of a guaranteed
$1.00 payout.

The bot automatically flags pairs as `HIGH` or `MEDIUM` risk based on:
- Presence of ambiguous resolution language ("official", "UMA oracle", "community resolution")
- Expiry date mismatches between platforms
- Differing criteria like "exceeding X hours" vs "occurring"

Pairs flagged `HIGH` are **never traded**. Review `MEDIUM` pairs manually before
adding them to `market_map.json`.

---

## Project Structure

```
arb_bot/
├── main.py                  # Entry point with CLI flags
├── config.py                # All config, loaded from .env
├── clients/
│   ├── kalshi_client.py     # Kalshi REST v2 + WebSocket (RSA-PSS auth)
│   └── polymarket_client.py # Polymarket CLOB + Gamma (EIP-712 auth)
├── core/
│   ├── market_matcher.py    # Fuzzy + manual market pair matching
│   ├── arb_detector.py      # Scans for profitable spreads
│   ├── executor.py          # Fires both legs via asyncio.gather
│   └── risk_manager.py      # Position limits, kill switch, daily loss cap
├── data/
│   ├── price_feed.py        # Unified WebSocket-fed price cache
│   └── market_map.json      # Manually curated + auto-discovered market pairs
├── dashboard/
│   └── server.py            # FastAPI + WebSocket live dashboard
├── utils/
│   ├── logger.py            # Structured JSON logging
│   ├── fee_calculator.py    # Fee-adjusted edge computation
│   └── alert.py             # Discord/Telegram webhook alerts
├── tests/
│   ├── test_arb_detector.py
│   └── test_fee_calculator.py
├── .env.example
├── requirements.txt
└── README.md
```

---

## Running Tests

```bash
pip install pytest pytest-asyncio
pytest arb_bot/tests/ -v
```

---

## Known Limitations

1. **Resolution risk is the primary danger.** Always verify pairs manually before
   live trading. The fuzzy matcher catches obvious pairs but cannot verify that
   resolution criteria are identical.

2. **Kalshi rate limits.** Free accounts: ~10 order writes per minute.
   Start with `MAX_POSITION_PER_MARKET=10` and low frequency.

3. **Polymarket US vs International.** `py-clob-client` targets Polymarket
   International. Polymarket US (CFTC-licensed) requires separate integration.
   Set `POLYMARKET_VERSION=us` in `.env` when support is added.

4. **Slippage.** The bot estimates available depth from WebSocket data. Order-book
   depth for a more accurate estimate requires REST calls (adds latency). The
   `MIN_FILL_RATIO` guard mitigates but does not eliminate slippage risk.

5. **Execution latency.** Both legs must fill within ~500ms. Network latency,
   API throttling, or blockchain congestion (Polymarket on Polygon) can cause
   partial fills. The executor handles this but monitor the kill switch.

6. **No tax / regulatory advice.** Consult a professional before trading.

---

## Backtest / Simulation

To simulate on historical data:

1. Collect price snapshots by logging `feed.snapshot()` every few seconds during
   a dry-run session.
2. Replay the snapshots through `ArbDetector.scan()` with your target parameters.
3. Count how many detected opportunities would have been profitable after full fees.

There is no built-in backtest runner yet; contributions welcome.

---

## Security Notes

- **Never commit `.env` or `kalshi_private_key.pem`** — both are in `.gitignore`.
- The Polymarket private key controls your on-chain wallet. Treat it like a seed
  phrase.
- The dashboard binds to `localhost` by default. Do not expose it publicly without
  authentication.
