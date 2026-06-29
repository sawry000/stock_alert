#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  daily_screener.py — Autonomous Halal Stock Pipeline v1.0                  ║
║                                                                              ║
║  WORKFLOW (รันทุกวัน อัตโนมัติ):                                           ║
║                                                                              ║
║  ┌─────────────────────────────────────────────────────────────────┐        ║
║  │  universe.json (1200+ tickers)                                  │        ║
║  │       ↓                                                         │        ║
║  │  [GATE 1] Halal Check — Typesense/Musaffa API                  │        ║
║  │       ↓ ✅ HALAL only                                           │        ║
║  │  [GATE 2] Purify% Filter — max_purify_pct (default 3%)         │        ║
║  │       ↓ ✅ purify ต่ำพอ                                         │        ║
║  │  [GATE 3] Liquidity — Volume + Price > $1                      │        ║
║  │       ↓ ✅                                                       │        ║
║  │  [GATE 4] ADR > threshold% (default 8%)                        │        ║
║  │       ↓ ✅                                                       │        ║
║  │  [GATE 5] Trend — Price > EMA50                                │        ║
║  │       ↓ ✅                                                       │        ║
║  │  [GATE 6] Momentum — RSI 40–70 + Volume 1.5x avg              │        ║
║  │       ↓ ✅ PASSED ALL GATES                                     │        ║
║  │  Gemini AI → เลือก Template → เพิ่มเข้า watchlist.json        │        ║
║  └─────────────────────────────────────────────────────────────────┘        ║
║                                                                              ║
║  REMOVAL CHECK (รันพร้อมกัน):                                               ║
║  ┌─────────────────────────────────────────────────────────────────┐        ║
║  │  watchlist.json (active alerts)                                 │        ║
║  │       ↓ re-check ทุกตัว                                         │        ║
║  │  [REMOVE CHECK 1] Halal status changed → NOT HALAL → ออก       │        ║
║  │  [REMOVE CHECK 2] Purify% เกิน threshold → ออก                 │        ║
║  │  [REMOVE CHECK 3] ADR ลดลงต่ำกว่า 5% ≥ 3 วันติด → ออก         │        ║
║  │  [REMOVE CHECK 4] Death Cross + Volume หดตัว → ออก             │        ║
║  │       ↓ ออก → กลับไป universe.json รอโอกาสใหม่                │        ║
║  └─────────────────────────────────────────────────────────────────┘        ║
║                                                                              ║
║  Run: python3 daily_screener.py                                              ║
║  Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, GEMINI_API_KEY                  ║
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

# ── Gemini client (อยู่ใน repo เดียวกัน) ────────────────────────────────────
try:
    from gemini_client import gemini_json as _gemini_json
    _GEMINI_AVAILABLE = True
except ImportError:
    _GEMINI_AVAILABLE = False

try:
    import yfinance as yf
except ImportError:
    print("Installing yfinance...")
    os.system("pip install yfinance --quiet --break-system-packages")
    import yfinance as yf

# ─── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR          = Path(__file__).parent
UNIVERSE_PATH     = BASE_DIR / "universe.json"
WATCHLIST_PATH    = BASE_DIR / "watchlist.json"
SCREENER_LOG_PATH = BASE_DIR / "screener_log.json"
SCREENER_STATE    = BASE_DIR / "screener_state.json"

# ─── Config defaults (override via universe.json settings) ───────────────────
DEFAULT_CFG = {
    "max_purify_pct":     3.0,    # % purify สูงสุดที่ยอมรับ (> นี้ = ข้าม)
    "min_adr_pct":        8.0,    # ADR ขั้นต่ำ (%)
    "min_price":          1.0,    # ราคาขั้นต่ำ ($)
    "min_avg_volume":  100000,    # Volume เฉลี่ยขั้นต่ำ (หุ้น/วัน)
    "rsi_min":           35.0,    # RSI ขั้นต่ำ (ไม่รับ oversold มาก)
    "rsi_max":           72.0,    # RSI สูงสุด (ไม่รับ overbought)
    "volume_ratio_min":   1.3,    # Volume วันนี้ / avg ขั้นต่ำ
    "require_above_ema50": True,  # ต้องอยู่เหนือ EMA50
    "max_watchlist":       60,    # จำนวนหุ้นสูงสุดใน watchlist
    "account_size_per_stock": 100,
    # Removal thresholds
    "remove_adr_below":   5.0,    # ADR ลงต่ำกว่านี้ = เตือนให้ออก
    "remove_adr_days":    3,      # ต้อง low ADR ติดกัน N วัน
    "remove_purify_pct":  4.0,    # purify เกินนี้ = ออกทันที
    # Gemini
    # Typesense/Musaffa
    "typesense_base": "https://0bs2hegi5nmtad4op.a1.typesense.net",
    "typesense_key":  "GRhZdTOnzVKId4Ln9G1PIvuIgn1TK0fH",
}

# ══════════════════════════════════════════════════════════════════════════════
#  UTILITIES
# ══════════════════════════════════════════════════════════════════════════════

def load_json(path, default):
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
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


def today_str():
    return now_utc().strftime("%Y-%m-%d")


def log_print(msg):
    print(f"[{now_str()}] {msg}")


# ══════════════════════════════════════════════════════════════════════════════
#  TELEGRAM
# ══════════════════════════════════════════════════════════════════════════════

def send_telegram(token, chat_id, text):
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id":   chat_id,
        "text":      text,
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
        except Exception as e:
            print(f"  [Telegram] attempt {attempt+1} failed: {e}")
            if attempt < 2:
                time.sleep(3)
    return False


# ══════════════════════════════════════════════════════════════════════════════
#  HALAL CHECK — Typesense/Musaffa API
#  (ported logic จาก SpeedZScreener.gs _fetchHalalData)
# ══════════════════════════════════════════════════════════════════════════════

