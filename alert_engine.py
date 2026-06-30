#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  Stock Alert Engine PRO v3.1 — Multi-Layer Signal Gate Edition             ║
║                                                                              ║
║  การปรับปรุงจาก v3:                                                        ║
║    ✅ account_size default = 100 USD (ต่อหุ้น 1 ตัว)                       ║
║    ✅ Tiered Alert System — Fast/Medium/Slow tier                            ║
║    ✅ Multi-Layer Signal Gate (4 ชั้น AND logic) สำหรับ BUY                ║
║    ✅ Gate config ใน watchlist.json — ยืดหยุ่นต่อหุ้น                      ║
║    ✅ Confirmation Window — รอ N รอบก่อน fire                               ║
║    ✅ Volatility Gate — กรองช่วง ATR spike                                  ║
║    ✅ Position Sizing $100/หุ้น — fractional shares support                 ║
║    ✅ Gemini AI add_stock integration                                         ║
║                                                                              ║
║  Run: python3 alert_engine.py                                                ║
║  Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, GEMINI_API_KEY (optional)       ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import json
import math
import os
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import yfinance as yf
except ImportError:
    print("Installing yfinance...")
    os.system("pip install yfinance --quiet --break-system-packages")
    import yfinance as yf

# ─── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR       = Path(__file__).parent
WATCHLIST_PATH = BASE_DIR / "watchlist.json"
STATE_PATH     = BASE_DIR / "state.json"
LOG_PATH       = BASE_DIR / "alert_log.json"

# ══════════════════════════════════════════════════════════════════════════════
#  UTILITIES
# ══════════════════════════════════════════════════════════════════════════════

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


def now_bkk_str():
    bkk = now_utc() + timedelta(hours=7)
    return bkk.strftime("%d/%m/%Y %H:%M ICT")


def minutes_since(iso_str):
    try:
        past = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return (now_utc() - past).total_seconds() / 60
    except Exception:
        return 9999


# ══════════════════════════════════════════════════════════════════════════════
#  TELEGRAM
# ══════════════════════════════════════════════════════════════════════════════

def send_telegram(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read())
                return result.get("ok", False)
        except urllib.error.URLError as e:
            print(f"  [Telegram] Attempt {attempt+1} failed: {e}")
            if attempt < 2:
                time.sleep(3)
    return False


# ══════════════════════════════════════════════════════════════════════════════
#  PRICE & HISTORY FETCH
# ══════════════════════════════════════════════════════════════════════════════

def fetch_quote(symbol):
    try:
        ticker = yf.Ticker(symbol)
        info   = ticker.fast_info
        price      = getattr(info, "last_price", None)
        prev_close = getattr(info, "previous_close", None)

        if price is None or prev_close is None:
            hist = ticker.history(period="5d", interval="1d")
            if hist.empty:
                print(f"  [{symbol}] No data returned")
                return None
            price      = float(hist["Close"].iloc[-1])
            prev_close = float(hist["Close"].iloc[-2]) if len(hist) > 1 else price

        price      = float(price)
        prev_close = float(prev_close) if prev_close else price

        hist_1d   = ticker.history(period="1d", interval="1m")
        today_vol = float(hist_1d["Volume"].sum()) if not hist_1d.empty else 0

        avg_vol_raw = getattr(info, "three_month_average_volume", None)
        avg_volume  = float(avg_vol_raw) if avg_vol_raw and avg_vol_raw > 0 else (today_vol or 1)

        change_pct = ((price - prev_close) / prev_close) * 100 if prev_close else 0

        return {
            "price":      price,
            "prev_close": prev_close,
            "change_pct": change_pct,
            "volume":     today_vol,
            "avg_volume": avg_volume,
        }
    except Exception as e:
        print(f"  [{symbol}] fetch_quote error: {e}")
        return None


def fetch_history(symbol, period="90d", interval="1d"):
    try:
        ticker = yf.Ticker(symbol)
        hist   = ticker.history(period=period, interval=interval)
        if hist.empty:
            return None
        return hist
    except Exception as e:
        print(f"  [{symbol}] fetch_history error: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
#  TECHNICAL HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _calc_ema(prices, period):
    if len(prices) < period:
        return [None] * len(prices)
    result = [None] * (period - 1)
    seed   = sum(prices[:period]) / period
    result.append(seed)
    k = 2 / (period + 1)
    for p in prices[period:]:
        result.append(p * k + result[-1] * (1 - k))
    return result


def _calc_rsi(closes, period=14):
    result = [None] * period
    if len(closes) <= period:
        return result + [None] * max(0, len(closes) - period)
    gains  = [max(closes[i] - closes[i-1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i-1] - closes[i], 0) for i in range(1, len(closes))]
    avg_g  = sum(gains[:period]) / period
    avg_l  = sum(losses[:period]) / period
    rsi_val = 100 - 100 / (1 + avg_g / avg_l) if avg_l > 0 else 100.0
    result.append(rsi_val)
    for i in range(period, len(gains)):
        avg_g   = (avg_g   * (period - 1) + gains[i])  / period
        avg_l   = (avg_l   * (period - 1) + losses[i]) / period
        rsi_val = 100 - 100 / (1 + avg_g / avg_l) if avg_l > 0 else 100.0
        result.append(rsi_val)
    return result


def _calc_atr(highs, lows, closes, period=14):
    if len(closes) < period + 2:
        return None
    trs = [
        max(highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i]  - closes[i-1]))
        for i in range(1, len(closes))
    ]
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


# ══════════════════════════════════════════════════════════════════════════════
#  TIERED ALERT CHECKERS
#  Tier 1 (Fast)   — RSI, Volume, % Change, Support/Resistance
#  Tier 2 (Medium) — MA Crossover, Alert Score
#  Tier 3 (Slow)   — MTF Alignment (cooldown ยาว, run น้อยรอบ)
# ══════════════════════════════════════════════════════════════════════════════

