# Polymarket Trading Bot

Autonomous prediction market trading bot for [Polymarket](https://polymarket.com) with layered risk management, paper-first safety policy, backtesting, and copy-trade detection.

> **LEGAL DISCLAIMER:** This software is provided for educational and research purposes. The operator is solely responsible for compliance with all applicable laws, regulations, and Polymarket's terms of service. Trading prediction markets involves substantial risk of loss. Past performance does not guarantee future results. This software does not constitute financial advice.

---

## Architecture

```
polymarket_bot/
├── scanner/          # Market feed, Polymarket API client, rate limiter
├── models/           # Pluggable probability estimation (ensemble, naive, ML-ready)
├── signals/          # EV calculator, arbitrage detection
├── execution/        # Paper executor, live executor, order lifecycle
├── risk/             # 7-layer risk manager (hard limits → compliance)
├── wallet_tracing/   # On-chain copy-trade detection and defense
├── backtest/         # Replay engine, synthetic data, Monte Carlo
├── monitoring/       # Structured logging, metrics, webhook alerts
├── cli/              # CLI control plane, live-enable passphrase gate
└── config.py         # YAML + env var config loader
```

## Quick Start

### Prerequisites

- Python 3.10+
- Docker (optional, for containerized paper mode)

### Installation

```bash
# Clone the repository
git clone <repo-url> && cd polymarket-bot

# Create virtual environment
python -m venv venv && source venv/bin/activate

# Install dependencies
pip install -e ".[dev]"

# Copy environment template
cp .env.example .env
# Edit .env with your settings (not required for paper mode)
```

### Run in Paper Mode (Default — Safe)

```bash
# Direct execution
polybot run --paper

# Or via Python
python run_example.py

# Or via Docker Compose
docker compose up polybot-paper
```

### Run Backtest

```bash
# CLI
polybot backtest --markets 20 --steps 100 --capital 10000

# Docker
docker compose run --rm polybot-backtest
```

### Run Tests

```bash
pytest tests/ -v
```

---

## Paper Mode vs Live Mode

| Feature | Paper Mode | Live Mode |
|---|---|---|
| **Default** | Yes | No |
| **Real transactions** | No (simulated) | Yes |
| **Requires private key** | No | Yes |
| **Requires passphrase** | No | Yes |
| **Requires env flag** | No | `ENABLE_LIVE_TRADING=true` |
| **First N trades** | Auto-executed | Manual approval required |
| **Circuit breakers** | Active | Active |

---

## Enabling Live Trading

**WARNING: Live trading uses real funds. Follow every step carefully.**

### Checklist

- [ ] Run backtest and review results
- [ ] Run paper mode for at least 48 hours and review logs
- [ ] Review and adjust `config/default.yaml` risk parameters
- [ ] Set up monitoring alerts (Slack webhook recommended)
- [ ] Secure your private key (use a secrets manager, never commit to git)
- [ ] Understand and accept all risks

### Steps

1. **Set environment variables:**
   ```bash
   export ENABLE_LIVE_TRADING=true
   export PRIVATE_KEY=<your-private-key>
   export WALLET_ADDRESS=<your-wallet-address>
   export POLYGON_RPC_URL=<your-rpc-endpoint>
   ```

2. **Update config:**
   ```yaml
   # config/default.yaml
   paper_mode: false
   first_live_trades_manual_review: 10  # First 10 trades need manual approval
   ```

3. **Run with live mode:**
   ```bash
   polybot run --no-paper
   ```

4. **Type the passphrase exactly when prompted:**
   ```
   I UNDERSTAND AND ENABLE LIVE TRADING
   ```

5. **Approve first 10 trades manually** (the bot will prompt you).

### Safety Gates (All Must Pass)

1. `ENABLE_LIVE_TRADING=true` environment variable
2. `paper_mode: false` in config
3. Runtime passphrase typed correctly
4. Valid `PRIVATE_KEY` configured
5. First N trades require manual approval

If **any** gate fails, no real transactions are broadcast.

---

## Risk Management Layers

| Layer | Name | Controls |
|---|---|---|
| **L0** | Hard Limits | Global exposure cap, per-market cap, per-trade max, max positions |
| **L1** | Position Sizing | Quarter-Kelly criterion, min liquidity, confidence scaling |
| **L2** | Execution Controls | Slippage tolerance, fill deviation check, order TTL |
| **L3** | Stop & Decay | Dynamic stop-loss, trailing stop, time-based exits |
| **L4** | Portfolio Hedging | Category concentration limits, correlation checks |
| **L5** | Circuit Breakers | Drawdown halt, consecutive loss halt, RPC failure halt, kill switch |
| **L6** | Compliance | Wash trading prevention, paper-first enforcement |

All layers are evaluated for every signal. Conservative defaults are set in `config/default.yaml`.

---

## Configuration

All parameters are in `config/default.yaml`. Key settings:

```yaml
# Safe defaults — start here
global_exposure_usd: 10000      # Total max exposure
per_market_exposure_usd: 1000   # Max per market
per_trade_max_usd: 500          # Max single trade
kelly_fraction: 0.25            # Quarter-Kelly (conservative)
slippage_tolerance_pct: 0.02    # 2% max slippage
max_drawdown_pct: 0.10          # Circuit breaker at 10% drawdown
```

Environment variables override secrets (never put secrets in YAML):
- `PRIVATE_KEY` — wallet signing key
- `WALLET_ADDRESS` — your Polygon wallet
- `POLYGON_RPC_URL` — RPC endpoint
- `ENABLE_LIVE_TRADING` — live trading gate
- `ALERT_WEBHOOK_URL` — Slack/webhook for alerts

---

## Copy-Trade Detection

The bot monitors on-chain activity for wallets that may be copying your trades:

- **Timing correlation**: Detects trades following yours within a configurable window
- **Pattern matching**: Same market, same side, proportional sizing
- **Defensive actions**: `reduce_size`, `randomize_timing`, or `flag_only`
- **Ethics**: Uses only public on-chain data; no deanonymization

Configure in `config/default.yaml`:
```yaml
copy_trade_detection:
  enabled: true
  time_window_seconds: 600
  similarity_threshold: 0.8
  response: "reduce_size"
```

---

## Backtesting

The backtest engine replays synthetic or historical data through the full pipeline:

```bash
polybot backtest --markets 50 --steps 200 --capital 25000
```

**Metrics produced:**
- Sharpe ratio, max drawdown, win rate
- Total P&L, average trade P&L
- Realized vs expected P&L ratio
- Monte Carlo stress test (1000 shuffled simulations)

---

## Security Checklist

- [ ] Private keys stored in environment variables or secrets manager
- [ ] `.env` file is in `.gitignore` (never committed)
- [ ] Structured logging never outputs private keys (sanitize processor)
- [ ] Live trading gated behind env flag + runtime passphrase
- [ ] First N live trades require manual approval
- [ ] Circuit breakers configured and tested
- [ ] Alert webhook configured for critical events
- [ ] Bot runs as non-root user in Docker
- [ ] Regular secret rotation (documented in `.env.example`)

---

## Project Structure

```
├── polymarket_bot/          # Main package
│   ├── scanner/             # Market data acquisition
│   ├── models/              # Probability estimation
│   ├── signals/             # Signal generation
│   ├── execution/           # Order execution
│   ├── risk/                # Risk management
│   ├── wallet_tracing/      # Copy-trade detection
│   ├── backtest/            # Backtesting engine
│   ├── monitoring/          # Logging and alerts
│   ├── cli/                 # Command-line interface
│   ├── config.py            # Configuration loader
│   └── types.py             # Shared domain types
├── tests/                   # Unit and integration tests
├── config/
│   └── default.yaml         # Default configuration
├── docs/
│   └── research_report.md   # Research on Polymarket bots
├── .github/workflows/
│   └── ci.yml               # CI pipeline
├── docker-compose.yml       # Docker setup
├── Dockerfile
├── pyproject.toml           # Python project config
├── run_example.py           # Example trade cycle script
├── .env.example             # Environment template
└── README.md
```

---

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest tests/ -v

# Type checking
mypy polymarket_bot/ --ignore-missing-imports

# Linting
ruff check polymarket_bot/ tests/

# Run example cycle
python run_example.py
```

---

## Research Report

See [docs/research_report.md](docs/research_report.md) for a comprehensive survey of:
- Top Polymarket bots and their strategies
- Infrastructure recommendations (API, WebSocket, rate limits)
- Copy-trade detection techniques
- Risk management best practices
- Conservative default rationale