def _parse_halal_fields(sd):
    """
    Returns: { "status": "HALAL"|"NOT_HALAL"|"UNKNOWN", "purify_pct": float|None }
    """
    result = {"status": "UNKNOWN", "purify_pct": None}
    halal  = False

    # 1. ETF Shariah Approved
    if sd.get("etf_type") == "ShariahApproved":
        halal = True
    if not halal and str(sd.get("isShariahApproved", "0")) == "1":
        halal = True

    # 2. shariahCompliantStatus
    if not halal:
        scs = str(sd.get("shariahCompliantStatus", "")).upper()
        if scs == "COMPLIANT":
            halal = True

    # 3. sharia_compliance
    if not halal:
        sc = str(sd.get("sharia_compliance", "")).upper()
        if sc in ("COMPLIANT", "COVERED", "HALAL", "YES", "PERMISSIBLE"):
            halal = True

    # 4. musaffaHalalRating
    if not halal:
        rating = sd.get("musaffaHalalRating")
        if rating is not None and rating != "":
            try:
                r_num = float(rating)
                if r_num >= 1:
                    halal = True
            except (ValueError, TypeError):
                r_str = str(rating).upper()
                if r_str in ("COMPLIANT", "COVERED", "HALAL", "YES", "PERMISSIBLE"):
                    halal = True

    # 5. ETF fallback: businessCompliantRatio >= 95
    if not halal and sd.get("_isETF"):
        bc_etf = float(sd.get("businessCompliantRatio") or 0)
        if bc_etf >= 95:
            halal = True

    # ── ตรวจว่ามีข้อมูล Halal เลยไหม ─────────────────────────────
    has_field = (
        sd.get("sharia_compliance")      is not None or
        sd.get("shariahCompliantStatus") is not None or
        sd.get("musaffaHalalRating")     is not None or
        sd.get("etf_type") == "ShariahApproved" or
        str(sd.get("isShariahApproved", "0")) == "1" or
        (sd.get("_isETF") and sd.get("businessCompliantRatio") is not None)
    )

    if not has_field:
        return {"status": "UNKNOWN", "purify_pct": None}

    result["status"] = "HALAL" if halal else "NOT_HALAL"

    # ── Purify% ────────────────────────────────────────────────────
    bc = sd.get("businessCompliantRatio")
    if bc is not None:
        try:
            bc_num = float(bc)
            result["purify_pct"] = round(max(0.0, 100.0 - bc_num), 2)
        except (ValueError, TypeError):
            result["purify_pct"] = None

    return result


def fetch_halal_data(ticker, cfg):
    """
    ดึง Halal status + Purify% จาก Typesense/Musaffa
    Returns: { "status": "HALAL"|"NOT_HALAL"|"UNKNOWN", "purify_pct": float|None }
    """
    ts_base = cfg.get("typesense_base", DEFAULT_CFG["typesense_base"])
    ts_key  = cfg.get("typesense_key",  DEFAULT_CFG["typesense_key"])

    sym_upper = ticker.upper()
    fields = ",".join([
        "sharia_compliance", "shariahCompliantStatus", "musaffaHalalRating",
        "businessCompliantRatio", "interestBearingDebtRatio",
        "interestBearingAssetsRatio", "doubtful_revenue_percent",
        "etf_type", "isShariahApproved",
    ])

    # ── stocks_data collection ──────────────────────────────────────
    try:
        id_filter = f"`{sym_upper}`"
        filter_by = f"$company_profile_collection_new(id:*)&&id:=[{id_filter}]"
        params = "&".join([
            f"x-typesense-api-key={ts_key}",
            "per_page=1",
            "q=*",
            f"include_fields=$stocks_data({fields}),",
            f"filter_by={urllib.parse.quote(filter_by)}",
        ])
        url = f"{ts_base}/collections/stocks_data/documents/search?{params}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        hits = data.get("hits", [])
        if hits:
            # Find exact match
            hit = next(
                (h for h in hits if (h.get("document") or {}).get("id", "").upper() == sym_upper),
                hits[0]
            )
            doc = hit.get("document", {})
            sd  = doc.get("stocks_data", doc)
            result = _parse_halal_fields(sd)
            if result["status"] != "UNKNOWN":
                return result
    except Exception as e:
        print(f"  [Halal-stocks] {ticker}: {e}")

    # ── ETF fallback ────────────────────────────────────────────────
    try:
        id_filter = f"`{sym_upper}`"
        params = "&".join([
            f"x-typesense-api-key={ts_key}",
            "per_page=1",
            "q=*",
            f"filter_by={urllib.parse.quote('id:=[' + id_filter + ']')}",
        ])
        url = f"{ts_base}/collections/etfs_data/documents/search?{params}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        hits = data.get("hits", [])
        if hits:
            doc = hits[0].get("document", {})
            doc["_isETF"] = True
            return _parse_halal_fields(doc)
    except Exception as e:
        print(f"  [Halal-etf] {ticker}: {e}")

    return {"status": "UNKNOWN", "purify_pct": None}


# ══════════════════════════════════════════════════════════════════════════════
#  TECHNICAL HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _calc_ema(prices, period):
    if len(prices) < period:
        return [None] * len(prices)
    result = [None] * (period - 1)
    seed   = sum(prices[:period]) / period
    result.append(seed)
    k = 2.0 / (period + 1)
    for p in prices[period:]:
        result.append(p * k + result[-1] * (1 - k))
    return result