# ── Tier 1: RSI ───────────────────────────────────────────────────────────────
def check_rsi(alert, symbol):
    period   = alert.get("period", 14)
    interval = alert.get("interval", "1d")
    lb_map   = {"1m":"5d","5m":"5d","15m":"30d","30m":"60d",
                "1h":"60d","4h":"60d","1d":"90d","1wk":"2y"}
    hist = fetch_history(symbol, period=lb_map.get(interval,"90d"), interval=interval)
    if hist is None or len(hist) < period + 2:
        return False, None, None, None

    closes   = list(hist["Close"].astype(float))
    rsi_list = _calc_rsi(closes, period)
    valid    = [(r, c) for r, c in zip(rsi_list, closes) if r is not None]
    if len(valid) < 2:
        return False, None, None, None

    curr_rsi, curr_price = valid[-1]
    prev_rsi, _          = valid[-2]
    condition      = alert.get("condition", "oversold")
    oversold_lvl   = alert.get("oversold_level", 30)
    overbought_lvl = alert.get("overbought_level", 70)
    threshold      = alert.get("threshold", None)
    extreme_lvl    = alert.get("extreme_level", None)

    triggered = False
    if   condition == "oversold":           triggered = curr_rsi <= oversold_lvl
    elif condition == "overbought":         triggered = curr_rsi >= overbought_lvl
    elif condition == "extreme_oversold":   triggered = curr_rsi <= (extreme_lvl or 20)
    elif condition == "extreme_overbought": triggered = curr_rsi >= (extreme_lvl or 80)
    elif condition == "below" and threshold is not None:   triggered = curr_rsi <= threshold
    elif condition == "above" and threshold is not None:   triggered = curr_rsi >= threshold
    elif condition == "turning_up":   triggered = curr_rsi > prev_rsi and curr_rsi < 40
    elif condition == "turning_down": triggered = curr_rsi < prev_rsi and curr_rsi > 60

    return triggered, round(curr_rsi, 2), round(prev_rsi, 2), curr_price


# ── Tier 1: Volume Spike ──────────────────────────────────────────────────────
def check_volume_spike(alert, quote):
    vol  = quote["volume"]
    avg  = quote["avg_volume"]
    mult = alert.get("multiplier", 2.0)
    if avg > 0 and vol >= avg * mult:
        return True, vol / avg
    return False, 0


# ── Tier 1: Percent Change ────────────────────────────────────────────────────
def check_percent_change(alert, quote):
    pct       = quote["change_pct"]
    direction = alert.get("direction", "down")
    threshold = alert.get("threshold_pct", 5.0)
    if direction == "down" and pct <= -threshold:
        return True, pct
    if direction == "up"   and pct >= threshold:
        return True, pct
    return False, pct


# ── Tier 1: Support / Resistance ─────────────────────────────────────────────
def check_support_resistance(alert, quote, symbol=None):
    price     = quote["price"]
    level     = alert.get("level", 0)
    direction = alert.get("direction", "break_below")
    if level > 0:
        triggered = (
            (direction == "break_below" and price < level) or
            (direction == "break_above" and price > level)
        )
        return triggered, price, level
    return False, price, None


# ── Tier 1: Price Target ──────────────────────────────────────────────────────
def check_price_target(alert, quote):
    price     = quote["price"]
    target    = alert["target_price"]
    direction = alert.get("direction", "below_or_equal")
    if direction == "below_or_equal" and price <= target:
        return True, price
    if direction == "above_or_equal" and price >= target:
        return True, price
    return False, price


# ── Tier 2: MA Crossover ─────────────────────────────────────────────────────
def check_ma_crossover(alert, symbol):
    fast_p    = alert.get("fast_period", 9)
    slow_p    = alert.get("slow_period", 21)
    ma_type   = alert.get("ma_type", "EMA").upper()
    interval  = alert.get("interval", "1d")
    condition = alert.get("condition", "golden_cross")
    lb_map    = {"1m":"5d","5m":"5d","15m":"30d","30m":"60d",
                 "1h":"60d","4h":"60d","1d":"180d","1wk":"3y"}
    hist = fetch_history(symbol, period=lb_map.get(interval,"180d"), interval=interval)
    if hist is None or len(hist) < slow_p * 2:
        return False, None, None, None, None

    closes = list(hist["Close"].astype(float))

    def sma(prices, p):
        result = [None] * (p - 1)
        for i in range(p - 1, len(prices)):
            result.append(sum(prices[i - p + 1:i + 1]) / p)
        return result

    fast_list = _calc_ema(closes, fast_p) if ma_type == "EMA" else sma(closes, fast_p)
    slow_list = _calc_ema(closes, slow_p) if ma_type == "EMA" else sma(closes, slow_p)

    pairs = [(f, s, c) for f, s, c in zip(fast_list, slow_list, closes)
             if f is not None and s is not None]
    if len(pairs) < 2:
        return False, None, None, None, None

    cf, cs, cp = pairs[-1]
    pf, ps, _  = pairs[-2]
    gap_pct    = ((cf - cs) / cs * 100) if cs != 0 else 0

    triggered = False
    if   condition == "golden_cross":  triggered = pf <= ps and cf > cs
    elif condition == "death_cross":   triggered = pf >= ps and cf < cs
    elif condition == "above_both":    triggered = cp > cf and cp > cs
    elif condition == "below_both":    triggered = cp < cf and cp < cs
    elif condition == "trend_bullish": triggered = cf > cs
    elif condition == "trend_bearish": triggered = cf < cs

    return triggered, round(cf, 4), round(cs, 4), round(cp, 4), round(gap_pct, 3)


