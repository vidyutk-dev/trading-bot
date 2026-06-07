import os
import requests
import pandas as pd
from datetime import datetime
import alpaca_trade_api as tradeapi

# ── CONFIG ────────────────────────────────────────────────────────────────────
ALPACA_KEY       = os.environ["ALPACA_KEY"]
ALPACA_SECRET    = os.environ["ALPACA_SECRET"]
ALPACA_URL       = "https://paper-api.alpaca.markets"
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
SUPABASE_URL     = os.environ["SUPABASE_URL"]
SUPABASE_KEY     = os.environ["SUPABASE_KEY"]

RISK_LEVEL    = os.environ.get("RISK_LEVEL", "medium")
RISK_PERCENT  = {"low": 0.01, "medium": 0.02, "high": 0.04}[RISK_LEVEL]
MAX_POSITIONS = 3
STOP_LOSS_PCT = 0.02

WATCHLIST = ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "SPY", "QQQ", "TSLA"]

# ── SETUP ─────────────────────────────────────────────────────────────────────
api = tradeapi.REST(ALPACA_KEY, ALPACA_SECRET, ALPACA_URL, api_version="v2")


def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")


def log_trade(symbol, action, quantity, price, value, rsi, ma20, ma50):
    """Save a trade record to Supabase."""
    try:
        requests.post(
            f"{SUPABASE_URL}/rest/v1/trades",
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal"
            },
            json={
                "symbol": symbol, "action": action,
                "quantity": quantity, "price": float(price),
                "value": float(value), "rsi": float(rsi),
                "ma20": float(ma20), "ma50": float(ma50),
                "risk_level": RISK_LEVEL
            },
            timeout=10
        )
        print(f"Trade logged to Supabase: {action} {symbol}")
    except Exception as e:
        print(f"Supabase trade log error: {e}")


def log_snapshot(portfolio_value, cash, daily_pnl, open_positions):
    """Save a portfolio snapshot to Supabase."""
    try:
        requests.post(
            f"{SUPABASE_URL}/rest/v1/portfolio_snapshots",
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal"
            },
            json={
                "portfolio_value": float(portfolio_value),
                "cash": float(cash),
                "daily_pnl": float(daily_pnl),
                "open_positions": open_positions
            },
            timeout=10
        )
        print("Portfolio snapshot logged to Supabase")
    except Exception as e:
        print(f"Supabase snapshot error: {e}")


def calculate_rsi(prices: pd.Series, period: int = 14) -> pd.Series:
    delta = prices.diff()
    gain  = delta.where(delta > 0, 0.0).rolling(period).mean()
    loss  = (-delta.where(delta < 0, 0.0)).rolling(period).mean()
    rs    = gain / loss
    return 100 - (100 / (1 + rs))


def get_signals(symbol: str) -> dict | None:
    try:
        bars  = api.get_bars(symbol, "1Day", limit=60).df
        if len(bars) < 50:
            return None
        close = bars["close"]
        rsi   = calculate_rsi(close)
        ma20  = close.rolling(20).mean()
        ma50  = close.rolling(50).mean()
        latest = {
            "symbol": symbol,
            "price":  round(float(close.iloc[-1]), 2),
            "rsi":    round(float(rsi.iloc[-1]), 2),
            "ma20":   round(float(ma20.iloc[-1]), 2),
            "ma50":   round(float(ma50.iloc[-1]), 2),
        }
        latest["buy"]  = latest["rsi"] < 35 and latest["ma20"] > latest["ma50"]
        latest["sell"] = latest["rsi"] > 65 or  latest["ma20"] < latest["ma50"]
        return latest
    except Exception as e:
        print(f"Signal error for {symbol}: {e}")
        return None