def _calc_rsi(closes, period=14):
    result = [None] * period
    if len(closes) <= period:
        return result + [None] * max(0, len(closes) - period)
    gains  = [max(closes[i] - closes[i-1], 0.0) for i in range(1, len(closes))]
    losses = [max(closes[i-1] - closes[i], 0.0) for i in range(1, len(closes))]
    avg_g  = sum(gains[:period])  / period
    avg_l  = sum(losses[:period]) / period
    rsi_val = 100.0 - 100.0 / (1 + avg_g / avg_l) if avg_l > 0 else 100.0
    result.append(rsi_val)
    for i in range(period, len(gains)):
        avg_g   = (avg_g   * (period - 1) + gains[i])  / period
        avg_l   = (avg_l   * (period - 1) + losses[i]) / period
        rsi_val = 100.0 - 100.0 / (1 + avg_g / avg_l) if avg_l > 0 else 100.0
        result.append(rsi_val)
    return result


def _calc_adr(highs, lows, n=20):
    pairs = list(zip(highs[-n:], lows[-n:]))
    if not pairs:
        return 0.0
    ranges = [(h - l) / l * 100 for h, l in pairs if l > 0]
    return sum(ranges) / len(ranges) if ranges else 0.0


# ══════════════════════════════════════════════════════════════════════════════
#  FETCH STOCK DATA
# ══════════════════════════════════════════════════════════════════════════════

