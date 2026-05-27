#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════╗
║  Stock Alert Engine PRO — GitHub Actions Edition                    ║
║  Version 2.0 — รวม 12 alert types ในไฟล์เดียว                     ║
║                                                                      ║
║  Alert Types ที่รองรับ:                                             ║
║    เดิม:  price_target, percent_change, volume_spike,               ║
║           support_resistance                                         ║
║    ใหม่:  rsi, ma_crossover, candle_pattern, news_sentiment,        ║
║           position_size, mtf_alignment, alert_score, backtest_check ║
║                                                                      ║
║  Run: python3 alert_engine.py                                        ║
║  Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID                          ║
╚══════════════════════════════════════════════════════════════════════╝
"""

import json
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
BASE_DIR        = Path(__file__).parent
WATCHLIST_PATH  = BASE_DIR / "watchlist.json"
STATE_PATH      = BASE_DIR / "state.json"
LOG_PATH        = BASE_DIR / "alert_log.json"

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
    """เวลาไทย UTC+7"""
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
    """ส่ง Telegram message พร้อม retry 2 ครั้ง"""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
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
#  PRICE FETCHER (เดิม — ใช้สำหรับทุก module)
# ══════════════════════════════════════════════════════════════════════════════

def fetch_quote(symbol):
    """
    ดึงราคา + volume ปัจจุบัน
    Returns: {price, prev_close, change_pct, volume, avg_volume} หรือ None
    """
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.fast_info

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

        hist_1d    = ticker.history(period="1d", interval="1m")
        today_vol  = float(hist_1d["Volume"].sum()) if not hist_1d.empty else 0

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
    """ดึง OHLCV history สำหรับ technical indicators"""
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
#  TECHNICAL HELPERS (ใช้ร่วมกันทุก module)
# ══════════════════════════════════════════════════════════════════════════════

def _calc_ema(prices, period):
    """คำนวณ EMA — คืน list ขนาดเท่ากับ prices"""
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
    """คำนวณ RSI — คืน list ขนาดเท่ากับ closes"""
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
    """คำนวณ ATR ล่าสุด"""
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


def _calc_entry_sl_tp(symbol, price, direction="bull", atr_sl_mult=1.5, rr1=1.5, rr2=3.0):
    """
    คำนวณ Entry / SL / TP1 / TP2 จาก ATR + Swing High/Low
    direction: "bull" = แนวโน้มขาขึ้น (ซื้อ), "bear" = แนวโน้มขาลง
    คืน dict หรือ None ถ้าดึงข้อมูลไม่ได้
    """
    try:
        hist = fetch_history(symbol, period="60d", interval="1d")
        if hist is None or len(hist) < 20:
            return None

        highs  = list(hist["High"].astype(float))
        lows   = list(hist["Low"].astype(float))
        closes = list(hist["Close"].astype(float))

        atr = _calc_atr(highs, lows, closes, 14)
        if not atr or atr <= 0:
            return None

        # Swing Low/High จาก 10 แท่งล่าสุด
        recent_lows  = lows[-10:]
        recent_highs = highs[-10:]
        swing_low    = min(recent_lows)
        swing_high   = max(recent_highs)

        if direction == "bull":
            entry    = round(price, 4)
            sl_atr   = round(price - atr * atr_sl_mult, 4)
            sl_swing = round(swing_low * 0.995, 4)
            # ถ้า swing_low สูงกว่าราคาปัจจุบัน ให้ใช้ ATR เป็น fallback
            if sl_swing >= entry:
                sl = sl_atr
            else:
                sl = round(max(sl_atr, sl_swing), 4)
            risk = entry - sl
            if risk <= 0:
                # fallback: ใช้ ATR x 1 เป็น SL อย่างน้อย
                sl   = round(entry - atr, 4)
                risk = entry - sl
            if risk <= 0:
                return None
            tp1 = round(entry + risk * rr1, 4)
            tp2 = round(entry + risk * rr2, 4)
        else:
            entry    = round(price, 4)
            sl_atr   = round(price + atr * atr_sl_mult, 4)
            sl_swing = round(swing_high * 1.005, 4)
            # ถ้า swing_high ต่ำกว่าราคาปัจจุบัน ให้ใช้ ATR เป็น fallback
            if sl_swing <= entry:
                sl = sl_atr
            else:
                sl = round(min(sl_atr, sl_swing), 4)
            risk = sl - entry
            if risk <= 0:
                sl   = round(entry + atr, 4)
                risk = sl - entry
            if risk <= 0:
                return None
            tp1 = round(entry - risk * rr1, 4)
            tp2 = round(entry - risk * rr2, 4)

        sl_pct  = round(abs(entry - sl) / entry * 100, 2)
        tp1_pct = round(abs(tp1 - entry) / entry * 100, 2)
        tp2_pct = round(abs(tp2 - entry) / entry * 100, 2)

        return {
            "direction": direction,
            "entry":     entry,
            "sl":        sl,
            "sl_pct":    sl_pct,
            "tp1":       tp1,
            "tp1_pct":   tp1_pct,
            "tp2":       tp2,
            "tp2_pct":   tp2_pct,
            "atr":       round(atr, 4),
            "rr1":       round(rr1, 1),
            "rr2":       round(rr2, 1),
        }
    except Exception as e:
        print(f"  [_calc_entry_sl_tp] error: {e}")
        return None


def _format_trade_box(trade, direction="bull"):
    """สร้างบล็อกข้อมูลเทรด สำหรับแนบใน alert message"""
    if not trade:
        return []
    if direction == "bull":
        action_label = "🟢 <b>สัญญาณ: ควรซื้อ</b>"
        entry_label  = "📥 จุดเข้าซื้อ"
    else:
        action_label = "🔴 <b>สัญญาณ: ระวัง / หลีกเลี่ยงซื้อ</b>"
        entry_label  = "📍 ราคาอ้างอิง"
    return [
        "",
        "─────────────────────────",
        action_label,
        f"{entry_label}:  <b>${trade['entry']:.4f}</b>",
        f"🛑 จุดตัดขาดทุน (SL):  <b>${trade['sl']:.4f}</b>  (-{trade['sl_pct']:.2f}%)",
        f"🎯 เป้าหมาย 1 (TP1):  <b>${trade['tp1']:.4f}</b>  (+{trade['tp1_pct']:.2f}%)  อัตราส่วน 1:{trade['rr1']}",
        f"🏆 เป้าหมาย 2 (TP2):  <b>${trade['tp2']:.4f}</b>  (+{trade['tp2_pct']:.2f}%)  อัตราส่วน 1:{trade['rr2']}",
        f"📐 ATR(14): ${trade['atr']:.4f}",
        "─────────────────────────",
    ]


# ══════════════════════════════════════════════════════════════════════════════
#  MODULE 1 (เดิม) — PRICE TARGET
# ══════════════════════════════════════════════════════════════════════════════

def check_price_target(alert, quote):
    price     = quote["price"]
    target    = alert["target_price"]
    direction = alert.get("direction", "below_or_equal")
    if direction == "below_or_equal" and price <= target:
        return True, price
    if direction == "above_or_equal" and price >= target:
        return True, price
    return False, price


# ══════════════════════════════════════════════════════════════════════════════
#  MODULE 2 (เดิม) — PERCENT CHANGE
# ══════════════════════════════════════════════════════════════════════════════

def check_percent_change(alert, quote):
    pct       = quote["change_pct"]
    direction = alert.get("direction", "down")
    threshold = alert.get("threshold_pct", 5.0)
    if direction == "down" and pct <= -threshold:
        return True, pct
    if direction == "up"   and pct >= threshold:
        return True, pct
    return False, pct


# ══════════════════════════════════════════════════════════════════════════════
#  MODULE 3 (เดิม) — VOLUME SPIKE
# ══════════════════════════════════════════════════════════════════════════════

def check_volume_spike(alert, quote):
    vol  = quote["volume"]
    avg  = quote["avg_volume"]
    mult = alert.get("multiplier", 2.0)
    if avg > 0 and vol >= avg * mult:
        return True, vol / avg
    return False, 0


# ══════════════════════════════════════════════════════════════════════════════
#  MODULE 4 (เดิม) — SUPPORT / RESISTANCE
# ══════════════════════════════════════════════════════════════════════════════

def check_support_resistance(alert, quote):
    price     = quote["price"]
    level     = alert.get("level", 0)
    direction = alert.get("direction", "break_below")
    # ❌ ถ้า level == 0 แสดงว่ายังไม่ได้ตั้งค่า — ข้ามทันที
    if level <= 0:
        print(f"  [support_resistance] ข้าม: level={level} ยังไม่ได้ตั้งค่า (ตั้งใน dashboard ก่อน)")
        return False, price
    if direction == "break_below" and price < level:
        return True, price
    if direction == "break_above" and price > level:
        return True, price
    return False, price


# ══════════════════════════════════════════════════════════════════════════════
#  MODULE 5 (ใหม่) — RSI ALERT
# ══════════════════════════════════════════════════════════════════════════════
# alert fields:
#   condition: oversold | overbought | extreme_oversold | extreme_overbought
#              | below | above | turning_up | turning_down
#   oversold_level:  (default 30)
#   overbought_level: (default 70)
#   extreme_level:   (default 20 สำหรับ oversold, 80 สำหรับ overbought)
#   threshold:       (สำหรับ below/above)
#   period:          (default 14)
#   interval:        (default 1d)

def check_rsi(alert, symbol):
    """
    ดึงข้อมูลและตรวจ RSI condition
    Returns: (triggered, rsi_value, prev_rsi, price)
    """
    period   = alert.get("period", 14)
    interval = alert.get("interval", "1d")

    lookback = {"1m":"5d","5m":"5d","15m":"30d","30m":"60d",
                "1h":"60d","4h":"60d","1d":"90d","1wk":"2y"}.get(interval, "90d")

    hist = fetch_history(symbol, period=lookback, interval=interval)
    if hist is None or len(hist) < period + 2:
        print(f"  [{symbol}] RSI: ข้อมูลไม่พอ")
        return False, None, None, None

    closes   = list(hist["Close"].astype(float))
    rsi_list = _calc_rsi(closes, period)

    # กรอง None ออก
    valid = [(r, c) for r, c in zip(rsi_list, closes) if r is not None]
    if len(valid) < 2:
        return False, None, None, None

    curr_rsi, curr_price = valid[-1]
    prev_rsi, _          = valid[-2]

    condition       = alert.get("condition", "oversold")
    oversold_lvl    = alert.get("oversold_level", 30)
    overbought_lvl  = alert.get("overbought_level", 70)
    extreme_lvl     = alert.get("extreme_level", None)
    threshold       = alert.get("threshold", None)

    triggered = False
    if condition == "oversold":
        triggered = curr_rsi <= oversold_lvl
    elif condition == "overbought":
        triggered = curr_rsi >= overbought_lvl
    elif condition == "extreme_oversold":
        lvl       = extreme_lvl if extreme_lvl is not None else 20
        triggered = curr_rsi <= lvl
    elif condition == "extreme_overbought":
        lvl       = extreme_lvl if extreme_lvl is not None else 80
        triggered = curr_rsi >= lvl
    elif condition == "below" and threshold is not None:
        triggered = curr_rsi <= threshold
    elif condition == "above" and threshold is not None:
        triggered = curr_rsi >= threshold
    elif condition == "turning_up":
        triggered = curr_rsi > prev_rsi and curr_rsi < 40
    elif condition == "turning_down":
        triggered = curr_rsi < prev_rsi and curr_rsi > 60

    return triggered, round(curr_rsi, 2), round(prev_rsi, 2), curr_price


# ══════════════════════════════════════════════════════════════════════════════
#  MODULE 6 (ใหม่) — MA CROSSOVER
# ══════════════════════════════════════════════════════════════════════════════
# alert fields:
#   condition: golden_cross | death_cross | above_both | below_both
#              | trend_bullish | trend_bearish | gap_expanding
#   fast_period: (default 9)
#   slow_period: (default 21)
#   ma_type:    EMA | SMA  (default EMA)
#   interval:   (default 1d)

def check_ma_crossover(alert, symbol):
    """
    ตรวจ MA crossover condition
    Returns: (triggered, fast_ma, slow_ma, price, gap_pct)
    """
    fast_p   = alert.get("fast_period", 9)
    slow_p   = alert.get("slow_period", 21)
    ma_type  = alert.get("ma_type", "EMA").upper()
    interval = alert.get("interval", "1d")
    condition = alert.get("condition", "golden_cross")

    lookback = {"1m":"5d","5m":"5d","15m":"30d","30m":"60d",
                "1h":"60d","4h":"60d","1d":"180d","1wk":"3y"}.get(interval, "180d")

    hist = fetch_history(symbol, period=lookback, interval=interval)
    if hist is None or len(hist) < slow_p * 2:
        print(f"  [{symbol}] MA: ข้อมูลไม่พอ")
        return False, None, None, None, None

    closes = list(hist["Close"].astype(float))

    if ma_type == "EMA":
        fast_list = _calc_ema(closes, fast_p)
        slow_list = _calc_ema(closes, slow_p)
    else:
        # SMA
        def sma(prices, p):
            result = [None] * (p - 1)
            for i in range(p - 1, len(prices)):
                result.append(sum(prices[i - p + 1:i + 1]) / p)
            return result
        fast_list = sma(closes, fast_p)
        slow_list = sma(closes, slow_p)

    # จับคู่ที่ทั้งคู่ไม่ใช่ None
    pairs = [(f, s, c) for f, s, c in zip(fast_list, slow_list, closes)
             if f is not None and s is not None]
    if len(pairs) < 2:
        return False, None, None, None, None

    cf, cs, cp   = pairs[-1]
    pf, ps, _    = pairs[-2]
    gap_pct      = ((cf - cs) / cs * 100) if cs != 0 else 0
    prev_gap_pct = ((pf - ps) / ps * 100) if ps != 0 else 0

    triggered = False
    if condition == "golden_cross":
        triggered = pf <= ps and cf > cs
    elif condition == "death_cross":
        triggered = pf >= ps and cf < cs
    elif condition == "above_both":
        triggered = cp > cf and cp > cs
    elif condition == "below_both":
        triggered = cp < cf and cp < cs
    elif condition == "trend_bullish":
        triggered = cf > cs
    elif condition == "trend_bearish":
        triggered = cf < cs
    elif condition == "gap_expanding":
        triggered = abs(gap_pct) > abs(prev_gap_pct)

    return triggered, round(cf, 4), round(cs, 4), round(cp, 4), round(gap_pct, 3)


# ══════════════════════════════════════════════════════════════════════════════
#  MODULE 7 (ใหม่) — CANDLE PATTERN
# ══════════════════════════════════════════════════════════════════════════════
# alert fields:
#   patterns: list เช่น ["hammer", "bullish_engulfing"]  หรือ ["all"]
#   match_any: true (default) | false
#   interval:  (default 1d)

CANDLE_DESC_TH = {
    "doji":               "Doji — ตลาดลังเล อาจกลับตัว",
    "hammer":             "Hammer 🔨 — กลับตัวขึ้นจากขาลง",
    "inverted_hammer":    "Inverted Hammer — กลับตัวขึ้นที่ก้น",
    "shooting_star":      "Shooting Star ⭐ — กลับตัวลงจากขาขึ้น",
    "hanging_man":        "Hanging Man — ระวังกลับตัวลง",
    "bullish_engulfing":  "Bullish Engulfing 🟢 — แท่งเขียวกลืนแดง",
    "bearish_engulfing":  "Bearish Engulfing 🔴 — แท่งแดงกลืนเขียว",
    "three_white_soldiers":"Three White Soldiers 🎖️ — 3 เขียวติดกัน",
    "three_black_crows":  "Three Black Crows 🪶 — 3 แดงติดกัน",
    "marubozu_bullish":   "Bullish Marubozu 💚 — ซื้อแรง ไม่มีไส้",
    "marubozu_bearish":   "Bearish Marubozu 💔 — ขายแรง ไม่มีไส้",
    "spinning_top":       "Spinning Top — ลังเลสองทาง",
    "morning_star":       "Morning Star 🌅 — 3 แท่ง กลับตัวขึ้น",
    "evening_star":       "Evening Star 🌆 — 3 แท่ง กลับตัวลง",
}


def _detect_candle_patterns(candles):
    """ตรวจ patterns จาก list ของ candle dicts"""
    if len(candles) < 3:
        return {}
    c0, c1, c2 = candles[-1], candles[-2], candles[-3]

    def parts(c):
        o, h, l, cl = c["o"], c["h"], c["l"], c["c"]
        rng  = max(h - l, 0.0001)
        body = abs(cl - o)
        return {
            "o": o, "h": h, "l": l, "c": cl,
            "body": body, "body_pct": body / rng * 100,
            "upper_wick": h - max(o, cl),
            "lower_wick": min(o, cl) - l,
            "upper_pct": (h - max(o, cl)) / rng * 100,
            "lower_pct": (min(o, cl) - l)  / rng * 100,
            "range": rng,
            "bull": cl >= o,
        }

    p0, p1, p2 = parts(c0), parts(c1), parts(c2)

    return {
        "doji":               p0["body_pct"] < 10,
        "spinning_top":       10 <= p0["body_pct"] <= 30 and p0["upper_pct"] >= 20 and p0["lower_pct"] >= 20,
        "hammer":             p0["lower_wick"] >= p0["body"] * 2 and p0["upper_pct"] < 20 and p0["body_pct"] >= 10 and not p0["bull"],
        "inverted_hammer":    p0["upper_wick"] >= p0["body"] * 2 and p0["lower_pct"] < 20 and p0["body_pct"] >= 10,
        "shooting_star":      p0["upper_wick"] >= p0["body"] * 2 and p0["lower_pct"] < 20 and p0["body_pct"] >= 10 and p1["bull"],
        "hanging_man":        p0["lower_wick"] >= p0["body"] * 2 and p0["upper_pct"] < 20 and p0["body_pct"] >= 10 and p1["bull"],
        "marubozu_bullish":   p0["bull"]  and p0["body_pct"] >= 90,
        "marubozu_bearish":   not p0["bull"] and p0["body_pct"] >= 90,
        "bullish_engulfing":  not p1["bull"] and p0["bull"]  and c0["o"] < c1["c"] and c0["c"] > c1["o"] and p0["body"] > p1["body"],
        "bearish_engulfing":  p1["bull"]  and not p0["bull"] and c0["o"] > c1["c"] and c0["c"] < c1["o"] and p0["body"] > p1["body"],
        "three_white_soldiers": all([p2["bull"], p1["bull"], p0["bull"], c1["c"] > c2["c"], c0["c"] > c1["c"], p2["body_pct"] >= 50, p1["body_pct"] >= 50, p0["body_pct"] >= 50]),
        "three_black_crows":    all([not p2["bull"], not p1["bull"], not p0["bull"], c1["c"] < c2["c"], c0["c"] < c1["c"], p2["body_pct"] >= 50, p1["body_pct"] >= 50, p0["body_pct"] >= 50]),
        "morning_star":       not p2["bull"] and p2["body_pct"] >= 50 and p1["body_pct"] <= 30 and p0["bull"] and p0["body_pct"] >= 50 and c0["c"] > (c2["o"] + c2["c"]) / 2,
        "evening_star":       p2["bull"] and p2["body_pct"] >= 50 and p1["body_pct"] <= 30 and not p0["bull"] and p0["body_pct"] >= 50 and c0["c"] < (c2["o"] + c2["c"]) / 2,
    }


def check_candle_pattern(alert, symbol):
    """
    ตรวจ candle pattern
    Returns: (triggered, found_patterns_list, candle_info_dict)
    """
    interval      = alert.get("interval", "1d")
    target_pats   = alert.get("patterns", ["hammer", "bullish_engulfing"])
    match_any     = alert.get("match_any", True)
    want_all      = "all" in target_pats

    lookback = {"1m":"5d","5m":"5d","15m":"30d","30m":"60d",
                "1h":"60d","4h":"60d","1d":"90d","1wk":"2y"}.get(interval, "90d")

    hist = fetch_history(symbol, period=lookback, interval=interval)
    if hist is None or len(hist) < 5:
        print(f"  [{symbol}] Candle: ข้อมูลไม่พอ")
        return False, [], {}

    candles = [
        {"o": float(r["Open"]), "h": float(r["High"]),
         "l": float(r["Low"]),  "c": float(r["Close"])}
        for _, r in hist.tail(10).iterrows()
    ]

    all_p = _detect_candle_patterns(candles)

    if want_all:
        found = [p for p, v in all_p.items() if v]
    else:
        found = [p for p in target_pats if all_p.get(p, False)]

    triggered = len(found) > 0 if match_any else (len(found) == len(target_pats) and not want_all)
    c0 = candles[-1]
    info = {
        "price": c0["c"],
        "change_pct": ((c0["c"] - c0["o"]) / c0["o"] * 100) if c0["o"] > 0 else 0,
        "is_bullish": c0["c"] >= c0["o"],
    }
    return triggered, found, info


# ══════════════════════════════════════════════════════════════════════════════
#  MODULE 8 (ใหม่) — NEWS SENTIMENT (แปลไทย)
# ══════════════════════════════════════════════════════════════════════════════
# alert fields:
#   condition: any | positive | negative | strong_positive | strong_negative | high_volume
#   min_news:  (default 1)
#   hours_back: (default 24)
#   max_news:  (default 5)
#   translate: true | false (default true)

POS_KW = ["surge","soar","rally","gain","rise","jump","beat","record","profit","growth",
           "bullish","upgrade","buy","strong","positive","exceed","outperform","boost",
           "breakthrough","acquisition","partnership","dividend","buyback","expand"]
NEG_KW = ["plunge","crash","drop","fall","decline","loss","miss","weak","bearish",
           "downgrade","sell","warning","risk","cut","layoff","lawsuit","investigation",
           "fraud","bankruptcy","recall","fine","penalty","debt","concern","disappoint"]


def _translate_th(text):
    """แปลเป็นภาษาไทยผ่าน MyMemory API (ฟรี)"""
    if not text or len(text.strip()) < 3:
        return text
    try:
        encoded = urllib.parse.quote(text[:400])
        url = f"https://api.mymemory.translated.net/get?q={encoded}&langpair=en|th"
        req = urllib.request.Request(url, headers={"User-Agent": "StockAlertBot/2.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
            translated = data.get("responseData", {}).get("translatedText", "")
            if translated and "INVALID" not in translated.upper() and len(translated) > 3:
                return translated
    except Exception:
        pass
    return text


def _sentiment_score(text):
    t = text.lower()
    pos = sum(1 for k in POS_KW if k in t)
    neg = sum(1 for k in NEG_KW if k in t)
    return pos - neg


def check_news_sentiment(alert, symbol):
    """
    ดึงข่าว วิเคราะห์ sentiment แปลไทย
    Returns: (triggered, news_list)
    """
    condition  = alert.get("condition", "any")
    min_news   = alert.get("min_news", 1)
    hours_back = alert.get("hours_back", 24)
    max_news   = alert.get("max_news", 5)
    do_translate = alert.get("translate", True)

    try:
        ticker    = yf.Ticker(symbol)
        raw_news  = ticker.news or []
    except Exception as e:
        print(f"  [{symbol}] News fetch error: {e}")
        return False, []

    now_ts  = now_utc().timestamp()
    cutoff  = now_ts - hours_back * 3600 if hours_back > 0 else 0
    results = []

    for item in raw_news:
        content = item.get("content", item)
        title   = content.get("title") or item.get("title") or ""
        pub_ts_raw = (content.get("pubDate") or item.get("providerPublishTime") or
                      item.get("published_at") or 0)
        link      = (content.get("canonicalUrl", {}).get("url") or
                     item.get("link") or item.get("url") or "")
        publisher = (content.get("provider", {}).get("displayName") or
                     item.get("publisher") or "Yahoo Finance")
        summary   = content.get("summary") or item.get("summary") or ""

        if not title:
            continue

        # แปลง timestamp
        try:
            if isinstance(pub_ts_raw, str):
                pub_dt  = datetime.fromisoformat(pub_ts_raw.replace("Z", "+00:00"))
                pub_ts  = pub_dt.timestamp()
            else:
                pub_ts  = float(pub_ts_raw)
        except Exception:
            pub_ts = 0

        if hours_back > 0 and pub_ts > 0 and pub_ts < cutoff:
            continue

        score = _sentiment_score(f"{title} {summary}")
        if score >= 2:
            label = "positive"
        elif score <= -2:
            label = "negative"
        elif score == 1:
            label = "slightly_positive"
        elif score == -1:
            label = "slightly_negative"
        else:
            label = "neutral"

        # แปลไทย
        title_th = _translate_th(title) if do_translate else title

        # Format เวลา BKK
        if pub_ts > 0:
            bkk_dt  = datetime.fromtimestamp(pub_ts, tz=timezone.utc) + timedelta(hours=7)
            pub_str = bkk_dt.strftime("%d/%m %H:%M ICT")
        else:
            pub_str = ""

        results.append({
            "title": title, "title_th": title_th,
            "score": score, "label": label,
            "publisher": publisher, "link": link, "pub_str": pub_str,
        })
        if len(results) >= max_news:
            break

    # Filter by condition
    if condition == "positive":
        matching = [n for n in results if n["score"] > 0]
    elif condition == "negative":
        matching = [n for n in results if n["score"] < 0]
    elif condition == "strong_positive":
        matching = [n for n in results if n["score"] >= 2]
    elif condition == "strong_negative":
        matching = [n for n in results if n["score"] <= -2]
    elif condition == "high_volume":
        matching = results if len(results) >= alert.get("min_news", 3) else []
    else:
        matching = results  # any

    triggered = len(matching) >= min_news
    return triggered, matching[:5]


# ══════════════════════════════════════════════════════════════════════════════
#  MODULE 9 (ใหม่) — POSITION SIZING (info เพิ่มใน alert message)
# ══════════════════════════════════════════════════════════════════════════════
# alert fields:
#   account_size: ขนาดพอร์ต (USD)
#   risk_pct:    % ที่ยอมขาดทุน (default 2.0)
#   stop_pct:    % stop loss จาก price (default ใช้ ATR 2x)
#   target_pct:  % target (optional)
#   method:      fixed_risk | half_kelly (default fixed_risk)
#   win_rate:    สำหรับ kelly (default 0.55)
#   condition:   triggered_always | on_price_alert (default triggered_always)
#
# Module นี้ไม่ trigger เองแต่แนบข้อมูล position size ไปกับ alert อื่น
# หรือ trigger ได้เองเมื่อ condition = "triggered_always"

def calc_position_size(alert, symbol, entry_price=None):
    """
    คำนวณ position size
    Returns: dict ผลลัพธ์
    """
    account = alert.get("account_size", 10000)
    risk_pct = alert.get("risk_pct", 2.0)
    method  = alert.get("method", "fixed_risk")
    win_rate = alert.get("win_rate", 0.55)
    stop_pct = alert.get("stop_pct", None)
    target_pct = alert.get("target_pct", None)

    if entry_price is None:
        q = fetch_quote(symbol)
        entry_price = q["price"] if q else 0

    if entry_price <= 0:
        return None

    # คำนวณ stop โดยใช้ ATR ถ้าไม่ได้กำหนด
    if stop_pct is None:
        hist = fetch_history(symbol, period="90d", interval="1d")
        if hist is not None and len(hist) >= 16:
            highs  = list(hist["High"].astype(float))
            lows   = list(hist["Low"].astype(float))
            closes = list(hist["Close"].astype(float))
            atr = _calc_atr(highs, lows, closes, 14)
            if atr:
                stop_pct = (atr * 2 / entry_price) * 100
            else:
                stop_pct = 2.0
        else:
            stop_pct = 2.0

    stop_price   = entry_price * (1 - stop_pct / 100)
    risk_per_sh  = entry_price - stop_price
    risk_amount  = account * (risk_pct / 100)
    shares       = int(risk_amount / risk_per_sh) if risk_per_sh > 0 else 0
    pos_value    = shares * entry_price
    pos_pct      = (pos_value / account) * 100 if account > 0 else 0

    result = {
        "entry": round(entry_price, 4),
        "stop":  round(stop_price, 4),
        "stop_pct": round(stop_pct, 2),
        "shares": shares,
        "pos_value": round(pos_value, 2),
        "pos_pct":   round(pos_pct, 1),
        "risk_amount": round(risk_amount, 2),
        "risk_pct": risk_pct,
        "method": method,
    }

    if target_pct:
        target_price = entry_price * (1 + target_pct / 100)
        rr = target_pct / stop_pct if stop_pct > 0 else 0
        result["target"]   = round(target_price, 4)
        result["target_pct"] = target_pct
        result["rr_ratio"] = round(rr, 2)

    # Kelly
    if method == "half_kelly" and target_pct and stop_pct:
        rr = target_pct / stop_pct
        kelly = max(0, win_rate - (1 - win_rate) / rr)
        hk_pct = kelly / 2 * 100
        result["half_kelly_pct"] = round(hk_pct, 1)

    return result


def check_position_size(alert, symbol, quote=None):
    """trigger เสมอ — แค่คำนวณ position แนบไปกับ alert"""
    condition = alert.get("condition", "triggered_always")
    if condition != "triggered_always":
        return False, None
    price = quote["price"] if quote else None
    pos   = calc_position_size(alert, symbol, entry_price=price)
    return True, pos


# ══════════════════════════════════════════════════════════════════════════════
#  MODULE 10 (ใหม่) — MULTI-TIMEFRAME ALIGNMENT
# ══════════════════════════════════════════════════════════════════════════════
# alert fields:
#   timeframes:        list เช่น ["1h","4h","1d"]
#   required_alignment: mostly_bullish | mostly_bearish | strong_bullish_all | strong_bearish_all
#   min_bullish:       (default 2)
#   min_bearish:       (default 2)

def _tf_trend(symbol, interval):
    """วิเคราะห์ trend ของ timeframe เดียว — returns (trend_str, score, rsi)"""
    lookback = {"1m":"5d","5m":"5d","15m":"30d","30m":"60d",
                "1h":"60d","4h":"60d","1d":"180d","1wk":"3y"}.get(interval, "90d")

    hist = fetch_history(symbol, period=lookback, interval=interval)
    if hist is None or len(hist) < 55:
        return "unknown", 0, None

    closes = list(hist["Close"].astype(float))
    price  = closes[-1]
    ema21  = _calc_ema(closes, 21)[-1]
    ema50  = _calc_ema(closes, 50)[-1]
    rsi_l  = _calc_rsi(closes, 14)
    valid_rsi = [r for r in rsi_l if r is not None]
    rsi = valid_rsi[-1] if valid_rsi else 50

    if ema21 is None or ema50 is None:
        return "unknown", 0, rsi

    score = 0
    score += 1 if price > ema21 else -1
    score += 1 if ema21 > ema50 else -1
    score += 1 if rsi > 50 else -1
    score += 1 if (len(closes) >= 6 and price > closes[-6]) else -1

    if score >= 3:
        trend = "strong_bullish"
    elif score >= 1:
        trend = "bullish"
    elif score <= -3:
        trend = "strong_bearish"
    elif score <= -1:
        trend = "bearish"
    else:
        trend = "neutral"

    return trend, score, round(rsi, 1)


def check_mtf_alignment(alert, symbol):
    """
    ตรวจ MTF alignment
    Returns: (triggered, results_dict)
    """
    timeframes  = alert.get("timeframes", ["1h", "4h", "1d"])
    required    = alert.get("required_alignment", "mostly_bullish")
    min_bull    = alert.get("min_bullish", 2)
    min_bear    = alert.get("min_bearish", 2)

    tf_results  = {}
    for tf in timeframes:
        trend, score, rsi = _tf_trend(symbol, tf)
        tf_results[tf] = {"trend": trend, "score": score, "rsi": rsi}
        print(f"  [{symbol}][{tf}] trend={trend} score={score:+d} rsi={rsi}")
        time.sleep(0.5)

    bull_count = sum(1 for d in tf_results.values() if "bullish" in d["trend"])
    bear_count = sum(1 for d in tf_results.values() if "bearish" in d["trend"])
    total      = len(timeframes)

    if bull_count == total:
        overall = "strong_bullish_all"
    elif bull_count >= total * 0.75:
        overall = "mostly_bullish"
    elif bear_count == total:
        overall = "strong_bearish_all"
    elif bear_count >= total * 0.75:
        overall = "mostly_bearish"
    elif bull_count > bear_count:
        overall = "leaning_bullish"
    elif bear_count > bull_count:
        overall = "leaning_bearish"
    else:
        overall = "mixed"

    if required in ("bullish", "mostly_bullish", "leaning_bullish"):
        triggered = bull_count >= min_bull
    elif required in ("bearish", "mostly_bearish", "leaning_bearish"):
        triggered = bear_count >= min_bear
    elif required == "strong_bullish_all":
        triggered = overall == "strong_bullish_all"
    elif required == "strong_bearish_all":
        triggered = overall == "strong_bearish_all"
    else:
        triggered = overall == required

    return triggered, {
        "timeframes": tf_results,
        "overall": overall,
        "bull_count": bull_count,
        "bear_count": bear_count,
        "total": total,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  MODULE 11 (ใหม่) — CONFIDENCE SCORE
# ══════════════════════════════════════════════════════════════════════════════
# alert fields:
#   direction:  bullish | bearish
#   min_score:  (default 65)
#   interval:   (default 1d)

def check_alert_score(alert, symbol):
    """
    คำนวณ confidence score 0-100
    Returns: (triggered, score, grade, breakdown)
    """
    direction = alert.get("direction", "bullish")
    min_score = alert.get("min_score", 65)
    interval  = alert.get("interval", "1d")
    is_bull   = direction == "bullish"

    lookback = {"1d": "90d", "4h": "60d", "1h": "60d"}.get(interval, "90d")
    hist = fetch_history(symbol, period=lookback, interval=interval)
    if hist is None or len(hist) < 50:
        print(f"  [{symbol}] Score: ข้อมูลไม่พอ")
        return False, 0, "F", {}

    closes  = list(hist["Close"].astype(float))
    highs   = list(hist["High"].astype(float))
    lows    = list(hist["Low"].astype(float))
    volumes = list(hist["Volume"].astype(float))
    price   = closes[-1]

    ema21 = _calc_ema(closes, 21)[-1] or price
    ema50 = _calc_ema(closes, 50)[-1] or price
    ema9  = _calc_ema(closes, 9)[-1]  or price

    rsi_list  = _calc_rsi(closes, 14)
    valid_rsi = [r for r in rsi_list if r is not None]
    rsi = valid_rsi[-1] if valid_rsi else 50

    avg_vol = sum(volumes[-21:-1]) / 20 if len(volumes) >= 21 else volumes[-1]
    vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1

    atr = _calc_atr(highs, lows, closes, 14) or 0
    atr_pct = (atr / price) * 100 if price > 0 else 0

    chg   = ((closes[-1] - closes[-2]) / closes[-2]) * 100 if len(closes) >= 2 and closes[-2] > 0 else 0
    chg5  = ((closes[-1] - closes[-6]) / closes[-6]) * 100 if len(closes) >= 6 and closes[-6] > 0 else 0

    h20 = max(highs[-21:-1]) if len(highs) >= 21 else highs[-1]
    l20 = min(lows[-21:-1])  if len(lows)  >= 21 else lows[-1]
    dist_res = ((h20 - price) / price * 100) if price > 0 else 0
    dist_sup = ((price - l20) / price * 100) if price > 0 else 0

    score = 0
    bd    = {}

    # 1. RSI (15)
    rsi_sc = 0
    if is_bull:
        rsi_sc = 15 if rsi<=20 else (12 if rsi<=30 else (8 if rsi<=40 else (4 if rsi<=50 else 0)))
    else:
        rsi_sc = 15 if rsi>=80 else (12 if rsi>=70 else (8 if rsi>=60 else (4 if rsi>=50 else 0)))
    bd["RSI"] = {"s": rsi_sc, "max": 15, "note": f"RSI={rsi:.1f}"}
    score += rsi_sc

    # 2. MA (20)
    ma_sc = 0
    if is_bull:
        if price > ema21: ma_sc += 7
        if ema21 > ema50: ma_sc += 8
        if price > ema9 and ema9 > ema21: ma_sc += 5
    else:
        if price < ema21: ma_sc += 7
        if ema21 < ema50: ma_sc += 8
        if price < ema9 and ema9 < ema21: ma_sc += 5
    bd["MA"] = {"s": ma_sc, "max": 20, "note": f"EMA9={ema9:.2f} EMA21={ema21:.2f} EMA50={ema50:.2f}"}
    score += ma_sc

    # 3. Volume (15)
    vol_sc = 15 if vol_ratio>=3 else (12 if vol_ratio>=2 else (8 if vol_ratio>=1.5 else (4 if vol_ratio>=1 else 0)))
    bd["Vol"] = {"s": vol_sc, "max": 15, "note": f"Vol={vol_ratio:.1f}x avg"}
    score += vol_sc

    # 4. Momentum (15)
    mom_sc = 0
    if is_bull:
        mom_sc += (8 if chg>=3 else (5 if chg>=1 else (2 if chg>=0 else 0)))
        mom_sc += (7 if chg5>=5 else (4 if chg5>=2 else 0))
    else:
        mom_sc += (8 if chg<=-3 else (5 if chg<=-1 else (2 if chg<=0 else 0)))
        mom_sc += (7 if chg5<=-5 else (4 if chg5<=-2 else 0))
    mom_sc = min(mom_sc, 15)
    bd["Mom"] = {"s": mom_sc, "max": 15, "note": f"1d={chg:+.1f}% 5d={chg5:+.1f}%"}
    score += mom_sc

    # 5. Volatility (10)
    vol2_sc = 10 if 1<=atr_pct<=4 else (6 if 0.5<=atr_pct<=7 else (2 if atr_pct<0.5 else 0))
    bd["ATR"] = {"s": vol2_sc, "max": 10, "note": f"ATR={atr_pct:.1f}%"}
    score += vol2_sc

    # 6. S/R (15)
    sr_sc = 0
    if is_bull:
        sr_sc += (10 if dist_sup<=2 else (6 if dist_sup<=5 else 0))
        sr_sc += (5 if dist_res>=5 else 0)
    else:
        sr_sc += (10 if dist_res<=2 else (6 if dist_res<=5 else 0))
        sr_sc += (5 if dist_sup>=5 else 0)
    sr_sc = min(sr_sc, 15)
    bd["S/R"] = {"s": sr_sc, "max": 15, "note": f"toRes={dist_res:.1f}% toSup={dist_sup:.1f}%"}
    score += sr_sc

    # 7. HTF (10)
    htf_sc = 10 if (is_bull and price > ema50) or (not is_bull and price < ema50) else 0
    bd["HTF"] = {"s": htf_sc, "max": 10, "note": f"price vs EMA50"}
    score += htf_sc

    total = min(score, 100)
    grade = "A" if total>=80 else ("B" if total>=65 else ("C" if total>=50 else "D"))
    triggered = total >= min_score
    return triggered, total, grade, bd


# ══════════════════════════════════════════════════════════════════════════════
#  MODULE 12 (ใหม่) — BACKTEST CHECK (เช็กก่อนเพิ่ม alert)
# ══════════════════════════════════════════════════════════════════════════════
# alert fields:
#   rule:            rsi_oversold | golden_cross | volume_spike | price_breakout | hammer | ...
#   days:            ย้อนหลัง (default 180)
#   hold_days:       ถือครอง (default 5)
#   min_win_rate:    trigger ถ้า win_rate >= X% (default 50)
#   min_avg_return:  trigger ถ้า avg_return >= X% (default 0)
#   interval:        (default 1d)

def check_backtest(alert, symbol):
    """
    รัน mini backtest แล้ว trigger ถ้า rule นี้ผ่าน threshold
    Returns: (triggered, result_dict)
    """
    rule         = alert.get("rule", "rsi_oversold")
    days         = alert.get("days", 180)
    hold_days    = alert.get("hold_days", 5)
    min_wr       = alert.get("min_win_rate", 50)
    min_ret      = alert.get("min_avg_return", 0)
    interval     = alert.get("interval", "1d")
    vol_mult     = alert.get("volume_multiplier", 2.0)
    rsi_thr      = alert.get("rsi_threshold", 30)

    warmup = 60
    total_days = days + warmup + hold_days + 10
    lookback   = f"{min(total_days, 730)}d"

    hist = fetch_history(symbol, period=lookback, interval=interval)
    if hist is None or len(hist) < warmup + hold_days + 5:
        print(f"  [{symbol}] Backtest: ข้อมูลไม่พอ")
        return False, {"error": "ข้อมูลไม่พอ"}

    opens  = list(hist["Open"].astype(float))
    highs  = list(hist["High"].astype(float))
    lows   = list(hist["Low"].astype(float))
    closes = list(hist["Close"].astype(float))
    vols   = list(hist["Volume"].astype(float))
    n      = len(closes)

    rsi_s  = _calc_rsi(closes, 14)
    ema9s  = _calc_ema(closes, 9)
    ema21s = _calc_ema(closes, 21)

    avg_vol_20 = [None] * n
    for i in range(20, n):
        avg_vol_20[i] = sum(vols[i-20:i]) / 20

    high_20 = [None] * n
    low_20  = [None] * n
    for i in range(20, n):
        high_20[i] = max(highs[i-20:i])
        low_20[i]  = min(lows[i-20:i])

    cutoff   = max(0, n - days - hold_days)
    end_idx  = n - hold_days
    trades   = []
    last_sig = -999

    for i in range(max(cutoff, warmup), end_idx):
        if i - last_sig < max(hold_days, 3):
            continue

        rsi    = rsi_s[i]  if i < len(rsi_s)  else None
        e9     = ema9s[i]  if i < len(ema9s)  else None
        e9p    = ema9s[i-1] if i > 0 and i-1 < len(ema9s)  else None
        e21    = ema21s[i] if i < len(ema21s) else None
        e21p   = ema21s[i-1] if i > 0 and i-1 < len(ema21s) else None
        av     = avg_vol_20[i]
        h20    = high_20[i]
        l20    = low_20[i]
        trig   = False

        if rule == "rsi_oversold":
            trig = rsi is not None and rsi <= rsi_thr
        elif rule == "rsi_overbought":
            trig = rsi is not None and rsi >= (100 - rsi_thr)
        elif rule == "golden_cross":
            trig = all(x is not None for x in [e9, e9p, e21, e21p]) and e9p <= e21p and e9 > e21
        elif rule == "death_cross":
            trig = all(x is not None for x in [e9, e9p, e21, e21p]) and e9p >= e21p and e9 < e21
        elif rule == "volume_spike":
            trig = av is not None and vols[i] >= av * vol_mult
        elif rule == "price_breakout":
            trig = h20 is not None and closes[i] > h20
        elif rule == "price_breakdown":
            trig = l20 is not None and closes[i] < l20
        elif rule == "hammer":
            o, h, l, c = opens[i], highs[i], lows[i], closes[i]
            rng = max(h - l, 0.0001)
            body = abs(c - o)
            trig = (min(o,c)-l) >= body*2 and (h-max(o,c))/rng < 0.2 and body/rng >= 0.1 and c < o
        elif rule == "three_soldiers":
            if i >= 2:
                trig = (closes[i]>opens[i] and closes[i-1]>opens[i-1] and closes[i-2]>opens[i-2]
                        and closes[i]>closes[i-1]>closes[i-2])

        if trig:
            entry    = closes[i]
            exit_idx = min(i + hold_days, n - 1)
            ex       = closes[exit_idx]
            is_short = rule in ("death_cross", "rsi_overbought", "price_breakdown")
            pnl      = ((entry - ex) / entry * 100) if is_short else ((ex - entry) / entry * 100)
            trades.append({"pnl": pnl, "win": pnl > 0})
            last_sig = i

    if not trades:
        return False, {"total_trades": 0, "error": "ไม่พบ signal ใน period นี้"}

    wins     = [t for t in trades if t["win"]]
    win_rate = len(wins) / len(trades) * 100
    avg_ret  = sum(t["pnl"] for t in trades) / len(trades)

    triggered = win_rate >= min_wr and avg_ret >= min_ret
    pf_denom  = abs(sum(t["pnl"] for t in trades if not t["win"]))
    pf        = abs(sum(t["pnl"] for t in wins)) / pf_denom if pf_denom > 0 else 99.0

    result = {
        "rule": rule, "total_trades": len(trades),
        "win_rate": round(win_rate, 1),
        "avg_return": round(avg_ret, 2),
        "best": round(max(t["pnl"] for t in trades), 2),
        "worst": round(min(t["pnl"] for t in trades), 2),
        "profit_factor": round(min(pf, 99), 2),
    }
    return triggered, result


# ══════════════════════════════════════════════════════════════════════════════
#  MESSAGE BUILDERS
# ══════════════════════════════════════════════════════════════════════════════

def _header(stock, quote, alert):
    """สร้าง header message มาตรฐาน"""
    emoji  = alert.get("emoji", "🔔")
    symbol = stock["symbol"]
    name   = stock["name"]
    price  = quote["price"]
    pct    = quote["change_pct"]
    arrow  = "📈" if pct >= 0 else "📉"
    sign   = "+" if pct >= 0 else ""
    tv     = f"https://www.tradingview.com/chart/?symbol={symbol}"
    return emoji, symbol, name, price, pct, arrow, sign, tv


def build_message(stock, alert, quote, triggered_value):
    """message สำหรับ alert types เดิม (price_target, percent_change, volume_spike, support_resistance)"""
    emoji, symbol, name, price, pct, arrow, sign, tv = _header(stock, quote, alert)
    atype  = alert["type"]
    tf     = stock.get("timeframe", "")
    note   = alert.get("note", "")
    action = alert.get("action", "")

    lines = [f"{emoji} <b>🔔 แจ้งเตือน: {symbol}</b> ({name})", ""]

    if atype == "price_target":
        target = alert["target_price"]
        dir_th = {"below_or_equal": "ราคาถึงจุดซื้อที่ตั้งไว้ ✅", "above_or_equal": "ราคาถึงเป้าหมายที่ตั้งไว้ ✅"}.get(alert.get("direction",""), "")
        lines += [
            f"🏷️ ประเภท: <b>เป้าหมายราคา</b>",
            f"💰 ราคาปัจจุบัน: <b>${price:.4f}</b>  {arrow} {sign}{pct:.2f}%",
            f"🎯 เป้าหมาย: <b>${target:.4f}</b>  {dir_th}",
        ]
        if action: lines.append(f"⚡ สัญญาณ: <b>{action}</b>")
        # Trade box: ซื้อเมื่อราคาถึง below_or_equal (จุดซื้อ), ขายเมื่อ above_or_equal (TP hit)
        _dir = alert.get("direction", "")
        if _dir == "below_or_equal":
            trade = _calc_entry_sl_tp(stock["symbol"], price, direction="bull")
            lines += _format_trade_box(trade, direction="bull")
        lines += [
            "",
            "📌 <b>แนะนำให้ทำ:</b>",
            "  1️⃣ เปิดกราฟยืนยันแนวรับ/แนวต้าน และปริมาณการซื้อขาย",
            "  2️⃣ รอให้แท่งเทียนปิดยืนยันก่อน อย่าเพิ่งเข้าซื้อทันที",
            "  3️⃣ ตั้งจุดตัดขาดทุนต่ำกว่าจุดต่ำสุดของแท่งเทียนที่ trigger",
        ]

    elif atype == "percent_change":
        direction = alert.get("direction", "down")
        thr = alert.get("threshold_pct", 5.0)
        if direction == "down":
            interp = f"⚠️ ราคาลดลง {abs(pct):.1f}% — ตรวจสอบจุดตัดขาดทุน"
            steps = [
                "  1️⃣ ถ้ากำลังถือหุ้นอยู่ → ตรวจว่าจุดตัดขาดทุนยังเหมาะสมอยู่ไหม",
                "  2️⃣ ดูปริมาณการซื้อขาย ว่าเป็นการเทขายจริงหรือแค่ราคาย่อตัวชั่วคราว",
                "  3️⃣ ดู RSI และแนวรับก่อนตัดสินใจว่าจะขายหรือถือต่อ",
            ]
        else:
            interp = f"🚀 ราคาขึ้น {pct:.1f}% — แรงซื้อเข้ามามาก"
            steps = [
                "  1️⃣ ตรวจปริมาณการซื้อขายยืนยันว่าราคาผ่านแนวต้านจริง",
                "  2️⃣ ถ้ายังไม่ได้เข้า → รอราคาย่อลงมาก่อน หรือรอจังหวะราคาทะลุแนวต้าน",
                "  3️⃣ ถ้าถือหุ้นอยู่แล้ว → เลื่อนจุดตัดขาดทุนตามราคาขึ้นมาด้วย",
            ]
        lines += [
            f"🏷️ ประเภท: <b>ราคาเปลี่ยนแปลงรุนแรง</b>",
            f"💰 ราคาปัจจุบัน: <b>${price:.4f}</b>",
            f"{arrow} เปลี่ยนแปลง: <b>{sign}{pct:.2f}%</b>  (เงื่อนไขที่ตั้งไว้: {thr}%)",
            f"💡 {interp}",
        ]
        if direction == "up":
            trade = _calc_entry_sl_tp(stock["symbol"], price, direction="bull")
            lines += _format_trade_box(trade, direction="bull")
        lines += [
            "",
            "📌 <b>แนะนำให้ทำ:</b>",
        ] + steps

    elif atype == "volume_spike":
        vol_x = triggered_value
        if pct >= 1.0:
            vol_type = "🟢 แรงซื้อเข้ามามาก — ราคาน่าจะผ่านแนวต้านได้"
            vol_action = [
                "  1️⃣ แรงซื้อสูง — อาจเข้าซื้อหรือเพิ่มจำนวนหุ้นที่ถืออยู่ได้",
                "  2️⃣ ตั้งจุดตัดขาดทุนต่ำกว่าจุดต่ำสุดของแท่งเทียนที่ผ่านแนวต้าน",
                "  3️⃣ เป้าหมายกำไร = แนวต้านถัดไปบนกราฟ",
            ]
        elif pct <= -1.0:
            vol_type = "🔴 แรงขายเข้ามามาก — ระวังราคาจะลงต่อ"
            vol_action = [
                "  1️⃣ ถ้าถือหุ้นอยู่ → พิจารณาขยับจุดตัดขาดทุนให้ใกล้ขึ้น",
                "  2️⃣ ยังไม่ควรซื้อเพิ่มในช่วงที่มีแรงขายหนัก รอราคานิ่งก่อน",
                "  3️⃣ ดูว่าแนวรับถัดไปอยู่ที่ราคาเท่าไหร่",
            ]
        else:
            vol_type = "🟡 ปริมาณการซื้อขายพุ่งสูง — ทิศทางยังไม่ชัดเจน"
            vol_action = [
                "  1️⃣ รอดูว่าราคาจะไปทิศทางไหนก่อน",
                "  2️⃣ ดูแท่งเทียนถัดไปว่าปิดบวกหรือลบ",
                "  3️⃣ ติดตามแรงซื้อ/แรงขายต่อในชั่วโมงถัดไป",
            ]
        mult_set = alert.get("multiplier", 2.0)
        lines += [
            f"🏷️ ประเภท: <b>ปริมาณการซื้อขายผิดปกติ</b>",
            f"💰 ราคาปัจจุบัน: <b>${price:.4f}</b>  {arrow} {sign}{pct:.2f}%",
            f"🔊 ปริมาณซื้อขาย: <b>{vol_x:.1f} เท่า</b>ของค่าเฉลี่ย  (เงื่อนไข: {mult_set} เท่า)",
            f"💡 {vol_type}",
        ]
        if pct >= 1.0:
            trade = _calc_entry_sl_tp(stock["symbol"], price, direction="bull")
            lines += _format_trade_box(trade, direction="bull")
        elif pct <= -1.0:
            trade = _calc_entry_sl_tp(stock["symbol"], price, direction="bear")
            lines += _format_trade_box(trade, direction="bear")
        lines += [
            "",
            "📌 <b>แนะนำให้ทำ:</b>",
        ] + vol_action

    elif atype == "support_resistance":
        level = alert.get("level", 0)
        direction = alert.get("direction", "break_below")
        if direction == "break_above":
            dir_th = "ราคาทะลุแนวต้านขึ้น 🚀"
            sr_steps = [
                "  1️⃣ ยืนยันว่าแท่งเทียนปิดเหนือแนวต้านจริงๆ",
                "  2️⃣ ตรวจปริมาณการซื้อขายว่าพุ่งขึ้นพร้อมกันไหม",
                "  3️⃣ เข้าซื้อเมื่อราคาผ่านแนวต้าน หรือรอราคาย้อนกลับมาทดสอบแนวเดิม",
            ]
        else:
            dir_th = "ราคาหลุดแนวรับลง ⚠️"
            sr_steps = [
                "  1️⃣ ถ้าถือหุ้นอยู่ → พิจารณาขายตัดขาดทุนทันที",
                "  2️⃣ ดูว่าแนวรับถัดไปอยู่ที่ราคาเท่าไหร่",
                "  3️⃣ อย่ารีบซื้อเพิ่ม รอสัญญาณกลับตัวให้ชัดก่อน",
            ]
        lines += [
            f"🏷️ ประเภท: <b>แนวรับ/แนวต้าน</b>",
            f"💰 ราคาปัจจุบัน: <b>${price:.4f}</b>  {arrow} {sign}{pct:.2f}%",
            f"⚠️ ระดับราคาสำคัญ: <b>${level:.4f}</b>  — {dir_th}",
        ]
        if direction == "break_above":
            trade = _calc_entry_sl_tp(stock["symbol"], price, direction="bull")
            lines += _format_trade_box(trade, direction="bull")
        lines += [
            "",
            "📌 <b>แนะนำให้ทำ:</b>",
        ] + sr_steps

    if tf:   lines.append(f"\n⏱ กรอบเวลา: {tf}")
    if note: lines.append(f"📋 หมายเหตุ: {note}")
    lines += ["", f"📊 <a href='{tv}'>ดูกราฟ TradingView</a>", f"🕐 {now_bkk_str()}"]
    return "\n".join(lines)


def build_rsi_message(stock, alert, rsi, prev_rsi, rsi_price, quote):
    emoji, symbol, name, price, pct, arrow, sign, tv = _header(stock, quote, alert)
    cond    = alert.get("condition", "oversold")
    period  = alert.get("period", 14)
    note    = alert.get("note", "")
    action  = alert.get("action", "")
    interval = alert.get("interval", "1d")
    rsi_arr = "↑" if rsi > prev_rsi else "↓"
    rsi_move = rsi - prev_rsi

    cond_th = {
        "oversold":          ("📉 ถูกเทขายมากเกินไป (Oversold)", "ค่า RSI ต่ำ — ราคาถูกขายหนักจนเกินไป มีโอกาสกลับตัวขึ้น"),
        "overbought":        ("📈 ถูกซื้อมากเกินไป (Overbought)", "ค่า RSI สูง — ราคาถูกซื้อหนักจนเกินไป ระวังราคาจะกลับตัวลง"),
        "extreme_oversold":  ("🔥 ถูกเทขายหนักมาก", "RSI ต่ำมากผิดปกติ — โอกาสหายาก แต่ความเสี่ยงสูง ใช้เงินน้อยก่อน"),
        "extreme_overbought":("🌡️ ถูกซื้อหนักมากผิดปกติ", "RSI สูงมาก — ระวังเป็นพิเศษ อย่าเพิ่มจำนวนหุ้น"),
        "turning_up":        ("↗️ RSI กำลังเริ่มเปลี่ยนทิศขึ้น", "แรงซื้อเริ่มกลับมา — สัญญาณเข้าซื้อเบื้องต้น"),
        "turning_down":      ("↘️ RSI กำลังเริ่มเปลี่ยนทิศลง", "แรงซื้อเริ่มอ่อนแรง — พิจารณาขยับจุดตัดขาดทุนให้ใกล้ขึ้น"),
    }
    cond_label, cond_desc = cond_th.get(cond, (cond, ""))

    action_map = {
        "oversold":          ["1️⃣ รอแท่งเทียนกลับตัว (รูป Hammer หรือ Engulfing) บนกราฟรายวัน", "2️⃣ ยืนยันปริมาณซื้อขายเพิ่มขึ้น + RSI เริ่มกลับขึ้น", "3️⃣ เข้าซื้อได้ พร้อมตั้งจุดตัดขาดทุนต่ำกว่าจุดต่ำสุดล่าสุด"],
        "extreme_oversold":  ["1️⃣ โอกาสหายาก — ใช้เงินลงทุนน้อยกว่าปกติก่อน", "2️⃣ รอแท่งเทียนกลับตัวอย่างน้อย 1 แท่ง", "3️⃣ เข้าซื้อทีละน้อย อย่าซื้อหมดในครั้งเดียว"],
        "overbought":        ["1️⃣ ถ้าถือหุ้นอยู่ → พิจารณาขายทำกำไรบางส่วน", "2️⃣ อย่าซื้อเพิ่มในโซนนี้", "3️⃣ ตั้งจุดตัดขาดทุนแบบเลื่อนตาม ป้องกันราคากลับตัว"],
        "extreme_overbought":["1️⃣ ระวังเป็นพิเศษ — หลีกเลี่ยงการซื้อ", "2️⃣ ถ้าถือหุ้นอยู่ → ขยับจุดตัดขาดทุนให้ใกล้ขึ้น หรือทำกำไร", "3️⃣ รอ RSI ลงมาต่ำกว่า 70 ก่อนพิจารณาซื้อใหม่"],
        "turning_up":        ["1️⃣ สัญญาณเร็วเกินไป — รอยืนยัน 1-2 แท่งก่อนเข้า", "2️⃣ ดูว่าปริมาณซื้อขายเพิ่มขึ้นพร้อมกับ RSI ไหม", "3️⃣ เข้าซื้อเมื่อราคาผ่านจุดสูงสุดของแท่งเทียนที่ trigger"],
        "turning_down":      ["1️⃣ ถ้าถือหุ้นอยู่ → พิจารณาขยับจุดตัดขาดทุนให้ใกล้ขึ้น", "2️⃣ อย่าซื้อเพิ่ม รอดูแรงซื้อก่อน", "3️⃣ ถ้า RSI ลงต่อ + ราคาหลุดแนวรับ → ขายตัดขาดทุน"],
    }
    steps = action_map.get(cond, ["1️⃣ ดูกราฟเพิ่มเติมก่อนตัดสินใจ"])

    lines = [
        f"{emoji} <b>🔔 แจ้งเตือน RSI: {symbol}</b> ({name})", "",
        f"🏷️ ประเภท: <b>สัญญาณ RSI</b>",
        f"📊 ค่า RSI({period}): <b>{rsi:.1f}</b> {rsi_arr} {rsi_move:+.1f}  (ค่าก่อนหน้า: {prev_rsi:.1f})",
        f"⚡ {cond_label} — {cond_desc}",
        f"💰 ราคาปัจจุบัน: <b>${price:.4f}</b>  {arrow} {sign}{pct:.2f}%",
        f"⏱ กรอบเวลา: {interval}",
    ]
    if action: lines.append(f"🎯 สัญญาณ: <b>{action}</b>")
    # Trade box สำหรับ oversold / extreme_oversold / turning_up
    if cond in ("oversold", "extreme_oversold", "turning_up"):
        trade = _calc_entry_sl_tp(stock["symbol"], price, direction="bull")
        lines += _format_trade_box(trade, direction="bull")
    lines += [
        "",
        "📌 <b>แนะนำให้ทำ:</b>",
    ] + [f"  {s}" for s in steps]
    if note: lines.append(f"\n📋 หมายเหตุ: {note}")
    lines += ["", f"📊 <a href='{tv}'>ดูกราฟ TradingView</a>", f"🕐 {now_bkk_str()}"]
    return "\n".join(lines)


def build_ma_message(stock, alert, fast_ma, slow_ma, ma_price, gap_pct, quote):
    emoji, symbol, name, price, pct, arrow, sign, tv = _header(stock, quote, alert)
    cond    = alert.get("condition", "golden_cross")
    fast_p  = alert.get("fast_period", 9)
    slow_p  = alert.get("slow_period", 21)
    ma_type = alert.get("ma_type", "EMA")
    note    = alert.get("note", "")
    action  = alert.get("action", "")
    interval = alert.get("interval", "1d")

    cond_info = {
        "golden_cross":  ("🌟 เส้นค่าเฉลี่ยตัดขึ้น — สัญญาณขาขึ้น!", "🟢", "แนวโน้มขาขึ้น",
                          ["1️⃣ ยืนยันว่าแท่งเทียนปิดเหนือเส้นค่าเฉลี่ยทั้งสองเส้น", "2️⃣ ตรวจปริมาณซื้อขายว่าเพิ่มขึ้นพร้อมกับการตัด", "3️⃣ เข้าซื้อได้ หรือรอราคาย่อมาทดสอบเส้นค่าเฉลี่ยเร็ว"]),
        "death_cross":   ("💀 เส้นค่าเฉลี่ยตัดลง — สัญญาณขาลง!", "🔴", "แนวโน้มขาลง",
                          ["1️⃣ ถ้าถือหุ้นอยู่ → พิจารณาขายหรือป้องกันความเสี่ยง", "2️⃣ อย่าซื้อ รอจนเส้นตัดกลับขึ้นก่อน", "3️⃣ ดูแนวรับถัดไปเผื่อจังหวะชอร์ต"]),
        "above_both":    ("🚀 ราคาอยู่เหนือเส้นค่าเฉลี่ยทั้งคู่", "🟢", "แนวโน้มขาขึ้น",
                          ["1️⃣ แนวโน้มชัดเจน — ถือหุ้นต่อได้", "2️⃣ ใช้เส้นค่าเฉลี่ยเร็วเป็นแนวรับแบบเคลื่อนที่", "3️⃣ ตั้งจุดตัดขาดทุนใต้เส้นค่าเฉลี่ยช้า"]),
        "below_both":    ("🔻 ราคาอยู่ต่ำกว่าเส้นค่าเฉลี่ยทั้งคู่", "🔴", "แนวโน้มขาลง",
                          ["1️⃣ ขาลงชัดเจน — หลีกเลี่ยงการซื้อ", "2️⃣ รอราคากลับขึ้นมาเหนือเส้นค่าเฉลี่ยเร็วก่อน", "3️⃣ ถ้าถือหุ้นอยู่ → ขยับจุดตัดขาดทุนให้ใกล้ขึ้น"]),
        "trend_bullish": ("📈 แนวโน้มกำลังขาขึ้น", "🟢", "แนวโน้มขาขึ้น",
                          ["1️⃣ ทิศทางเป็นขาขึ้น — หาจังหวะซื้อ", "2️⃣ รอ RSI ย่อลงก่อนเข้า", "3️⃣ ดูแท่งเทียนกลับตัวบนกราฟ 4 ชั่วโมง"]),
        "trend_bearish": ("📉 แนวโน้มกำลังขาลง", "🔴", "แนวโน้มขาลง",
                          ["1️⃣ ทิศทางเป็นขาลง — หลีกเลี่ยงการซื้อ", "2️⃣ รอสัญญาณกลับตัวที่ชัดเจนก่อน", "3️⃣ ถ้าถือหุ้นอยู่ → ทบทวนจุดตัดขาดทุน"]),
        "gap_expanding": ("↔️ ช่องห่างเส้นค่าเฉลี่ยกว้างขึ้น — แรงส่งเพิ่มขึ้น", "🟡", "แนวโน้มแรงขึ้น",
                          ["1️⃣ แนวโน้มกำลังเร่ง — ติดตามแรงซื้อ/ขาย", "2️⃣ อาจเพิ่มหุ้นได้ถ้าแนวโน้มตรงกับที่คาด", "3️⃣ ระวังราคาวิ่งเร็วเกินไป ดู RSI ประกอบด้วย"]),
    }
    label, trend_icon, trend_word, steps = cond_info.get(cond, (cond, "🟡", "ไม่ทราบ", []))

    lines = [
        f"{emoji} <b>🔔 แจ้งเตือนเส้นค่าเฉลี่ย: {symbol}</b> ({name})", "",
        f"🏷️ ประเภท: <b>สัญญาณเส้นค่าเฉลี่ย (MA)</b>",
        f"⚡ {label}",
        f"💰 ราคาปัจจุบัน: <b>${price:.4f}</b>  {arrow} {sign}{pct:.2f}%",
        f"📊 {ma_type}{fast_p}/{slow_p}  (กรอบเวลา: {interval}):",
        f"  • เส้นเร็ว {ma_type}{fast_p}: <b>${fast_ma:.4f}</b>",
        f"  • เส้นช้า {ma_type}{slow_p}: <b>${slow_ma:.4f}</b>",
        f"  • ส่วนต่าง: <b>{gap_pct:+.2f}%</b>",
        f"{trend_icon} แนวโน้ม: <b>{trend_word}</b>",
    ]
    if action: lines.append(f"🎯 สัญญาณ: <b>{action}</b>")
    # Trade box สำหรับ bullish conditions
    bullish_conds = ("golden_cross", "above_both", "trend_bullish", "gap_expanding")
    bearish_conds = ("death_cross", "below_both", "trend_bearish")
    if cond in bullish_conds:
        trade = _calc_entry_sl_tp(stock["symbol"], price, direction="bull")
        lines += _format_trade_box(trade, direction="bull")
    elif cond in bearish_conds:
        trade = _calc_entry_sl_tp(stock["symbol"], price, direction="bear")
        lines += _format_trade_box(trade, direction="bear")
    lines += [
        "",
        "📌 <b>แนะนำให้ทำ:</b>",
    ] + [f"  {s}" for s in steps]
    if note: lines.append(f"\n📋 หมายเหตุ: {note}")
    lines += ["", f"📊 <a href='{tv}'>ดูกราฟ TradingView</a>", f"🕐 {now_bkk_str()}"]
    return "\n".join(lines)


def build_candle_message(stock, alert, found_patterns, candle_info, quote):
    emoji, symbol, name, price, pct, arrow, sign, tv = _header(stock, quote, alert)
    note   = alert.get("note", "")
    action = alert.get("action", "")
    interval = alert.get("interval", "1d")

    pat_lines = "\n".join(
        f"  • {CANDLE_DESC_TH.get(p, p)}" for p in found_patterns
    ) or "  • ไม่พบรูปแบบแท่งเทียนที่ระบุ"

    bullish_pats = {"hammer","inverted_hammer","bullish_engulfing","three_white_soldiers","marubozu_bullish","morning_star"}
    bearish_pats = {"shooting_star","hanging_man","bearish_engulfing","three_black_crows","marubozu_bearish","evening_star"}
    is_bull_pat  = any(p in bullish_pats for p in found_patterns)
    is_bear_pat  = any(p in bearish_pats for p in found_patterns)

    if is_bull_pat:
        interp = "🟢 รูปแบบแท่งเทียนบวก — มีโอกาสราคากลับตัวขึ้น"
        steps  = [
            "1️⃣ รอแท่งเทียนถัดไปยืนยัน (ควรเป็นแท่งเทียนสีเขียว)",
            "2️⃣ ตรวจปริมาณซื้อขายว่าเพิ่มขึ้นพร้อมกับรูปแบบนี้",
            "3️⃣ เข้าซื้อเมื่อราคาผ่านจุดสูงสุดของแท่งเทียนรูปแบบนั้น",
            "4️⃣ ตั้งจุดตัดขาดทุนต่ำกว่าจุดต่ำสุดของแท่งเทียนรูปแบบนั้น",
        ]
    elif is_bear_pat:
        interp = "🔴 รูปแบบแท่งเทียนลบ — ระวังราคากลับตัวลง"
        steps  = [
            "1️⃣ ถ้าถือหุ้นอยู่ → ขยับจุดตัดขาดทุนให้ใกล้ขึ้น หรือทำกำไรบางส่วน",
            "2️⃣ อย่าซื้อเพิ่มในโซนนี้",
            "3️⃣ รอยืนยันด้วยแท่งเทียนสีแดงแท่งถัดไปก่อนตัดสินใจ",
        ]
    else:
        interp = "🟡 รูปแบบแท่งเทียนกลางๆ — ทิศทางยังไม่ชัดเจน"
        steps  = [
            "1️⃣ ยังไม่ชัดเจน — รอดูแท่งเทียนถัดไปก่อน",
            "2️⃣ ดู RSI และปริมาณซื้อขายประกอบ",
            "3️⃣ ไม่ควรเทรดโดยอิงจากรูปแบบที่ยังไม่ชัดเพียงอย่างเดียว",
        ]

    lines = [
        f"{emoji} <b>🔔 แจ้งเตือนแท่งเทียน: {symbol}</b> ({name})", "",
        f"🏷️ ประเภท: <b>รูปแบบแท่งเทียน</b>",
        f"🕯️ รูปแบบที่พบ (กรอบเวลา: {interval}):", pat_lines,
        f"💡 {interp}",
        f"💰 ราคาปัจจุบัน: <b>${price:.4f}</b>  {arrow} {sign}{pct:.2f}%",
    ]
    if action: lines.append(f"🎯 สัญญาณ: <b>{action}</b>")
    # Trade box สำหรับ bullish/bearish pattern
    if is_bull_pat:
        trade = _calc_entry_sl_tp(stock["symbol"], price, direction="bull")
        lines += _format_trade_box(trade, direction="bull")
    elif is_bear_pat:
        trade = _calc_entry_sl_tp(stock["symbol"], price, direction="bear")
        lines += _format_trade_box(trade, direction="bear")
    lines += [
        "",
        "📌 <b>แนะนำให้ทำ:</b>",
    ] + [f"  {s}" for s in steps]
    if note: lines.append(f"\n📋 หมายเหตุ: {note}")
    lines += ["", f"📊 <a href='{tv}'>ดูกราฟ TradingView</a>", f"🕐 {now_bkk_str()}"]
    return "\n".join(lines)


def build_news_message(stock, alert, news_list, quote):
    emoji, symbol, name, price, pct, arrow, sign, tv = _header(stock, quote, alert)
    cond = alert.get("condition", "any")
    cond_th = {
        "any":             "ข่าวใหม่",
        "positive":        "ข่าวบวก",
        "negative":        "ข่าวลบ",
        "strong_positive": "ข่าวบวกแรง",
        "strong_negative": "ข่าวลบแรง",
        "high_volume":     "ข่าวหลายชิ้น",
    }
    sent_icon = {"positive":"🟢","slightly_positive":"🟡","negative":"🔴","slightly_negative":"🟠","neutral":"⚪"}

    lines = [
        f"{emoji} <b>🔔 แจ้งเตือนข่าว: {symbol}</b> ({name})",
        f"🏷️ ประเภท: <b>ข่าวหุ้น</b>  —  {cond_th.get(cond, cond)} พบ {len(news_list)} ข่าว",
        f"💰 ราคาปัจจุบัน: <b>${price:.4f}</b>  {arrow} {sign}{pct:.2f}%", "",
    ]
    for i, n in enumerate(news_list[:4], 1):
        si       = sent_icon.get(n["label"], "⚪")
        title_d  = n["title_th"] if n["title_th"] != n["title"] else n["title"]
        lines.append(f"{i}. {si} <b>{title_d}</b>")
        if n.get("link"):
            lines.append(f"   🔗 <a href='{n['link']}'>{n['publisher']}</a>  {n['pub_str']}")
        else:
            lines.append(f"   📰 {n['publisher']}  {n['pub_str']}")
        lines.append("")

    lines += [f"📊 <a href='{tv}'>ดูกราฟ TradingView</a>", f"🕐 {now_bkk_str()}"]
    return "\n".join(lines)


def build_position_message(stock, alert, pos, quote):
    emoji, symbol, name, price, pct, arrow, sign, tv = _header(stock, quote, alert)
    note = alert.get("note", "")

    size_warn = "✅ ขนาดเหมาะสม" if pos["pos_pct"] <= 20 else "⚠️ ถือหุ้นเยอะเกิน — ระวังความเสี่ยง"
    rr_line  = f"  • อัตราส่วนความเสี่ยง/ผลตอบแทน: <b>1:{pos['rr_ratio']:.1f}</b>" if pos.get("rr_ratio") else ""
    tgt_line = f"  • เป้าหมายกำไร: <b>${pos['target']:.4f}</b> (+{pos.get('target_pct',0):.1f}%)" if pos.get("target") else ""
    hk_line  = f"  • แนะนำถือ (Half-Kelly): <b>{pos.get('half_kelly_pct',0):.1f}%</b> ของพอร์ต" if pos.get("half_kelly_pct") else ""

    if pos["pos_pct"] > 20:
        risk_note = "⚠️ ถือหุ้นเกิน 20% ของพอร์ต — พิจารณาลดจำนวน"
    elif pos["pos_pct"] > 15:
        risk_note = "🟡 ขนาดที่ถืออยู่ระดับกลาง — ติดตามใกล้ชิด"
    else:
        risk_note = "🟢 ขนาดที่ถืออยู่เหมาะสม — ความเสี่ยงอยู่ในกรอบ"

    lines = [
        f"{emoji} <b>🔔 แจ้งเตือนขนาดที่ถือ: {symbol}</b> ({name})", "",
        f"🏷️ ประเภท: <b>การจัดการขนาดที่ถือ</b>",
        f"💰 ราคาอ้างอิงตอนเข้า: <b>${pos['entry']:.4f}</b>  {arrow} {sign}{pct:.2f}%",
        f"🛑 จุดตัดขาดทุน: <b>${pos['stop']:.4f}</b>  (-{pos['stop_pct']:.1f}% จากราคาเข้า)",
    ]
    if tgt_line: lines.append(tgt_line)
    lines += [
        "",
        f"📊 ผลคำนวณ ({pos['method']}):",
        f"  • จำนวน: <b>{pos['shares']:,} หุ้น</b>",
        f"  • มูลค่า: <b>${pos['pos_value']:,.2f}</b>  ({pos['pos_pct']:.1f}% ของพอร์ต)",
        f"  • ความเสี่ยง: <b>${pos['risk_amount']:,.2f}</b>  ({pos['risk_pct']}% ของพอร์ต)",
    ]
    if rr_line: lines.append(rr_line)
    if hk_line: lines.append(hk_line)
    lines += [
        "",
        f"{size_warn}",
        f"💡 {risk_note}",
        "",
        "📌 <b>แนะนำให้ทำ:</b>",
        f"  1️⃣ ยืนยันสัญญาณ (RSI / หลายกรอบเวลา / แท่งเทียน) ก่อนเข้าจริง",
        f"  2️⃣ เข้าซื้อ {pos['shares']:,} หุ้น ที่ราคาใกล้ ${pos['entry']:.2f}",
        f"  3️⃣ ตั้งจุดตัดขาดทุนที่ ${pos['stop']:.2f} ทันทีหลังเข้า",
        f"  4️⃣ ห้ามซื้อเพิ่มถ้าราคาลงมาใกล้จุดตัดขาดทุน",
    ]
    if note: lines.append(f"\n📋 หมายเหตุ: {note}")
    lines += ["", f"📊 <a href='{tv}'>ดูกราฟ TradingView</a>", f"🕐 {now_bkk_str()}"]
    return "\n".join(lines)


def build_mtf_message(stock, alert, mtf_result, quote):
    emoji, symbol, name, price, pct, arrow, sign, tv = _header(stock, quote, alert)
    note    = alert.get("note", "")
    overall = mtf_result["overall"]
    trend_icon = {
        "strong_bullish": "🟢🟢", "bullish": "🟢",
        "neutral": "⚪", "bearish": "🔴",
        "strong_bearish": "🔴🔴", "unknown": "❓"
    }
    align_th = {
        "strong_bullish_all": "🟢🟢🟢 ทุกกรอบเวลาเป็นขาขึ้น — แนวโน้มแข็งแกร่งมาก",
        "mostly_bullish":     "🟢🟢 ส่วนใหญ่เป็นขาขึ้น — แนวโน้มขาขึ้น",
        "leaning_bullish":    "🟡🟢 เอนไปทางขาขึ้น — ยังไม่แข็งแกร่งพอ",
        "strong_bearish_all": "🔴🔴🔴 ทุกกรอบเวลาเป็นขาลง — ระวังขาลงรุนแรง!",
        "mostly_bearish":     "🔴🔴 ส่วนใหญ่เป็นขาลง — แนวโน้มขาลง",
        "leaning_bearish":    "🟡🔴 เอนไปทางขาลง — ระวัง",
        "mixed":              "⚪ สัญญาณขัดแย้ง — ทิศทางยังไม่ชัด รอสัญญาณ",
    }

    action_map = {
        "strong_bullish_all": [
            "1️⃣ แนวโน้มแข็งแกร่งทุกกรอบเวลา — ถือหุ้นหรือเข้าซื้อใหม่ได้",
            "2️⃣ ใช้เส้นค่าเฉลี่ยบนกราฟ 4 ชั่วโมงเป็นแนวรับแบบเคลื่อนที่",
            "3️⃣ ตั้งจุดตัดขาดทุนใต้จุดต่ำสุดของการกลับตัวล่าสุดบนกราฟรายวัน",
        ],
        "mostly_bullish": [
            "1️⃣ ทิศทางเป็นขาขึ้น — หาจังหวะซื้อบนกราฟกรอบเล็ก",
            "2️⃣ รอ RSI ย่อลงหรือแท่งเทียนกลับตัวบนกราฟ 1H/4H",
            "3️⃣ ตั้งจุดตัดขาดทุนใต้เส้นค่าเฉลี่ย 21 วันบนกราฟ 4 ชั่วโมง",
        ],
        "leaning_bullish": [
            "1️⃣ สัญญาณอ่อน — ใช้เงินลงทุนน้อยกว่าปกติ",
            "2️⃣ ต้องการสัญญาณเพิ่ม เช่น ปริมาณซื้อขายพุ่งหรือรูปแบบแท่งเทียน",
            "3️⃣ อย่าลงทุนหมดในครั้งเดียว รอสัญญาณยืนยันเพิ่ม",
        ],
        "strong_bearish_all": [
            "1️⃣ ระวัง! ทุกกรอบเวลาเป็นขาลง — ไม่ควรซื้อ",
            "2️⃣ ถ้าถือหุ้นอยู่ → พิจารณาขายตัดขาดทุนทันที",
            "3️⃣ รอแนวโน้มกลับตัวก่อนเข้าซื้อใหม่",
        ],
        "mostly_bearish": [
            "1️⃣ ทิศทางเป็นขาลง — หลีกเลี่ยงการซื้อ",
            "2️⃣ ถ้าถือหุ้นอยู่ → ขยับจุดตัดขาดทุนให้ใกล้ขึ้น",
            "3️⃣ รอสัญญาณส่วนใหญ่กลับมาเป็นขาขึ้นก่อน",
        ],
        "mixed": [
            "1️⃣ สัญญาณยังขัดแย้งกัน — ยังไม่ควรเทรด",
            "2️⃣ รอให้กรอบเวลาใหญ่ (รายวัน) กลับมาเป็นขาขึ้นก่อน",
            "3️⃣ ติดตามการแจ้งเตือนรอบถัดไป",
        ],
    }
    steps = action_map.get(overall, ["1️⃣ ดูกราฟเพิ่มเติมก่อนตัดสินใจ"])

    lines = [
        f"{emoji} <b>🔔 แจ้งเตือนหลายกรอบเวลา: {symbol}</b> ({name})", "",
        f"🏷️ ประเภท: <b>วิเคราะห์หลายกรอบเวลา</b>",
        f"📡 ภาพรวม: <b>{align_th.get(overall, overall)}</b>",
        f"💰 ราคาปัจจุบัน: <b>${price:.4f}</b>  {arrow} {sign}{pct:.2f}%",
        f"📊 ขาขึ้น:{mtf_result['bull_count']}/{mtf_result['total']}  ขาลง:{mtf_result['bear_count']}/{mtf_result['total']}", "",
        "⏱ รายละเอียดแต่ละกรอบเวลา:",
    ]
    for tf, d in mtf_result["timeframes"].items():
        ti = trend_icon.get(d["trend"], "⚪")
        rsi_val = d["rsi"]
        rsi_str = f"RSI={rsi_val}" if rsi_val else ""
        rsi_warn = ""
        if rsi_val and rsi_val >= 75:
            rsi_warn = " ⚠️ ถูกซื้อมากเกินไป"
        elif rsi_val and rsi_val <= 25:
            rsi_warn = " 🔥 ถูกขายหนักมาก"
        elif rsi_val and rsi_val >= 70:
            rsi_warn = " ⚡ ใกล้โซนถูกซื้อมากเกิน"
        trend_th = d['trend'].replace('strong_bullish','ขาขึ้นแรง').replace('bullish','ขาขึ้น').replace('strong_bearish','ขาลงแรง').replace('bearish','ขาลง').replace('neutral','กลางๆ').replace('unknown','ไม่ชัด')
        lines.append(f"  {ti} <b>{tf}</b>: {trend_th.upper()}  {rsi_str}{rsi_warn}")

    lines += [
        "",
        "📌 <b>แนะนำให้ทำ:</b>",
    ] + [f"  {s}" for s in steps]
    # Trade box สำหรับ bullish alignment
    bullish_aligns = ("strong_bullish_all", "mostly_bullish")
    bearish_aligns = ("strong_bearish_all", "mostly_bearish")
    if overall in bullish_aligns:
        trade = _calc_entry_sl_tp(stock["symbol"], price, direction="bull")
        lines += _format_trade_box(trade, direction="bull")
    elif overall in bearish_aligns:
        trade = _calc_entry_sl_tp(stock["symbol"], price, direction="bear")
        lines += _format_trade_box(trade, direction="bear")
    if note: lines.append(f"\n📋 หมายเหตุ: {note}")
    lines += ["", f"📊 <a href='{tv}'>ดูกราฟ TradingView</a>", f"🕐 {now_bkk_str()}"]
    return "\n".join(lines)


def build_score_message(stock, alert, total_score, grade, breakdown, quote):
    emoji, symbol, name, price, pct, arrow, sign, tv = _header(stock, quote, alert)
    note      = alert.get("note", "")
    direction = alert.get("direction", "bullish")
    min_score = alert.get("min_score", 65)
    filled    = int(total_score / 10)
    bar       = "█" * filled + "░" * (10 - filled)

    grade_th = {
        "A": ("🔥 สัญญาณแข็งแกร่งมาก", "เชื่อถือได้สูง"),
        "B": ("✅ สัญญาณดี", "ควรเทรด"),
        "C": ("🟡 สัญญาณปานกลาง", "ระมัดระวังเพิ่มขึ้น"),
        "D": ("❌ สัญญาณอ่อน", "ควรรอสัญญาณที่ดีกว่า"),
    }
    grade_label, grade_desc = grade_th.get(grade, (grade, ""))

    direction_th = "ขาขึ้น" if direction == "bullish" else "ขาลง"

    action_map = {
        "A": [
            "1️⃣ คะแนนสูงมาก — เทรดได้ ตั้งจุดตัดขาดทุนเสมอ",
            "2️⃣ ใช้เงินลงทุนตามความเสี่ยงที่รับได้ 2% ของพอร์ต",
            "3️⃣ เป้าหมายกำไร = แนวต้านถัดไป จุดตัดขาดทุนใต้จุดต่ำสุดล่าสุด",
        ],
        "B": [
            "1️⃣ คะแนนดี — เทรดได้ แต่ยืนยันด้วยหลายกรอบเวลาก่อน",
            "2️⃣ ใช้เงินลงทุนปกติ (ความเสี่ยง 2%)",
            "3️⃣ ตั้งจุดตัดขาดทุนก่อน แล้วค่อยดูเป้าหมายกำไร",
        ],
        "C": [
            "1️⃣ คะแนนกลางๆ — เข้าได้แต่ลดเงินลงทุนลง 50%",
            "2️⃣ ต้องการการยืนยันเพิ่ม (ปริมาณซื้อขาย + รูปแบบแท่งเทียน)",
            "3️⃣ ระวังสัญญาณหลอก — ตั้งจุดตัดขาดทุนใกล้ๆ",
        ],
        "D": [
            "1️⃣ คะแนนต่ำ — ยังไม่ควรเทรด",
            "2️⃣ รอสัญญาณที่ดีขึ้น หรือกรอบเวลาใหญ่สอดคล้องกัน",
            "3️⃣ ติดตามการแจ้งเตือนรอบถัดไป",
        ],
    }
    steps = action_map.get(grade, [])

    lines = [
        f"{emoji} <b>🔔 แจ้งเตือนคะแนนสัญญาณ: {symbol}</b> ({name})", "",
        f"🏷️ ประเภท: <b>คะแนนความน่าเชื่อถือสัญญาณ</b>",
        f"🎯 คะแนน: <b>{total_score}/100</b>  [{bar}]",
        f"📊 เกรด: <b>{grade}</b>  — {grade_label} ({grade_desc})",
        f"📈 ทิศทาง: <b>{direction_th}</b>  (คะแนนขั้นต่ำ: {min_score})",
        f"💰 ราคาปัจจุบัน: <b>${price:.4f}</b>  {arrow} {sign}{pct:.2f}%",
        "",
        "📋 คะแนนย่อย:",
    ]
    for comp, info in breakdown.items():
        pct_comp = int(info["s"] / info["max"] * 100) if info["max"] > 0 else 0
        bar_mini = "█" * (info["s"] // 3) + "░" * ((info["max"] - info["s"]) // 3)
        lines.append(f"  • {comp}: <b>{info['s']}/{info['max']}</b> [{bar_mini}] — {info['note']}")

    lines += [
        "",
        "📌 <b>แนะนำให้ทำ:</b>",
    ] + [f"  {s}" for s in steps]
    # Trade box สำหรับ grade A/B bullish เท่านั้น
    if grade in ("A", "B") and direction == "bullish":
        trade = _calc_entry_sl_tp(stock["symbol"], price, direction="bull")
        lines += _format_trade_box(trade, direction="bull")
    if note: lines.append(f"\n📋 หมายเหตุ: {note}")
    lines += ["", f"📊 <a href='{tv}'>ดูกราฟ TradingView</a>", f"🕐 {now_bkk_str()}"]
    return "\n".join(lines)


def build_backtest_message(stock, alert, result, quote):
    emoji, symbol, name, price, pct, arrow, sign, tv = _header(stock, quote, alert)
    note = alert.get("note", "")

    if "error" in result:
        return f"{emoji} <b>🔔 ทดสอบย้อนหลัง: {symbol}</b>\n❌ {result['error']}"

    wr = result["win_rate"]
    wr_bar = "█" * int(wr/10) + "░" * (10 - int(wr/10))
    rating = ("🔥 ยอดเยี่ยม" if wr>=60 and result["avg_return"]>=2 else
              ("✅ ดี" if wr>=50 and result["avg_return"]>=1 else
               ("🟡 พอใช้" if wr>=45 else "❌ ต่ำกว่าเกณฑ์")))

    lines = [
        f"{emoji} <b>🔔 แจ้งเตือนทดสอบย้อนหลัง: {symbol}</b> ({name})", "",
        f"🏷️ ประเภท: <b>ผลทดสอบย้อนหลัง</b>",
        f"🔬 กฎที่ใช้: <b>{result['rule']}</b>",
        f"📊 อัตราชนะ: <b>{wr:.1f}%</b>  [{wr_bar}]",
        f"  • จำนวนเทรด: {result['total_trades']}  เฉลี่ย: {result['avg_return']:+.2f}%/เทรด",
        f"  • ดีสุด: {result['best']:+.2f}%  แย่สุด: {result['worst']:+.2f}%",
        f"  • อัตราส่วนกำไร/ขาดทุน: {result['profit_factor']:.2f}",
        f"⭐ {rating}",
        f"💰 ราคาปัจจุบัน: <b>${price:.4f}</b>  {arrow} {sign}{pct:.2f}%",
    ]
    if note: lines.append(f"📋 หมายเหตุ: {note}")
    lines += ["", f"📊 <a href='{tv}'>ดูกราฟ TradingView</a>", f"🕐 {now_bkk_str()}"]
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
#  DAILY SUMMARY
# ══════════════════════════════════════════════════════════════════════════════

def build_daily_summary(watchlist, quotes_cache):
    lines = [
        "📊 <b>สรุปรายวัน — รายการหุ้นที่ติดตาม</b>",
        f"🕐 {now_bkk_str()}", "",
    ]
    for stock in watchlist:
        symbol = stock["symbol"]
        name   = stock["name"]
        quote  = quotes_cache.get(symbol)
        if not quote:
            lines.append(f"• <b>{symbol}</b> — ⚠️ ไม่มีข้อมูล")
            continue
        price = quote["price"]
        pct   = quote["change_pct"]
        arrow = "📈" if pct >= 0 else "📉"
        sign  = "+" if pct >= 0 else ""
        lines.append(
            f"• <b>{symbol}</b> ({name})\n"
            f"  💰 ${price:.4f}  {arrow} {sign}{pct:.2f}%"
        )
        for alert in stock.get("alerts", []):
            if alert["type"] == "price_target":
                target   = alert["target_price"]
                diff_pct = ((price - target) / target) * 100
                diff_s   = "+" if diff_pct >= 0 else ""
                lines.append(
                    f"  🎯 เป้าหมาย ${target:.4f} — "
                    f"{'✅ ถึงเป้าแล้ว' if abs(diff_pct) < 0.5 else f'{diff_s}{diff_pct:.1f}% จากเป้า'}"
                )
        lines.append("")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    config   = load_json(WATCHLIST_PATH, {})
    settings = config.get("settings", {})
    watchlist = config.get("watchlist", [])

    token   = os.environ.get(settings.get("telegram_bot_token_env", "TELEGRAM_BOT_TOKEN"), "")
    chat_id = os.environ.get(settings.get("telegram_chat_id_env", "TELEGRAM_CHAT_ID"), "")

    if not token or not chat_id:
        print("ERROR: TELEGRAM_BOT_TOKEN หรือ TELEGRAM_CHAT_ID ไม่ได้ตั้งค่า")
        sys.exit(1)

    default_cooldown = settings.get("cooldown_minutes", 60)
    state            = load_json(STATE_PATH, {})
    log              = load_json(LOG_PATH, [])
    quotes_cache     = {}
    fired_count      = 0

    print(f"[{now_str()}] เริ่ม alert check — {len(watchlist)} symbols")

    for stock in watchlist:
        if not stock.get("enabled", True):
            continue

        symbol = stock["symbol"]
        print(f"\n[{symbol}] กำลังดึงข้อมูล...")

        quote = fetch_quote(symbol)
        if quote is None:
            print(f"  [{symbol}] ข้ามเนื่องจากไม่มีข้อมูล")
            continue

        quotes_cache[symbol] = quote
        print(f"  [{symbol}] Price=${quote['price']:.4f}  Chg={quote['change_pct']:+.2f}%  Vol={quote['volume']:.0f}")

        sym_state = state.get(symbol, {})

        for alert in stock.get("alerts", []):
            alert_id = alert["id"]
            atype    = alert["type"]
            cooldown = alert.get("cooldown_minutes", default_cooldown)

            # Cooldown check
            last_fired = sym_state.get(alert_id, {}).get("last_fired", "")
            if last_fired and minutes_since(last_fired) < cooldown:
                remaining = cooldown - minutes_since(last_fired)
                print(f"  [{alert_id}] Cooldown เหลือ {remaining:.0f} นาที")
                continue

            triggered        = False
            triggered_value  = 0
            msg              = None

            # ─── Route to correct checker ─────────────────────────────────

            if atype == "price_target":
                triggered, triggered_value = check_price_target(alert, quote)
                if triggered:
                    msg = build_message(stock, alert, quote, triggered_value)

            elif atype == "percent_change":
                triggered, triggered_value = check_percent_change(alert, quote)
                if triggered:
                    msg = build_message(stock, alert, quote, triggered_value)

            elif atype == "volume_spike":
                triggered, triggered_value = check_volume_spike(alert, quote)
                if triggered:
                    msg = build_message(stock, alert, quote, triggered_value)

            elif atype == "support_resistance":
                triggered, triggered_value = check_support_resistance(alert, quote)
                if triggered:
                    msg = build_message(stock, alert, quote, triggered_value)

            elif atype == "rsi":
                triggered, rsi, prev_rsi, rsi_price = check_rsi(alert, symbol)
                if triggered and rsi is not None:
                    msg = build_rsi_message(stock, alert, rsi, prev_rsi, rsi_price, quote)
                    triggered_value = rsi

            elif atype == "ma_crossover":
                triggered, fast_ma, slow_ma, ma_price, gap_pct = check_ma_crossover(alert, symbol)
                if triggered and fast_ma is not None:
                    msg = build_ma_message(stock, alert, fast_ma, slow_ma, ma_price, gap_pct, quote)
                    triggered_value = gap_pct or 0

            elif atype == "candle_pattern":
                triggered, found_pats, candle_info = check_candle_pattern(alert, symbol)
                if triggered:
                    msg = build_candle_message(stock, alert, found_pats, candle_info, quote)
                    triggered_value = len(found_pats)

            elif atype == "news_sentiment":
                triggered, news_list = check_news_sentiment(alert, symbol)
                if triggered:
                    msg = build_news_message(stock, alert, news_list, quote)
                    triggered_value = len(news_list)

            elif atype == "position_size":
                triggered, pos = check_position_size(alert, symbol, quote)
                if triggered and pos:
                    msg = build_position_message(stock, alert, pos, quote)
                    triggered_value = pos.get("shares", 0)

            elif atype == "mtf_alignment":
                triggered, mtf_result = check_mtf_alignment(alert, symbol)
                if triggered:
                    msg = build_mtf_message(stock, alert, mtf_result, quote)
                    triggered_value = mtf_result.get("bull_count", 0)

            elif atype == "alert_score":
                triggered, total_score, grade, breakdown = check_alert_score(alert, symbol)
                if triggered:
                    msg = build_score_message(stock, alert, total_score, grade, breakdown, quote)
                    triggered_value = total_score

            elif atype == "backtest_check":
                triggered, bt_result = check_backtest(alert, symbol)
                if triggered:
                    msg = build_backtest_message(stock, alert, bt_result, quote)
                    triggered_value = bt_result.get("win_rate", 0)

            else:
                print(f"  [{alert_id}] ❓ Unknown type: {atype}")
                continue

            if not triggered:
                print(f"  [{alert_id}] ไม่ trigger ({atype})")
                continue

            if msg is None:
                print(f"  [{alert_id}] Triggered แต่ไม่มี message")
                continue

            print(f"  [{alert_id}] ✅ TRIGGERED! กำลังส่ง Telegram...")
            success = send_telegram(token, chat_id, msg)

            if success:
                if symbol not in state:
                    state[symbol] = {}
                state[symbol][alert_id] = {"last_fired": now_str()}
                log.append({
                    "timestamp":  now_str(),
                    "symbol":     symbol,
                    "alert_id":   alert_id,
                    "type":       atype,
                    "price":      quote["price"],
                    "change_pct": quote["change_pct"],
                    "value":      triggered_value,
                })
                fired_count += 1
                print(f"  [{alert_id}] ✅ Telegram ส่งสำเร็จ")
            else:
                print(f"  [{alert_id}] ❌ Telegram ส่งไม่สำเร็จ")

        time.sleep(1)

    # ─── Daily Summary ──────────────────────────────────────────────────
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
