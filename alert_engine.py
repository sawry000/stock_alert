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
#  MODULE 4 — SUPPORT / RESISTANCE  (Auto-Detect ถ้า level == 0)
# ══════════════════════════════════════════════════════════════════════════════
#
#  ถ้า level > 0  → ใช้ level ที่ตั้งไว้ (manual mode)
#  ถ้า level == 0 → คำนวณอัตโนมัติจาก Swing High/Low + Cluster (auto mode)
#
#  Auto-detect fields (ใช้ได้เมื่อ level == 0):
#    auto_lookback:   จำนวนแท่งย้อนหลัง (default 60)
#    auto_swing_bars: จำนวนแท่งซ้าย/ขวาสำหรับ swing point (default 2)
#    auto_min_dist_pct: ระยะขั้นต่ำจากราคาปัจจุบัน % (default 0.5)
#    auto_max_dist_pct: ระยะสูงสุดจากราคาปัจจุบัน % (default 25.0)

def _find_auto_sr_levels(symbol, direction, alert):
    """
    คำนวณแนวรับ/ต้านอัตโนมัติจาก Swing High/Low + ATR Cluster
    Returns: (level, all_supports, all_resistances, atr)
    """
    lookback   = alert.get("auto_lookback",    60)
    swing_bars = alert.get("auto_swing_bars",   2)
    min_dist   = alert.get("auto_min_dist_pct", 0.5)
    max_dist   = alert.get("auto_max_dist_pct", 25.0)

    # ดึงข้อมูล — เพิ่ม buffer สำหรับ warmup
    fetch_bars = lookback + swing_bars * 2 + 10
    hist = fetch_history(symbol, period=f"{min(fetch_bars + 30, 365)}d", interval="1d")
    if hist is None or len(hist) < swing_bars * 2 + 5:
        print(f"  [{symbol}] Auto S/R: ข้อมูลไม่พอ")
        return None, [], [], None

    highs  = list(hist["High"].astype(float))
    lows   = list(hist["Low"].astype(float))
    closes = list(hist["Close"].astype(float))

    # ใช้เฉพาะ lookback แท่งล่าสุด
    use_n  = min(len(closes), lookback)
    h      = highs[-use_n:]
    l      = lows[-use_n:]
    c      = closes[-use_n:]
    price  = closes[-1]

    # ATR สำหรับ cluster threshold
    atr = _calc_atr(highs, lows, closes, 14)
    if not atr or atr <= 0:
        atr = price * 0.02  # fallback 2%
    cluster_thr = max(atr * 0.6, price * 0.003)  # อย่างน้อย 0.3% ของราคา

    sb = swing_bars

    # ─── หา Swing Highs ─────────────────────────────────────────────────
    swing_highs = []
    for i in range(sb, len(h) - sb):
        is_peak = all(h[i] >= h[i - k] for k in range(1, sb + 1)) and \
                  all(h[i] >= h[i + k] for k in range(1, sb + 1))
        if is_peak:
            swing_highs.append(h[i])

    # ─── หา Swing Lows ──────────────────────────────────────────────────
    swing_lows = []
    for i in range(sb, len(l) - sb):
        is_trough = all(l[i] <= l[i - k] for k in range(1, sb + 1)) and \
                    all(l[i] <= l[i + k] for k in range(1, sb + 1))
        if is_trough:
            swing_lows.append(l[i])

    # เพิ่ม Recent High/Low 20 แท่ง และ EMA key levels
    if len(h) >= 20:
        swing_highs.append(max(h[-20:]))
        swing_lows.append(min(l[-20:]))
    if len(h) >= 50:
        swing_highs.append(max(h[-50:]))
        swing_lows.append(min(l[-50:]))

    # EMA เป็น dynamic S/R
    ema21_list = _calc_ema(closes, 21)
    ema50_list = _calc_ema(closes, 50)
    ema21 = next((v for v in reversed(ema21_list) if v is not None), None)
    ema50 = next((v for v in reversed(ema50_list) if v is not None), None)
    if ema21: swing_lows.append(ema21); swing_highs.append(ema21)
    if ema50: swing_lows.append(ema50); swing_highs.append(ema50)

    # ─── Cluster: รวม levels ที่ใกล้กัน ─────────────────────────────────
    def cluster(levels, thr):
        if not levels:
            return []
        levels = sorted(set(round(v, 4) for v in levels))
        clusters, cur = [], [levels[0]]
        for lv in levels[1:]:
            if lv - cur[-1] <= thr:
                cur.append(lv)
            else:
                clusters.append(cur)
                cur = [lv]
        clusters.append(cur)
        result = []
        for cl in clusters:
            centroid = sum(cl) / len(cl)
            # นับ touches: จำนวน close ที่อยู่ภายใน ±cluster_thr ของ centroid
            touches = sum(1 for cv in c if abs(cv - centroid) <= thr * 1.5)
            result.append({
                "level":   round(centroid, 4),
                "touches": max(len(cl), touches),
                "raw_count": len(cl),
            })
        return result

    sup_clusters = cluster(swing_lows,  cluster_thr)
    res_clusters = cluster(swing_highs, cluster_thr)

    # ─── กรองระยะ min/max จากราคา ────────────────────────────────────────
    supports = [
        s for s in sup_clusters
        if s["level"] < price * (1 - min_dist / 100)
        and s["level"] > price * (1 - max_dist / 100)
    ]
    resistances = [
        r for r in res_clusters
        if r["level"] > price * (1 + min_dist / 100)
        and r["level"] < price * (1 + max_dist / 100)
    ]

    # ─── เรียงจากใกล้สุด ─────────────────────────────────────────────────
    supports    = sorted(supports,    key=lambda x: price - x["level"])
    resistances = sorted(resistances, key=lambda x: x["level"] - price)

    # เลือก level ที่ดีที่สุด
    best_level = None
    if direction == "break_below" and supports:
        # เลือก support ที่ใกล้สุด (nearest first) ที่มี touches >= 1
        best_level = supports[0]["level"]
    elif direction == "break_above" and resistances:
        best_level = resistances[0]["level"]

    return best_level, supports, resistances, round(atr, 4)


