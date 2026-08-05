import os
import time
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timezone
import alpaca_trade_api as tradeapi

# ── CONFIG ────────────────────────────────────────────────────────────────────
ALPACA_KEY       = os.environ["ALPACA_KEY"]
ALPACA_SECRET    = os.environ["ALPACA_SECRET"]
ALPACA_URL       = "https://paper-api.alpaca.markets"
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
SUPABASE_URL     = os.environ["SUPABASE_URL"]
SUPABASE_KEY     = os.environ["SUPABASE_KEY"]

RISK_LEVEL       = os.environ.get("RISK_LEVEL", "medium")
MAX_POSITIONS    = 5        # Increased from 3 — more opportunities
STOP_LOSS_PCT    = 0.015    # Tighter stop: 1.5%
TAKE_PROFIT_PCT  = 0.03     # Take profit at 3%

# Score thresholds
STRONG_BUY_SCORE = 80       # Large position
BUY_SCORE        = 65       # Standard position
SELL_SCORE       = 40       # Exit position

# Position sizing by score (% of portfolio)
POSITION_SIZES = {
    "strong": {"low": 0.02, "medium": 0.04, "high": 0.06},
    "normal": {"low": 0.01, "medium": 0.02, "high": 0.03},
}

# Expanded watchlist — high volume, momentum-driven stocks
WATCHLIST = [
    "AAPL", "MSFT", "NVDA", "META", "GOOGL",
    "AMZN", "TSLA", "SPY",  "QQQ",  "AMD",
]

api = tradeapi.REST(ALPACA_KEY, ALPACA_SECRET, ALPACA_URL, api_version="v2")


# ── HELPERS ───────────────────────────────────────────────────────────────────