# ── Tier 2: Alert Score ───────────────────────────────────────────────────────
def check_alert_score(alert, symbol):
    direction = alert.get("direction", "bullish")
    min_score = alert.get("min_score", 65)
    interval  = alert.get("interval", "1d")
    is_bull   = direction == "bullish"
    lb_map    = {"1d": "90d", "4h": "60d", "1h": "60d"}
    hist = fetch_history(symbol, period=lb_map.get(interval,"90d"), interval=interval)
    if hist is None or len(hist) < 50:
        return False, 0, "F", {}

    closes  = list(hist["Close"].astype(float))
    highs   = list(hist["High"].astype(float))
    lows    = list(hist["Low"].astype(float))
    volumes = list(hist["Volume"].astype(float))
    price   = closes[-1]

    ema9  = _calc_ema(closes, 9)[-1]  or price
    ema21 = _calc_ema(closes, 21)[-1] or price
    ema50 = _calc_ema(closes, 50)[-1] or price
    rsi_l = _calc_rsi(closes, 14)
    rsi   = next((r for r in reversed(rsi_l) if r is not None), 50)

    avg_vol   = sum(volumes[-21:-1]) / 20 if len(volumes) >= 21 else volumes[-1]
    vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1
    atr       = _calc_atr(highs, lows, closes, 14) or 0
    atr_pct   = (atr / price) * 100 if price > 0 else 0
    chg       = ((closes[-1] - closes[-2]) / closes[-2]) * 100 if len(closes) >= 2 and closes[-2] > 0 else 0
    chg5      = ((closes[-1] - closes[-6]) / closes[-6]) * 100 if len(closes) >= 6 and closes[-6] > 0 else 0
    h20       = max(highs[-21:-1]) if len(highs) >= 21 else highs[-1]
    l20       = min(lows[-21:-1])  if len(lows)  >= 21 else lows[-1]
    dist_res  = ((h20 - price) / price * 100) if price > 0 else 0
    dist_sup  = ((price - l20) / price * 100) if price > 0 else 0

    sc = 0
    bd = {}

    rsi_sc = 0
    if is_bull:
        rsi_sc = 15 if rsi<=20 else (12 if rsi<=30 else (8 if rsi<=40 else (4 if rsi<=50 else 0)))
    else:
        rsi_sc = 15 if rsi>=80 else (12 if rsi>=70 else (8 if rsi>=60 else (4 if rsi>=50 else 0)))
    bd["RSI"] = {"s": rsi_sc, "max": 15, "note": f"RSI={rsi:.1f}"}
    sc += rsi_sc

    ma_sc = 0
    if is_bull:
        if price > ema21: ma_sc += 7
        if ema21 > ema50: ma_sc += 8
        if price > ema9 and ema9 > ema21: ma_sc += 5
    else:
        if price < ema21: ma_sc += 7
        if ema21 < ema50: ma_sc += 8
        if price < ema9 and ema9 < ema21: ma_sc += 5
    bd["MA"] = {"s": ma_sc, "max": 20, "note": f"EMA9={ema9:.2f} EMA21={ema21:.2f}"}
    sc += ma_sc

    vol_sc = 15 if vol_ratio>=3 else (12 if vol_ratio>=2 else (8 if vol_ratio>=1.5 else (4 if vol_ratio>=1 else 0)))
    bd["Vol"] = {"s": vol_sc, "max": 15, "note": f"Vol={vol_ratio:.1f}x"}
    sc += vol_sc

    mom_sc = 0
    if is_bull:
        mom_sc += (8 if chg>=3 else (5 if chg>=1 else (2 if chg>=0 else 0)))
        mom_sc += (7 if chg5>=5 else (4 if chg5>=2 else 0))
    else:
        mom_sc += (8 if chg<=-3 else (5 if chg<=-1 else (2 if chg<=0 else 0)))
        mom_sc += (7 if chg5<=-5 else (4 if chg5<=-2 else 0))
    mom_sc = min(mom_sc, 15)
    bd["Mom"] = {"s": mom_sc, "max": 15, "note": f"1d={chg:+.1f}% 5d={chg5:+.1f}%"}
    sc += mom_sc

    atr_sc = 10 if 1<=atr_pct<=4 else (6 if 0.5<=atr_pct<=7 else (2 if atr_pct<0.5 else 0))
    bd["ATR"] = {"s": atr_sc, "max": 10, "note": f"ATR={atr_pct:.1f}%"}
    sc += atr_sc

    sr_sc = 0
    if is_bull:
        sr_sc += (10 if dist_sup<=2 else (6 if dist_sup<=5 else 0))
        sr_sc += (5 if dist_res>=5 else 0)
    else:
        sr_sc += (10 if dist_res<=2 else (6 if dist_res<=5 else 0))
        sr_sc += (5 if dist_sup>=5 else 0)
    sr_sc = min(sr_sc, 15)
    bd["S/R"] = {"s": sr_sc, "max": 15, "note": f"toRes={dist_res:.1f}% toSup={dist_sup:.1f}%"}
    sc += sr_sc

    htf_sc = 10 if (is_bull and price > ema50) or (not is_bull and price < ema50) else 0
    bd["HTF"] = {"s": htf_sc, "max": 10, "note": "price vs EMA50"}
    sc += htf_sc

    total = min(sc, 100)
    grade = "A" if total>=80 else ("B" if total>=65 else ("C" if total>=50 else "D"))
    return total >= min_score, total, grade, bd


# ── Tier 3: MTF Alignment ─────────────────────────────────────────────────────
def check_mtf_alignment(alert, symbol):
    timeframes = alert.get("timeframes", ["1h", "4h", "1d"])
    required   = alert.get("required_alignment", "mostly_bullish")
    min_bull   = alert.get("min_bullish", 2)
    min_bear   = alert.get("min_bearish", 2)
    lb_map     = {"1m":"5d","5m":"5d","15m":"30d","30m":"60d",
                  "1h":"60d","4h":"60d","1d":"180d","1wk":"3y"}
    tf_results = {}
    for tf in timeframes:
        hist = fetch_history(symbol, period=lb_map.get(tf,"90d"), interval=tf)
        if hist is None or len(hist) < 55:
            tf_results[tf] = {"trend": "unknown", "score": 0, "rsi": None}
            time.sleep(0.3)
            continue
        closes = list(hist["Close"].astype(float))
        price  = closes[-1]
        ema21  = _calc_ema(closes, 21)[-1]
        ema50  = _calc_ema(closes, 50)[-1]
        rsi_l  = _calc_rsi(closes, 14)
        rsi    = next((r for r in reversed(rsi_l) if r is not None), 50)
        sc = 0
        sc += 1 if (ema21 and price > ema21) else -1
        sc += 1 if (ema21 and ema50 and ema21 > ema50) else -1
        sc += 1 if rsi > 50 else -1
        sc += 1 if (len(closes) >= 6 and price > closes[-6]) else -1
        trend = ("strong_bullish" if sc >= 3 else "bullish" if sc >= 1 else
                 "strong_bearish" if sc <= -3 else "bearish" if sc <= -1 else "neutral")
        tf_results[tf] = {"trend": trend, "score": sc, "rsi": round(rsi, 1)}
        time.sleep(0.4)

    bull_count = sum(1 for d in tf_results.values() if "bullish" in d["trend"])
    bear_count = sum(1 for d in tf_results.values() if "bearish" in d["trend"])
    total      = len(timeframes)

    if bull_count == total:     overall = "strong_bullish_all"
    elif bull_count >= total*0.75: overall = "mostly_bullish"
    elif bear_count == total:   overall = "strong_bearish_all"
    elif bear_count >= total*0.75: overall = "mostly_bearish"
    elif bull_count > bear_count: overall = "leaning_bullish"
    elif bear_count > bull_count: overall = "leaning_bearish"
    else:                       overall = "mixed"

    if required in ("bullish","mostly_bullish","leaning_bullish"):
        triggered = bull_count >= min_bull
    elif required in ("bearish","mostly_bearish","leaning_bearish"):
        triggered = bear_count >= min_bear
    elif required == "strong_bullish_all": triggered = overall == "strong_bullish_all"
    elif required == "strong_bearish_all": triggered = overall == "strong_bearish_all"
    else: triggered = overall == required

    return triggered, {"timeframes": tf_results, "overall": overall,
                       "bull_count": bull_count, "bear_count": bear_count, "total": total}