def _atr_fallback_level(symbol, price, direction, atr_mult=2.0):
    """
    ATR-based fallback level เมื่อ swing auto-detect หา support ไม่ได้
    SL = entry × (1 - ATR×mult/price)   break_below
    TP = entry × (1 + ATR×mult/price)   break_above
    Returns: (level, atr) หรือ (None, None)
    """
    try:
        hist = fetch_history(symbol, period="90d", interval="1d")
        if hist is None or len(hist) < 16:
            return None, None
        highs  = list(hist["High"].astype(float))
        lows   = list(hist["Low"].astype(float))
        closes = list(hist["Close"].astype(float))
        atr    = _calc_atr(highs, lows, closes, 14)
        if not atr or atr <= 0:
            return None, None
        if direction == "break_below":
            level = round(price - atr * atr_mult, 4)
        else:
            level = round(price + atr * atr_mult, 4)
        return level, round(atr, 4)
    except Exception as e:
        print(f"  [{symbol}] ATR fallback error: {e}")
        return None, None


def check_support_resistance(alert, quote, symbol=None):
    """
    ตรวจ support/resistance break
    - level > 0 : ใช้ค่าที่ตั้งไว้ (manual)
    - level == 0: คำนวณจาก swing high/low อัตโนมัติ (auto)
                  → ถ้าหา swing ไม่ได้ → fallback ATR×2 อัตโนมัติ (ไม่ silent fail)

    Returns: (triggered, price, used_level, supports, resistances, atr)
    """
    price     = quote["price"]
    level     = alert.get("level", 0)
    direction = alert.get("direction", "break_below")

    # ─── Manual mode ──────────────────────────────────────────────────────
    if level > 0:
        triggered = (
            (direction == "break_below" and price < level) or
            (direction == "break_above" and price > level)
        )
        return triggered, price, level, [], [], None

    # ─── Auto mode ────────────────────────────────────────────────────────
    if not symbol:
        print(f"  [support_resistance] Auto mode ต้องการ symbol")
        return False, price, None, [], [], None

    print(f"  [{symbol}] S/R Auto-detect กำลังคำนวณ...")
    best_level, supports, resistances, atr = _find_auto_sr_levels(symbol, direction, alert)

    # ─── ATR Fallback เมื่อ swing detect ล้มเหลว ──────────────────────────
    if best_level is None:
        print(f"  [{symbol}] S/R Auto: swing หา level ไม่ได้ → ใช้ ATR×2 fallback")
        best_level, atr = _atr_fallback_level(symbol, price, direction, atr_mult=2.0)
        if best_level is None:
            print(f"  [{symbol}] S/R: ATR fallback ล้มเหลวด้วย — ข้าม")
            return False, price, None, [], [], None
        print(f"  [{symbol}] S/R ATR fallback: level={best_level:.4f}  atr={atr}")

    dist_pct = abs(price - best_level) / price * 100
    print(f"  [{symbol}] S/R: direction={direction} level={best_level:.4f} dist={dist_pct:.1f}%")

    triggered = (
        (direction == "break_below" and price < best_level) or
        (direction == "break_above" and price > best_level)
    )
    return triggered, price, best_level, supports, resistances, atr


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


def _news_hash(title):
    """สร้าง hash สั้นจาก title เพื่อ dedup news"""
    import hashlib
    return hashlib.md5(title.strip().lower().encode()).hexdigest()[:12]


