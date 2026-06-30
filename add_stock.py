#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  add_stock.py — AI-Powered Stock Template Selector                         ║
║  ใช้ Gemini AI วิเคราะห์หุ้นแล้วเลือก template ที่เหมาะสมอัตโนมัติ       ║
║                                                                              ║
║  Usage:                                                                      ║
║    python3 add_stock.py SYMBOL "Stock Name"                                  ║
║    python3 add_stock.py AAPL "Apple Inc."                                    ║
║                                                                              ║
║  Env: GEMINI_API_KEY, GITHUB_TOKEN, GITHUB_REPO (owner/repo)                ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import json
import math
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

try:
    import yfinance as yf
except ImportError:
    os.system("pip install yfinance --quiet --break-system-packages")
    import yfinance as yf

# ── Gemini client (อยู่ใน repo เดียวกัน ไม่ต้องผ่าน Netlify) ────────────────
try:
    from gemini_client import gemini_json as _gemini_json
    _GEMINI_AVAILABLE = True
except ImportError:
    _GEMINI_AVAILABLE = False

BASE_DIR       = Path(__file__).parent
WATCHLIST_PATH = BASE_DIR / "watchlist.json"


# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
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


def now_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ══════════════════════════════════════════════════════════════════════════════
#  STEP 1: FETCH STOCK INFO FROM YFINANCE
# ══════════════════════════════════════════════════════════════════════════════