# ── Tier 3: MA Death Cross (SELL only) ───────────────────────────────────────
def check_ma_death_cross(symbol, fast_p=9, slow_p=21):
    hist = fetch_history(symbol, period="180d", interval="1d")
    if hist is None or len(hist) < slow_p * 2:
        return False, None, None
    closes = list(hist["Close"].astype(float))
    fast_l = _calc_ema(closes, fast_p)
    slow_l = _calc_ema(closes, slow_p)
    pairs  = [(f, s) for f, s in zip(fast_l, slow_l) if f and s]
    if len(pairs) < 2:
        return False, None, None
    cf, cs = pairs[-1]
    pf, ps = pairs[-2]
    return pf >= ps and cf < cs, round(cf, 4), round(cs, 4)


# ══════════════════════════════════════════════════════════════════════════════
#  GATE LAYER 1 — MACRO CONTEXT
# ══════════════════════════════════════════════════════════════════════════════

def get_macro_context():
    market_down = False
    btc_down    = False
    spy_chg     = 0.0
    btc_chg     = 0.0
    try:
        spy_q = fetch_quote("SPY")
        if spy_q:
            spy_chg     = spy_q.get("change_pct", 0)
            market_down = spy_chg < -1.0
            print(f"[Macro] SPY  {spy_chg:+.2f}%  {'DOWN ⚠️' if market_down else 'OK'}")
    except Exception:
        pass
    try:
        btc_q = fetch_quote("BTC-USD")
        if btc_q:
            btc_chg  = btc_q.get("change_pct", 0)
            btc_down = btc_chg < -3.0
            print(f"[Macro] BTC  {btc_chg:+.2f}%  {'CRASH ⚠️' if btc_down else 'OK'}")
    except Exception:
        pass
    return market_down, btc_down, spy_chg, btc_chg


# ══════════════════════════════════════════════════════════════════════════════
#  POSITION SIZE CALCULATOR  (account_size default = 100 USD)
# ══════════════════════════════════════════════════════════════════════════════

def calc_position_size(pos_cfg, symbol, entry_price=None):
    """
    คำนวณ position size จาก budget $100/หุ้น
    รองรับ fractional shares (crypto/ETF) และ whole shares (หุ้นทั่วไป)
    """
    account    = pos_cfg.get("account_size", 100)   # ← default $100
    risk_pct   = pos_cfg.get("risk_pct",    2.0)
    stop_pct   = pos_cfg.get("stop_pct",    None)
    target_pct = pos_cfg.get("target_pct",  None)

    if entry_price is None:
        q = fetch_quote(symbol)
        entry_price = q["price"] if q else 0
    if entry_price <= 0:
        return None

    atr_val = None
    if stop_pct is None:
        hist = fetch_history(symbol, period="90d", interval="1d")
        if hist is not None and len(hist) >= 16:
            highs  = list(hist["High"].astype(float))
            lows   = list(hist["Low"].astype(float))
            closes = list(hist["Close"].astype(float))
            atr_val = _calc_atr(highs, lows, closes, 14)
            if atr_val:
                raw_stop_pct = (atr_val * 2 / entry_price) * 100
                stop_pct     = min(raw_stop_pct, 15.0)
            else:
                stop_pct = 5.0
        else:
            stop_pct = 5.0

    stop_price  = entry_price * (1 - stop_pct / 100)
    risk_per_sh = entry_price - stop_price

    # คำนวณจำนวนหุ้นจาก budget $100
    shares_frac = account / entry_price
    shares_int  = math.floor(shares_frac)
    is_frac     = shares_int == 0   # ราคาสูงกว่า $100 → ต้อง fractional
    disp_shares = round(shares_frac, 6) if is_frac else shares_int
    pos_value   = round(disp_shares * entry_price, 2)
    pos_pct     = round(pos_value / account * 100, 1) if account > 0 else 0
    actual_risk = round(disp_shares * risk_per_sh, 2)
    risk_amount = round(account * risk_pct / 100, 2)

    result = {
        "entry":         round(entry_price, 4),
        "stop":          round(stop_price,  4),
        "stop_pct":      round(stop_pct,    2),
        "shares":        disp_shares,
        "shares_int":    shares_int,
        "shares_frac":   round(shares_frac, 6),
        "is_fractional": is_frac,
        "pos_value":     pos_value,
        "pos_pct":       pos_pct,
        "risk_amount":   risk_amount,
        "actual_risk":   actual_risk,
        "risk_per_sh":   round(risk_per_sh, 4),
        "risk_pct":      risk_pct,
        "account":       account,
        "atr":           round(atr_val, 4) if atr_val else None,
    }

    if target_pct:
        tp = entry_price * (1 + target_pct / 100)
        rr = target_pct / stop_pct if stop_pct > 0 else 0
        profit_usd = round(disp_shares * (tp - entry_price), 2)
        result["target"]     = round(tp, 4)
        result["target_pct"] = target_pct
        result["rr_ratio"]   = round(rr, 2)
        result["target_usd"] = profit_usd

    return result


# ══════════════════════════════════════════════════════════════════════════════
#  CONFIRMATION WINDOW
# ══════════════════════════════════════════════════════════════════════════════

def check_confirmation_window(sym_state, alert_id, required_hits=1):
    hits = sym_state.get(f"_confirm_{alert_id}", 0) + 1
    return hits >= required_hits, hits