def check_news_sentiment(alert, symbol, sym_state=None):
    """
    ดึงข่าว วิเคราะห์ sentiment แปลไทย
    — ป้องกัน spam ด้วย news title dedup (hash-based)
    Returns: (triggered, news_list)
    """
    condition    = alert.get("condition", "any")
    min_news     = alert.get("min_news", 1)
    hours_back   = alert.get("hours_back", 24)
    max_news     = alert.get("max_news", 5)
    do_translate = alert.get("translate", True)

    # ─── โหลด seen news hashes จาก state ─────────────────────────────
    alert_id  = alert.get("id", "")
    seen_hashes = set()
    if sym_state and alert_id:
        seen_hashes = set(sym_state.get(alert_id, {}).get("seen_news", []))

    try:
        ticker   = yf.Ticker(symbol)
        raw_news = ticker.news or []
    except Exception as e:
        print(f"  [{symbol}] News fetch error: {e}")
        return False, []

    now_ts  = now_utc().timestamp()
    cutoff  = now_ts - hours_back * 3600 if hours_back > 0 else 0
    results = []
    new_hashes = []

    for item in raw_news:
        content   = item.get("content", item)
        title     = content.get("title") or item.get("title") or ""
        pub_ts_raw = (content.get("pubDate") or item.get("providerPublishTime") or
                      item.get("published_at") or 0)
        link      = (content.get("canonicalUrl", {}).get("url") or
                     item.get("link") or item.get("url") or "")
        publisher = (content.get("provider", {}).get("displayName") or
                     item.get("publisher") or "Yahoo Finance")
        summary   = content.get("summary") or item.get("summary") or ""

        if not title:
            continue

        # ─── NEWS DEDUP: ข้าม news ที่เคย fire แล้ว ─────────────────
        nh = _news_hash(title)
        if nh in seen_hashes:
            continue

        try:
            if isinstance(pub_ts_raw, str):
                pub_dt = datetime.fromisoformat(pub_ts_raw.replace("Z", "+00:00"))
                pub_ts = pub_dt.timestamp()
            else:
                pub_ts = float(pub_ts_raw)
        except Exception:
            pub_ts = 0

        if hours_back > 0 and pub_ts > 0 and pub_ts < cutoff:
            continue

        score = _sentiment_score(f"{title} {summary}")
        if score >= 2:   label = "positive"
        elif score <= -2: label = "negative"
        elif score == 1:  label = "slightly_positive"
        elif score == -1: label = "slightly_negative"
        else:             label = "neutral"

        title_th = _translate_th(title) if do_translate else title

        if pub_ts > 0:
            bkk_dt  = datetime.fromtimestamp(pub_ts, tz=timezone.utc) + timedelta(hours=7)
            pub_str = bkk_dt.strftime("%d/%m %H:%M ICT")
        else:
            pub_str = ""

        results.append({
            "title": title, "title_th": title_th,
            "score": score, "label": label,
            "publisher": publisher, "link": link, "pub_str": pub_str,
            "hash": nh,
        })
        new_hashes.append(nh)
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
        matching = results

    triggered = len(matching) >= min_news
    return triggered, matching[:5], new_hashes


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

    lines = [f"{emoji} <b>ALERT: {symbol}</b> ({name})", ""]

    if atype == "price_target":
        target = alert["target_price"]
        dir_th = {"below_or_equal": "ราคาถึงโซน Buy ✅", "above_or_equal": "ราคาถึง Target ✅"}.get(alert.get("direction",""), "")
        lines += [
            f"💰 Price: <b>${price:.4f}</b>  {arrow} {sign}{pct:.2f}%",
            f"🎯 Target: <b>${target:.4f}</b>  {dir_th}",
        ]
        if action: lines.append(f"⚡ Signal: <b>{action}</b>")
        lines += [
            "",
            "📌 <b>สิ่งที่ควรทำ:</b>",
            f"  1️⃣ เปิดชาร์ท TradingView ยืนยัน S/R + Volume",
            f"  2️⃣ รอ candle ปิดยืนยัน ไม่เข้า market order ทันที",
            f"  3️⃣ ตั้ง Stop Loss ต่ำกว่า low ของ trigger candle",
        ]

    elif atype == "percent_change":
        direction = alert.get("direction", "down")
        thr = alert.get("threshold_pct", 5.0)
        if direction == "down":
            interp = f"⚠️ ราคาลง {abs(pct):.1f}% — ตรวจสอบ Stop Loss"
            steps = [
                "  1️⃣ ถ้าถือ position อยู่ → ตรวจ Stop Loss ยัง valid มั้ย",
                "  2️⃣ ดู Volume ว่าเป็น sell-off จริงหรือแค่ pullback",
                "  3️⃣ ดู RSI & Support ก่อนตัดสินใจ cut หรือ hold",
            ]
        else:
            interp = f"🚀 ราคาขึ้น {pct:.1f}% — Momentum surge"
            steps = [
                "  1️⃣ ตรวจ Volume ยืนยัน breakout จริง",
                "  2️⃣ ถ้ายังไม่ได้เข้า → รอ pullback หรือเข้า breakout",
                "  3️⃣ ตั้ง Trailing Stop ถ้าถืออยู่แล้ว",
            ]
        lines += [
            f"💰 Price: <b>${price:.4f}</b>",
            f"{arrow} Change: <b>{sign}{pct:.2f}%</b>  (trigger ที่ {thr}%)",
            f"💡 {interp}",
            "",
            "📌 <b>สิ่งที่ควรทำ:</b>",
        ] + steps

    elif atype == "volume_spike":
        vol_x = triggered_value
        # Interpretation: price direction + volume = buy or sell pressure
        if pct >= 1.0:
            vol_type = "🟢 Buy Volume — Breakout Confirm"
            vol_action = [
                "  1️⃣ Momentum สูง — อาจเข้า หรือ add position ได้",
                "  2️⃣ ตั้ง Stop ใต้ breakout candle low",
                "  3️⃣ Target = แนวต้านถัดไป บน chart",
            ]
        elif pct <= -1.0:
            vol_type = "🔴 Sell Volume — Distribution Warning"
            vol_action = [
                "  1️⃣ ถ้าถือ position → พิจารณา tighten stop",
                "  2️⃣ อย่าเข้าซื้อตอน volume ขาย — รอ stabilize ก่อน",
                "  3️⃣ ดู support ถัดไปว่าอยู่ที่ราคาเท่าไหร่",
            ]
        else:
            vol_type = "🟡 Volume Spike — Neutral (ราคาไม่ชัด)"
            vol_action = [
                "  1️⃣ รอดูทิศทางราคาให้ชัดขึ้น",
                "  2️⃣ ดู candle ต่อไปว่าปิด Bull หรือ Bear",
                "  3️⃣ ติดตาม momentum ต่อในชั่วโมงถัดไป",
            ]
        mult_set = alert.get("multiplier", 2.0)
        lines += [
            f"💰 Price: <b>${price:.4f}</b>  {arrow} {sign}{pct:.2f}%",
            f"🔊 Volume: <b>{vol_x:.1f}x</b> above average  (trigger ที่ {mult_set}x)",
            f"💡 {vol_type}",
            "",
            "📌 <b>สิ่งที่ควรทำ:</b>",
        ] + vol_action

    elif atype == "support_resistance":
        level = alert.get("level", 0)
        direction = alert.get("direction", "break_below")
        if direction == "break_above":
            dir_th = "ทะลุแนวต้านขึ้น 🚀"
            sr_steps = [
                "  1️⃣ ยืนยัน candle ปิดเหนือแนวต้านจริง",
                "  2️⃣ ตรวจ Volume ว่า spike หรือเปล่า",
                "  3️⃣ เข้าซื้อ Breakout หรือรอ retest แนวต้านเดิม",
            ]
        else:
            dir_th = "หลุดแนวรับลง ⚠️"
            sr_steps = [
                "  1️⃣ ถ้าถือ position → พิจารณา cut loss ทันที",
                "  2️⃣ ดู support ถัดไปว่าอยู่ที่เท่าไหร่",
                "  3️⃣ อย่ารีบ averaging down — รอสัญญาณ reversal ก่อน",
            ]
        lines += [
            f"💰 Price: <b>${price:.4f}</b>  {arrow} {sign}{pct:.2f}%",
            f"⚠️ Level: <b>${level:.4f}</b>  — {dir_th}",
            "",
            "📌 <b>สิ่งที่ควรทำ:</b>",
        ] + sr_steps

    if tf:   lines.append(f"\n⏱ Timeframe: {tf}")
    if note: lines.append(f"📋 Note: {note}")
    lines += ["", f"📊 <a href='{tv}'>TradingView</a>", f"🕐 {now_bkk_str()}"]
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
        "oversold":          ("📉 Oversold", "RSI ต่ำ — ราคาถูก oversold อาจกลับตัวขึ้น"),
        "overbought":        ("📈 Overbought", "RSI สูง — ราคาถูก overbought ระวังกลับตัวลง"),
        "extreme_oversold":  ("🔥 Extreme Oversold", "RSI ต่ำมาก — โอกาสทอง แต่ risk สูง ใช้ size เล็ก"),
        "extreme_overbought":("🌡️ Extreme Overbought", "RSI สูงมาก — ระวังมาก อย่าเพิ่ม position"),
        "turning_up":        ("↗️ RSI Turning Up", "Momentum เริ่มกลับตัวขึ้น — สัญญาณ entry ต้น"),
        "turning_down":      ("↘️ RSI Turning Down", "Momentum อ่อนแรง — พิจารณา tighten stop"),
    }
    cond_label, cond_desc = cond_th.get(cond, (cond, ""))

    # Next action based on condition
    action_map = {
        "oversold":          ["1️⃣ รอ candle กลับตัว (Hammer / Engulfing) บน 1D", "2️⃣ ยืนยัน Volume ขึ้น + RSI เริ่ม turn up", "3️⃣ เข้าซื้อ พร้อมตั้ง Stop ใต้ recent low"],
        "extreme_oversold":  ["1️⃣ โอกาสหายาก — ใช้ position size เล็กลงก่อน", "2️⃣ รอ candle reversal อย่างน้อย 1 แท่ง", "3️⃣ เข้าแบบ scale-in ไม่ all-in ครั้งเดียว"],
        "overbought":        ["1️⃣ ถ้าถือ position → พิจารณา take profit บางส่วน", "2️⃣ อย่า add position ใหม่ในโซนนี้", "3️⃣ ตั้ง trailing stop ป้องกัน reversal"],
        "extreme_overbought":["1️⃣ ระวังมาก — หลีกเลี่ยงเข้าซื้อ", "2️⃣ ถ้าถือ → tighten stop หรือ take profit", "3️⃣ รอ RSI กลับมา < 70 ก่อนพิจารณา re-entry"],
        "turning_up":        ["1️⃣ สัญญาณเร็ว — รอยืนยัน 1-2 แท่งก่อนเข้า", "2️⃣ ดู Volume ว่าเพิ่มขึ้นพร้อม RSI มั้ย", "3️⃣ Entry: breakout เหนือ high ของ trigger candle"],
        "turning_down":      ["1️⃣ ถ้าถือ → พิจารณา tighten stop", "2️⃣ อย่า add — รอดู momentum ก่อน", "3️⃣ ถ้า RSI ลงต่อ + break support → cut"],
    }
    steps = action_map.get(cond, ["1️⃣ ดูชาร์ทยืนยันก่อนตัดสินใจ"])

    lines = [
        f"{emoji} <b>RSI ALERT: {symbol}</b> ({name})", "",
        f"📊 RSI({period}): <b>{rsi:.1f}</b> {rsi_arr} {rsi_move:+.1f}  (ก่อนหน้า: {prev_rsi:.1f})",
        f"⚡ {cond_label} — {cond_desc}",
        f"💰 Price: <b>${price:.4f}</b>  {arrow} {sign}{pct:.2f}%",
        f"⏱ Interval: {interval}",
    ]
    if action: lines.append(f"🎯 Signal: <b>{action}</b>")
    lines += [
        "",
        "📌 <b>สิ่งที่ควรทำ:</b>",
    ] + [f"  {s}" for s in steps]
    if note: lines.append(f"\n📋 Note: {note}")
    lines += ["", f"📊 <a href='{tv}'>TradingView</a>", f"🕐 {now_bkk_str()}"]
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
        "golden_cross":  ("🌟 Golden Cross — สัญญาณขาขึ้น!", "🟢", "BULLISH",
                          ["1️⃣ ยืนยัน candle ปิดเหนือ MA ทั้งคู่", "2️⃣ ตรวจ Volume เพิ่มขึ้นพร้อม cross", "3️⃣ Entry: เข้าซื้อได้ หรือรอ pullback มา test EMA fast"]),
        "death_cross":   ("💀 Death Cross — สัญญาณขาลง!", "🔴", "BEARISH",
                          ["1️⃣ ถ้าถือ position → พิจารณา cut หรือ hedge", "2️⃣ อย่าเข้าซื้อ — รอ Golden Cross กลับมาก่อน", "3️⃣ ดู support ถัดไป เผื่อ short opportunity"]),
        "above_both":    ("🚀 ราคาเหนือ MA ทั้งคู่", "🟢", "BULLISH",
                          ["1️⃣ Trend ชัดเจน — ถือ position ต่อได้", "2️⃣ ใช้ EMA fast เป็น dynamic support", "3️⃣ Stop ใต้ EMA slow"]),
        "below_both":    ("🔻 ราคาต่ำกว่า MA ทั้งคู่", "🔴", "BEARISH",
                          ["1️⃣ Downtrend ชัด — หลีกเลี่ยงซื้อ", "2️⃣ รอ price กลับมาเหนือ EMA fast ก่อน", "3️⃣ ถ้าถือ → tighten stop"]),
        "trend_bullish": ("📈 Trend กำลังขาขึ้น", "🟢", "BULLISH",
                          ["1️⃣ Bias ขาขึ้น — หา buy setup", "2️⃣ รอ RSI pullback ก่อนเข้า", "3️⃣ ดู candle reversal บน 4H"]),
        "trend_bearish": ("📉 Trend กำลังขาลง", "🔴", "BEARISH",
                          ["1️⃣ Bias ขาลง — หลีกเลี่ยงซื้อ", "2️⃣ รอ reversal signal ที่ชัดเจนก่อน", "3️⃣ ถ้าถือ → review stop"]),
        "gap_expanding": ("↔️ ช่องว่าง MA ขยาย — Momentum แรงขึ้น", "🟡", "STRONG TREND",
                          ["1️⃣ Trend กำลังเร่ง — ติดตาม momentum", "2️⃣ อาจเป็นโอกาส add position ถ้า trend ตรงกับ bias", "3️⃣ ระวัง overextension — ดู RSI ด้วย"]),
    }
    label, trend_icon, trend_word, steps = cond_info.get(cond, (cond, "🟡", "UNKNOWN", []))

    lines = [
        f"{emoji} <b>MA CROSS ALERT: {symbol}</b> ({name})", "",
        f"⚡ {label}",
        f"💰 Price: <b>${price:.4f}</b>  {arrow} {sign}{pct:.2f}%",
        f"📊 {ma_type}{fast_p}/{slow_p}  ({interval}):",
        f"  • Fast {ma_type}{fast_p}: <b>${fast_ma:.4f}</b>",
        f"  • Slow {ma_type}{slow_p}: <b>${slow_ma:.4f}</b>",
        f"  • Gap: <b>{gap_pct:+.2f}%</b>",
        f"{trend_icon} Trend: <b>{trend_word}</b>",
    ]
    if action: lines.append(f"🎯 Signal: <b>{action}</b>")
    lines += [
        "",
        "📌 <b>สิ่งที่ควรทำ:</b>",
    ] + [f"  {s}" for s in steps]
    if note: lines.append(f"\n📋 Note: {note}")
    lines += ["", f"📊 <a href='{tv}'>TradingView</a>", f"🕐 {now_bkk_str()}"]
    return "\n".join(lines)


