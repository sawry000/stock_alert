#!/usr/bin/env python3
"""
Stock Price Alert Engine
Runs on GitHub Actions — checks prices and sends Telegram alerts.
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import yfinance as yf
except ImportError:
    print("Installing yfinance...")
    os.system("pip install yfinance --quiet --break-system-packages")
    import yfinance as yf

# ─── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
WATCHLIST_PATH = BASE_DIR / "watchlist.json"
STATE_PATH = BASE_DIR / "state.json"
LOG_PATH = BASE_DIR / "alert_log.json"

# ─── Helpers ──────────────────────────────────────────────────────────────────

def load_json(path, default):
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return default
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def now_utc():
    return datetime.now(timezone.utc)


def now_str():
    return now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")


def minutes_since(iso_str):
    try:
        past = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return (now_utc() - past).total_seconds() / 60
    except Exception:
        return 9999


# ─── Telegram ─────────────────────────────────────────────────────────────────

def send_telegram(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
            return result.get("ok", False)
    except urllib.error.URLError as e:
        print(f"  [Telegram Error] {e}")
        return False


# ─── Price Fetcher ────────────────────────────────────────────────────────────

def fetch_quote(symbol):
    """
    Returns dict with: price, prev_close, change_pct, volume, avg_volume
    Returns None on failure.
    """
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.fast_info

        price = getattr(info, "last_price", None)
        prev_close = getattr(info, "previous_close", None)
        volume = getattr(info, "three_month_average_volume", None)

        # Fallback: use history for more reliable data
        if price is None or prev_close is None:
            hist = ticker.history(period="5d", interval="1d")
            if hist.empty:
                print(f"  [{symbol}] No data returned from yfinance")
                return None
            price = float(hist["Close"].iloc[-1])
            prev_close = float(hist["Close"].iloc[-2]) if len(hist) > 1 else price
            volume = float(hist["Volume"].iloc[-1])

        price = float(price)
        prev_close = float(prev_close) if prev_close else price

        # Today's volume from intraday
        hist_1d = ticker.history(period="1d", interval="1m")
        today_volume = float(hist_1d["Volume"].sum()) if not hist_1d.empty else 0

        # Average volume (approximate from 3-month avg)
        avg_vol = getattr(info, "three_month_average_volume", None)
        avg_volume = float(avg_vol) if avg_vol and avg_vol > 0 else (today_volume or 1)

        change_pct = ((price - prev_close) / prev_close) * 100 if prev_close else 0

        return {
            "price": price,
            "prev_close": prev_close,
            "change_pct": change_pct,
            "volume": today_volume,
            "avg_volume": avg_volume
        }
    except Exception as e:
        print(f"  [{symbol}] Fetch error: {e}")
        return None


# ─── Alert Checkers ───────────────────────────────────────────────────────────

def check_price_target(alert, quote):
    price = quote["price"]
    target = alert["target_price"]
    direction = alert.get("direction", "below_or_equal")
    if direction == "below_or_equal" and price <= target:
        return True, price
    if direction == "above_or_equal" and price >= target:
        return True, price
    return False, price


def check_percent_change(alert, quote):
    pct = quote["change_pct"]
    direction = alert.get("direction", "down")
    threshold = alert.get("threshold_pct", 5.0)
    if direction == "down" and pct <= -threshold:
        return True, pct
    if direction == "up" and pct >= threshold:
        return True, pct
    return False, pct


def check_volume_spike(alert, quote):
    vol = quote["volume"]
    avg = quote["avg_volume"]
    mult = alert.get("multiplier", 2.0)
    if avg > 0 and vol >= avg * mult:
        ratio = vol / avg
        return True, ratio
    return False, 0


def check_support_resistance(alert, quote):
    price = quote["price"]
    level = alert["level"]
    direction = alert.get("direction", "break_below")
    if direction == "break_below" and price < level:
        return True, price
    if direction == "break_above" and price > level:
        return True, price
    return False, price


# ─── Message Builder ──────────────────────────────────────────────────────────

def build_message(stock, alert, quote, triggered_value):
    emoji = alert.get("emoji", "🔔")
    atype = alert["type"]
    symbol = stock["symbol"]
    name = stock["name"]
    price = quote["price"]
    pct = quote["change_pct"]
    pct_arrow = "📈" if pct >= 0 else "📉"
    pct_sign = "+" if pct >= 0 else ""
    tf = stock.get("timeframe", "")
    note = alert.get("note", "")
    action = alert.get("action", "")
    tv_link = f"https://www.tradingview.com/chart/?symbol={symbol}"

    lines = []
    lines.append(f"{emoji} <b>ALERT: {symbol}</b> ({name})")
    lines.append(f"")

    if atype == "price_target":
        target = alert["target_price"]
        lines.append(f"💰 Price: <b>${price:.4f}</b>  {pct_arrow} {pct_sign}{pct:.2f}%")
        lines.append(f"🎯 Target hit: <b>${target:.4f}</b>")
        if action:
            lines.append(f"⚡ Signal: <b>{action}</b>")
    elif atype == "percent_change":
        lines.append(f"💰 Price: <b>${price:.4f}</b>")
        lines.append(f"{pct_arrow} Change: <b>{pct_sign}{pct:.2f}%</b>")
    elif atype == "volume_spike":
        lines.append(f"💰 Price: <b>${price:.4f}</b>  {pct_arrow} {pct_sign}{pct:.2f}%")
        lines.append(f"🔊 Volume: <b>{triggered_value:.1f}x</b> above average")
    elif atype == "support_resistance":
        level = alert["level"]
        lines.append(f"💰 Price: <b>${price:.4f}</b>  {pct_arrow} {pct_sign}{pct:.2f}%")
        lines.append(f"⚠️ Broke level: <b>${level:.4f}</b>")

    if tf:
        lines.append(f"⏱ Timeframe: {tf}")
    if note:
        lines.append(f"📋 Note: {note}")

    lines.append(f"")
    lines.append(f"📊 <a href='{tv_link}'>View on TradingView</a>")
    lines.append(f"🕐 {now_str()}")

    return "\n".join(lines)


def build_daily_summary(watchlist, quotes_cache):
    lines = []
    lines.append("📊 <b>Daily Watchlist Summary</b>")
    lines.append(f"🕐 {now_str()}")
    lines.append("")

    for stock in watchlist:
        symbol = stock["symbol"]
        name = stock["name"]
        quote = quotes_cache.get(symbol)
        if not quote:
            lines.append(f"• <b>{symbol}</b> — ⚠️ No data")
            continue
        price = quote["price"]
        pct = quote["change_pct"]
        pct_arrow = "📈" if pct >= 0 else "📉"
        pct_sign = "+" if pct >= 0 else ""
        lines.append(
            f"• <b>{symbol}</b> ({name})\n"
            f"  💰 ${price:.4f}  {pct_arrow} {pct_sign}{pct:.2f}%"
        )

        for alert in stock.get("alerts", []):
            if alert["type"] == "price_target":
                target = alert["target_price"]
                diff_pct = ((price - target) / target) * 100
                diff_sign = "+" if diff_pct >= 0 else ""
                lines.append(
                    f"  🎯 Target ${target:.4f} — "
                    f"{'✅ HIT' if abs(diff_pct) < 0.5 else f'{diff_sign}{diff_pct:.1f}% away'}"
                )

        lines.append("")

    return "\n".join(lines)


# ─── Main Logic ───────────────────────────────────────────────────────────────

def main():
    # Load config
    config = load_json(WATCHLIST_PATH, {})
    settings = config.get("settings", {})
    watchlist = config.get("watchlist", [])

    # Load secrets
    token = os.environ.get(settings.get("telegram_bot_token_env", "TELEGRAM_BOT_TOKEN"), "")
    chat_id = os.environ.get(settings.get("telegram_chat_id_env", "TELEGRAM_CHAT_ID"), "")

    if not token or not chat_id:
        print("ERROR: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set in environment.")
        sys.exit(1)

    default_cooldown = settings.get("cooldown_minutes", 60)

    # Load state (tracks last alert times)
    state = load_json(STATE_PATH, {})

    # Load log
    log = load_json(LOG_PATH, [])

    quotes_cache = {}
    fired_count = 0

    print(f"[{now_str()}] Starting alert check — {len(watchlist)} symbols")

    for stock in watchlist:
        if not stock.get("enabled", True):
            continue

        symbol = stock["symbol"]
        print(f"\n[{symbol}] Fetching quote...")

        quote = fetch_quote(symbol)
        if quote is None:
            print(f"  [{symbol}] Skipping — no data")
            continue

        quotes_cache[symbol] = quote
        print(
            f"  [{symbol}] Price=${quote['price']:.4f}  "
            f"Chg={quote['change_pct']:+.2f}%  "
            f"Vol={quote['volume']:.0f} (avg {quote['avg_volume']:.0f})"
        )

        alerts = stock.get("alerts", [])
        sym_state = state.get(symbol, {})

        for alert in alerts:
            alert_id = alert["id"]
            atype = alert["type"]
            cooldown = alert.get("cooldown_minutes", default_cooldown)

            # Cooldown check
            last_fired = sym_state.get(alert_id, {}).get("last_fired", "")
            if last_fired and minutes_since(last_fired) < cooldown:
                remaining = cooldown - minutes_since(last_fired)
                print(f"  [{alert_id}] Skipping — cooldown {remaining:.0f}m remaining")
                continue

            # Check condition
            triggered = False
            triggered_value = 0

            if atype == "price_target":
                triggered, triggered_value = check_price_target(alert, quote)
            elif atype == "percent_change":
                triggered, triggered_value = check_percent_change(alert, quote)
            elif atype == "volume_spike":
                triggered, triggered_value = check_volume_spike(alert, quote)
            elif atype == "support_resistance":
                triggered, triggered_value = check_support_resistance(alert, quote)

            if not triggered:
                print(f"  [{alert_id}] Not triggered")
                continue

            print(f"  [{alert_id}] TRIGGERED! Sending Telegram...")

            msg = build_message(stock, alert, quote, triggered_value)
            success = send_telegram(token, chat_id, msg)

            if success:
                # Update state
                if symbol not in state:
                    state[symbol] = {}
                state[symbol][alert_id] = {"last_fired": now_str()}

                # Append to log
                log.append({
                    "timestamp": now_str(),
                    "symbol": symbol,
                    "alert_id": alert_id,
                    "type": atype,
                    "price": quote["price"],
                    "change_pct": quote["change_pct"]
                })

                fired_count += 1
                print(f"  [{alert_id}] Telegram sent OK")
            else:
                print(f"  [{alert_id}] Telegram FAILED")

        # Small delay between symbols to avoid rate limits
        time.sleep(1)

    # Daily summary check
    summary_hour = settings.get("daily_summary_hour_utc", 1)
    current_hour = now_utc().hour
    summary_state = state.get("__daily_summary__", {})
    last_summary = summary_state.get("last_sent", "")
    today_str = now_utc().strftime("%Y-%m-%d")

    if (
        current_hour == summary_hour
        and (not last_summary or not last_summary.startswith(today_str))
        and quotes_cache
    ):
        print("\n[Daily Summary] Sending...")
        msg = build_daily_summary(watchlist, quotes_cache)
        success = send_telegram(token, chat_id, msg)
        if success:
            state["__daily_summary__"] = {"last_sent": now_str()}
            print("[Daily Summary] Sent OK")

    # Save state and log
    save_json(STATE_PATH, state)
    save_json(LOG_PATH, log[-500:])  # Keep last 500 entries

    print(f"\n[{now_str()}] Done. {fired_count} alert(s) fired.")


if __name__ == "__main__":
    main()
