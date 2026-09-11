"""A minimal, dependency-free live dashboard for watching memecoin_bot trade.

This is a **read-only viewer**: it never places trades, never writes to the
portfolio, and has no effect on the bot's own decisions. It reads the same
SQLite database the bot writes to (see storage.py) to show the equity
curve and trade history, and additionally does a light, public,
unauthenticated DexScreener price lookup for currently open positions so
the chart can show live mark-to-market movement between the bot's own
scan cycles.

Usage:
    python -m memecoin_bot.dashboard --port 8765

Then open http://127.0.0.1:8765 in a browser. The page polls
`/api/state` every few seconds and redraws itself; no external JS
libraries or CDNs are used, so it works fully offline aside from the
optional live-price lookups.
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

from memecoin_bot.bot import pick_best_pair_per_token
from memecoin_bot.config import Settings
from memecoin_bot.data.dexscreener import DexScreenerClient
from memecoin_bot.storage import Storage

logger = logging.getLogger(__name__)


def _fetch_live_prices(
    dex_client: DexScreenerClient, addresses: List[str], chain_id: str
) -> Dict[str, float]:
    """Best-effort live price lookup for currently open positions.

    Never raises: a network hiccup here must not break the dashboard, it
    should just fall back to showing entry prices for that poll.
    """
    if not addresses:
        return {}
    try:
        pairs = dex_client.get_pairs_for_tokens(addresses, chain_id=chain_id)
        best = pick_best_pair_per_token(pairs)
        return {p.base_token_address: p.price_usd for p in best}
    except Exception:  # noqa: BLE001 - dashboard must never crash on a bad poll
        logger.warning("Live price lookup failed for dashboard poll", exc_info=True)
        return {}


def _augment_position(row: Dict[str, Any], current_price: Optional[float]) -> Dict[str, Any]:
    row = dict(row)
    if current_price is not None:
        market_value = row["quantity"] * current_price
        unrealized_pnl_usd = market_value - row["size_usd"]
        unrealized_pnl_pct = (unrealized_pnl_usd / row["size_usd"] * 100.0) if row["size_usd"] else 0.0
        row["current_price"] = current_price
        row["market_value_usd"] = market_value
        row["unrealized_pnl_usd"] = unrealized_pnl_usd
        row["unrealized_pnl_pct"] = unrealized_pnl_pct
    else:
        row["current_price"] = None
        row["market_value_usd"] = None
        row["unrealized_pnl_usd"] = None
        row["unrealized_pnl_pct"] = None
    return row


class DashboardHandler(BaseHTTPRequestHandler):
    # Set on a per-server subclass via make_server(); shared across requests
    # on that server instance (ThreadingHTTPServer runs one handler per
    # request but the class attributes persist).
    settings: Settings
    dex_client: DexScreenerClient

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        pass  # keep console output quiet; the bot's own logs matter more

    def _send_json(self, payload: Dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        if self.path.startswith("/api/state"):
            self._handle_state()
        elif self.path in ("/", "/index.html"):
            self._send_html(INDEX_HTML)
        else:
            self.send_response(404)
            self.end_headers()

    def _handle_state(self) -> None:
        try:
            storage = Storage(self.settings.db_path)
            try:
                equity_curve = storage.get_equity_curve(limit=5000)
                open_trades = storage.get_open_trades()
                recent_trades = storage.get_recent_trades(limit=25, status="closed")
                stats = storage.get_summary_stats()
            finally:
                storage.close()

            addresses = [row["token_address"] for row in open_trades]
            live_prices = _fetch_live_prices(self.dex_client, addresses, self.settings.chain_id)
            augmented_positions = [
                _augment_position(row, live_prices.get(row["token_address"]))
                for row in open_trades
            ]

            cash_usd = stats["latest_equity"]["cash_usd"] if stats["latest_equity"] else self.settings.starting_balance_usd
            live_market_value = sum(
                p["market_value_usd"] for p in augmented_positions if p["market_value_usd"] is not None
            )
            live_unpriced_cost_basis = sum(
                p["size_usd"] for p in augmented_positions if p["market_value_usd"] is None
            )
            live_equity_usd = cash_usd + live_market_value + live_unpriced_cost_basis

            payload = {
                "equity_curve": equity_curve,
                "open_positions": augmented_positions,
                "recent_trades": recent_trades,
                "stats": stats,
                "starting_balance_usd": self.settings.starting_balance_usd,
                "live": {
                    "timestamp": time.time(),
                    "equity_usd": live_equity_usd,
                    "cash_usd": cash_usd,
                    "priced_positions": sum(1 for p in augmented_positions if p["current_price"] is not None),
                    "total_positions": len(augmented_positions),
                },
            }
            self._send_json(payload)
        except Exception as exc:  # noqa: BLE001 - dashboard must never crash on a bad poll
            logger.warning("Dashboard state request failed", exc_info=True)
            self._send_json({"error": str(exc)}, status=500)


def make_server(settings: Settings, port: int) -> ThreadingHTTPServer:
    dex_client = DexScreenerClient(
        base_url=settings.dexscreener_base_url, timeout=settings.http_timeout_seconds
    )
    handler_cls = type(
        "BoundDashboardHandler",
        (DashboardHandler,),
        {"settings": settings, "dex_client": dex_client},
    )
    return ThreadingHTTPServer(("127.0.0.1", port), handler_cls)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only live dashboard for memecoin_bot paper trading (equity chart + positions)."
    )
    parser.add_argument("--port", type=int, default=8765, help="Port to serve the dashboard on (default: 8765).")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()

    port = args.port
    server: Optional[ThreadingHTTPServer] = None
    for attempt in range(10):
        try:
            server = make_server(settings, port)
            break
        except OSError:
            logger.warning("Port %d in use, trying %d", port, port + 1)
            port += 1
    if server is None:
        raise RuntimeError("Could not find an available port for the dashboard server")

    print(f"memecoin_bot dashboard: http://127.0.0.1:{port}  (db={settings.db_path})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>memecoin_bot — live paper trading</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 24px; background: #0b0e14; color: #e6e6e6;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  }
  h1 { font-size: 18px; font-weight: 600; margin: 0 0 4px 0; }
  .banner {
    background: #1b2430; border: 1px solid #2c3a4d; border-radius: 8px;
    padding: 8px 14px; font-size: 13px; color: #9fd3ff; margin-bottom: 18px;
  }
  .stats-row { display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 18px; }
  .stat-card {
    background: #131926; border: 1px solid #232d3d; border-radius: 10px;
    padding: 12px 16px; min-width: 130px; flex: 1;
  }
  .stat-label { font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; color: #7d8ba1; margin-bottom: 4px; }
  .stat-value { font-size: 20px; font-weight: 600; }
  .pos { color: #4ade80; }
  .neg { color: #f87171; }
  .neutral { color: #e6e6e6; }
  #chart-wrap {
    background: #101623; border: 1px solid #232d3d; border-radius: 10px;
    padding: 16px; margin-bottom: 20px;
  }
  canvas { width: 100%; height: 320px; display: block; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid #1f2937; white-space: nowrap; }
  th { color: #7d8ba1; font-weight: 500; font-size: 11px; text-transform: uppercase; }
  .section { background: #101623; border: 1px solid #232d3d; border-radius: 10px; padding: 16px; margin-bottom: 20px; overflow-x: auto; }
  .section h2 { font-size: 14px; margin: 0 0 10px 0; color: #c7d2e0; }
  .empty { color: #5b6779; font-size: 13px; padding: 8px 0; }
  .badge { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 11px; font-weight: 600; }
  .badge.win { background: #14532d; color: #86efac; }
  .badge.loss { background: #5b1a1a; color: #fca5a5; }
  #updated { font-size: 12px; color: #5b6779; margin-top: 4px; }
</style>
</head>
<body>
  <h1>memecoin_bot &mdash; live paper trading</h1>
  <div class="banner">📝 Simulated trades only &mdash; paper portfolio, not financial advice. No real funds are ever at risk.</div>

  <div class="stats-row" id="stats-row"></div>

  <div id="chart-wrap">
    <canvas id="chart" width="1000" height="320"></canvas>
  </div>

  <div class="section">
    <h2>Open positions</h2>
    <div id="positions"></div>
  </div>

  <div class="section">
    <h2>Recent closed trades</h2>
    <div id="trades"></div>
  </div>

  <div id="updated">connecting...</div>

<script>
function fmtUsd(v) {
  if (v === null || v === undefined) return '—';
  const sign = v < 0 ? '-' : '';
  return sign + '$' + Math.abs(v).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
}
function fmtPct(v) {
  if (v === null || v === undefined) return '—';
  const sign = v >= 0 ? '+' : '';
  return sign + v.toFixed(1) + '%';
}
function fmtPrice(v) {
  if (v === null || v === undefined) return '—';
  return v < 0.01 ? '$' + v.toFixed(8) : '$' + v.toFixed(4);
}
function colorClass(v) {
  if (v === null || v === undefined) return 'neutral';
  return v > 0 ? 'pos' : (v < 0 ? 'neg' : 'neutral');
}
function fmtTime(ts) {
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString();
}

function renderStats(state) {
  const stats = state.stats;
  const live = state.live;
  const startingBalance = state.starting_balance_usd;
  const totalPnl = live.equity_usd - startingBalance;
  const totalPnlPct = startingBalance ? (totalPnl / startingBalance * 100.0) : 0;

  const cards = [
    ['Live equity', fmtUsd(live.equity_usd), colorClass(totalPnl)],
    ['Total P&L', fmtUsd(totalPnl) + ' (' + fmtPct(totalPnlPct) + ')', colorClass(totalPnl)],
    ['Cash', fmtUsd(live.cash_usd), 'neutral'],
    ['Open positions', live.total_positions + ' (' + live.priced_positions + ' live-priced)', 'neutral'],
    ['Realized P&L', fmtUsd(stats.total_realized_pnl_usd), colorClass(stats.total_realized_pnl_usd)],
    ['Closed trades', stats.total_trades + ' (win rate ' + stats.win_rate_pct.toFixed(1) + '%)', 'neutral'],
  ];
  document.getElementById('stats-row').innerHTML = cards.map(function(c) {
    return '<div class="stat-card"><div class="stat-label">' + c[0] + '</div>' +
           '<div class="stat-value ' + c[2] + '">' + c[1] + '</div></div>';
  }).join('');
}

function renderPositions(positions) {
  const el = document.getElementById('positions');
  if (!positions.length) { el.innerHTML = '<div class="empty">No open positions right now.</div>'; return; }
  let html = '<table><thead><tr>' +
    '<th>Symbol</th><th>Entry</th><th>Current</th><th>Size</th><th>Unrealized P&L</th><th>Stop / Take</th><th>Opened</th>' +
    '</tr></thead><tbody>';
  positions.forEach(function(p) {
    html += '<tr>' +
      '<td>' + p.symbol + '</td>' +
      '<td>' + fmtPrice(p.entry_price) + '</td>' +
      '<td>' + fmtPrice(p.current_price) + '</td>' +
      '<td>' + fmtUsd(p.size_usd) + '</td>' +
      '<td class="' + colorClass(p.unrealized_pnl_usd) + '">' + fmtUsd(p.unrealized_pnl_usd) + ' (' + fmtPct(p.unrealized_pnl_pct) + ')</td>' +
      '<td>' + fmtPrice(p.stop_loss_price) + ' / ' + fmtPrice(p.take_profit_price) + '</td>' +
      '<td>' + fmtTime(p.opened_at) + '</td>' +
      '</tr>';
  });
  html += '</tbody></table>';
  el.innerHTML = html;
}

function renderTrades(trades) {
  const el = document.getElementById('trades');
  if (!trades.length) { el.innerHTML = '<div class="empty">No closed trades yet.</div>'; return; }
  let html = '<table><thead><tr>' +
    '<th>Result</th><th>Symbol</th><th>Entry</th><th>Exit</th><th>P&L</th><th>Reason</th><th>Closed</th>' +
    '</tr></thead><tbody>';
  trades.forEach(function(t) {
    const win = t.realized_pnl_usd > 0;
    html += '<tr>' +
      '<td><span class="badge ' + (win ? 'win' : 'loss') + '">' + (win ? 'WIN' : 'LOSS') + '</span></td>' +
      '<td>' + t.symbol + '</td>' +
      '<td>' + fmtPrice(t.entry_price) + '</td>' +
      '<td>' + fmtPrice(t.exit_price) + '</td>' +
      '<td class="' + colorClass(t.realized_pnl_usd) + '">' + fmtUsd(t.realized_pnl_usd) + ' (' + fmtPct(t.realized_pnl_pct) + ')</td>' +
      '<td>' + t.exit_reason + '</td>' +
      '<td>' + fmtTime(t.closed_at) + '</td>' +
      '</tr>';
  });
  html += '</tbody></table>';
  el.innerHTML = html;
}

function drawChart(equityCurve, startingBalance, livePoint) {
  const canvas = document.getElementById('chart');
  const dpr = window.devicePixelRatio || 1;
  const cssWidth = canvas.clientWidth || 1000;
  const cssHeight = 320;
  canvas.width = cssWidth * dpr;
  canvas.height = cssHeight * dpr;
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssWidth, cssHeight);

  const points = equityCurve.map(function(p) { return {t: p.timestamp, v: p.equity_usd}; });
  if (livePoint) points.push({t: livePoint.timestamp, v: livePoint.equity_usd, live: true});
  if (points.length < 2) {
    ctx.fillStyle = '#5b6779';
    ctx.font = '13px sans-serif';
    ctx.fillText('Waiting for more equity history...', 12, cssHeight / 2);
    return;
  }

  const padding = {top: 16, right: 16, bottom: 24, left: 70};
  const plotW = cssWidth - padding.left - padding.right;
  const plotH = cssHeight - padding.top - padding.bottom;

  const values = points.map(function(p) { return p.v; }).concat([startingBalance]);
  let minV = Math.min.apply(null, values);
  let maxV = Math.max.apply(null, values);
  const span = (maxV - minV) || 1;
  minV -= span * 0.08;
  maxV += span * 0.08;

  const minT = points[0].t;
  const maxT = points[points.length - 1].t;
  const tSpan = (maxT - minT) || 1;

  function xFor(t) { return padding.left + (t - minT) / tSpan * plotW; }
  function yFor(v) { return padding.top + (1 - (v - minV) / (maxV - minV)) * plotH; }

  // Gridlines + labels
  ctx.strokeStyle = '#1f2937';
  ctx.fillStyle = '#5b6779';
  ctx.font = '11px sans-serif';
  ctx.lineWidth = 1;
  const gridLines = 4;
  for (let i = 0; i <= gridLines; i++) {
    const v = minV + (maxV - minV) * (i / gridLines);
    const y = yFor(v);
    ctx.beginPath();
    ctx.moveTo(padding.left, y);
    ctx.lineTo(cssWidth - padding.right, y);
    ctx.stroke();
    ctx.fillText('$' + v.toFixed(0), 4, y + 4);
  }

  // Starting balance reference line
  const startY = yFor(startingBalance);
  ctx.setLineDash([4, 4]);
  ctx.strokeStyle = '#3b4a63';
  ctx.beginPath();
  ctx.moveTo(padding.left, startY);
  ctx.lineTo(cssWidth - padding.right, startY);
  ctx.stroke();
  ctx.setLineDash([]);

  const lastValue = points[points.length - 1].v;
  const lineColor = lastValue >= startingBalance ? '#4ade80' : '#f87171';
  const fillColor = lastValue >= startingBalance ? 'rgba(74, 222, 128, 0.12)' : 'rgba(248, 113, 113, 0.12)';

  // Area fill
  ctx.beginPath();
  ctx.moveTo(xFor(points[0].t), yFor(points[0].v));
  points.forEach(function(p) { ctx.lineTo(xFor(p.t), yFor(p.v)); });
  ctx.lineTo(xFor(points[points.length - 1].t), padding.top + plotH);
  ctx.lineTo(xFor(points[0].t), padding.top + plotH);
  ctx.closePath();
  ctx.fillStyle = fillColor;
  ctx.fill();

  // Line
  ctx.beginPath();
  points.forEach(function(p, i) {
    const x = xFor(p.t), y = yFor(p.v);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.strokeStyle = lineColor;
  ctx.lineWidth = 2;
  ctx.stroke();

  // Live point marker (pulsing-ish dot at the tip)
  const last = points[points.length - 1];
  ctx.beginPath();
  ctx.arc(xFor(last.t), yFor(last.v), 4, 0, Math.PI * 2);
  ctx.fillStyle = lineColor;
  ctx.fill();
  ctx.strokeStyle = '#0b0e14';
  ctx.lineWidth = 1.5;
  ctx.stroke();
}

async function poll() {
  try {
    const res = await fetch('/api/state', {cache: 'no-store'});
    const state = await res.json();
    if (state.error) { throw new Error(state.error); }
    renderStats(state);
    renderPositions(state.open_positions);
    renderTrades(state.recent_trades);
    drawChart(state.equity_curve, state.starting_balance_usd, state.live);
    document.getElementById('updated').textContent = 'Last updated: ' + new Date().toLocaleTimeString();
  } catch (err) {
    document.getElementById('updated').textContent = 'Connection error: ' + err.message;
  }
}

poll();
setInterval(poll, 5000);
window.addEventListener('resize', poll);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