def build_candle_message(stock, alert, found_patterns, candle_info, quote):
    emoji, symbol, name, price, pct, arrow, sign, tv = _header(stock, quote, alert)
    note   = alert.get("note", "")
    action = alert.get("action", "")
    interval = alert.get("interval", "1d")

    pat_lines = "\n".join(
        f"  • {CANDLE_DESC_TH.get(p, p)}" for p in found_patterns
    ) or "  • ไม่พบ pattern ที่ระบุ"

    # Determine if bullish or bearish patterns
    bullish_pats = {"hammer","inverted_hammer","bullish_engulfing","three_white_soldiers","marubozu_bullish","morning_star"}
    bearish_pats = {"shooting_star","hanging_man","bearish_engulfing","three_black_crows","marubozu_bearish","evening_star"}
    is_bull_pat  = any(p in bullish_pats for p in found_patterns)
    is_bear_pat  = any(p in bearish_pats for p in found_patterns)

    if is_bull_pat:
        interp = "🟢 Bullish Pattern — โอกาสกลับตัวขึ้น"
        steps  = [
            "1️⃣ รอ candle ถัดไปยืนยัน (ควรเป็น green candle)",
            "2️⃣ ตรวจ Volume เพิ่มขึ้นพร้อม pattern",
            "3️⃣ Entry เหนือ high ของ pattern candle",
            "4️⃣ Stop Loss ใต้ low ของ pattern",
        ]
    elif is_bear_pat:
        interp = "🔴 Bearish Pattern — ระวังกลับตัวลง"
        steps  = [
            "1️⃣ ถ้าถือ position → tighten stop หรือ take profit",
            "2️⃣ อย่าเข้าซื้อใหม่ในโซนนี้",
            "3️⃣ รอ pattern bearish ยืนยันด้วย red candle ถัดไป",
        ]
    else:
        interp = "🟡 Neutral Pattern — ลังเล รอดูทิศทาง"
        steps  = [
            "1️⃣ ยังไม่ชัด — รอดู candle ถัดไปก่อน",
            "2️⃣ ดู RSI + Volume ประกอบ",
            "3️⃣ ไม่ควรเข้าเทรดจาก neutral pattern เดี่ยวๆ",
        ]

    lines = [
        f"{emoji} <b>CANDLE ALERT: {symbol}</b> ({name})", "",
        f"🕯️ Pattern ที่พบ ({interval}):", pat_lines,
        f"💡 {interp}",
        f"💰 Price: <b>${price:.4f}</b>  {arrow} {sign}{pct:.2f}%",
    ]
    if action: lines.append(f"🎯 Signal: <b>{action}</b>")
    lines += [
        "",
        "📌 <b>สิ่งที่ควรทำ:</b>",
    ] + [f"  {s}" for s in steps]
    if note: lines.append(f"\n📋 Note: {note}")
    lines += ["", f"📊 <a href='{tv}'>TradingView</a>", f"🕐 {now_bkk_str()}"]
    return "\n".join(lines)