def save_confirmation_hit(state, symbol, alert_id, hits):
    state.setdefault(symbol, {})[f"_confirm_{alert_id}"] = hits


def reset_confirmation(state, symbol, alert_id):
    state.get(symbol, {}).pop(f"_confirm_{alert_id}", None)


# ══════════════════════════════════════════════════════════════════════════════
#  MESSAGE BUILDERS
# ══════════════════════════════════════════════════════════════════════════════

def _header(stock, quote, emoji="🔔"):
    symbol = stock["symbol"]
    name   = stock["name"]
    price  = quote["price"]
    pct    = quote["change_pct"]
    arrow  = "📈" if pct >= 0 else "📉"
    sign   = "+" if pct >= 0 else ""
    tv     = f"https://www.tradingview.com/chart/?symbol={symbol}"
    return emoji, symbol, name, price, pct, arrow, sign, tv


def _pos_block(pos):
    """สร้างบล็อก position size สำหรับแนบท้าย message"""
    if not pos:
        return []
    is_frac  = pos.get("is_fractional", False)
    sh_str   = f"{pos['shares']:.6f} หุ้น (fractional)" if is_frac else f"{int(pos['shares']):,} หุ้น"
    lines    = [
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"📦 Position (งบ ${pos['account']:.0f}):",
        f"  • ซื้อ: <b>{sh_str}</b>",
        f"  • ใช้เงิน: <b>${pos['pos_value']:,.2f}</b>",
        f"  🛑 Stop: <b>${pos['stop']:.4f}</b>  (-{pos['stop_pct']:.1f}%)",
    ]
    if pos.get("atr"):
        lines.append(f"  • ATR(14): ${pos['atr']:.4f}")
    if pos.get("target"):
        lines.append(f"  🎯 Target: <b>${pos['target']:.4f}</b>  (+{pos['target_pct']:.1f}%)  R:R=1:{pos['rr_ratio']:.1f}")
        if pos.get("target_usd"):
            lines.append(f"  • กำไรถ้าถึง Target: ~+${pos['target_usd']:.2f}")
    lines.append(f"  ⚠️ เสี่ยงขาดทุน: -${pos['actual_risk']:.2f} ถ้า SL โดน")
    return lines


def build_buy_message(stock, quote, alert_type, detail, pos):
    emoji, symbol, name, price, pct, arrow, sign, tv = _header(stock, quote, "🚀")

    type_labels = {
        "rsi":              "RSI Oversold",
        "ma_crossover":     "MA Golden Cross",
        "alert_score":      "Confidence Score",
        "mtf_alignment":    "MTF Alignment",
        "volume_spike":     "Volume Spike",
        "percent_change":   "Price Surge",
        "support_resistance": "Breakout",
        "price_target":     "Price Target Hit",
    }
    label = type_labels.get(alert_type, alert_type.upper())

    lines = [
        f"🚀 <b>BUY SIGNAL: {symbol}</b> ({name})",
        f"⚡ สัญญาณ: <b>{label}</b>",
        "",
        f"💰 ราคา: <b>${price:.4f}</b>  {arrow} {sign}{pct:.2f}%",
        f"📋 {detail}",
    ]
    lines += _pos_block(pos)
    lines += [
        "",
        "📌 <b>ทำตามนี้:</b>",
        f"  1️⃣ เข้าซื้อที่ราคาใกล้ <b>${price:.4f}</b>",
        f"  2️⃣ ตั้ง Stop Loss ทันทีที่ <b>${pos['stop']:.4f}</b>" if pos else "  2️⃣ ตั้ง Stop Loss ทันทีหลังซื้อ",
        "  3️⃣ ไม่ all-in — ใช้ขนาด position ข้างบน",
        "  ❌ ถ้า SL โดน → ออกทันที ไม่รอ",
        "",
        f"📊 <a href='{tv}'>TradingView</a>",
        f"🕐 {now_bkk_str()}",
    ]
    return "\n".join(lines)


def build_sell_message(stock, quote, reason, detail=""):
    emoji, symbol, name, price, pct, arrow, sign, tv = _header(stock, quote, "🛑")
    reason_th = {
        "sl_break":    "🛑 หลุด Stop Loss / แนวรับสำคัญ",
        "death_cross": "💀 Death Cross — EMA9 ตัดลงใต้ EMA21",
        "pct_drop":    f"📉 ราคาลง {abs(pct):.1f}% วันเดียว",
        "score_bear":  "🔴 Confidence Score ขาลงสูง",
    }.get(reason, reason)

    lines = [
        f"🛑 <b>SELL SIGNAL: {symbol}</b> ({name})",
        "",
        f"⚡ {reason_th}",
        f"💰 ราคา: <b>${price:.4f}</b>  {arrow} {sign}{pct:.2f}%",
    ]
    if detail:
        lines.append(f"📋 {detail}")
    lines += [
        "",
        "📌 <b>สิ่งที่ควรทำ:</b>",
        "  1️⃣ ขายออกทันที — อย่ารอ",
        "  2️⃣ อย่า average down",
        "  3️⃣ รอ BUY signal ใหม่ก่อน re-entry",
        "",
        f"📊 <a href='{tv}'>TradingView</a>",
        f"🕐 {now_bkk_str()}",
    ]
    return "\n".join(lines)


def build_info_message(stock, quote, alert_type, detail):
    """Message สำหรับ info alerts (volume watch, news, etc.)"""
    emoji, symbol, name, price, pct, arrow, sign, tv = _header(stock, quote, "📢")
    lines = [
        f"📢 <b>INFO: {symbol}</b> ({name})",
        f"🏷️ {alert_type.replace('_',' ').upper()}",
        "",
        f"💰 ราคา: <b>${price:.4f}</b>  {arrow} {sign}{pct:.2f}%",
        f"📋 {detail}",
        "",
        f"📊 <a href='{tv}'>TradingView</a>",
        f"🕐 {now_bkk_str()}",
    ]
    return "\n".join(lines)