def run_bot():
    print(f"\n🤖 Bot started at {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")

    clock = api.get_clock()
    if not clock.is_open:
        print("Market is closed — nothing to do.")
        send_telegram("🕐 <b>Bot checked in</b>\nMarket is currently closed.\nWill trade automatically when market opens Mon-Fri 2:30pm-9pm UK time.")
        return

    account       = api.get_account()
    portfolio_val = float(account.portfolio_value)
    cash          = float(account.cash)
    positions     = api.list_positions()
    held_symbols  = {p.symbol: p for p in positions}

    print(f"Portfolio: ${portfolio_val:.2f} | Cash: ${cash:.2f} | Positions: {len(positions)}")

    # Stop-loss check
    for symbol, pos in held_symbols.items():
        entry   = float(pos.avg_entry_price)
        current = float(pos.current_price)
        pnl_pct = (current - entry) / entry
        if pnl_pct <= -STOP_LOSS_PCT:
            try:
                api.submit_order(symbol=symbol, qty=pos.qty,
                                 side="sell", type="market", time_in_force="day")
                log_trade(symbol, "stop-loss", int(pos.qty), current,
                          current * int(pos.qty), 0, 0, 0)
                send_telegram(
                    f"🛑 <b>STOP LOSS HIT</b>\n"
                    f"Stock: {symbol}\n"
                    f"Entry: ${entry:.2f}  →  Now: ${current:.2f}\n"
                    f"Loss: {pnl_pct*100:.1f}%\nPosition closed automatically.")
            except Exception as e:
                print(f"Stop-loss order failed for {symbol}: {e}")

    # Sell signals
    for symbol, pos in held_symbols.items():
        sig = get_signals(symbol)
        if sig and sig["sell"]:
            try:
                api.submit_order(symbol=symbol, qty=pos.qty,
                                 side="sell", type="market", time_in_force="day")
                log_trade(symbol, "sell", int(pos.qty), sig["price"],
                          sig["price"] * int(pos.qty),
                          sig["rsi"], sig["ma20"], sig["ma50"])
                send_telegram(
                    f"📤 <b>SELL ORDER</b>\nStock: {symbol}\n"
                    f"Price: ${sig['price']:.2f}\n"
                    f"RSI: {sig['rsi']}  |  MA20: ${sig['ma20']}  |  MA50: ${sig['ma50']}")
            except Exception as e:
                print(f"Sell order failed for {symbol}: {e}")

    # Refresh
    positions    = api.list_positions()
    held_symbols = {p.symbol for p in positions}
    num_open     = len(positions)

    # Buy signals
    if num_open < MAX_POSITIONS and cash > 10:
        for symbol in WATCHLIST:
            if num_open >= MAX_POSITIONS:
                break
            if symbol in held_symbols:
                continue
            sig = get_signals(symbol)
            if not sig or not sig["buy"]:
                continue
            trade_value = portfolio_val * RISK_PERCENT
            qty         = int(trade_value / sig["price"])
            if qty < 1 or trade_value > cash:
                continue
            try:
                api.submit_order(symbol=symbol, qty=qty,
                                 side="buy", type="market", time_in_force="day")
                num_open += 1
                log_trade(symbol, "buy", qty, sig["price"],
                          trade_value, sig["rsi"], sig["ma20"], sig["ma50"])
                send_telegram(
                    f"📈 <b>BUY ORDER</b>\nStock: {symbol}\n"
                    f"Shares: {qty}  @  ${sig['price']:.2f}\n"
                    f"Value: ${trade_value:.2f}\n"
                    f"RSI: {sig['rsi']}  |  MA20: ${sig['ma20']}  |  MA50: ${sig['ma50']}\n"
                    f"Risk level: {RISK_LEVEL.upper()}")
            except Exception as e:
                print(f"Buy order failed for {symbol}: {e}")

    # Snapshot & summary
    account      = api.get_account()
    daily_pl     = float(account.equity) - float(account.last_equity)
    port_val     = float(account.portfolio_value)
    cash_final   = float(account.cash)

    log_snapshot(port_val, cash_final, daily_pl, num_open)

    pl_emoji = "🟢" if daily_pl >= 0 else "🔴"
    send_telegram(
        f"{pl_emoji} <b>Bot Run Complete</b>\n"
        f"Time: {datetime.utcnow().strftime('%H:%M UTC')}\n"
        f"Portfolio: ${port_val:.2f}\n"
        f"Today's P&L: ${daily_pl:+.2f}\n"
        f"Open positions: {num_open}/{MAX_POSITIONS}\n"
        f"Risk mode: {RISK_LEVEL.upper()}")
    print("Run complete.")


if __name__ == "__main__":
    run_bot()
