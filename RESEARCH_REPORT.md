# Polymarket Trading Bot Research Report
## Top Strategies, Infrastructure, and Recommended Defaults

*Prepared for the Polymarket autonomous trading bot project — April 2026*

---

## 1. Executive Summary

Prediction markets like Polymarket have become increasingly dominated by algorithmic trading.
As of early 2026, an estimated **14 of the top 20 profitable wallets** on Polymarket's public
leaderboard are bots. The average arbitrage opportunity window has collapsed to **2.7 seconds**
(down from 12.3 seconds in 2024), with 73% of arbitrage profits captured by sub-100ms
execution bots. For new entrants, this means pure latency arbitrage is nearly impossible
without institutional-grade infrastructure; the edge now lies in **model quality** and
**multi-strategy adaptation**.

---

## 2. Top Strategies Used by Profitable Polymarket Bots

### 2.1 Complement/MEE Arbitrage (Pure Risk-Free)

The most reliable strategy when available. For binary markets:

```
If YES_ask + NO_ask < $1.00 → Buy both sides → lock profit = $1.00 - combined_cost
```

**Reality check**: Polymarket's taker fee (~2%) means you need at least a **2% underround**
to profit. These opportunities are rare and fleeting — typically lasting 1–5 seconds when
the CLOB temporarily mis-prices during high-volume periods. Top arb bots maintain <50ms
order placement latency via co-location with Polymarket's Cloudflare-fronted CLOB on AWS
us-east-1.

**Implementation**: Our `ArbitrageDetector` implements this. Conservative sizing to stay
under 5% of book depth avoids depleting the opportunity before fills complete.

### 2.2 Latency Arbitrage vs. External Signals

The most profitable strategy in 2025–2026. Methodology:
1. Subscribe to Binance/Coinbase WebSocket for BTC/ETH/SOL real-time price feeds
2. Compare to Polymarket's implied probability for "BTC > $X by date" markets
3. When crypto price moves significantly, Polymarket prices lag by 2–15 seconds
4. Buy underpriced side before market makers update their quotes

One documented bot turned **$313 into $414,000 in a single month** using this approach
exclusively on BTC/ETH/SOL 15-minute up/down markets with a reported 98% win rate.
(Source: Medium/@aulegabriel381, Feb 2026)

**Note**: This requires crypto exchange API integration not included in this scaffold —
it is the highest-alpha extension to add for live trading.

### 2.3 Ensemble Model-Based Mispricing

The strategy implemented in our `EVCalculator` and `EnsembleProbabilityModel`:

1. **Market-implied baseline**: Use current price as anchor
2. **Complement constraint**: Normalize YES+NO=1.0 to detect directional bias
3. **Volume/liquidity signal**: Low V/L ratio → stale pricing → more opportunity

An AI-powered bot reportedly generated **$2.2 million in two months** using ensemble
probability models trained on news sentiment + social data to identify undervalued contracts,
with continuous retraining. (Source: Medium/@stevenn.hansen, Feb 2026)

**Recommended extensions**:
- Add news sentiment scoring (NewsAPI, GDELT)
- Add prediction aggregation (Metaculus, Manifold Markets)
- Add calibrated forecaster consensus
- Fine-tune on Polymarket historical resolution data

### 2.4 Market Making

Market makers provide continuous two-sided quotes, earning the bid-ask spread:

```
Quote bid = market_mid - half_spread
Quote ask = market_mid + half_spread
```

**Performance**: 78–85% win rate with 1–3% monthly returns and low volatility.
The risk is **inventory accumulation** — if prices move against you before you
can hedge, losses accumulate.

