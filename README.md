# Polymarket Autonomous Trading Bot

A production-ready, autonomous Polymarket trading bot with layered risk management,
copy-trade defense, backtesting, and a hard paper-first safety policy.

> **⚠️ LEGAL DISCLAIMER**: This software is for educational and research purposes.
> Trading on prediction markets involves substantial financial risk. You are solely
> responsible for compliance with local laws and Polymarket's Terms of Service.
> This software does NOT constitute financial advice. Polymarket restricts access
> from certain jurisdictions — verify your eligibility before use.

---

## Features

| Feature | Status |
|---------|--------|
| Market scanning (REST + WebSocket) | ✅ |
| EV-based mispricing signals | ✅ |
| Complement + MEE arbitrage detection | ✅ |
| Ensemble probability model | ✅ |
| Paper trading simulator (slippage, partial fills, latency) | ✅ |
| Live trading (gated, passphrase-confirmed) | ✅ |
| 7-layer risk manager (Kelly sizing, stops, circuit breakers) | ✅ |
| On-chain copy-trade detection (Polygon) | ✅ |
| Backtest engine with Monte Carlo stress tests | ✅ |
| Structured audit logs | ✅ |
| Slack/Discord/email alerts | ✅ |
| CLI control plane with emergency kill switch | ✅ |
| Docker Compose (paper mode) | ✅ |
| CI pipeline | ✅ |

---

## Quick Start

### Prerequisites

- Python 3.10+
- (Optional) Docker & Docker Compose

### 1. Clone and install

```bash
git clone <repo-url>
cd Polymarketbotimmachud

python -m venv .venv
source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env — add POLYGON_RPC_URL at minimum
# NEVER add POLYMARKET_PRIVATE_KEY until you are ready for live trading
```

### 3. Run example (no API keys required)

```bash
python run_example.py
```

This runs a single scan → signal → risk check → paper execution cycle using
synthetic market data. See `logs/example_run.json` for full structured output.

---

## Running in Paper Mode

Paper mode is the **default**. All trades are simulated with realistic slippage,
partial fills, and latency. No real funds are used.

### Option A: CLI

```bash
python -m polymarket_bot.cli.control run --paper
```

### Option B: Docker Compose

```bash
docker compose up
```

The Docker image explicitly sets `ENABLE_LIVE_TRADING=false` and cannot be changed
without modifying the compose file.

---

## Running Backtests

```bash
# Default: 20 synthetic markets × 500 time steps
python run_backtest.py

# Custom parameters
python run_backtest.py --markets 50 --steps 1000 --output logs/my_backtest.json

# Via CLI
python -m polymarket_bot.cli.control backtest --markets 20 --steps 500
```

Sample output:
```
============================================================
  BACKTEST PERFORMANCE REPORT
============================================================
  Total Trades       : 47
  Win Rate           : 62.0%
  Total P&L          : $312.45
  Avg Win            : $18.20
  Avg Loss           : $-8.40
  Profit Factor      : 2.17
  Sharpe Ratio       : 1.842
  Sortino Ratio      : 2.103
  Max Drawdown       : 8.3%
  Calmar Ratio       : 3.76

  --- Monte Carlo (1000 simulations) ---
  P&L P5  / P50 / P95: $-120 / $295 / $780
  DD  P50 / P95 / P99: 6.1% / 14.2% / 18.9%
============================================================
```

---

## Enabling Live Trading

> **⚠️ WARNING**: Live trading uses real funds. Proceed only after extensive paper
> trading validation and thorough review of all risk parameters.

### Pre-flight Checklist

- [ ] Paper traded for at least 30 days with positive P&L
- [ ] Reviewed and understood all risk parameters in `config/default.yaml`
- [ ] Created a **dedicated bot wallet** with limited funds (never use your main wallet)
- [ ] Funded the bot wallet with only the capital you are willing to risk entirely
- [ ] Set `POLYMARKET_PRIVATE_KEY` and `POLYMARKET_FUNDER_ADDRESS` securely (env vars, not code)
- [ ] Verified you are in a jurisdiction where Polymarket is accessible
- [ ] Read and accepted Polymarket's Terms of Service
- [ ] Set `per_trade_max_usd` and `global_exposure_usd` to conservative values
- [ ] Configured `first_live_trades_manual_review: 10` (default) for manual approval of first 10 trades
- [ ] Set up alert webhooks (Slack/Discord) for circuit breaker events

### Step-by-Step Live Enablement

**Step 1**: Set environment variables

```bash
export ENABLE_LIVE_TRADING=true
export POLYMARKET_PRIVATE_KEY="0x..."    # Bot wallet private key
export POLYMARKET_FUNDER_ADDRESS="0x..." # Bot wallet address
export POLYMARKET_CHAIN_ID=137
```

**Step 2**: Enable in config

```yaml
# config/default.yaml (or your custom config)
enable_live_trading: true
paper_mode: false
first_live_trades_manual_review: 10
```

**Step 3**: Arm the bot via CLI

```bash
python -m polymarket_bot.cli.control enable-live
```

You will be prompted to type the exact passphrase:
```
I UNDERSTAND AND ENABLE LIVE TRADING
```