def send_telegram(message: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")


def log_trade(symbol, action, quantity, price, value, score, rsi_5m, rsi_15m, vwap_dev):
    try:
        requests.post(
            f"{SUPABASE_URL}/rest/v1/trades",
            headers={"apikey": SUPABASE_KEY,
                     "Authorization": f"Bearer {SUPABASE_KEY}",
                     "Content-Type": "application/json",
                     "Prefer": "return=minimal"},
            json={"symbol": symbol, "action": action, "quantity": quantity,
                  "price": float(price), "value": float(value),
                  "rsi": float(rsi_5m), "ma20": float(rsi_15m),
                  "ma50": float(vwap_dev), "risk_level": f"{RISK_LEVEL}|score:{score}"},
            timeout=10)
    except Exception as e:
        print(f"Supabase trade log error: {e}")


def log_snapshot(portfolio_value, cash, daily_pnl, open_positions):
    try:
        requests.post(
            f"{SUPABASE_URL}/rest/v1/portfolio_snapshots",
            headers={"apikey": SUPABASE_KEY,
                     "Authorization": f"Bearer {SUPABASE_KEY}",
                     "Content-Type": "application/json",
                     "Prefer": "return=minimal"},
            json={"portfolio_value": float(portfolio_value), "cash": float(cash),
                  "daily_pnl": float(daily_pnl), "open_positions": open_positions},
            timeout=10)
    except Exception as e:
        print(f"Supabase snapshot error: {e}")


# Scheduling is now handled by the /trigger endpoint in dashboard.py
# (pinged by UptimeRobot every 5 minutes during market hours).
# No more GitHub Actions self-chaining needed.


# ── INDICATORS ────────────────────────────────────────────────────────────────

def calculate_rsi(prices: pd.Series, period: int = 14) -> float:
    delta = prices.diff()
    gain  = delta.where(delta > 0, 0.0).rolling(period).mean()
    loss  = (-delta.where(delta < 0, 0.0)).rolling(period).mean()
    rs    = gain / loss
    rsi   = 100 - (100 / (1 + rs))
    return round(float(rsi.iloc[-1]), 2)


def calculate_macd(prices: pd.Series):
    """Returns (macd_line, signal_line, histogram) latest values."""
    ema12 = prices.ewm(span=12, adjust=False).mean()
    ema26 = prices.ewm(span=26, adjust=False).mean()
    macd  = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    hist   = macd - signal
    return float(macd.iloc[-1]), float(signal.iloc[-1]), float(hist.iloc[-1]), float(hist.iloc[-2])


def calculate_vwap(bars: pd.DataFrame) -> float:
    """VWAP = cumulative(typical_price × volume) / cumulative(volume)."""
    typical = (bars["high"] + bars["low"] + bars["close"]) / 3
    vwap    = (typical * bars["volume"]).cumsum() / bars["volume"].cumsum()
    return float(vwap.iloc[-1])


def get_volume_ratio(bars_1min: pd.DataFrame) -> float:
    """Current 5-min volume vs average 5-min volume over last hour."""
    try:
        vol_5min   = bars_1min["volume"].iloc[-5:].sum()   # last 5 mins
        avg_5min   = bars_1min["volume"].rolling(5).sum().mean()
        return round(vol_5min / avg_5min, 2) if avg_5min > 0 else 1.0
    except:
        return 1.0


# ── SCORING ENGINE ────────────────────────────────────────────────────────────

def score_symbol(symbol: str) -> dict | None:
    """
    Cohen-Inspired Multi-Signal Momentum Scoring.

    Points breakdown (total 100):
    ┌─────────────────────────────┬────────┐
    │ Signal                      │  Max   │
    ├─────────────────────────────┼────────┤
    │ VWAP Deviation              │  30 pt │
    │ RSI Multi-timeframe (5m/15m)│  25 pt │
    │ Volume Surge                │  25 pt │
    │ MACD Momentum               │  20 pt │
    └─────────────────────────────┴────────┘

    Buy  if score ≥ 65
    Sell if score < 40
    """
    score       = 0
    breakdown   = {}

    try:
        # ── Fetch bars ────────────────────────────────────────────────────────
        bars_1m  = api.get_bars(symbol, "1Min",  limit=120).df
        bars_15m = api.get_bars(symbol, "15Min", limit=60).df
        bars_1d  = api.get_bars(symbol, "1Day",  limit=60).df

        if bars_1m.empty or bars_15m.empty or len(bars_1d) < 30:
            return None

        current_price = float(bars_1m["close"].iloc[-1])

        # ── Signal 1: VWAP Deviation (max 30 pts) ────────────────────────────
        # Where is price relative to fair value?
        # Best entry: just below VWAP (institutional support level)
        vwap      = calculate_vwap(bars_1m)
        vwap_dev  = (current_price - vwap) / vwap * 100  # % deviation

        if -0.5 <= vwap_dev <= 0.0:
            vwap_pts = 30   # Ideal: price just under VWAP, about to reclaim
        elif -1.0 <= vwap_dev < -0.5:
            vwap_pts = 20   # Deeper discount — possible entry
        elif 0.0 < vwap_dev <= 0.3:
            vwap_pts = 15   # Just above VWAP — momentum confirmed
        elif -1.5 <= vwap_dev < -1.0:
            vwap_pts = 10   # Too far below — possible value trap
        else:
            vwap_pts = 0    # Too far above or below VWAP
        score += vwap_pts
        breakdown["vwap"] = f"{vwap_pts}pt (dev:{vwap_dev:+.2f}%)"

        # ── Signal 2: Multi-timeframe RSI (max 25 pts) ───────────────────────
        # 5-min RSI shows short-term momentum
        # 15-min RSI shows medium-term trend direction
        rsi_5m  = calculate_rsi(bars_1m["close"].resample("5min").last().dropna(), 14)
        rsi_15m = calculate_rsi(bars_15m["close"], 14)

        rsi_pts = 0
        if 40 <= rsi_5m <= 55:   rsi_pts += 15   # 5m: recovering but not overbought
        elif 35 <= rsi_5m < 40:  rsi_pts += 10   # 5m: slightly oversold
        elif 55 < rsi_5m <= 60:  rsi_pts += 8    # 5m: moderate momentum
        if rsi_15m > 50:         rsi_pts += 10   # 15m: medium trend is up
        elif rsi_15m > 45:       rsi_pts += 5    # 15m: neutral but recovering

        score += rsi_pts
        breakdown["rsi"] = f"{rsi_pts}pt (5m:{rsi_5m} 15m:{rsi_15m})"

        # ── Signal 3: Volume Surge (max 25 pts) ──────────────────────────────
        # Institutional money leaves footprints in volume
        vol_ratio = get_volume_ratio(bars_1m)

        if vol_ratio >= 2.5:    vol_pts = 25   # Massive institutional surge
        elif vol_ratio >= 2.0:  vol_pts = 20   # Strong surge
        elif vol_ratio >= 1.5:  vol_pts = 14   # Moderate interest
        elif vol_ratio >= 1.2:  vol_pts = 8    # Slight above average
        else:                   vol_pts = 0    # No unusual activity
        score += vol_pts
        breakdown["volume"] = f"{vol_pts}pt (ratio:{vol_ratio}x)"

        # ── Signal 4: MACD Momentum (max 20 pts) ─────────────────────────────
        # MACD on 5-min bars catches momentum shifts early
        bars_5m = bars_1m["close"].resample("5min").last().dropna()
        macd_line, signal_line, hist_now, hist_prev = calculate_macd(bars_5m)

        macd_pts = 0
        if hist_now > 0:                        macd_pts += 10  # Histogram positive
        if hist_now > hist_prev and hist_now > 0: macd_pts += 10 # Histogram growing (acceleration)
        elif hist_now > hist_prev:              macd_pts += 5   # Improving even if negative

        score += macd_pts
        breakdown["macd"] = f"{macd_pts}pt (hist:{hist_now:.3f})"

        print(f"  {symbol}: score={score} | {breakdown}")

        return {
            "symbol":    symbol,
            "score":     score,
            "price":     current_price,
            "vwap":      round(vwap, 2),
            "vwap_dev":  round(vwap_dev, 2),
            "rsi_5m":    rsi_5m,
            "rsi_15m":   rsi_15m,
            "vol_ratio": vol_ratio,
            "macd_hist": round(hist_now, 4),
            "buy":       score >= BUY_SCORE,
            "strong_buy":score >= STRONG_BUY_SCORE,
            "sell":      score < SELL_SCORE,
            "breakdown": breakdown,
        }

    except Exception as e:
        print(f"Scoring error for {symbol}: {e}")
        return None


def get_sell_signal(symbol: str, entry_price: float) -> dict:
    """Check if we should exit an existing position."""
    try:
        bars_1m = api.get_bars(symbol, "1Min", limit=60).df
        if bars_1m.empty:
            return {"sell": False}

        current_price = float(bars_1m["close"].iloc[-1])
        pnl_pct       = (current_price - entry_price) / entry_price

        # Hard stop loss
        if pnl_pct <= -STOP_LOSS_PCT:
            return {"sell": True, "reason": "stop-loss", "pnl_pct": pnl_pct}

        # Take profit
        if pnl_pct >= TAKE_PROFIT_PCT:
            return {"sell": True, "reason": "take-profit", "pnl_pct": pnl_pct}

        # Score-based exit
        sig = score_symbol(symbol)
        if sig and sig["sell"]:
            return {"sell": True, "reason": "signal-exit", "pnl_pct": pnl_pct}

        # VWAP extended too far above (overextended)
        if sig and sig["vwap_dev"] > 1.5:
            return {"sell": True, "reason": "vwap-overextended", "pnl_pct": pnl_pct}

        return {"sell": False, "pnl_pct": pnl_pct}

    except Exception as e:
        print(f"Sell signal error for {symbol}: {e}")
        return {"sell": False}


# ── MAIN BOT ──────────────────────────────────────────────────────────────────

def run_bot():
    print(f"\n🤖 Bot started at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

    # ── Market check ──────────────────────────────────────────────────────────
    clock = api.get_clock()
    if not clock.is_open:
        print("Market closed — saving snapshot, stopping chain.")
        account = api.get_account()
        log_snapshot(float(account.portfolio_value), float(account.cash), 0, 0)
        send_telegram("🕐 <b>Bot checked in</b>\nMarket is closed. Resuming next session.")
        return

    # ── Account info ──────────────────────────────────────────────────────────
    account       = api.get_account()
    portfolio_val = float(account.portfolio_value)
    cash          = float(account.cash)
    positions     = api.list_positions()
    held          = {p.symbol: p for p in positions}
    num_open      = len(positions)

    print(f"Portfolio: ${portfolio_val:,.2f} | Cash: ${cash:,.2f} | Positions: {num_open}")

    # ── Exit management ───────────────────────────────────────────────────────
    for symbol, pos in held.items():
        entry   = float(pos.avg_entry_price)
        current = float(pos.current_price)
        result  = get_sell_signal(symbol, entry)

        if result["sell"]:
            reason  = result.get("reason", "signal")
            pnl_pct = result.get("pnl_pct", 0)
            try:
                api.submit_order(symbol=symbol, qty=pos.qty,
                                 side="sell", type="market", time_in_force="day")
                log_trade(symbol, reason, int(pos.qty), current,
                          current * int(pos.qty), 0, 0, 0, 0)

                emoji = "✅" if pnl_pct > 0 else "🛑"
                send_telegram(
                    f"{emoji} <b>{reason.upper().replace('-',' ')}</b>\n"
                    f"Stock: {symbol}\n"
                    f"Entry: ${entry:.2f}  →  Exit: ${current:.2f}\n"
                    f"P&L: {pnl_pct*100:+.2f}%\n"
                    f"Reason: {reason}")
                print(f"Exited {symbol}: {reason} ({pnl_pct*100:+.2f}%)")
            except Exception as e:
                print(f"Exit order failed for {symbol}: {e}")

    # ── Refresh positions ─────────────────────────────────────────────────────
    positions = api.list_positions()
    held      = {p.symbol for p in positions}
    num_open  = len(positions)

    # ── Entry scanning ────────────────────────────────────────────────────────
    if num_open < MAX_POSITIONS and cash > 100:
        print(f"\nScanning {len(WATCHLIST)} stocks for entries...")
        candidates = []

        for symbol in WATCHLIST:
            if symbol in held:
                continue
            sig = score_symbol(symbol)
            if sig and sig["buy"]:
                candidates.append(sig)

        # Sort by score — take best opportunities first
        candidates.sort(key=lambda x: x["score"], reverse=True)

        for sig in candidates:
            if num_open >= MAX_POSITIONS:
                break

            symbol   = sig["symbol"]
            score    = sig["score"]
            is_strong = sig["strong_buy"]

            size_key     = "strong" if is_strong else "normal"
            trade_pct    = POSITION_SIZES[size_key][RISK_LEVEL]
            trade_value  = portfolio_val * trade_pct
            qty          = int(trade_value / sig["price"])

            if qty < 1 or trade_value > cash:
                print(f"  Skipping {symbol}: insufficient cash for {qty} shares")
                continue

            try:
                api.submit_order(symbol=symbol, qty=qty,
                                 side="buy", type="market", time_in_force="day")
                num_open += 1
                cash     -= trade_value

                log_trade(symbol, "buy", qty, sig["price"], trade_value,
                          score, sig["rsi_5m"], sig["rsi_15m"], sig["vwap_dev"])

                strength_label = "💥 STRONG BUY" if is_strong else "📈 BUY"
                send_telegram(
                    f"{strength_label} — Score: {score}/100\n"
                    f"Stock: <b>{symbol}</b>\n"
                    f"Shares: {qty}  @  ${sig['price']:.2f}\n"
                    f"Value: ${trade_value:,.2f}\n\n"
                    f"📊 <b>Signal Breakdown</b>\n"
                    f"VWAP deviation: {sig['vwap_dev']:+.2f}%\n"
                    f"RSI 5min: {sig['rsi_5m']}  |  RSI 15min: {sig['rsi_15m']}\n"
                    f"Volume surge: {sig['vol_ratio']}x average\n"
                    f"MACD histogram: {sig['macd_hist']}\n"
                    f"Risk level: {RISK_LEVEL.upper()}")
                print(f"Bought {symbol}: {qty} shares @ ${sig['price']:.2f} (score:{score})")

            except Exception as e:
                print(f"Buy order failed for {symbol}: {e}")

    # ── Summary ───────────────────────────────────────────────────────────────
    account    = api.get_account()
    daily_pl   = float(account.equity) - float(account.last_equity)
    port_val   = float(account.portfolio_value)
    cash_final = float(account.cash)

    log_snapshot(port_val, cash_final, daily_pl, num_open)

    pl_emoji = "🟢" if daily_pl >= 0 else "🔴"
    send_telegram(
        f"{pl_emoji} <b>Bot Run Complete</b>\n"
        f"Time: {datetime.now(timezone.utc).strftime('%H:%M UTC')}\n"
        f"Portfolio: ${port_val:,.2f}\n"
        f"Today's P&L: ${daily_pl:+.2f}\n"
        f"Open positions: {num_open}/{MAX_POSITIONS}\n"
        f"Risk mode: {RISK_LEVEL.upper()}")

if __name__ == "__main__":
    run_bot()