**Key heuristics from top market makers**:
- Max single-position duration: 24 hours
- Spread must exceed 2× fee rate to be profitable
- Skew quotes based on current inventory (less aggressive on the side you're long)
- Use time-weighted average price (TWAP) to exit positions gracefully

Market making is **not** implemented in this scaffold (it requires a separate quoting
loop and inventory management system), but the risk manager and execution layer
support it as an extension.

### 2.5 Copy Trading

Monitoring profitable wallets' public on-chain activity and replicating their trades:

- Simple implementation: watch Polygon for OrderFilled events from high-performing addresses
- Delay: typical copy-trade latency is 0.5–3 seconds (same block or next block)
- Arms race: top traders now use 3+ wallets specifically to hide primary strategy

**Limitations**:
1. Profitable wallets become targets; performance degrades once widely copied
2. Timing risk — you may buy into a position the original trader is about to exit
3. Survivorship bias — you only see current wallets, not all historical attempts

Our `CopyTradeDetector` implements **defensive** copy-trade monitoring (detecting when
WE are being copied) rather than offensive copying.

---

## 3. Infrastructure Recommendations

### 3.1 API Architecture

| Component | Recommendation | Rationale |
|-----------|---------------|-----------|
| Market data | WebSocket primary, REST fallback | 5–10× lower latency than polling |
| Order placement | REST POST /order | No WS order API available |
| Market discovery | Gamma REST API | Best structured market metadata |
| On-chain monitoring | web3.py with HTTP provider | Reliable; WS unstable on public nodes |

**WebSocket endpoint**: `wss://ws-subscriptions-clob.polymarket.com/ws/market`
Subscribing to `market` channel gives real-time price changes and book updates.
Limit: 5 concurrent connections per IP.

### 3.2 Rate Limits (Polymarket CLOB, as of 2026)

| Endpoint | Burst | Sustained |
|----------|-------|-----------|
| POST /order | 3,500/10s | 60/s avg |
| DELETE /order | 3,000/10s | 50/s avg |
| GET /book, /price | 1,500/10s | 25/s avg |
| WebSocket connections | 5/IP | — |

**Recommended bot limits** (conservative, leaves headroom):
- Orders: 50/10s (1.4% of burst limit)
- Data requests: 100/10s (6.7% of burst limit)
- WS connections: 3/IP

Use **exponential backoff** starting at 1s, capping at 60s, on HTTP 429 responses.

### 3.3 Latency Optimization

- **Server co-location**: Polymarket infrastructure is on AWS us-east-1. Using a VPS
  in Virginia reduces round-trip latency by 30–80ms vs. other US regions.
  (Source: newyorkcityservers.com, 2026)
- **Connection reuse**: Keep CLOB HTTP connections alive (httpx AsyncClient with keep-alive)
- **Pre-signing**: Pre-compute EIP-712 signatures where possible
- **Async everything**: Never block the event loop during I/O

### 3.4 Polygon RPC Provider Recommendations

| Provider | Notes |
|----------|-------|
| Chainstack | Lowest latency dedicated nodes, paid |
| Infura | Reliable, rate-limited on free tier |
| Alchemy | Good free tier, WebSocket support |
| Public (polygon-rpc.com) | Free, ~100ms additional latency, rate-limited |

For live trading, use a **dedicated Chainstack or Alchemy node** to ensure consistent
<50ms RPC response times.

---

## 4. Copy-Trade Defense Strategies

Top wallets use several documented defensive approaches:

### 4.1 Multi-Wallet Architecture
Split capital across 3–5 wallets:
- Primary alpha wallet (small trades, never publicized)
- Execution wallets (larger size, copy-traded by bots)
- Decoy wallets (intentional noise trades to confuse copiers)

### 4.2 Randomized Timing
Add uniform random delay (0–30 seconds) to order placement after signal generation.
This disrupts timing-correlation copy-trade detectors.

**Our implementation**: `CopyTradeResponse.RANDOMIZE_TIMING` — when a copy trader is
detected, we add delay to our subsequent orders.

### 4.3 Size Obfuscation
Break large orders into multiple smaller tranches (TWAP execution).
Each tranche looks like an independent small trade.

**Our implementation**: `CopyTradeResponse.REDUCE_SIZE` — reduce order size when
a copy trader is active to minimize the value we leak to copiers.

### 4.4 Order Type Diversification
Mix limit orders (visible on book) with FOK market orders (not visible until filled).
Limit orders are observable before execution; market orders are opaque.

---

## 5. Key Risk Management Findings

### 5.1 Position Sizing
- **Full Kelly** is universally agreed to be too aggressive for prediction markets
  due to fat-tailed resolution distributions
- **Quarter-Kelly** (25% of full Kelly) is the standard conservative choice:
  captures ~75% of optimal growth with ~50% less drawdown
- Hard cap at 2–5% of portfolio per trade, regardless of Kelly output

### 5.2 Market Selection
Avoid markets with:
- Less than $5,000 liquidity (fill risk, wide spreads)
- Less than 1 hour to resolution (pure speculation, no model value)
- Extreme prices (>95% or <5%): overfit to sentiment, low alpha
- Very high volume/liquidity ratio (>5:1): prices already well-discovered

### 5.3 Category Diversification
Never exceed 40% exposure in a single category (crypto, politics, sports, etc.).
Correlated markets (e.g., all crypto outcome markets) can all resolve badly together.

### 5.4 Drawdown Control
Empirical finding: bots that enforce a 10% portfolio drawdown circuit breaker
recover faster and outperform over 12+ months versus bots without a breaker,
due to psychological and capital preservation benefits.

---

## 6. Recommended Defaults Summary

Based on the above research, the following defaults are encoded in `config/default.yaml`:

```yaml
ev_threshold: 0.02          # Minimum 2% EV after fees
kelly_fraction: 0.25        # Quarter-Kelly
min_liquidity_usd: 5000     # Skip thin markets
per_trade_max_usd: 500      # Hard size cap
global_exposure_usd: 10000  # Total portfolio cap
drawdown_circuit_breaker: 10%
stop_loss_pct: 15%
max_daily_loss_usd: 2000
slippage_tolerance: 2%
```

These are deliberately conservative. The bot should prove itself in paper mode
for at least 30 days before considering live deployment.

---

## 7. Sources

- Polymarket official docs: https://docs.polymarket.com
- py-clob-client GitHub: https://github.com/Polymarket/py-clob-client
- Polymarket Agents: https://github.com/Polymarket/agents
- CTF Exchange contracts: https://docs.polymarket.com/resources/contract-addresses
- "Beyond Simple Arbitrage" (ILLUMINATION/Medium, 2026)
- "Building a Polymarket BTC 15-Minute Trading Bot" (@aulegabriel381, Medium, Feb 2026)
- "Best Polymarket Trading Bots 2026" (@stevenn.hansen, Medium, Feb 2026)
- "Prediction Markets Are Turning Into a Bot Playground" (Finance Magnates)
- "Arbitrage Bots Dominate Polymarket With Millions in Profits" (Yahoo Finance)
- Polymarket Rate Limits Guide (agentbets.ai, March 2026)
- Polymarket Server Locations & Latency (newyorkcityservers.com)
- "Decoding Polymarket On-Chain Order Data" (yzc.me)
- Kelly Criterion Wikipedia: https://en.wikipedia.org/wiki/Kelly_criterion
- NautilusTrader Polymarket integration: https://nautilustrader.io/docs/latest/integrations/polymarket/

---

## 8. Legal Disclaimer

*This research is provided for educational and informational purposes only. Nothing in
this report constitutes financial advice. Trading on prediction markets involves
substantial financial risk. The operator is solely responsible for compliance with all
applicable local laws, regulations, and Polymarket's Terms of Service. Polymarket
restricts access from certain jurisdictions — verify your eligibility before use.
Past performance of any described strategy does not guarantee future results.*