def build_news_message(stock, alert, news_list, quote):
    emoji, symbol, name, price, pct, arrow, sign, tv = _header(stock, quote, alert)
    cond = alert.get("condition", "any")
    cond_th = {"any":"ข่าวใหม่","positive":"ข่าวบวก","negative":"ข่าวลบ",
               "strong_positive":"ข่าวบวกแรง","strong_negative":"ข่าวลบแรง","high_volume":"ข่าวเยอะมาก"}
    sent_icon = {"positive":"🟢","slightly_positive":"🟡","negative":"🔴","slightly_negative":"🟠","neutral":"⚪"}

    lines = [
        f"{emoji} <b>NEWS ALERT: {symbol}</b> ({name})",
        f"📋 {cond_th.get(cond, cond)} — พบ {len(news_list)} ข่าว",
        f"💰 Price: <b>${price:.4f}</b>  {arrow} {sign}{pct:.2f}%", "",
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

    lines += [f"📊 <a href='{tv}'>TradingView</a>", f"🕐 {now_bkk_str()}"]
    return "\n".join(lines)


def build_position_message(stock, alert, pos, quote):
    emoji, symbol, name, price, pct, arrow, sign, tv = _header(stock, quote, alert)
    note = alert.get("note", "")

    size_warn = "✅ ขนาด OK" if pos["pos_pct"] <= 20 else "⚠️ Position ใหญ่ — ระวัง Risk"
    rr_line  = f"  • R:R Ratio: <b>1:{pos['rr_ratio']:.1f}</b>" if pos.get("rr_ratio") else ""
    tgt_line = f"  • Target: <b>${pos['target']:.4f}</b> (+{pos.get('target_pct',0):.1f}%)" if pos.get("target") else ""
    hk_line  = f"  • Half-Kelly: <b>{pos.get('half_kelly_pct',0):.1f}%</b> พอร์ต" if pos.get("half_kelly_pct") else ""

    # Risk color assessment
    if pos["pos_pct"] > 20:
        risk_note = "⚠️ ขนาด position เกิน 20% — พิจารณาลด shares"
    elif pos["pos_pct"] > 15:
        risk_note = "🟡 ขนาด position ปานกลาง — ติดตามใกล้ชิด"
    else:
        risk_note = "🟢 ขนาด position เหมาะสม — ความเสี่ยงอยู่ในกรอบ"

    lines = [
        f"{emoji} <b>POSITION SIZE: {symbol}</b> ({name})", "",
        f"💰 Entry Reference: <b>${pos['entry']:.4f}</b>  {arrow} {sign}{pct:.2f}%",
        f"🛑 Stop Loss: <b>${pos['stop']:.4f}</b>  (-{pos['stop_pct']:.1f}% จาก entry)",
    ]
    if tgt_line: lines.append(tgt_line)
    lines += [
        "",
        f"📊 ผลคำนวณ ({pos['method']}):",
        f"  • จำนวน: <b>{pos['shares']:,} หุ้น</b>",
        f"  • มูลค่า: <b>${pos['pos_value']:,.2f}</b>  ({pos['pos_pct']:.1f}% พอร์ต)",
        f"  • Risk: <b>${pos['risk_amount']:,.2f}</b>  ({pos['risk_pct']}% พอร์ต)",
    ]
    if rr_line: lines.append(rr_line)
    if hk_line: lines.append(hk_line)
    lines += [
        "",
        f"{size_warn}",
        f"💡 {risk_note}",
        "",
        "📌 <b>สิ่งที่ควรทำ:</b>",
        f"  1️⃣ ยืนยัน signal (RSI / MTF / Candle) ก่อนเข้าจริง",
        f"  2️⃣ เข้าซื้อ {pos['shares']:,} หุ้น ที่ราคาใกล้ ${pos['entry']:.2f}",
        f"  3️⃣ ตั้ง Stop Loss ที่ ${pos['stop']:.2f} ทันทีหลังเข้า",
        f"  4️⃣ ไม่ Average Down ถ้า price ลงหา Stop",
    ]
    if note: lines.append(f"\n📋 Note: {note}")
    lines += ["", f"📊 <a href='{tv}'>TradingView</a>", f"🕐 {now_bkk_str()}"]
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
        "strong_bullish_all": "🟢🟢🟢 ทุก TF Bullish — แนวโน้มแข็งมาก",
        "mostly_bullish":     "🟢🟢 ส่วนใหญ่ Bullish — แนวโน้มขาขึ้น",
        "leaning_bullish":    "🟡🟢 เอนไปทาง Bullish — ยังไม่แข็งแกร่ง",
        "strong_bearish_all": "🔴🔴🔴 ทุก TF Bearish — ระวังขาลงแรง!",
        "mostly_bearish":     "🔴🔴 ส่วนใหญ่ Bearish — แนวโน้มขาลง",
        "leaning_bearish":    "🟡🔴 เอนไปทาง Bearish — ระวัง",
        "mixed":              "⚪ Mixed — ทิศทางยังไม่ชัด รอสัญญาณ",
    }

    # Next action by alignment
    action_map = {
        "strong_bullish_all": [
            "1️⃣ Trend แข็งแกร่งทุก TF — ถือ position หรือเข้าใหม่ได้",
            "2️⃣ ใช้ EMA บน 4H เป็น dynamic support",
            "3️⃣ Stop Loss ใต้ low ของ swing ล่าสุดบน 1D",
        ],
        "mostly_bullish": [
            "1️⃣ Bias ขาขึ้น — หา Buy setup บน TF เล็ก",
            "2️⃣ รอ RSI pullback หรือ candle reversal บน 1H/4H",
            "3️⃣ ตั้ง Stop ใต้ EMA21 บน 4H",
        ],
        "leaning_bullish": [
            "1️⃣ Bias อ่อน — ใช้ position size เล็ก",
            "2️⃣ ต้องการ signal เพิ่ม เช่น Volume spike หรือ candle pattern",
            "3️⃣ อย่า all-in — รอยืนยันมากขึ้น",
        ],
        "strong_bearish_all": [
            "1️⃣ ระวัง! ทุก TF ขาลง — ไม่ควรซื้อ",
            "2️⃣ ถ้าถือ position → พิจารณา cut loss ทันที",
            "3️⃣ รอ trend กลับตัวก่อนเข้าใหม่",
        ],
        "mostly_bearish": [
            "1️⃣ Bias ขาลง — หลีกเลี่ยงการซื้อ",
            "2️⃣ ถ้าถือ → tighten stop loss",
            "3️⃣ รอ MTF กลับมา mostly_bullish ก่อน",
        ],
        "mixed": [
            "1️⃣ สัญญาณยังขัดแย้ง — อย่าเพิ่ง trade",
            "2️⃣ รอให้ TF ใหญ่ (1D) กลับมา Bullish ก่อน",
            "3️⃣ ติดตาม alert รอบถัดไป",
        ],
    }
    steps = action_map.get(overall, ["1️⃣ ดูชาร์ทเพิ่มเติมก่อนตัดสินใจ"])

    lines = [
        f"{emoji} <b>MTF ALERT: {symbol}</b> ({name})", "",
        f"📡 Alignment: <b>{align_th.get(overall, overall)}</b>",
        f"💰 Price: <b>${price:.4f}</b>  {arrow} {sign}{pct:.2f}%",
        f"📊 Bull:{mtf_result['bull_count']}/{mtf_result['total']}  Bear:{mtf_result['bear_count']}/{mtf_result['total']}", "",
        "⏱ รายละเอียด:",
    ]
    for tf, d in mtf_result["timeframes"].items():
        ti = trend_icon.get(d["trend"], "⚪")
        rsi_val = d["rsi"]
        rsi_str = f"RSI={rsi_val}" if rsi_val else ""
        # RSI warning flags
        rsi_warn = ""
        if rsi_val and rsi_val >= 75:
            rsi_warn = " ⚠️ Overbought"
        elif rsi_val and rsi_val <= 25:
            rsi_warn = " 🔥 Extreme Oversold"
        elif rsi_val and rsi_val >= 70:
            rsi_warn = " ⚡ Near Overbought"
        lines.append(f"  {ti} <b>{tf}</b>: {d['trend'].replace('_',' ').upper()}  {rsi_str}{rsi_warn}")

    lines += [
        "",
        "📌 <b>สิ่งที่ควรทำ:</b>",
    ] + [f"  {s}" for s in steps]
    if note: lines.append(f"\n📋 Note: {note}")
    lines += ["", f"📊 <a href='{tv}'>TradingView</a>", f"🕐 {now_bkk_str()}"]
    return "\n".join(lines)