def build_daily_summary(watchlist, quotes_cache):
    gainers = sorted(
        [(s["symbol"], quotes_cache[s["symbol"]]["price"], quotes_cache[s["symbol"]]["change_pct"])
         for s in watchlist if s["symbol"] in quotes_cache and quotes_cache[s["symbol"]]["change_pct"] > 0],
        key=lambda x: -x[2],
    )
    losers = sorted(
        [(s["symbol"], quotes_cache[s["symbol"]]["price"], quotes_cache[s["symbol"]]["change_pct"])
         for s in watchlist if s["symbol"] in quotes_cache and quotes_cache[s["symbol"]]["change_pct"] < 0],
        key=lambda x: x[2],
    )
    lines = [
        "<b>📊 สรุปประจำวัน — Stock Alert Pro v3.1</b>",
        f"🕐 {now_bkk_str()}", "",
        f"ขึ้น: {len(gainers)} ตัว  |  ลง: {len(losers)} ตัว  |  ดูอยู่: {len(watchlist)} ตัว",
        "", "━━━━━━━━━━━━━━━━━━━━━━━━━",
        "<b>🔥 ขึ้นแรงสุด 5 ตัว:</b>",
    ]
    for sym, p, chg in gainers[:5]:
        lines.append(f"  📈 <b>{sym}</b> ${p:.2f}  +{chg:.1f}%")
    lines += ["", "<b>💧 ลงแรงสุด 5 ตัว:</b>"]
    for sym, p, chg in losers[:5]:
        lines.append(f"  📉 <b>{sym}</b> ${p:.2f}  {chg:.1f}%")
    lines += [
        "", "━━━━━━━━━━━━━━━━━━━━━━━━━",
        "<b>📋 รายชื่อทั้งหมด:</b>",
    ]
    for stock in watchlist:
        sym = stock["symbol"]
        q   = quotes_cache.get(sym)
        if not q:
            lines.append(f"  • {sym} — ไม่มีข้อมูล")
            continue
        arr = "📈" if q["change_pct"] >= 0 else "📉"
        sgn = "+" if q["change_pct"] >= 0 else ""
        lines.append(f"  • <b>{sym}</b> ${q['price']:.2f}  {arr} {sgn}{q['change_pct']:.1f}%")
    lines += [
        "", "━━━━━━━━━━━━━━━━━━━━━━━━━",
        "💡 กฎสำคัญ: ตั้ง Stop Loss ทุกครั้ง | งบ $100/หุ้น",
        f"🤖 Stock Alert Pro v3.1  •  {now_bkk_str()}",
    ]
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN — Tiered Alert Orchestration
#
#  SELL alerts  → Tier 1, ตรวจทุกรอบ (fast response)
#  BUY  alerts  → Tier 1+2+3 แยกตาม type และ cooldown ของแต่ละ alert
#
#  BUY gate logic (ต้องผ่านก่อน fire):
#    1. Macro suppress (SPY < -1% หรือ BTC < -3%)
#    2. ไม่มี open position สำหรับหุ้นตัวนั้น
#    3. Cooldown ยังไม่หมด
# ══════════════════════════════════════════════════════════════════════════════

BTC_LINKED = {"RIOT", "MARA", "CLSK", "HUT", "BITF", "COIN", "MSTR"}