def fetch_stock_data(ticker):
    """
    ดึงข้อมูลทั้งหมดที่ต้องการสำหรับ gate screening
    Returns dict หรือ None ถ้าไม่มีข้อมูล
    """
    try:
        t    = yf.Ticker(ticker)
        hist = t.history(period="90d", interval="1d")
        if hist is None or hist.empty or len(hist) < 20:
            return None

        closes  = list(hist["Close"].astype(float))
        highs   = list(hist["High"].astype(float))
        lows    = list(hist["Low"].astype(float))
        volumes = list(hist["Volume"].astype(float))

        price      = closes[-1]
        prev_close = closes[-2] if len(closes) >= 2 else price
        change_pct = ((price - prev_close) / prev_close * 100) if prev_close > 0 else 0.0
        adr_pct    = _calc_adr(highs, lows, 20)

        # Volume
        today_vol = volumes[-1]
        avg_vol   = sum(volumes[-21:-1]) / 20 if len(volumes) >= 21 else today_vol
        vol_ratio = today_vol / avg_vol if avg_vol > 0 else 1.0

        # EMA50
        ema50_list = _calc_ema(closes, 50)
        ema50      = next((v for v in reversed(ema50_list) if v is not None), None)
        above_ema50 = (price > ema50) if ema50 else False

        # RSI14
        rsi_list  = _calc_rsi(closes, 14)
        rsi       = next((v for v in reversed(rsi_list) if v is not None), 50.0)

        # 52W range
        high_52w = max(highs) if highs else price
        low_52w  = min(lows)  if lows  else price

        # Info (ต้องการ beta, sector, market_cap)
        info       = {}
        beta       = 1.0
        sector     = "Unknown"
        industry   = "Unknown"
        market_cap = 0
        name       = ticker
        try:
            info       = t.info or {}
            beta       = float(info.get("beta") or 1.0)
            sector     = info.get("sector")   or "Unknown"
            industry   = info.get("industry") or "Unknown"
            market_cap = info.get("marketCap") or 0
            name       = info.get("longName") or info.get("shortName") or ticker
        except Exception:
            pass

        return {
            "ticker":       ticker,
            "name":         name,
            "price":        round(price, 4),
            "change_pct":   round(change_pct, 2),
            "adr_pct":      round(adr_pct, 2),
            "volume":       int(today_vol),
            "avg_volume":   int(avg_vol),
            "vol_ratio":    round(vol_ratio, 2),
            "rsi":          round(rsi, 1),
            "ema50":        round(ema50, 4) if ema50 else None,
            "above_ema50":  above_ema50,
            "high_52w":     round(high_52w, 4),
            "low_52w":      round(low_52w, 4),
            "beta":         round(beta, 2),
            "sector":       sector,
            "industry":     industry,
            "market_cap_m": round(market_cap / 1_000_000, 1) if market_cap else 0,
        }
    except Exception as e:
        print(f"  [Data] {ticker}: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
#  GATE FILTERS
# ══════════════════════════════════════════════════════════════════════════════

def run_gates(ticker, data, halal, cfg):
    """
    รัน Gates 1-6 ทั้งหมด
    Returns: (passed: bool, gates_detail: dict)
    """
    gates = {}

    # GATE 1: Halal
    halal_status = halal.get("status", "UNKNOWN")
    gates["halal"] = {
        "pass": halal_status == "HALAL",
        "value": halal_status,
    }
    if not gates["halal"]["pass"]:
        return False, gates

    # GATE 2: Purify%
    purify   = halal.get("purify_pct")
    max_pur  = cfg.get("max_purify_pct", DEFAULT_CFG["max_purify_pct"])
    pur_pass = (purify is None) or (purify <= max_pur)
    gates["purify"] = {
        "pass":  pur_pass,
        "value": purify,
        "max":   max_pur,
    }
    if not pur_pass:
        return False, gates

    # GATE 3: Price + Liquidity
    min_price = cfg.get("min_price",      DEFAULT_CFG["min_price"])
    min_vol   = cfg.get("min_avg_volume", DEFAULT_CFG["min_avg_volume"])
    liq_pass  = data["price"] >= min_price and data["avg_volume"] >= min_vol
    gates["liquidity"] = {
        "pass":   liq_pass,
        "price":  data["price"],
        "avvol":  data["avg_volume"],
    }
    if not liq_pass:
        return False, gates

    # GATE 4: ADR
    min_adr  = cfg.get("min_adr_pct", DEFAULT_CFG["min_adr_pct"])
    adr_pass = data["adr_pct"] >= min_adr
    gates["adr"] = {
        "pass":  adr_pass,
        "value": data["adr_pct"],
        "min":   min_adr,
    }
    if not adr_pass:
        return False, gates

    # GATE 5: Trend (Price > EMA50)
    if cfg.get("require_above_ema50", DEFAULT_CFG["require_above_ema50"]):
        trend_pass = data.get("above_ema50", False)
        gates["trend"] = {
            "pass":  trend_pass,
            "price": data["price"],
            "ema50": data.get("ema50"),
        }
        if not trend_pass:
            return False, gates
    else:
        gates["trend"] = {"pass": True, "skipped": True}

    # GATE 6: Momentum (RSI range + Volume ratio)
    rsi_min   = cfg.get("rsi_min",          DEFAULT_CFG["rsi_min"])
    rsi_max   = cfg.get("rsi_max",          DEFAULT_CFG["rsi_max"])
    vol_r_min = cfg.get("volume_ratio_min", DEFAULT_CFG["volume_ratio_min"])
    rsi       = data.get("rsi", 50.0)
    vol_ratio = data.get("vol_ratio", 1.0)
    mom_pass  = (rsi_min <= rsi <= rsi_max) and (vol_ratio >= vol_r_min)
    gates["momentum"] = {
        "pass":      mom_pass,
        "rsi":       rsi,
        "vol_ratio": vol_ratio,
    }
    if not mom_pass:
        return False, gates

    return True, gates


# ══════════════════════════════════════════════════════════════════════════════
#  REMOVAL CHECK — ตรวจหุ้นใน watchlist ว่าต้องออกหรือไม่
# ══════════════════════════════════════════════════════════════════════════════

def check_removal(ticker, data, halal, scr_state, cfg):
    """
    ตรวจสอบว่าหุ้นที่อยู่ใน watchlist ควรถูกปลดออกหรือไม่
    ต้องรอหลักฐานชัดเจนหลายวันก่อนตัดสินใจ

    Returns: (should_remove: bool, reason: str)
    """
    # ── ตรวจ 1: Halal status เปลี่ยน → ออกทันที ───────────────────────
    halal_status = halal.get("status", "UNKNOWN")
    if halal_status == "NOT_HALAL":
        return True, "❌ HALAL status เปลี่ยนเป็น NOT HALAL"

    # ── ตรวจ 2: Purify% เกิน threshold → ออกทันที ────────────────────
    purify      = halal.get("purify_pct")
    remove_pur  = cfg.get("remove_purify_pct", DEFAULT_CFG["remove_purify_pct"])
    if purify is not None and purify > remove_pur:
        return True, f"⚠️ Purify% = {purify:.1f}% เกิน {remove_pur}%"

    # ── ตรวจ 3: ADR ลดลง ≥ N วันติด ──────────────────────────────────
    remove_adr  = cfg.get("remove_adr_below", DEFAULT_CFG["remove_adr_below"])
    remove_days = int(cfg.get("remove_adr_days", DEFAULT_CFG["remove_adr_days"]))
    sym_state   = scr_state.get(ticker, {})
    low_adr_days = int(sym_state.get("low_adr_days", 0))

    if data and data.get("adr_pct", 999) < remove_adr:
        low_adr_days += 1
    else:
        low_adr_days = 0   # reset ถ้า ADR กลับมาปกติ

    # อัปเดตค่าใน state
    scr_state.setdefault(ticker, {})["low_adr_days"] = low_adr_days

    if low_adr_days >= remove_days:
        return True, (
            f"📉 ADR = {data.get('adr_pct', 0):.1f}% "
            f"ต่ำกว่า {remove_adr}% ติดกัน {low_adr_days} วัน"
        )

    # ── ตรวจ 4: Death Cross (EMA9 ตัดลงใต้ EMA21) ────────────────────
    # ตรวจเฉพาะตอนที่ไม่มี open position (ป้องกันตัดออกตอนมี trade อยู่)
    if data is None:
        return False, ""

    return False, ""


# ══════════════════════════════════════════════════════════════════════════════
#  GEMINI AI — Template Selection
# ══════════════════════════════════════════════════════════════════════════════

def ai_select_template(data, halal, api_key, cfg):
    """
    ให้ Gemini วิเคราะห์และเลือก template
    ใช้ gemini_client.py ที่อยู่ใน repo เดียวกัน (ไม่ผ่าน Netlify)
    Returns: (template: str, reason: str, confirm_hits: int)
    """
    if not api_key or not _GEMINI_AVAILABLE:
        return _rule_based_template(data)

    purify_str = f"{halal.get('purify_pct', 0):.1f}%" if halal.get("purify_pct") is not None else "N/A"

    prompt = (
        "You are a quantitative trading system assistant. "
        "Analyze this halal-certified stock and select the best alert template.\n\n"
        f"Stock: {data['ticker']} — {data['name']}\n"
        f"Price: ${data['price']}\n"
        f"Market Cap: ${data['market_cap_m']}M\n"
        f"Beta: {data['beta']}\n"
        f"Sector: {data['sector']}\n"
        f"Industry: {data['industry']}\n"
        f"ADR (20d avg): {data['adr_pct']}%\n"
        f"RSI(14): {data['rsi']}\n"
        f"Volume ratio: {data['vol_ratio']}x\n"
        f"Above EMA50: {data['above_ema50']}\n"
        f"Purify%: {purify_str}\n\n"
        "Template Options:\n"
        "1. VOLATILE — ADR>12%, beta>1.5, biotech/crypto/penny/micro-cap, speculative. "
        "Fast RSI + Volume spike + Score 60\n"
        "2. GROWTH — ADR 8-12%, tech/healthcare/fintech mid-cap. "
        "RSI + MA Golden Cross + Score 65 + MTF\n"
        "3. STABLE — ADR<8%, ETF/large-cap/dividend, conservative. "
        "RSI low + Score 70+ + MTF conservative\n"
        "4. MOMENTUM — News/catalyst driven, sector rotation. "
        "% Change + Volume 2x + Score 62\n\n"
        "Rules:\n"
        "- ADR>15% OR market cap<$100M OR beta>2 → VOLATILE\n"
        "- Biotechnology/Drug/Pharmaceutical sector → VOLATILE\n"
        "- ETF → STABLE\n"
        "- ADR 8-12% AND tech/software/semiconductor → GROWTH\n"
        "- News-driven, irregular spikes → MOMENTUM\n\n"
        "Respond ONLY with a JSON object, no markdown:\n"
        '{"template": "GROWTH", "reason": "Thai reason max 50 chars", "confirm_hits": 1}\n\n'
        "confirm_hits = 1 for VOLATILE/MOMENTUM, 2 for GROWTH/STABLE"
    )

    try:
        result       = _gemini_json(prompt, max_tokens=256, temperature=0.1, api_key=api_key)
        template     = result.get("template", "GROWTH").upper()
        reason       = result.get("reason", "AI เลือกอัตโนมัติ")
        confirm_hits = int(result.get("confirm_hits", 1))
        if template not in ("VOLATILE", "GROWTH", "STABLE", "MOMENTUM"):
            template = "GROWTH"
        return template, reason, confirm_hits
    except Exception as e:
        print(f"  [Gemini] {data['ticker']}: {e} → fallback rule-based")
        return _rule_based_template(data)


def _rule_based_template(data):
    """Fallback template selection ถ้า Gemini ไม่พร้อม"""
    adr    = data.get("adr_pct", 10)
    beta   = data.get("beta",    1.0)
    mc     = data.get("market_cap_m", 500)
    sector = data.get("sector",   "")

    if adr > 15 or mc < 100 or beta > 2 or "Biotech" in sector or "Drug" in sector:
        return "VOLATILE", "ADR/beta สูง หรือ spec sector", 1
    elif adr < 6:
        return "STABLE", "ADR ต่ำ conservative", 2
    elif adr >= 8:
        return "GROWTH", "ADR 8-12% growth", 1
    else:
        return "MOMENTUM", "ใช้ momentum play", 1


# ══════════════════════════════════════════════════════════════════════════════
#  BUILD STOCK ENTRY (ประกาศรวมไว้ก่อน watchlist logic)
# ══════════════════════════════════════════════════════════════════════════════

def _sell_alerts(sym):
    return [
        {
            "id": f"{sym}_SL_BREAK", "emoji": "🚨",
            "type": "support_resistance", "action": "SELL",
            "cooldown_minutes": 30, "note": "Auto stop loss",
            "enabled": True, "level": 0, "direction": "break_below",
        },
        {
            "id": f"{sym}_PCT_DROP", "emoji": "📉",
            "type": "percent_change", "action": "SELL",
            "cooldown_minutes": 480, "note": "Drop >= 5% single day",
            "enabled": True, "threshold_pct": 5, "direction": "down",
        },
        {
            "id": f"{sym}_MA_DEATH", "emoji": "💀",
            "type": "ma_crossover", "action": "SELL",
            "cooldown_minutes": 1440, "note": "Death Cross EMA9/21",
            "enabled": True, "condition": "death_cross",
            "ma_type": "EMA", "interval": "1d",
            "fast_period": 9, "slow_period": 21,
        },
        {
            "id": f"{sym}_SCORE_BEAR", "emoji": "🐻",
            "type": "alert_score", "action": "SELL",
            "cooldown_minutes": 240, "note": "Bearish score >= 70",
            "enabled": True, "direction": "bearish",
            "min_score": 70, "interval": "1d",
        },
    ]


def build_stock_entry(ticker, name, template, confirm_hits, halal, account_size=100):
    sym = ticker.upper()

    if template == "VOLATILE":
        buy_alerts = [
            {
                "id": f"{sym}_RSI_OS", "emoji": "💚",
                "type": "rsi", "action": "BUY",
                "cooldown_minutes": 180, "note": "RSI Oversold Fast",
                "enabled": True, "condition": "oversold",
                "interval": "1d", "period": 14,
                "oversold_level": 32, "overbought_level": 70,
            },
            {
                "id": f"{sym}_VOL_3X", "emoji": "🔥",
                "type": "volume_spike", "action": "BUY",
                "cooldown_minutes": 60, "note": "Volume 3x breakout",
                "enabled": True, "multiplier": 3,
            },
            {
                "id": f"{sym}_PCT_UP", "emoji": "📈",
                "type": "percent_change", "action": "BUY",
                "cooldown_minutes": 120, "note": "Price surge >= 6%",
                "enabled": True, "threshold_pct": 6, "direction": "up",
            },
            {
                "id": f"{sym}_SCORE_60", "emoji": "🎯",
                "type": "alert_score", "action": "BUY",
                "cooldown_minutes": 180, "note": "Score >= 60 bullish",
                "enabled": True, "direction": "bullish",
                "min_score": 60, "interval": "1d",
            },
        ]
    elif template == "GROWTH":
        buy_alerts = [
            {
                "id": f"{sym}_RSI_OS", "emoji": "💚",
                "type": "rsi", "action": "BUY",
                "cooldown_minutes": 240, "note": "RSI Oversold 1D",
                "enabled": True, "condition": "oversold",
                "interval": "1d", "period": 14,
                "oversold_level": 30, "overbought_level": 70,
            },
            {
                "id": f"{sym}_MA_GOLD", "emoji": "✨",
                "type": "ma_crossover", "action": "BUY",
                "cooldown_minutes": 1440, "note": "Golden Cross EMA9/21",
                "enabled": True, "condition": "golden_cross",
                "ma_type": "EMA", "interval": "1d",
                "fast_period": 9, "slow_period": 21,
            },
            {
                "id": f"{sym}_SCORE_65", "emoji": "🎯",
                "type": "alert_score", "action": "BUY",
                "cooldown_minutes": 240, "note": "Score >= 65 bullish",
                "enabled": True, "direction": "bullish",
                "min_score": 65, "interval": "1d",
            },
            {
                "id": f"{sym}_MTF_BULL", "emoji": "🔭",
                "type": "mtf_alignment", "action": "BUY",
                "cooldown_minutes": 480, "note": "1H+4H+1D bullish align",
                "enabled": True,
                "timeframes": ["1h", "4h", "1d"],
                "required_alignment": "mostly_bullish",
                "min_bullish": 2,
            },
        ]
    elif template == "STABLE":
        buy_alerts = [
            {
                "id": f"{sym}_RSI_OS", "emoji": "💚",
                "type": "rsi", "action": "BUY",
                "cooldown_minutes": 480, "note": "RSI Oversold Conservative",
                "enabled": True, "condition": "oversold",
                "interval": "1d", "period": 14,
                "oversold_level": 28, "overbought_level": 72,
            },
            {
                "id": f"{sym}_SCORE_70", "emoji": "🎯",
                "type": "alert_score", "action": "BUY",
                "cooldown_minutes": 480, "note": "Score >= 70 high confidence",
                "enabled": True, "direction": "bullish",
                "min_score": 70, "interval": "1d",
            },
            {
                "id": f"{sym}_MTF_BULL", "emoji": "🔭",
                "type": "mtf_alignment", "action": "BUY",
                "cooldown_minutes": 720, "note": "MTF 4H+1D aligned",
                "enabled": True,
                "timeframes": ["4h", "1d"],
                "required_alignment": "mostly_bullish",
                "min_bullish": 2,
            },
        ]
    else:  # MOMENTUM
        buy_alerts = [
            {
                "id": f"{sym}_PCT_UP", "emoji": "📈",
                "type": "percent_change", "action": "BUY",
                "cooldown_minutes": 120, "note": "Momentum >= 5%",
                "enabled": True, "threshold_pct": 5, "direction": "up",
            },
            {
                "id": f"{sym}_VOL_2X", "emoji": "🔥",
                "type": "volume_spike", "action": "BUY",
                "cooldown_minutes": 60, "note": "Volume 2x confirm",
                "enabled": True, "multiplier": 2,
            },
            {
                "id": f"{sym}_SCORE_62", "emoji": "🎯",
                "type": "alert_score", "action": "BUY",
                "cooldown_minutes": 180, "note": "Score >= 62 bullish",
                "enabled": True, "direction": "bullish",
                "min_score": 62, "interval": "1d",
            },
        ]

    purify_pct = halal.get("purify_pct")
    return {
        "symbol":        sym,
        "name":          name,
        "market":        "US",
        "timeframe":     "1D",
        "enabled":       True,
        "confirm_hits":  confirm_hits,
        "template":      template,
        "halal_status":  halal.get("status", "UNKNOWN"),
        "purify_pct":    purify_pct,
        "added_by":      "auto_screener",
        "added_at":      now_str(),
        "screened_at":   now_str(),
        "position_alert": {
            "account_size": account_size,
            "risk_pct":     2.0,
            "target_pct":   8.0,
        },
        "alerts": buy_alerts + _sell_alerts(sym),
    }


# ══════════════════════════════════════════════════════════════════════════════
#  WATCHLIST MANAGER
# ══════════════════════════════════════════════════════════════════════════════

def load_watchlist():
    data = load_json(WATCHLIST_PATH, {"settings": {}, "watchlist": []})
    return data.get("settings", {}), data.get("watchlist", [])


def save_watchlist(settings, watchlist):
    save_json(WATCHLIST_PATH, {"settings": settings, "watchlist": watchlist})


def get_watchlist_symbols(watchlist):
    return {s["symbol"].upper() for s in watchlist}


def add_to_watchlist(settings, watchlist, entry, max_wl):
    sym   = entry["symbol"]
    syms  = get_watchlist_symbols(watchlist)

    if sym in syms:
        # Update template + halal info ถ้ามีอยู่แล้ว
        for i, s in enumerate(watchlist):
            if s["symbol"] == sym:
                watchlist[i]["halal_status"] = entry["halal_status"]
                watchlist[i]["purify_pct"]   = entry["purify_pct"]
                watchlist[i]["screened_at"]  = entry["screened_at"]
                break
        return False, "exists"

    if len(watchlist) >= max_wl:
        return False, "full"

    watchlist.append(entry)
    save_watchlist(settings, watchlist)
    return True, "added"


def remove_from_watchlist(settings, watchlist, sym, reason):
    before = len(watchlist)
    watchlist_new = [s for s in watchlist if s["symbol"].upper() != sym.upper()]
    if len(watchlist_new) < before:
        save_watchlist(settings, watchlist_new)
        return True
    return False


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN SCREENER PIPELINE
# ══════════════════════════════════════════════════════════════════════════════

def main():
    # ── Load config ───────────────────────────────────────────────────────────
    universe_data = load_json(UNIVERSE_PATH, {})
    cfg           = {**DEFAULT_CFG, **universe_data.get("settings", {})}
    universe      = universe_data.get("universe", [])

    if not universe:
        log_print("⚠️ universe.json ว่าง — ไม่มีหุ้นให้ scan")
        log_print("💡 เพิ่มหุ้นผ่าน Web UI หรือใส่ใน universe.json")
        return

    # ── Env vars ──────────────────────────────────────────────────────────────
    token     = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id   = os.environ.get("TELEGRAM_CHAT_ID",   "")
    api_key   = os.environ.get("GEMINI_API_KEY",     "")
    acct_size = int(os.environ.get("ACCOUNT_SIZE_PER_STOCK", "100"))

    max_wl    = int(cfg.get("max_watchlist", DEFAULT_CFG["max_watchlist"]))

    # ── Load state ────────────────────────────────────────────────────────────
    scr_state = load_json(SCREENER_STATE, {})
    scr_log   = load_json(SCREENER_LOG_PATH, [])

    # ── Load watchlist ────────────────────────────────────────────────────────
    wl_settings, watchlist = load_watchlist()
    wl_syms                = get_watchlist_symbols(watchlist)

    log_print(f"{'='*65}")
    log_print(f"AUTONOMOUS HALAL SCREENER — รันวันที่ {today_str()}")
    log_print(f"Universe: {len(universe)} tickers  |  Watchlist: {len(watchlist)}/{max_wl}")
    log_print(f"Gemini AI: {'✅ enabled' if api_key else '⚠️ rule-based fallback'}")
    log_print(f"{'='*65}")

    # ═════════════════════════════════════════════════════════════════════════
    #  PHASE 1: REMOVAL CHECK — ตรวจหุ้นที่อยู่ใน watchlist
    # ═════════════════════════════════════════════════════════════════════════
    log_print("\n── PHASE 1: REMOVAL CHECK ──────────────────────────────────")
    removed_syms   = []
    halal_rechecked = {}

    for stock in list(watchlist):
        sym = stock["symbol"]
        log_print(f"[Remove Check] {sym}")

        # Re-check halal สำหรับทุกหุ้นใน watchlist (สำคัญมาก!)
        time.sleep(0.5)
        halal_now = fetch_halal_data(sym, cfg)
        halal_rechecked[sym] = halal_now
        log_print(f"  Halal: {halal_now['status']}  Purify: {halal_now.get('purify_pct')}%")

        # ดึงข้อมูลตลาดปัจจุบัน
        time.sleep(0.8)
        data_now = fetch_stock_data(sym)

        should_remove, rm_reason = check_removal(sym, data_now, halal_now, scr_state, cfg)

        if should_remove:
            log_print(f"  🔴 REMOVE: {rm_reason}")
            removed_syms.append({"symbol": sym, "reason": rm_reason, "at": now_str()})

            # ส่ง Telegram แจ้งเตือน
            msg = (
                f"🔴 <b>ปลดออกจาก Watchlist: {sym}</b>\n"
                f"เหตุผล: {rm_reason}\n"
                f"→ กลับไปรอคิวใน Universe\n"
                f"🕐 {now_bkk_str()}"
            )
            send_telegram(token, chat_id, msg)

            # ลบออก watchlist
            remove_from_watchlist(wl_settings, watchlist, sym, rm_reason)
            wl_syms.discard(sym)
            time.sleep(0.5)
        else:
            log_print(f"  ✅ ยังคง watchlist ต่อ")
            # อัปเดต halal info ล่าสุดใน watchlist entry
            for i, s in enumerate(watchlist):
                if s["symbol"] == sym:
                    watchlist[i]["halal_status"] = halal_now.get("status", "UNKNOWN")
                    watchlist[i]["purify_pct"]   = halal_now.get("purify_pct")
                    watchlist[i]["screened_at"]  = now_str()
                    break

    # บันทึก watchlist หลัง removal
    save_watchlist(wl_settings, watchlist)

    # ═════════════════════════════════════════════════════════════════════════
    #  PHASE 2: UNIVERSE SCAN — หาตัวใหม่
    # ═════════════════════════════════════════════════════════════════════════
    log_print(f"\n── PHASE 2: UNIVERSE SCAN ({len(universe)} tickers) ──────────")
    log_print(f"   Watchlist ปัจจุบัน: {len(watchlist)}/{max_wl} slots")

    added_syms  = []
    scan_results = []

    for ticker_entry in universe:
        # รองรับทั้ง string และ dict
        if isinstance(ticker_entry, str):
            sym  = ticker_entry.upper()
            name = sym
        else:
            sym  = ticker_entry.get("symbol", "").upper()
            name = ticker_entry.get("name", sym)

        if not sym:
            continue

        # ข้ามถ้าอยู่ใน watchlist แล้ว (ยกเว้นตัวที่เพิ่งถูกลบออก)
        if sym in wl_syms:
            continue

        # ตรวจว่า watchlist เต็มหรือยัง
        if len(watchlist) >= max_wl:
            log_print(f"  ⚠️ Watchlist เต็ม {max_wl} ตัว — หยุด scan")
            break

        log_print(f"\n[Scan] {sym} ({name})")

        # ── GATE 1+2: Halal + Purify ──────────────────────────────
        time.sleep(0.5)
        halal = fetch_halal_data(sym, cfg)
        log_print(f"  Halal: {halal['status']}  Purify: {halal.get('purify_pct')}%")

        if halal["status"] == "NOT_HALAL":
            log_print(f"  ❌ Gate 1: NOT HALAL — ข้าม")
            scan_results.append({
                "symbol": sym, "gate_fail": "halal",
                "halal": halal["status"], "at": now_str()
            })
            time.sleep(0.3)
            continue

        purify = halal.get("purify_pct")
        max_pur = cfg.get("max_purify_pct", DEFAULT_CFG["max_purify_pct"])
        if purify is not None and purify > max_pur:
            log_print(f"  ❌ Gate 2: Purify {purify:.1f}% > {max_pur}% — ข้าม")
            scan_results.append({
                "symbol": sym, "gate_fail": "purify",
                "purify_pct": purify, "at": now_str()
            })
            time.sleep(0.3)
            continue

        # ── GATE 3-6: Technical ───────────────────────────────────
        time.sleep(1.0)
        data = fetch_stock_data(sym)
        if data is None:
            log_print(f"  ❌ ไม่มีข้อมูลตลาด — ข้าม")
            continue

        log_print(
            f"  Price=${data['price']}  ADR={data['adr_pct']}%  "
            f"RSI={data['rsi']}  Vol={data['vol_ratio']}x  "
            f"EMA50={'✅' if data['above_ema50'] else '❌'}"
        )

        passed, gates = run_gates(sym, data, halal, cfg)

        if not passed:
            fail_gate = next(
                (k for k, v in gates.items() if not v.get("pass", True)), "unknown"
            )
            log_print(f"  ❌ Gate fail: {fail_gate}")
            scan_results.append({
                "symbol": sym, "gate_fail": fail_gate,
                "adr": data["adr_pct"], "rsi": data["rsi"], "at": now_str()
            })
            time.sleep(0.3)
            continue

        log_print(f"  ✅ ผ่านทุก Gate!")

        # ── AI Template Selection ─────────────────────────────────
        time.sleep(0.5)
        template, reason, confirm_hits = ai_select_template(data, halal, api_key, cfg)
        log_print(f"  Template: {template}  ({reason})  confirm={confirm_hits}")

        # ── Build entry + Add to watchlist ────────────────────────
        entry  = build_stock_entry(sym, name, template, confirm_hits, halal, acct_size)
        ok, status = add_to_watchlist(wl_settings, watchlist, entry, max_wl)

        if ok:
            wl_syms.add(sym)
            added_syms.append({
                "symbol":   sym,
                "name":     name,
                "template": template,
                "reason":   reason,
                "adr":      data["adr_pct"],
                "rsi":      data["rsi"],
                "purify":   purify,
                "at":       now_str(),
            })
            log_print(f"  ➕ เพิ่มเข้า watchlist แล้ว ({len(watchlist)}/{max_wl})")

            # ส่ง Telegram แจ้งเตือนตัวใหม่
            purify_str = f"{purify:.1f}%" if purify is not None else "N/A"
            msg = (
                f"✅ <b>เพิ่มหุ้นใหม่: {sym}</b> ({name})\n"
                f"🏷️ Template: <b>{template}</b>  ({reason})\n"
                f"📊 ADR: {data['adr_pct']:.1f}%  RSI: {data['rsi']:.0f}"
                f"  Vol: {data['vol_ratio']:.1f}x\n"
                f"✅ Halal | Purify: {purify_str}\n"
                f"💰 งบ: ${acct_size}/หุ้น\n"
                f"🕐 {now_bkk_str()}"
            )
            send_telegram(token, chat_id, msg)
        else:
            log_print(f"  ℹ️ ไม่ได้เพิ่ม: {status}")

        scan_results.append({
            "symbol": sym, "passed": True, "template": template,
            "adr": data["adr_pct"], "rsi": data["rsi"], "at": now_str()
        })
        time.sleep(0.8)

    # ═════════════════════════════════════════════════════════════════════════
    #  PHASE 3: SUMMARY REPORT
    # ═════════════════════════════════════════════════════════════════════════
    log_print(f"\n{'='*65}")
    log_print(f"SUMMARY — {now_bkk_str()}")
    log_print(f"  Universe scanned : {len(scan_results)} tickers")
    log_print(f"  ➕ Added to WL   : {len(added_syms)}")
    log_print(f"  🔴 Removed from WL: {len(removed_syms)}")
    log_print(f"  📋 Watchlist now : {len(watchlist)}/{max_wl}")
    log_print(f"{'='*65}")

    if added_syms or removed_syms:
        lines = [
            "<b>📊 Screener รายงานประจำวัน</b>",
            f"🕐 {now_bkk_str()}",
            "",
            f"Universe: {len(universe)} ตัว | Watchlist: {len(watchlist)}/{max_wl}",
            "",
        ]
        if added_syms:
            lines.append(f"<b>➕ เพิ่มใหม่ {len(added_syms)} ตัว:</b>")
            for a in added_syms[:10]:
                pur = f"{a['purify']:.1f}%" if a.get("purify") is not None else "N/A"
                lines.append(
                    f"  • <b>{a['symbol']}</b> [{a['template']}]"
                    f" ADR={a['adr']:.1f}% Purify={pur}"
                )
        if removed_syms:
            lines.append(f"\n<b>🔴 ปลดออก {len(removed_syms)} ตัว:</b>")
            for r in removed_syms[:10]:
                lines.append(f"  • <b>{r['symbol']}</b> — {r['reason']}")
        lines.append(f"\n🤖 Halal Auto Screener v1.0")
        send_telegram(token, chat_id, "\n".join(lines))

    # ── บันทึก log + state ────────────────────────────────────────────────────
    today = today_str()
    scr_log.append({
        "date":    today,
        "scanned": len(scan_results),
        "added":   len(added_syms),
        "removed": len(removed_syms),
        "wl_size": len(watchlist),
        "added_syms":   [a["symbol"] for a in added_syms],
        "removed_syms": [r["symbol"] for r in removed_syms],
    })
    save_json(SCREENER_LOG_PATH, scr_log[-90:])   # เก็บ 90 วัน
    save_json(SCREENER_STATE, scr_state)

    # อัปเดต watchlist จาก halal re-check (screened_at)
    save_watchlist(wl_settings, watchlist)

    log_print("✅ Screener เสร็จสมบูรณ์")


if __name__ == "__main__":
    main()