def build_score_message(stock, alert, total_score, grade, breakdown, quote):
    emoji, symbol, name, price, pct, arrow, sign, tv = _header(stock, quote, alert)
    note      = alert.get("note", "")
    direction = alert.get("direction", "bullish")
    min_score = alert.get("min_score", 65)
    filled    = int(total_score / 10)
    bar       = "█" * filled + "░" * (10 - filled)

    grade_th = {
        "A": ("🔥 สัญญาณแข็งมาก", "เชื่อถือได้สูง"),
        "B": ("✅ สัญญาณดี", "ควรเทรด"),
        "C": ("🟡 สัญญาณปานกลาง", "เพิ่มความระมัดระวัง"),
        "D": ("❌ สัญญาณอ่อน", "ควรรอสัญญาณที่ดีกว่า"),
    }
    grade_label, grade_desc = grade_th.get(grade, (grade, ""))

    action_map = {
        "A": [
            "1️⃣ Score สูงมาก — เทรดได้ ตั้ง Stop เสมอ",
            "2️⃣ ใช้ Position Size ตาม risk 2% ของพอร์ต",
            "3️⃣ Target = แนวต้านถัดไป, Stop ใต้ recent low",
        ],
        "B": [
            "1️⃣ Score ดี — เทรดได้ แต่ยืนยันด้วย MTF ก่อน",
            "2️⃣ ใช้ Position Size ปกติ (risk 2%)",
            "3️⃣ ตั้ง Stop Loss ก่อน ค่อยดู Target",
        ],
        "C": [
            "1️⃣ Score กลางๆ — เข้าได้แต่ลด position size ลง 50%",
            "2️⃣ ต้องการ confirmation เพิ่ม (Volume + Candle)",
            "3️⃣ ระวัง false signal — Stop tight",
        ],
        "D": [
            "1️⃣ Score ต่ำ — ยังไม่ควรเทรด",
            "2️⃣ รอ signal ที่ดีขึ้น หรือ TF ใหญ่ align",
            "3️⃣ ติดตาม alert รอบต่อไป",
        ],
    }
    steps = action_map.get(grade, [])

    lines = [
        f"{emoji} <b>SCORE ALERT: {symbol}</b> ({name})", "",
        f"🎯 Confidence Score: <b>{total_score}/100</b>  [{bar}]",
        f"📊 Grade: <b>{grade}</b>  — {grade_label} ({grade_desc})",
        f"📈 Direction: <b>{direction.upper()}</b>  (min score: {min_score})",
        f"💰 Price: <b>${price:.4f}</b>  {arrow} {sign}{pct:.2f}%",
        "",
        "📋 คะแนนย่อย:",
    ]
    for comp, info in breakdown.items():
        pct_comp = int(info["s"] / info["max"] * 100) if info["max"] > 0 else 0
        bar_mini = "█" * (info["s"] // 3) + "░" * ((info["max"] - info["s"]) // 3)
        lines.append(f"  • {comp}: <b>{info['s']}/{info['max']}</b> [{bar_mini}] — {info['note']}")

    lines += [
        "",
        "📌 <b>สิ่งที่ควรทำ:</b>",
    ] + [f"  {s}" for s in steps]
    if note: lines.append(f"\n📝 Note: {note}")
    lines += ["", f"📊 <a href='{tv}'>TradingView</a>", f"🕐 {now_bkk_str()}"]
    return "\n".join(lines)


def build_backtest_message(stock, alert, result, quote):
    emoji, symbol, name, price, pct, arrow, sign, tv = _header(stock, quote, alert)
    note = alert.get("note", "")

    if "error" in result:
        return f"{emoji} <b>BACKTEST: {symbol}</b>\n❌ {result['error']}"

    wr = result["win_rate"]
    wr_bar = "█" * int(wr/10) + "░" * (10 - int(wr/10))
    rating = ("🔥 Excellent" if wr>=60 and result["avg_return"]>=2 else
              ("✅ Good" if wr>=50 and result["avg_return"]>=1 else
               ("🟡 Fair" if wr>=45 else "❌ Poor")))

    lines = [
        f"{emoji} <b>BACKTEST ALERT: {symbol}</b> ({name})", "",
        f"🔬 Rule: <b>{result['rule']}</b>",
        f"📊 Win Rate: <b>{wr:.1f}%</b>  [{wr_bar}]",
        f"  • Trades: {result['total_trades']}  Avg: {result['avg_return']:+.2f}%/trade",
        f"  • Best: {result['best']:+.2f}%  Worst: {result['worst']:+.2f}%",
        f"  • Profit Factor: {result['profit_factor']:.2f}",
        f"⭐ {rating}",
        f"💰 Price: <b>${price:.4f}</b>  {arrow} {sign}{pct:.2f}%",
    ]
    if note: lines.append(f"📋 Note: {note}")
    lines += ["", f"📊 <a href='{tv}'>TradingView</a>", f"🕐 {now_bkk_str()}"]
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
#  DAILY SUMMARY
# ══════════════════════════════════════════════════════════════════════════════

def build_daily_summary(watchlist, quotes_cache):
    lines = [
        "📊 <b>Daily Watchlist Summary</b>",
        f"🕐 {now_bkk_str()}", "",
    ]
    for stock in watchlist:
        symbol = stock["symbol"]
        name   = stock["name"]
        quote  = quotes_cache.get(symbol)
        if not quote:
            lines.append(f"• <b>{symbol}</b> — ⚠️ No data")
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
                    f"  🎯 Target ${target:.4f} — "
                    f"{'✅ HIT' if abs(diff_pct) < 0.5 else f'{diff_s}{diff_pct:.1f}% away'}"
                )
        lines.append("")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    config    = load_json(WATCHLIST_PATH, {})
    settings  = config.get("settings", {})
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

    # ── P7: Market context — ตรวจ SPY เพื่อ market-wide context ──────────
    market_down = False
    try:
        spy_q = fetch_quote("SPY")
        if spy_q and spy_q.get("change_pct", 0) < -1.0:
            market_down = True
            print(f"[Market] SPY {spy_q['change_pct']:+.2f}% — DOWN day: PCT_DROP cooldown เพิ่มเป็น 480m")
    except Exception:
        pass

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

        # ── P6: ติดตาม BUY signals ที่ fired ใน run นี้ เพื่อ trigger position_size ──
        buy_triggered_this_run = False

        for alert in stock.get("alerts", []):
            alert_id  = alert["id"]
            atype     = alert["type"]

            # ── P2+P7: Dynamic cooldown สำหรับ PCT_DROP ──────────────────
            if "DROP" in alert_id:
                # market down day → cooldown 480m, ปกติ 60m
                cooldown = 480 if market_down else alert.get("cooldown_minutes", default_cooldown)
            else:
                cooldown = alert.get("cooldown_minutes", default_cooldown)

            # ── P6: position_size — trigger หลัง BUY signal ──────────────
            if atype == "position_size":
                if not buy_triggered_this_run:
                    print(f"  [{alert_id}] ข้าม: ยังไม่มี BUY signal ใน run นี้")
                    continue
                # reset cooldown ให้ 0 เพื่อให้ fire ตาม BUY เสมอ
                cooldown = 0

            # Cooldown check
            last_fired = sym_state.get(alert_id, {}).get("last_fired", "")
            if cooldown > 0 and last_fired and minutes_since(last_fired) < cooldown:
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
                # ── P1: ส่ง symbol ให้ check เสมอ เพื่อใช้ auto+fallback ──
                triggered, triggered_value, used_level, supports, resistances, atr = \
                    check_support_resistance(alert, quote, symbol=symbol)
                if triggered:
                    # อัปเดต level จริงใน alert เพื่อแสดงใน message
                    alert_for_msg = dict(alert)
                    if used_level:
                        alert_for_msg["level"] = used_level
                    msg = build_message(stock, alert_for_msg, quote, triggered_value)

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
                # ── P4: ส่ง sym_state เพื่อ dedup news ──────────────────
                result_news = check_news_sentiment(alert, symbol, sym_state=sym_state)
                triggered, news_list, new_hashes = result_news if len(result_news) == 3 else (*result_news, [])
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
                # ── P5: state key ใช้ alert_id ตรงๆ (ไม่มี nested bug) ──
                state[symbol][alert_id] = {"last_fired": now_str()}

                # ── P4: บันทึก seen_news hashes ──────────────────────────
                if atype == "news_sentiment" and new_hashes:
                    existing = state[symbol].get(alert_id, {})
                    old_seen = existing.get("seen_news", [])
                    # เก็บแค่ 50 hashes ล่าสุด
                    merged = list(dict.fromkeys(old_seen + new_hashes))[-50:]
                    state[symbol][alert_id] = {"last_fired": now_str(), "seen_news": merged}

                # ── P6: mark ว่า BUY fired ────────────────────────────────
                if alert.get("action") == "BUY":
                    buy_triggered_this_run = True

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