def fetch_stock_info(symbol):
    """ดึงข้อมูลหุ้นจาก yfinance สำหรับส่งให้ Gemini วิเคราะห์"""
    print(f"[Step 1] ดึงข้อมูล {symbol} จาก yfinance...")
    try:
        ticker = yf.Ticker(symbol)
        info   = ticker.info

        # ดึง price history 90 วัน
        hist = ticker.history(period="90d", interval="1d")

        price      = info.get("currentPrice") or info.get("regularMarketPrice") or 0
        prev_close = info.get("previousClose") or price
        market_cap = info.get("marketCap") or 0
        beta       = info.get("beta") or 1.0
        sector     = info.get("sector") or "Unknown"
        industry   = info.get("industry") or "Unknown"
        avg_vol    = info.get("averageVolume") or info.get("averageVolume10days") or 0
        name       = info.get("longName") or info.get("shortName") or symbol

        # คำนวณ ADR (Average Daily Range %) จาก 20 วันล่าสุด
        adr_pct = 0.0
        if not hist.empty and len(hist) >= 10:
            recent = hist.tail(20)
            daily_ranges = ((recent["High"] - recent["Low"]) / recent["Low"] * 100).tolist()
            adr_pct = round(sum(daily_ranges) / len(daily_ranges), 2)

        # คำนวณ 52W range position
        high_52w = info.get("fiftyTwoWeekHigh") or price
        low_52w  = info.get("fiftyTwoWeekLow") or price
        range_52w_pct = round((price - low_52w) / (high_52w - low_52w) * 100, 1) if high_52w > low_52w else 50

        return {
            "symbol":        symbol,
            "name":          name,
            "price":         round(float(price), 4),
            "market_cap":    market_cap,
            "market_cap_m":  round(market_cap / 1_000_000, 1) if market_cap else 0,
            "beta":          round(float(beta), 2),
            "sector":        sector,
            "industry":      industry,
            "avg_volume":    avg_vol,
            "adr_pct":       adr_pct,
            "high_52w":      round(float(high_52w), 4),
            "low_52w":       round(float(low_52w), 4),
            "range_52w_pct": range_52w_pct,
        }
    except Exception as e:
        print(f"  [Error] fetch_stock_info: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
#  STEP 2: GEMINI AI TEMPLATE SELECTION
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
#  STEP 2: GEMINI AI TEMPLATE SELECTION
# ══════════════════════════════════════════════════════════════════════════════

def ai_select_template(stock_info, api_key):
    """
    ให้ Gemini วิเคราะห์หุ้นและเลือก template
    ใช้ gemini_client.py ที่อยู่ใน repo เดียวกัน (ไม่ผ่าน Netlify)
    Templates: VOLATILE, GROWTH, STABLE, MOMENTUM
    """
    print("[Step 2] Gemini AI วิเคราะห์หุ้น...")

    if not api_key or not _GEMINI_AVAILABLE:
        print("  ⚠️ Gemini ไม่พร้อม → ใช้ rule-based fallback")
        return _fallback_template(stock_info)

    prompt = (
        "You are a quantitative trading system assistant. Analyze this stock and select the best alert template.\n\n"
        f"Stock Data:\n"
        f"- Symbol: {stock_info['symbol']}\n"
        f"- Name: {stock_info['name']}\n"
        f"- Price: ${stock_info['price']}\n"
        f"- Market Cap: ${stock_info['market_cap_m']}M\n"
        f"- Beta: {stock_info['beta']}\n"
        f"- Sector: {stock_info['sector']}\n"
        f"- Industry: {stock_info['industry']}\n"
        f"- ADR (Average Daily Range %): {stock_info['adr_pct']}%\n"
        f"- 52W High: ${stock_info['high_52w']}\n"
        f"- 52W Low: ${stock_info['low_52w']}\n"
        f"- Price position in 52W range: {stock_info['range_52w_pct']}%\n"
        f"- Avg Daily Volume: {stock_info['avg_volume']:,}\n\n"
        "Template Options:\n"
        "1. VOLATILE — High ADR (>12%), high beta (>1.5), biotech/crypto/penny/micro-cap, speculative\n"
        "2. GROWTH — Medium ADR (8-12%), tech/healthcare/fintech mid-cap, established momentum\n"
        "3. STABLE — Low ADR (<8%), ETF/large-cap/dividend, conservative\n"
        "4. MOMENTUM — News/catalyst driven, sector rotation, ADR varies\n\n"
        "Rules:\n"
        "- If ADR > 15% OR market cap < $100M OR beta > 2 → VOLATILE\n"
        "- If sector is Biotechnology/Drug/Pharmaceutical → VOLATILE\n"
        "- If it's an ETF → STABLE\n"
        "- If ADR 8-12% AND tech/software/semiconductor → GROWTH\n"
        "- If news-driven, small cap with irregular spikes → MOMENTUM\n\n"
        "Respond with ONLY a JSON object, no markdown, no explanation:\n"
        '{"template": "GROWTH", "reason": "brief reason in Thai max 50 chars", "confirm_hits": 1}\n\n'
        "confirm_hits = 1 for volatile/momentum, 2 for growth/stable"
    )

    try:
        result       = _gemini_json(prompt, max_tokens=256, temperature=0.1, api_key=api_key)
        template     = result.get("template", "GROWTH").upper()
        reason       = result.get("reason", "AI เลือกอัตโนมัติ")
        confirm_hits = result.get("confirm_hits", 1)
        if template not in ("VOLATILE", "GROWTH", "STABLE", "MOMENTUM"):
            template = "GROWTH"
        print(f"  AI เลือก: {template}  ({reason})")
        return template, reason, confirm_hits
    except Exception as e:
        print(f"  [Warning] Gemini error: {e} → fallback ใช้ ADR rule")
        return _fallback_template(stock_info)


def _fallback_template(stock_info):
    """Rule-based fallback ถ้า Gemini ไม่พร้อม"""
    adr = stock_info.get("adr_pct", 10)
    beta = stock_info.get("beta", 1.0)
    mc   = stock_info.get("market_cap_m", 500)
    sec  = stock_info.get("sector", "")
    if adr > 15 or mc < 100 or beta > 2 or "Biotech" in sec or "Drug" in sec:
        return "VOLATILE", "ADR/beta สูง", 1
    elif adr < 6:
        return "STABLE", "ADR ต่ำ", 2
    elif adr >= 8:
        return "GROWTH", "ADR ปานกลาง", 1
    else:
        return "MOMENTUM", "ADR ต่ำกว่า 8%", 1


# ══════════════════════════════════════════════════════════════════════════════
#  STEP 3: BUILD STOCK ENTRY FROM TEMPLATE
# ══════════════════════════════════════════════════════════════════════════════

def build_stock_entry(symbol, name, template, confirm_hits=1, account_size=100):
    """สร้าง stock entry ตาม template ที่ Gemini เลือก"""
    sym = symbol.upper()

    def sell_alerts():
        return [
            {"id": f"{sym}_SL_BREAK", "emoji": "🚨", "type": "support_resistance",
             "action": "SELL", "cooldown_minutes": 30, "note": "Auto stop loss",
             "enabled": True, "level": 0, "direction": "break_below"},
            {"id": f"{sym}_PCT_DROP", "emoji": "📉", "type": "percent_change",
             "action": "SELL", "cooldown_minutes": 480, "note": "Drop ≥5% single day",
             "enabled": True, "threshold_pct": 5, "direction": "down"},
            {"id": f"{sym}_MA_DEATH", "emoji": "💀", "type": "ma_crossover",
             "action": "SELL", "cooldown_minutes": 1440, "note": "Death Cross EMA9/21",
             "enabled": True, "condition": "death_cross", "ma_type": "EMA",
             "interval": "1d", "fast_period": 9, "slow_period": 21},
            {"id": f"{sym}_SCORE_BEAR", "emoji": "🐻", "type": "alert_score",
             "action": "SELL", "cooldown_minutes": 240, "note": "Bearish score ≥70",
             "enabled": True, "direction": "bearish", "min_score": 70, "interval": "1d"},
        ]

    if template == "VOLATILE":
        buy_alerts = [
            {"id": f"{sym}_RSI_OS", "emoji": "💚", "type": "rsi", "action": "BUY",
             "cooldown_minutes": 180, "note": "RSI Oversold Fast", "enabled": True,
             "condition": "oversold", "interval": "1d", "period": 14,
             "oversold_level": 32, "overbought_level": 70},
            {"id": f"{sym}_VOL_3X", "emoji": "🔥", "type": "volume_spike", "action": "BUY",
             "cooldown_minutes": 60, "note": "Volume 3x breakout", "enabled": True,
             "multiplier": 3},
            {"id": f"{sym}_PCT_UP", "emoji": "📈", "type": "percent_change", "action": "BUY",
             "cooldown_minutes": 120, "note": "Price surge ≥6%", "enabled": True,
             "threshold_pct": 6, "direction": "up"},
            {"id": f"{sym}_SCORE_60", "emoji": "🎯", "type": "alert_score", "action": "BUY",
             "cooldown_minutes": 180, "note": "Score ≥60 bullish", "enabled": True,
             "direction": "bullish", "min_score": 60, "interval": "1d"},
        ]

    elif template == "GROWTH":
        buy_alerts = [
            {"id": f"{sym}_RSI_OS", "emoji": "💚", "type": "rsi", "action": "BUY",
             "cooldown_minutes": 240, "note": "RSI Oversold 1D", "enabled": True,
             "condition": "oversold", "interval": "1d", "period": 14,
             "oversold_level": 30, "overbought_level": 70},
            {"id": f"{sym}_MA_GOLD", "emoji": "✨", "type": "ma_crossover", "action": "BUY",
             "cooldown_minutes": 1440, "note": "Golden Cross EMA9/21", "enabled": True,
             "condition": "golden_cross", "ma_type": "EMA", "interval": "1d",
             "fast_period": 9, "slow_period": 21},
            {"id": f"{sym}_SCORE_65", "emoji": "🎯", "type": "alert_score", "action": "BUY",
             "cooldown_minutes": 240, "note": "Score ≥65 bullish", "enabled": True,
             "direction": "bullish", "min_score": 65, "interval": "1d"},
            {"id": f"{sym}_MTF_BULL", "emoji": "🔭", "type": "mtf_alignment", "action": "BUY",
             "cooldown_minutes": 480, "note": "1H+4H+1D aligned", "enabled": True,
             "timeframes": ["1h", "4h", "1d"],
             "required_alignment": "mostly_bullish", "min_bullish": 2},
        ]

    elif template == "STABLE":
        buy_alerts = [
            {"id": f"{sym}_RSI_OS", "emoji": "💚", "type": "rsi", "action": "BUY",
             "cooldown_minutes": 480, "note": "RSI Oversold Conservative", "enabled": True,
             "condition": "oversold", "interval": "1d", "period": 14,
             "oversold_level": 28, "overbought_level": 72},
            {"id": f"{sym}_SCORE_70", "emoji": "🎯", "type": "alert_score", "action": "BUY",
             "cooldown_minutes": 480, "note": "Score ≥70 high confidence", "enabled": True,
             "direction": "bullish", "min_score": 70, "interval": "1d"},
            {"id": f"{sym}_MTF_BULL", "emoji": "🔭", "type": "mtf_alignment", "action": "BUY",
             "cooldown_minutes": 720, "note": "MTF 4H+1D aligned", "enabled": True,
             "timeframes": ["4h", "1d"], "required_alignment": "mostly_bullish", "min_bullish": 2},
        ]

    else:  # MOMENTUM
        buy_alerts = [
            {"id": f"{sym}_PCT_UP", "emoji": "📈", "type": "percent_change", "action": "BUY",
             "cooldown_minutes": 120, "note": "Momentum ≥5%", "enabled": True,
             "threshold_pct": 5, "direction": "up"},
            {"id": f"{sym}_VOL_2X", "emoji": "🔥", "type": "volume_spike", "action": "BUY",
             "cooldown_minutes": 60, "note": "Volume 2x confirm", "enabled": True,
             "multiplier": 2},
            {"id": f"{sym}_SCORE_62", "emoji": "🎯", "type": "alert_score", "action": "BUY",
             "cooldown_minutes": 180, "note": "Score ≥62 bullish", "enabled": True,
             "direction": "bullish", "min_score": 62, "interval": "1d"},
        ]

    return {
        "symbol":   sym,
        "name":     name,
        "market":   "US",
        "timeframe":"1D",
        "enabled":  True,
        "confirm_hits": confirm_hits,
        "template": template,
        "added_at": now_str(),
        # ── Gate tuning (ใช้ใน alert_engine.py) ───────────────────────
        "re_entry_cooldown_minutes":   180 if template in ("VOLATILE", "MOMENTUM") else 240,
        "post_sell_cooldown_minutes":  90  if template in ("VOLATILE", "MOMENTUM") else 120,
        "buy_suppress_drop_pct":       3.0,
        "position_alert": {
            "account_size": account_size,
            "risk_pct":     2.0,
            "target_pct":   8.0,
        },
        "alerts": buy_alerts + sell_alerts(),
    }


# ══════════════════════════════════════════════════════════════════════════════
#  STEP 4: ADD TO WATCHLIST.JSON
# ══════════════════════════════════════════════════════════════════════════════

def add_to_watchlist(stock_entry):
    """เพิ่มหุ้นเข้า watchlist.json"""
    data     = load_json(WATCHLIST_PATH, {"settings": {}, "watchlist": []})
    watchlist = data.get("watchlist", [])
    sym       = stock_entry["symbol"]

    # Check duplicate
    existing = [i for i, s in enumerate(watchlist) if s["symbol"] == sym]
    if existing:
        print(f"  [{sym}] มีอยู่แล้วใน watchlist (index {existing[0]}) — อัปเดต template")
        watchlist[existing[0]] = stock_entry
    else:
        watchlist.append(stock_entry)
        print(f"  [{sym}] เพิ่มเข้า watchlist แล้ว (รวม {len(watchlist)} ตัว)")

    data["watchlist"] = watchlist
    save_json(WATCHLIST_PATH, data)
    return len(watchlist)


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    # ── รับ arguments ────────────────────────────────────────────────
    if len(sys.argv) < 2:
        print("Usage: python3 add_stock.py SYMBOL [\"Stock Name\"]")
        print("Example: python3 add_stock.py AAPL \"Apple Inc.\"")
        sys.exit(1)

    symbol       = sys.argv[1].upper().strip()
    custom_name  = sys.argv[2].strip() if len(sys.argv) > 2 else None
    api_key      = os.environ.get("GEMINI_API_KEY", "")
    account_size = int(os.environ.get("ACCOUNT_SIZE_PER_STOCK", "100"))

    print(f"\n{'='*60}")
    print(f"  ADD STOCK: {symbol}")
    print(f"  Budget per stock: ${account_size}")
    print(f"  Gemini AI: {'✅ enabled' if api_key else '⚠️ fallback (rule-based)'}")
    print(f"{'='*60}\n")

    # ── Step 1: Fetch info ─────────────────────────────────────────
    stock_info = fetch_stock_info(symbol)
    if stock_info is None:
        print(f"❌ ไม่พบข้อมูลหุ้น {symbol} ใน yfinance")
        sys.exit(1)

    name = custom_name or stock_info["name"]
    print(f"  Name: {name}")
    print(f"  Price: ${stock_info['price']}")
    print(f"  ADR: {stock_info['adr_pct']}%")
    print(f"  Beta: {stock_info['beta']}")
    print(f"  Market Cap: ${stock_info['market_cap_m']}M")
    print(f"  Sector: {stock_info['sector']}")

    # ── Step 2: AI Template Selection ──────────────────────────────
    if api_key:
        template, reason, confirm_hits = ai_select_template(stock_info, api_key)
    else:
        # Rule-based fallback
        adr  = stock_info.get("adr_pct", 10)
        beta = stock_info.get("beta", 1.0)
        mc   = stock_info.get("market_cap_m", 500)
        sec  = stock_info.get("sector", "")
        if adr > 15 or mc < 100 or beta > 2 or "Biotech" in sec:
            template, reason, confirm_hits = "VOLATILE", "ADR/beta/cap สูง", 1
        elif adr < 6:
            template, reason, confirm_hits = "STABLE", "ADR ต่ำ", 2
        elif adr >= 8:
            template, reason, confirm_hits = "GROWTH", "ADR 8-12%", 1
        else:
            template, reason, confirm_hits = "MOMENTUM", "ใช้ momentum", 1

    print(f"\n  📋 Template: {template}")
    print(f"  📝 เหตุผล: {reason}")
    print(f"  ✅ Confirm hits: {confirm_hits}")

    # ── Step 3: Build entry ────────────────────────────────────────
    stock_entry = build_stock_entry(symbol, name, template, confirm_hits, account_size)

    # ── Step 4: Save ───────────────────────────────────────────────
    print(f"\n[Step 3] บันทึกเข้า watchlist.json...")
    total = add_to_watchlist(stock_entry)

    print(f"\n{'='*60}")
    print(f"✅ เสร็จสิ้น!")
    print(f"   {symbol} ({name})")
    print(f"   Template: {template}  |  Budget: ${account_size}")
    print(f"   Alerts: {len(stock_entry['alerts'])} รายการ")
    print(f"   Total watchlist: {total} stocks")
    print(f"{'='*60}\n")

    # Output JSON สำหรับ integration กับ web
    result = {
        "success": True,
        "symbol":   symbol,
        "name":     name,
        "template": template,
        "reason":   reason,
        "alerts_count": len(stock_entry["alerts"]),
        "total_stocks": total,
    }
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