**Step 4**: Confirm the first 10 trades manually

Each of the first `first_live_trades_manual_review` trades will prompt:
```
MANUAL APPROVAL REQUIRED (trade #1)
Market : Will BTC exceed $100k by December?
Side   : BUY  @ 0.4230
Size   : $45.00
EV     : 0.0782
Type 'yes' to approve or 'no' to skip:
```

---

## Risk Management Layers

| Layer | Description | Default |
|-------|-------------|---------|
| L0 Hard Limits | Global/market/trade caps, max positions | $10k / $1k / $500 / 10 |
| L1 Position Sizing | Quarter-Kelly + liquidity floor | 0.25× Kelly, $5k min liquidity |
| L2 Execution | Slippage cap, fill deviation, order TTL | 2% / 3% / 300s |
| L3 Stop/Decay | Stop-loss + time-decay exit | 15% / 48h before resolution |
| L4 Hedging | Category concentration cap | 40% max per category |
| L5 Circuit Breakers | Drawdown, anomalous fill, daily loss | 10% / 3σ / $2k |
| L6 Compliance | Anti-manipulation, paper-first, live gate | Always enforced |

---

## Security Checklist

- **Never hardcode private keys** — use environment variables or a secrets manager
- **Use a dedicated bot wallet** — fund only with capital you can afford to lose
- **Rotate keys periodically** — update `POLYMARKET_PRIVATE_KEY` quarterly
- **Monitor the audit log** — `logs/audit.jsonl` logs every signing event
- **Use the kill switch** if anything looks wrong: `polybot emergency-stop`
- **The bot never logs private keys** — a secret-scrubbing log filter is active

### Secret Rotation

```bash
# 1. Generate new wallet
# 2. Update env var
export POLYMARKET_PRIVATE_KEY="0x<new_key>"
# 3. Update funder address
export POLYMARKET_FUNDER_ADDRESS="0x<new_address>"
# 4. Restart bot
```

---

## Configuration Reference

All parameters are in `config/default.yaml`. Key settings:

```yaml
paper_mode: true                  # Always start in paper mode
ev_threshold: 0.02                # Minimum 2% EV after fees
risk:
  global_exposure_usd: 10000     # Total portfolio cap
  per_market_exposure_usd: 1000  # Per-market cap
  per_trade_max_usd: 500         # Per-trade cap
  kelly_fraction: 0.25           # Quarter-Kelly (conservative)
  drawdown_circuit_breaker_pct: 0.10  # Trip at 10% drawdown
copy_trade_detection:
  similarity_threshold: 0.8      # Trip defensive action at 80% score
  response: reduce_size          # Options: flag_only, reduce_size, randomize_timing
```

---

## Project Structure

```
polymarket_bot/
├── config/         Config loading and validation
├── scanner/        Market feed (REST + WebSocket), CLOB client, Gamma client
├── models/         Pluggable probability models (ensemble, complement, volume)
├── signals/        EV calculator, arbitrage detector (complement, MEE)
├── execution/      Paper executor, live executor (gated), order manager
├── risk/           7-layer risk manager with circuit breaker
├── wallet_tracing/ On-chain copy-trade detection (Polygon)
├── backtest/       Replay engine, synthetic data generator, metrics, Monte Carlo
├── monitoring/     Structured logging, Slack/Discord alerting
├── cli/            Click CLI: run, backtest, status, enable-live, emergency-stop
└── bot.py          Main orchestrator

config/
└── default.yaml    Conservative defaults

tests/
├── conftest.py     Shared fixtures
├── test_signals.py Kelly criterion, EV, arbitrage unit tests
├── test_risk.py    All 7 risk layers unit tests
├── test_execution.py  Paper executor, order manager, live gate tests
├── test_models.py  Probability model tests
├── test_backtest.py   Engine, metrics, Monte Carlo tests
└── test_integration.py  End-to-end pipeline tests
```

---

## Running Tests

```bash
# All tests
pytest tests/ -v

# Unit tests only (fast, no I/O)
pytest tests/ -v --ignore=tests/test_integration.py

# With coverage
pytest tests/ --cov=polymarket_bot --cov-report=term-missing

# Integration tests
pytest tests/test_integration.py -v -m integration
```

---

## Research Report

See [RESEARCH_REPORT.md](./RESEARCH_REPORT.md) for:
- Top Polymarket bot strategies (arbitrage, latency arb, model-based, market making)
- Infrastructure recommendations (WebSocket vs REST, latency, rate limits)
- Copy-trade defense strategies used by top traders
- Recommended configuration defaults with citations

---

## Architecture Notes

- **Async throughout**: `asyncio`-native for high-concurrency I/O
- **Dependency injection**: All external clients are injectable → easy mocking in tests
- **Typed**: Full type hints throughout, compatible with `mypy`
- **Structured logging**: Every decision logged with full context for auditability
- **Paper-first**: Live trading requires two explicit gates (env var + passphrase)
- **Conservative defaults**: All limits intentionally conservative; relax deliberately

---

## License

MIT License — see LICENSE file.

**Use at your own risk. The authors accept no responsibility for financial losses.**