def main():
    config    = load_json(WATCHLIST_PATH, {})
    settings  = config.get("settings", {})
    watchlist = config.get("watchlist", [])

    token   = os.environ.get(settings.get("telegram_bot_token_env", "TELEGRAM_BOT_TOKEN"), "")
    chat_id = os.environ.get(settings.get("telegram_chat_id_env",   "TELEGRAM_CHAT_ID"),   "")

    if not token or not chat_id:
        print("ERROR: TELEGRAM_BOT_TOKEN หรือ TELEGRAM_CHAT_ID ไม่ได้ตั้งค่า")
        sys.exit(1)

    default_cooldown = settings.get("cooldown_minutes", 60)
    state            = load_json(STATE_PATH, {})
    log              = load_json(LOG_PATH,   [])
    quotes_cache     = {}
    fired_count      = 0

    # ── Macro Gate ────────────────────────────────────────────────────
    print(f"\n[{now_str()}] ── MACRO CONTEXT CHECK ──")
    market_down, btc_down, spy_chg, btc_chg = get_macro_context()

    print(f"\n[{now_str()}] เริ่ม alert check — {len(watchlist)} symbols")

    for stock in watchlist:
        if not stock.get("enabled", True):
            continue

        symbol    = stock["symbol"]
        name      = stock.get("name", symbol)
        pos_cfg   = stock.get("position_alert", {"account_size": 100, "risk_pct": 2.0})
        confirm_n = stock.get("confirm_hits", 1)

        print(f"\n{'─'*60}")
        print(f"[{symbol}] {name}")

        quote = fetch_quote(symbol)
        if quote is None:
            print(f"  [{symbol}] ข้ามเนื่องจากไม่มีข้อมูล")
            continue

        quotes_cache[symbol] = quote
        print(f"  Price=${quote['price']:.4f}  Chg={quote['change_pct']:+.2f}%  Vol={quote['volume']:.0f}")

        sym_state    = state.get(symbol, {})
        price        = quote["price"]

        # ── Re-entry cooldown (แทน open_position suppress ถาวร) ─────
        # ไม่ block BUY ตลอดไปแค่เพราะเคย BUY มาก่อน — ให้โอกาส entry ใหม่
        # ได้ถ้าผ่านไปนานพอ (re_entry_cooldown_minutes ตั้งค่าได้ต่อหุ้น)
        reentry_cd   = stock.get("re_entry_cooldown_minutes", 240)  # default 4 ชม.
        last_buy_at  = sym_state.get("last_buy_at", "")
        in_reentry_cd = bool(last_buy_at) and minutes_since(last_buy_at) < reentry_cd

        # ── Sell cooldown — เพิ่ง SELL ไปไม่นาน ห้าม BUY ซ้ำเร็วเกินไป ──
        sell_cd      = stock.get("post_sell_cooldown_minutes", 120)  # default 2 ชม.
        last_sell_at = sym_state.get("last_sell_at", "")
        in_sell_cd   = bool(last_sell_at) and minutes_since(last_sell_at) < sell_cd

        # ── Price-drop filter — ลงหนักวันนี้ ห้าม BUY แม้ signal ผ่าน ──
        drop_threshold = stock.get("buy_suppress_drop_pct", 3.0)
        price_dropping  = quote["change_pct"] <= -drop_threshold

        # ── Macro suppress for BUY ──────────────────────────────────
        suppress_buy    = False
        suppress_reason = ""
        if market_down and not in_reentry_cd:
            suppress_buy    = True
            suppress_reason = f"SPY ลง {spy_chg:.1f}%"
        if btc_down and symbol in BTC_LINKED and not in_reentry_cd:
            suppress_buy    = True
            suppress_reason = f"BTC ลง {btc_chg:.1f}%"
        if in_sell_cd:
            suppress_buy    = True
            suppress_reason = f"เพิ่ง SELL ไป {minutes_since(last_sell_at):.0f} นาทีที่แล้ว (cooldown {sell_cd}m)"
        if price_dropping:
            suppress_buy    = True
            suppress_reason = f"ราคาลง {quote['change_pct']:.1f}% วันนี้ (เกิน -{drop_threshold}%)"
        if suppress_buy:
            print(f"  [Suppress BUY] {suppress_reason}")

        # ════════════════════════════════════════════════════════════
        #  PROCESS EACH ALERT IN WATCHLIST
        # ════════════════════════════════════════════════════════════
        for alert in stock.get("alerts", []):
            if not alert.get("enabled", True):
                continue

            alert_id = alert["id"]
            atype    = alert["type"]
            action   = alert.get("action", "")
            cooldown = alert.get("cooldown_minutes", default_cooldown)

            # ── Cooldown check ─────────────────────────────────────
            last_fired = sym_state.get(alert_id, {}).get("last_fired", "")
            if last_fired and minutes_since(last_fired) < cooldown:
                rem = cooldown - minutes_since(last_fired)
                print(f"  [{alert_id}] cooldown {rem:.0f}m")
                continue

            # ── BUY suppression (macro / sell-cooldown / price-drop) ─
            if action == "BUY" and suppress_buy:
                print(f"  [{alert_id}] suppressed: {suppress_reason}")
                continue
            # ── Re-entry cooldown เฉพาะ BUY (ไม่บล็อกถาวรเหมือนเดิม) ──
            if action == "BUY" and in_reentry_cd:
                rem = reentry_cd - minutes_since(last_buy_at)
                print(f"  [{alert_id}] re-entry cooldown {rem:.0f}m (BUY ล่าสุด {minutes_since(last_buy_at):.0f}m ที่แล้ว)")
                continue

            triggered = False
            msg       = None
            tval      = 0

            # ════════════════════════════════════════════════════════
            #  TIER 1 — FAST CHECKS (ไม่ต้องดึง history เพิ่ม)
            # ════════════════════════════════════════════════════════

            if atype == "volume_spike":
                triggered, tval = check_volume_spike(alert, quote)
                if triggered:
                    detail = f"Volume {tval:.1f}x ค่าเฉลี่ย (เงื่อนไข {alert.get('multiplier',2)}x)"
                    if action == "BUY":
                        pos = calc_position_size(pos_cfg, symbol, price)
                        msg = build_buy_message(stock, quote, atype, detail, pos)
                    elif action == "SELL":
                        msg = build_sell_message(stock, quote, "vol_alarm", detail)
                    else:
                        msg = build_info_message(stock, quote, atype, detail)

            elif atype == "percent_change":
                triggered, tval = check_percent_change(alert, quote)
                if triggered:
                    detail = f"เปลี่ยนแปลง {tval:+.2f}% (เงื่อนไข {alert.get('threshold_pct',5)}%)"
                    if action == "BUY":
                        pos = calc_position_size(pos_cfg, symbol, price)
                        msg = build_buy_message(stock, quote, atype, detail, pos)
                    elif action == "SELL":
                        msg = build_sell_message(stock, quote, "pct_drop", detail)
                    else:
                        msg = build_info_message(stock, quote, atype, detail)

            elif atype == "support_resistance":
                triggered, tval, level = check_support_resistance(alert, quote, symbol)
                if triggered:
                    lvl_str = f"${level:.4f}" if level else "auto"
                    detail  = f"ราคา {alert.get('direction','').replace('_',' ')} แนวระดับ {lvl_str}"
                    if action == "BUY":
                        pos = calc_position_size(pos_cfg, symbol, price)
                        msg = build_buy_message(stock, quote, atype, detail, pos)
                    elif action == "SELL":
                        msg = build_sell_message(stock, quote, "sl_break", detail)
                    else:
                        msg = build_info_message(stock, quote, atype, detail)

            elif atype == "price_target":
                triggered, tval = check_price_target(alert, quote)
                if triggered:
                    detail = f"ราคา ${price:.4f} ถึงเป้า ${alert.get('target_price',0):.4f}"
                    if action == "BUY":
                        pos = calc_position_size(pos_cfg, symbol, price)
                        msg = build_buy_message(stock, quote, atype, detail, pos)
                    elif action == "SELL":
                        msg = build_sell_message(stock, quote, "target_hit", detail)
                    else:
                        msg = build_info_message(stock, quote, atype, detail)

            # ════════════════════════════════════════════════════════
            #  TIER 1 — RSI (ดึง history แต่เร็ว — 1 API call)
            # ════════════════════════════════════════════════════════

            elif atype == "rsi":
                triggered, rsi, prev_rsi, rsi_price = check_rsi(alert, symbol)
                if triggered and rsi is not None:
                    tval   = rsi
                    cond   = alert.get("condition", "oversold")
                    detail = f"RSI({alert.get('period',14)}) = {rsi:.1f}  (ก่อนหน้า {prev_rsi:.1f})  [{cond}]"
                    if action == "BUY":
                        # BUY confirmation window
                        ready, hit = check_confirmation_window(sym_state, alert_id, confirm_n)
                        save_confirmation_hit(state, symbol, alert_id, hit)
                        if ready:
                            pos = calc_position_size(pos_cfg, symbol, price)
                            msg = build_buy_message(stock, quote, atype, detail, pos)
                            reset_confirmation(state, symbol, alert_id)
                        else:
                            print(f"  [{alert_id}] RSI confirm {hit}/{confirm_n} รอบ")
                            triggered = False
                    elif action == "SELL":
                        msg = build_sell_message(stock, quote, "score_bear", detail)
                    else:
                        msg = build_info_message(stock, quote, atype, detail)

            # ════════════════════════════════════════════════════════
            #  TIER 2 — MEDIUM (2-3 API calls)
            # ════════════════════════════════════════════════════════

            elif atype == "ma_crossover":
                cond = alert.get("condition", "golden_cross")
                if cond == "death_cross":
                    triggered, fast_ma, slow_ma = check_ma_death_cross(
                        symbol,
                        alert.get("fast_period", 9),
                        alert.get("slow_period", 21)
                    )
                    if triggered:
                        detail = f"EMA{alert.get('fast_period',9)}={fast_ma}  ตัดลงใต้  EMA{alert.get('slow_period',21)}={slow_ma}"
                        msg = build_sell_message(stock, quote, "death_cross", detail)
                else:
                    triggered, fast_ma, slow_ma, ma_price, gap_pct = check_ma_crossover(alert, symbol)
                    if triggered and fast_ma is not None:
                        tval   = gap_pct or 0
                        fast_p = alert.get("fast_period", 9)
                        slow_p = alert.get("slow_period", 21)
                        mtype  = alert.get("ma_type", "EMA")
                        detail = f"{mtype}{fast_p}={fast_ma:.4f}  ตัดขึ้นเหนือ  {mtype}{slow_p}={slow_ma:.4f}  gap={gap_pct:+.2f}%"
                        if action == "BUY":
                            ready, hit = check_confirmation_window(sym_state, alert_id, confirm_n)
                            save_confirmation_hit(state, symbol, alert_id, hit)
                            if ready:
                                pos = calc_position_size(pos_cfg, symbol, price)
                                msg = build_buy_message(stock, quote, atype, detail, pos)
                                reset_confirmation(state, symbol, alert_id)
                            else:
                                print(f"  [{alert_id}] MA confirm {hit}/{confirm_n} รอบ")
                                triggered = False
                        else:
                            msg = build_info_message(stock, quote, atype, detail)

            elif atype == "alert_score":
                triggered, score, grade, bd = check_alert_score(alert, symbol)
                if triggered:
                    tval   = score
                    detail = f"Score {score}/100 เกรด {grade}  ({alert.get('direction','bullish')})"
                    if action == "BUY":
                        ready, hit = check_confirmation_window(sym_state, alert_id, confirm_n)
                        save_confirmation_hit(state, symbol, alert_id, hit)
                        if ready:
                            pos = calc_position_size(pos_cfg, symbol, price)
                            msg = build_buy_message(stock, quote, atype, detail, pos)
                            reset_confirmation(state, symbol, alert_id)
                        else:
                            print(f"  [{alert_id}] Score confirm {hit}/{confirm_n} รอบ")
                            triggered = False
                    elif action == "SELL":
                        msg = build_sell_message(stock, quote, "score_bear", detail)
                    else:
                        msg = build_info_message(stock, quote, atype, detail)

            # ════════════════════════════════════════════════════════
            #  TIER 3 — SLOW (MTF — 3+ API calls, cooldown ยาว)
            # ════════════════════════════════════════════════════════

            elif atype == "mtf_alignment":
                triggered, mtf_result = check_mtf_alignment(alert, symbol)
                if triggered:
                    tval    = mtf_result.get("bull_count", 0)
                    overall = mtf_result.get("overall", "")
                    detail  = f"MTF: {overall}  bull={tval}/{mtf_result.get('total',3)}"
                    if action == "BUY":
                        ready, hit = check_confirmation_window(sym_state, alert_id, confirm_n)
                        save_confirmation_hit(state, symbol, alert_id, hit)
                        if ready:
                            pos = calc_position_size(pos_cfg, symbol, price)
                            msg = build_buy_message(stock, quote, atype, detail, pos)
                            reset_confirmation(state, symbol, alert_id)
                        else:
                            print(f"  [{alert_id}] MTF confirm {hit}/{confirm_n} รอบ")
                            triggered = False
                    else:
                        msg = build_info_message(stock, quote, atype, detail)

            # ── Skip unknown types ──────────────────────────────────
            else:
                print(f"  [{alert_id}] ❓ type ไม่รู้จัก: {atype}")
                continue

            if not triggered:
                print(f"  [{alert_id}] ไม่ trigger ({atype})")
                continue
            if msg is None:
                print(f"  [{alert_id}] triggered แต่ไม่มี message")
                continue

            print(f"  [{alert_id}] ✅ TRIGGERED! ส่ง Telegram...")
            success = send_telegram(token, chat_id, msg)

            if success:
                state.setdefault(symbol, {})[alert_id] = {"last_fired": now_str()}

                # ── Track BUY/SELL timestamps (สำหรับ re-entry + sell cooldown) ──
                if action == "BUY":
                    state[symbol]["last_buy_at"]  = now_str()
                    state[symbol]["open_entry"]   = price
                    state[symbol]["open_time"]    = now_str()
                elif action == "SELL":
                    state[symbol]["last_sell_at"] = now_str()


                log.append({
                    "timestamp":  now_str(),
                    "symbol":     symbol,
                    "alert_id":   alert_id,
                    "type":       atype,
                    "action":     action,
                    "price":      price,
                    "change_pct": quote["change_pct"],
                    "value":      tval,
                })
                fired_count += 1
                print(f"  [{alert_id}] ✅ ส่งสำเร็จ")
            else:
                print(f"  [{alert_id}] ❌ ส่งไม่สำเร็จ")

        time.sleep(0.5)

    # ── Daily Summary ─────────────────────────────────────────────────
    summary_hour  = settings.get("daily_summary_hour_utc", 1)
    current_hour  = now_utc().hour
    summary_state = state.get("__daily_summary__", {})
    last_summary  = summary_state.get("last_sent", "")
    today_str     = now_utc().strftime("%Y-%m-%d")

    if (current_hour == summary_hour
            and (not last_summary or not last_summary.startswith(today_str))
            and quotes_cache):
        print("\n[Daily Summary] กำลังส่ง...")
        msg     = build_daily_summary(watchlist, quotes_cache)
        success = send_telegram(token, chat_id, msg)
        if success:
            state["__daily_summary__"] = {"last_sent": now_str()}
            print("[Daily Summary] ✅ ส่งสำเร็จ")

    save_json(STATE_PATH, state)
    save_json(LOG_PATH, log[-500:])
    print(f"\n[{now_str()}] เสร็จสิ้น — fire {fired_count} alert(s)")


if __name__ == "__main__":
    main()
