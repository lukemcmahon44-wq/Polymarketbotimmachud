"""FastAPI + WebSocket live dashboard.

Runs on localhost:8000 (configurable via DASHBOARD_HOST / DASHBOARD_PORT).
Pushes JSON updates over WebSocket every 2 seconds to connected browsers.
"""

import asyncio
import json
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from arb_bot.utils.logger import get_logger

logger = get_logger(__name__)

app = FastAPI(title="Arb Bot Dashboard", docs_url=None, redoc_url=None)

# Injected by main.py at startup
_price_feed = None
_risk_manager = None


def init_dashboard(price_feed, risk_manager) -> None:
    global _price_feed, _risk_manager
    _price_feed = price_feed
    _risk_manager = risk_manager


# ---------------------------------------------------------------------------
# HTML UI (inline — no build step needed)
# ---------------------------------------------------------------------------

_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Arb Bot — Live Dashboard</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: 'Courier New', monospace; background: #0d1117; color: #c9d1d9; padding: 16px; }
    h1 { color: #58a6ff; margin-bottom: 12px; font-size: 1.3rem; }
    h2 { color: #8b949e; font-size: 0.9rem; margin: 16px 0 6px; text-transform: uppercase; letter-spacing: 1px; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 12px; }
    .card { background: #161b22; border: 1px solid #30363d; border-radius: 6px; padding: 14px; }
    .card .label { font-size: 0.75rem; color: #8b949e; }
    .card .value { font-size: 1.6rem; font-weight: bold; margin-top: 4px; }
    .green { color: #3fb950; }
    .red   { color: #f85149; }
    .yellow{ color: #d29922; }
    .killed{ background: #3d0000; border-color: #f85149; }
    table { width: 100%; border-collapse: collapse; font-size: 0.82rem; }
    th { text-align: left; padding: 6px 8px; background: #21262d; color: #8b949e; border-bottom: 1px solid #30363d; }
    td { padding: 5px 8px; border-bottom: 1px solid #21262d; }
    tr.opportunity { background: #0d2818; }
    .status { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; }
    .status.ok  { background: #3fb950; }
    .status.err { background: #f85149; }
    #ws-status { font-size: 0.75rem; color: #8b949e; float: right; }
  </style>
</head>
<body>
  <h1>Arb Bot <span id="ws-status">● connecting</span></h1>

  <div class="grid">
    <div class="card" id="card-pnl">
      <div class="label">Daily P&amp;L</div>
      <div class="value" id="daily-pnl">--</div>
    </div>
    <div class="card">
      <div class="label">Global Exposure</div>
      <div class="value" id="exposure">--</div>
    </div>
    <div class="card">
      <div class="label">Open Positions</div>
      <div class="value" id="positions">--</div>
    </div>
    <div class="card" id="card-kill">
      <div class="label">Kill Switch</div>
      <div class="value" id="kill-status">--</div>
    </div>
  </div>

  <h2>Active Opportunities</h2>
  <table id="opps-table">
    <thead><tr>
      <th>Market</th><th>Kalshi Leg</th><th>Poly Leg</th>
      <th>Gross</th><th>Net Edge</th><th>Size $</th><th>Risk</th>
    </tr></thead>
    <tbody id="opps-body"><tr><td colspan="7" style="color:#8b949e">waiting for data…</td></tr></tbody>
  </table>

  <h2>Recent Trades</h2>
  <table id="trades-table">
    <thead><tr>
      <th>Time</th><th>Market</th><th>Size $</th><th>Edge</th><th>Status</th>
    </tr></thead>
    <tbody id="trades-body"><tr><td colspan="5" style="color:#8b949e">no trades yet</td></tr></tbody>
  </table>

  <script>
    const wsProto = location.protocol === 'https:' ? 'wss' : 'ws';
    let ws;
    function connect() {
      ws = new WebSocket(`${wsProto}://${location.host}/ws`);
      ws.onopen  = () => { document.getElementById('ws-status').textContent = '● live'; };
      ws.onclose = () => { document.getElementById('ws-status').textContent = '● reconnecting'; setTimeout(connect, 3000); };
      ws.onmessage = (evt) => { render(JSON.parse(evt.data)); };
    }

    function render(d) {
      const risk = d.risk || {};
      // P&L card
      const pnl = (risk.daily_pnl ?? 0).toFixed(2);
      const pnlEl = document.getElementById('daily-pnl');
      pnlEl.textContent = `$${pnl}`;
      pnlEl.className = 'value ' + (risk.daily_pnl >= 0 ? 'green' : 'red');

      document.getElementById('exposure').textContent = `$${(risk.global_exposure ?? 0).toFixed(2)}`;
      document.getElementById('positions').textContent = Object.keys(risk.positions || {}).length;

      const killEl  = document.getElementById('kill-status');
      const killCard = document.getElementById('card-kill');
      killEl.textContent = risk.killed ? 'ACTIVE' : 'OK';
      killEl.className   = 'value ' + (risk.killed ? 'red' : 'green');
      killCard.className  = 'card' + (risk.killed ? ' killed' : '');

      // Opportunities
      const opps = d.opportunities || [];
      const tbody = document.getElementById('opps-body');
      if (opps.length === 0) {
        tbody.innerHTML = '<tr><td colspan="7" style="color:#8b949e">no opportunities above threshold</td></tr>';
      } else {
        tbody.innerHTML = opps.map(o => `
          <tr class="opportunity">
            <td>${o.kalshi_title.slice(0,40)}</td>
            <td>${o.kalshi_leg} @ ${o.kalshi_price.toFixed(3)}</td>
            <td>${o.poly_leg} @ ${o.poly_price.toFixed(3)}</td>
            <td>${(o.gross*100).toFixed(2)}%</td>
            <td class="green">${(o.net_edge*100).toFixed(2)}%</td>
            <td>$${o.size.toFixed(0)}</td>
            <td class="${o.risk==='HIGH'?'red':o.risk==='MEDIUM'?'yellow':'green'}">${o.risk}</td>
          </tr>`).join('');
      }

      // Trades
      const trades = d.trades || [];
      const tbody2 = document.getElementById('trades-body');
      if (trades.length === 0) {
        tbody2.innerHTML = '<tr><td colspan="5" style="color:#8b949e">no trades yet</td></tr>';
      } else {
        tbody2.innerHTML = trades.slice(-20).reverse().map(t => `
          <tr>
            <td>${t.time}</td>
            <td>${t.market.slice(0,35)}</td>
            <td>$${t.size.toFixed(0)}</td>
            <td>${(t.net_edge*100).toFixed(2)}%</td>
            <td class="${t.success?'green':'red'}">${t.success?(t.dry_run?'DRY RUN':'FILLED'):'PARTIAL'}</td>
          </tr>`).join('');
      }
    }
    connect();
  </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return _HTML


@app.get("/health")
async def health():
    return {"status": "ok", "ts": datetime.utcnow().isoformat()}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    logger.info("Dashboard WebSocket client connected")
    try:
        while True:
            payload = _build_payload()
            await ws.send_text(json.dumps(payload))
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        logger.info("Dashboard WebSocket client disconnected")
    except Exception as exc:
        logger.warning(f"Dashboard WS error: {exc}")


def _build_payload() -> dict:
    from arb_bot.core.executor import get_trade_log

    risk_state = {}
    if _risk_manager is not None:
        s = _risk_manager.state
        risk_state = {
            "daily_pnl": s.daily_pnl,
            "global_exposure": s.global_exposure,
            "positions": s.positions,
            "partial_fill_count": s.partial_fill_count,
            "killed": s.killed,
            "kill_reason": s.kill_reason,
        }

    # Latest scanned opportunities (injected by main loop)
    opps = []
    for o in _latest_opportunities:
        opps.append({
            "kalshi_title": o.pair.kalshi_title,
            "kalshi_leg": o.kalshi_leg,
            "kalshi_price": o.kalshi_price,
            "poly_leg": o.polymarket_leg,
            "poly_price": o.polymarket_price,
            "gross": o.gross_spread,
            "net_edge": o.net_edge,
            "size": o.max_size_usdc,
            "risk": o.pair.resolution_risk,
        })

    trades_data = []
    for tr in get_trade_log():
        trades_data.append({
            "time": tr.executed_at.strftime("%H:%M:%S"),
            "market": tr.opportunity.pair.kalshi_title,
            "size": tr.size_usdc,
            "net_edge": tr.opportunity.net_edge,
            "success": tr.success,
            "dry_run": tr.kalshi_order is None and tr.success,
        })

    return {
        "risk": risk_state,
        "opportunities": opps,
        "trades": trades_data,
    }


# Latest opportunities set by main loop
_latest_opportunities: list = []


def update_opportunities(opps: list) -> None:
    global _latest_opportunities
    _latest_opportunities = opps


async def start_dashboard(price_feed, risk_manager, host: str, port: int) -> None:
    import uvicorn
    init_dashboard(price_feed, risk_manager)
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()
