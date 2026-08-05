import os
import requests
from flask import Flask, render_template_string, request, jsonify
from datetime import datetime, timezone

from bot import run_bot

app = Flask(__name__)

SUPABASE_URL    = os.environ["SUPABASE_URL"]
SUPABASE_KEY    = os.environ["SUPABASE_KEY"]
TRIGGER_SECRET  = os.environ.get("TRIGGER_SECRET", "")

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}"
}

def db_get(table, params=""):
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{table}?{params}",
                     headers=HEADERS, timeout=10)
    return r.json() if r.ok else []

HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trading Bot Dashboard</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #0f172a; color: #e2e8f0; min-height: 100vh; }
  .header { background: #1e293b; padding: 20px 32px;
            border-bottom: 1px solid #334155;
            display: flex; align-items: center; gap: 12px; }
  .header h1 { font-size: 20px; font-weight: 700; color: #f8fafc; }
  .header .sub { font-size: 13px; color: #64748b; margin-left: auto; }
  .live { width: 8px; height: 8px; border-radius: 50%;
          background: #22c55e; box-shadow: 0 0 6px #22c55e; }
  .container { max-width: 1100px; margin: 0 auto; padding: 28px 20px; }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
           gap: 16px; margin-bottom: 28px; }
  .card { background: #1e293b; border: 1px solid #334155;
          border-radius: 12px; padding: 20px; }
  .card .label { font-size: 12px; color: #64748b; text-transform: uppercase;
                 letter-spacing: .5px; margin-bottom: 8px; }
  .card .value { font-size: 26px; font-weight: 700; color: #f8fafc; }
  .card .value.green { color: #22c55e; }
  .card .value.red   { color: #ef4444; }
  .card .value.blue  { color: #6366f1; }
  .section { background: #1e293b; border: 1px solid #334155;
             border-radius: 12px; padding: 24px; margin-bottom: 24px; }
  .section h2 { font-size: 15px; font-weight: 600; color: #94a3b8;
                text-transform: uppercase; letter-spacing: .5px; margin-bottom: 20px; }
  .chart-wrap { position: relative; height: 240px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; padding: 10px 14px; color: #64748b;
       border-bottom: 1px solid #334155; font-weight: 500; }
  td { padding: 10px 14px; border-bottom: 1px solid #1e293b; color: #cbd5e1; }
  tr:last-child td { border-bottom: none; }
  .badge { display: inline-block; padding: 2px 10px; border-radius: 999px;
           font-size: 11px; font-weight: 600; text-transform: uppercase; }
  .badge.buy      { background: #14532d; color: #4ade80; }
  .badge.sell     { background: #450a0a; color: #f87171; }
  .badge.stop-loss{ background: #431407; color: #fb923c; }
  .empty { color: #475569; text-align: center; padding: 32px;
           font-size: 14px; }
  .refresh { font-size: 12px; color: #475569; margin-top: 24px; text-align: center; }
</style>
</head>
<body>

<div class="header">
  <div class="live"></div>
  <h1>🤖 Trading Bot Dashboard</h1>
  <span class="sub">Paper trading · Updated {{ updated }}</span>
</div>

<div class="container">

  <!-- Stat cards -->
  <div class="cards">
    <div class="card">
      <div class="label">Portfolio Value</div>
      <div class="value blue">${{ portfolio_value }}</div>
    </div>
    <div class="card">
      <div class="label">Today's P&L</div>
      <div class="value {{ 'green' if daily_pnl_raw >= 0 else 'red' }}">${{ daily_pnl }}</div>
    </div>
    <div class="card">
      <div class="label">Cash Available</div>
      <div class="value">${{ cash }}</div>
    </div>
    <div class="card">
      <div class="label">Open Positions</div>
      <div class="value">{{ open_positions }} / 5</div>
    </div>
    <div class="card">
      <div class="label">Total Trades</div>
      <div class="value">{{ total_trades }}</div>
    </div>
  </div>

  <!-- Chart -->
  <div class="section">
    <h2>Portfolio Value Over Time</h2>
    <div class="chart-wrap">
      <canvas id="chart"></canvas>
    </div>
  </div>

  <!-- Trade log -->
  <div class="section">
    <h2>Recent Trades</h2>
    {% if trades %}
    <table>
      <thead>
        <tr>
          <th>Time (UTC)</th>
          <th>Stock</th>
          <th>Action</th>
          <th>Shares</th>
          <th>Price</th>
          <th>Value</th>
          <th>Score</th>
        </tr>
      </thead>
      <tbody>
        {% for t in trades %}
        <tr>
          <td>{{ t.time }}</td>
          <td><b>{{ t.symbol }}</b></td>
          <td><span class="badge {{ t.action }}">{{ t.action }}</span></td>
          <td>{{ t.quantity }}</td>
          <td>${{ t.price }}</td>
          <td>${{ t.value }}</td>
          <td>{{ t.score }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    {% else %}
    <div class="empty">No trades yet — bot will start trading when the market opens.</div>
    {% endif %}
  </div>

  <div class="refresh">Auto-refreshes every 5 minutes · Data from Supabase · Trades via Alpaca</div>

</div>

<script>
const labels = {{ chart_labels | safe }};
const values = {{ chart_values | safe }};
const ctx = document.getElementById('chart').getContext('2d');
new Chart(ctx, {
  type: 'line',
  data: {
    labels,
    datasets: [{
      label: 'Portfolio ($)',
      data: values,
      borderColor: '#6366f1',
      backgroundColor: 'rgba(99,102,241,0.1)',
      borderWidth: 2,
      pointRadius: 3,
      pointBackgroundColor: '#6366f1',
      fill: true,
      tension: 0.3
    }]
  },
  options: {
    responsive: true,
    maintainAspectRatio: false,
    plugins: { legend: { display: false } },
    scales: {
      x: { ticks: { color: '#64748b', maxTicksLimit: 10 },
           grid:  { color: '#1e293b' } },
      y: { ticks: { color: '#64748b', callback: v => '$' + v.toLocaleString() },
           grid:  { color: '#334155' } }
    }
  }
});
setTimeout(() => location.reload(), 300000); // refresh every 5 mins
</script>
</body>
</html>
"""

@app.route("/")
def index():
    # Portfolio snapshots for chart
    snaps = db_get("portfolio_snapshots",
                   "order=created_at.asc&limit=200&select=created_at,portfolio_value,cash,daily_pnl,open_positions")

    # Latest snapshot for cards
    latest = snaps[-1] if snaps else {}
    portfolio_value = f"{float(latest.get('portfolio_value', 0)):,.2f}" if latest else "—"
    cash_val        = f"{float(latest.get('cash', 0)):,.2f}"           if latest else "—"
    daily_pnl_raw   = float(latest.get('daily_pnl', 0))               if latest else 0
    daily_pnl       = f"{daily_pnl_raw:+,.2f}"
    open_positions  = latest.get('open_positions', 0)                  if latest else 0

    # Chart data
    chart_labels = [s["created_at"][11:16] for s in snaps] if snaps else []
    chart_values = [round(float(s["portfolio_value"]), 2) for s in snaps] if snaps else []

    # Recent trades
    raw_trades = db_get("trades", "order=created_at.desc&limit=30")

    total_trades = len(raw_trades)
    trades = []
    for t in raw_trades:
        # Score is stored in risk_level field as "medium|score:78"
        risk_raw = t.get("risk_level", "")
        score = "—"
        if "score:" in risk_raw:
            try:
                score = risk_raw.split("score:")[1].split("|")[0]
            except:
                score = "—"
        trades.append({
            "time":     t["created_at"][5:16].replace("T", " "),
            "symbol":   t.get("symbol", ""),
            "action":   t.get("action", ""),
            "quantity": t.get("quantity", 0),
            "price":    f"{float(t.get('price', 0)):.2f}",
            "value":    f"{float(t.get('value', 0)):,.2f}",
            "score":    score,
        })

    return render_template_string(HTML,
        portfolio_value=portfolio_value,
        daily_pnl=daily_pnl,
        daily_pnl_raw=daily_pnl_raw,
        cash=cash_val,
        open_positions=open_positions,
        total_trades=total_trades,
        trades=trades,
        chart_labels=str(chart_labels),
        chart_values=str(chart_values),
        updated=datetime.utcnow().strftime("%d %b %Y, %H:%M UTC")
    )


@app.route("/trigger")
def trigger():
    """
    Called by UptimeRobot every 5 minutes.
    Only runs the bot during market hours (Mon-Fri 13:30-20:00 UTC).
    Protected by TRIGGER_SECRET query param.
    """
    # Secret check — UptimeRobot passes X-Trigger-Secret header
    if TRIGGER_SECRET and request.headers.get("X-Trigger-Secret") != TRIGGER_SECRET:
        return jsonify({"status": "unauthorized"}), 401

    now = datetime.now(timezone.utc)
    is_weekday     = now.weekday() < 5
    market_open    = now.replace(hour=13, minute=30, second=0, microsecond=0)
    market_close   = now.replace(hour=20, minute=0,  second=0, microsecond=0)
    is_market_hours = market_open <= now <= market_close

    if not is_weekday or not is_market_hours:
        return jsonify({
            "status":  "skipped",
            "reason":  "outside market hours",
            "time_utc": now.strftime("%H:%M UTC"),
            "weekday": now.weekday()
        })

    try:
        run_bot()
        return jsonify({"status": "ok", "time_utc": now.strftime("%H:%M UTC")})
    except Exception as e:
        return jsonify({"status": "error", "reason": str(e)}), 500


@app.route("/health")
def health():
    """Simple health check so Render and UptimeRobot know the service is alive."""
    return jsonify({"status": "ok", "time_utc": datetime.utcnow().strftime("%H:%M UTC")})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
