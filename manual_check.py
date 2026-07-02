#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  manual_check.py — Server-side "Check Halal ที่เลือก" helper                 ║
║                                                                              ║
║  ทำไมต้องมีไฟล์นี้:                                                          ║
║  Yahoo Finance (query1/query2.finance.yahoo.com) ไม่ส่ง                     ║
║  Access-Control-Allow-Origin header ให้ browser เรียกตรงได้ — เรียกจาก       ║
║  client-side JS จึงโดน CORS บล็อคเสมอ ต้องเรียกจาก server เท่านั้น           ║
║  สคริปต์นี้จึงรันผ่าน GitHub Actions (workflow_dispatch) แทน ซึ่งไม่มี        ║
║  ข้อจำกัด CORS เพราะไม่ใช่ browser — ใช้ yfinance เหมือนกับ                  ║
║  daily_screener.py เป๊ะ (ตัวเดียวกับที่พิสูจน์แล้วว่าเสถียรจาก scan          ║
║  อัตโนมัติทุกวัน) ไม่ต้องสมัคร API key ใดๆ เพิ่ม                             ║
║                                                                              ║
║  สคริปต์นี้จงใจ "ไม่ import daily_screener.py" — คัดลอกเฉพาะฟังก์ชันคำนวณ   ║
║  ล้วนๆ (_calc_ema/_calc_rsi/_calc_adr) มาแบบ verbatim แทน เพื่อไม่ให้        ║
║  import daily_screener.py ทั้งไฟล์แล้วดึงเอา side-effect อื่น (เช่น          ║
║  gemini_client) มาโดยไม่ตั้งใจ — คงเหลือ dependency เดียวคือ yfinance        ║
║                                                                              ║
║  Env: ไม่ต้องตั้ง secret ใดๆ — yfinance ดึงข้อมูลสาธารณะแบบเดียวกับ          ║
║  daily_screener.py                                                          ║
║                                                                              ║
║  Usage: python3 manual_check.py SYM1,SYM2,SYM3                              ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import json
import math
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import yfinance as yf
except ImportError:
    print("Installing yfinance...")
    os.system("pip install yfinance --quiet --break-system-packages")
    import yfinance as yf

# ─── Paths (ตรงกับ daily_screener.py — รันจาก repo root เดียวกัน) ────────────
BASE_DIR      = Path(__file__).parent
UNIVERSE_PATH = BASE_DIR / "universe.json"

MAX_SYMBOLS_PER_RUN = 50   # กันคนกดเลือกทั้ง 1200+ ตัวมาทีเดียว (ทำให้ Action รันนานเกินไป)

# ─── Gate thresholds default (override ได้จาก universe.json -> settings) ─────
# ต้องตรงกับ DEFAULT_CFG ใน daily_screener.py และ uniEvalGate() ใน dashboard
# เพื่อให้ผลลัพธ์ตรงกันไม่ว่าจะเช็คจากทางไหน
DEFAULT_CFG = {
    "max_purify_pct":      5.0,
    "min_adr_pct":         8.0,
    "min_price":           1.0,
    "min_avg_volume":  100000,
    "rsi_min":            35.0,
    "rsi_max":            72.0,
    "volume_ratio_min":    1.3,
    "require_above_ema50": True,
}


# ══════════════════════════════════════════════════════════════════════════════
#  UTILITIES (คัดลอกจาก daily_screener.py แบบ verbatim เพื่อผลลัพธ์ตรงกัน)
# ══════════════════════════════════════════════════════════════════════════════

def load_json(path, default):
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default


def _sanitize_for_json(obj):
    """กัน NaN/Infinity หลุดเข้า JSON — browser JSON.parse() จะ throw ถ้ามี"""
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_for_json(v) for v in obj]
    return obj


def save_json(path, data):
    clean = _sanitize_for_json(data)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=2, allow_nan=False)


def now_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_bkk_str():
    bkk = datetime.now(timezone.utc) + timedelta(hours=7)
    return bkk.strftime("%d/%m/%Y %H:%M ICT")


def update_universe_entry(universe_data, sym, **fields):
    """merge field เข้า entry เดิม ไม่ทับทั้ง object (กัน halal_status ที่ client
    เพิ่งเขียนไปหายเวลา Action มาเขียนทับทีหลัง)"""
    for _ui, _ue in enumerate(universe_data.get("universe", [])):
        _us = (_ue if isinstance(_ue, str) else _ue.get("symbol", "")).upper()
        if _us != sym.upper():
            continue
        if isinstance(_ue, str):
            base = {"symbol": sym.upper(), "name": sym.upper(), "added_at": now_str()}
            base.update(fields)
            universe_data["universe"][_ui] = base
        else:
            _ue.update(fields)
        return True
    return False


def _calc_ema(prices, period):
    if len(prices) < period:
        return [None] * len(prices)
    result = [None] * (period - 1)
    seed = sum(prices[:period]) / period
    result.append(seed)
    k = 2.0 / (period + 1)
    for p in prices[period:]:
        result.append(p * k + result[-1] * (1 - k))
    return result


def _calc_rsi(closes, period=14):
    result = [None] * period
    if len(closes) <= period:
        return result + [None] * max(0, len(closes) - period)
    gains  = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
    avg_g  = sum(gains[:period]) / period
    avg_l  = sum(losses[:period]) / period
    rsi_val = 100.0 - 100.0 / (1 + avg_g / avg_l) if avg_l > 0 else 100.0
    result.append(rsi_val)
    for i in range(period, len(gains)):
        avg_g   = (avg_g * (period - 1) + gains[i]) / period
        avg_l   = (avg_l * (period - 1) + losses[i]) / period
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
#  YFINANCE FETCH
# ══════════════════════════════════════════════════════════════════════════════

def fetch_ohlcv_yf(symbol, period="6mo"):
    """คืน (closes, highs, lows, volumes) หรือ None ถ้าไม่มีข้อมูลจริง
    Raises บน network/lookup error (แยกจาก "ไม่มีข้อมูล" โดยเจตนา — จะได้ไม่
    swallow error เงียบๆ เหมือน bug เดิมที่เจอกับการยิง Yahoo ตรงจาก browser)"""
    try:
        t = yf.Ticker(symbol)
        hist = t.history(period=period, interval="1d", auto_adjust=False, timeout=20)
    except Exception as e:
        raise RuntimeError(f"yfinance fetch error: {e}") from e

    if hist is None or hist.empty:
        return None  # ticker มีจริงแต่ไม่มีข้อมูลช่วงนี้ (delisted ฯลฯ) — ไม่ใช่ error

    hist = hist.dropna(subset=["Close", "High", "Low"])
    if len(hist) < 20:
        return None  # ข้อมูลน้อยเกินไปสำหรับคำนวณ ADR20/RSI14

    closes = hist["Close"].tolist()
    highs  = hist["High"].tolist()
    lows   = hist["Low"].tolist()
    vols   = hist["Volume"].fillna(0).tolist()
    return closes, highs, lows, vols


def compute_technical(symbol):
    candles = fetch_ohlcv_yf(symbol)
    if candles is None:
        return None
    closes, highs, lows, volumes = candles

    price = closes[-1]
    if price is None or math.isnan(price):
        return None

    adr_pct = _calc_adr(highs, lows, 20)
    if math.isnan(adr_pct):
        return None

    today_vol = volumes[-1] if volumes else 0
    last20vol = volumes[-21:-1]
    avg_vol   = (sum(last20vol) / len(last20vol)) if last20vol else today_vol
    vol_ratio = (today_vol / avg_vol) if avg_vol > 0 else 1.0

    ema50_list  = _calc_ema(closes, 50)
    ema50       = next((v for v in reversed(ema50_list) if v is not None), None)
    above_ema50 = (price > ema50) if ema50 else False

    rsi_list = _calc_rsi(closes, 14)
    rsi      = next((v for v in reversed(rsi_list) if v is not None), 50.0)

    return {
        "price":       round(price, 4),
        "adr_pct":     round(adr_pct, 2),
        "avg_volume":  int(avg_vol),
        "vol_ratio":   round(vol_ratio, 2),
        "rsi":         round(rsi, 1),
        "above_ema50": above_ema50,
    }


def eval_gate(entry, tech, cfg):
    """ตรรกะเดียวกับ run_gates() ใน daily_screener.py และ uniEvalGate() ใน
    dashboard (เวอร์ชันย่อ 6 ขั้นแบบเดียวกับที่ dashboard ใช้)"""
    halal_status = entry.get("halal_status") or "UNKNOWN"
    purify       = entry.get("purify_pct")

    if halal_status != "HALAL":
        return "halal"
    if purify is not None and purify > cfg["max_purify_pct"]:
        return "purify"
    if tech is None:
        return "no_data"
    if tech["price"] < cfg["min_price"] or tech["avg_volume"] < cfg["min_avg_volume"]:
        return "liquidity"
    if tech["adr_pct"] < cfg["min_adr_pct"]:
        return "adr"
    if cfg["require_above_ema50"] and not tech["above_ema50"]:
        return "trend"
    if not (cfg["rsi_min"] <= tech["rsi"] <= cfg["rsi_max"]) or tech["vol_ratio"] < cfg["volume_ratio_min"]:
        return "momentum"
    return "PASSED"


# ══════════════════════════════════════════════════════════════════════════════
#  GIT COMMIT + PUSH (กัน race condition กับ daily-screener/alert-engine ที่
#  อาจรันคาบเกี่ยวกัน — pull --rebase ก่อน push ทุกครั้ง + retry ถ้าโดน reject)
# ══════════════════════════════════════════════════════════════════════════════

def git_commit_push(commit_msg, max_attempts=3):
    os.system("git config user.name 'manual-check-bot'")
    os.system("git config user.email 'actions@github.com'")
    os.system("git add universe.json")

    commit_rc = os.system(f'git commit -m "{commit_msg}"')
    if commit_rc != 0:
        print("[git] ไม่มีอะไรเปลี่ยน หรือ commit ล้มเหลว — ข้ามการ push")
        return

    for attempt in range(1, max_attempts + 1):
        os.system("git pull --rebase --autostash origin HEAD")
        push_rc = os.system("git push origin HEAD")
        if push_rc == 0:
            print(f"[git] push สำเร็จ (attempt {attempt}/{max_attempts})")
            return
        print(f"[git] push ล้มเหลว attempt {attempt}/{max_attempts} — ลองใหม่...")
        time.sleep(3)

    # ตรงกับหลักการ "failures should surface, not be swallowed" — ห้าม
    # กลืน error เงียบๆ แบบที่เคยเกิดกับ `git push || echo "Nothing to push"`
    print("::error::git push ล้มเหลวครบทุก attempt — ตรวจสอบ permission ของ GITHUB_TOKEN "
          "(ต้องเป็น 'Read and write' ใน Settings > Actions > General > Workflow permissions)")
    sys.exit(1)


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        print("::error::ต้องระบุ symbols เช่น: python3 manual_check.py AEHR,ADVB,AEIS")
        sys.exit(1)

    symbols = [s.strip().upper() for s in sys.argv[1].split(",") if s.strip()]
    if not symbols:
        print("::error::parse symbols แล้วไม่เหลือตัวที่ถูกต้องเลย")
        sys.exit(1)
    if len(symbols) > MAX_SYMBOLS_PER_RUN:
        print(f"::warning::ขอ {len(symbols)} symbols — ตัดเหลือ {MAX_SYMBOLS_PER_RUN} "
              f"ตัวแรก (กัน yfinance ยิงถี่เกินไปจนโดน Yahoo throttle ชั่วคราว)")
        symbols = symbols[:MAX_SYMBOLS_PER_RUN]

    universe_data = load_json(UNIVERSE_PATH, {"settings": {}, "universe": []})
    cfg = {**DEFAULT_CFG, **(universe_data.get("settings") or {})}

    results = []
    for i, sym in enumerate(symbols):
        print(f"[{i + 1}/{len(symbols)}] {sym} ...")

        entry = next(
            (e for e in universe_data.get("universe", [])
             if (e if isinstance(e, str) else e.get("symbol", "")).upper() == sym),
            {}
        )
        if isinstance(entry, str):
            entry = {}

        try:
            tech = compute_technical(sym)
        except Exception as e:
            print(f"  [yfinance] {sym}: {e}")
            tech = None

        gate = eval_gate(entry, tech, cfg)
        checked_at = now_str()

        fields = {"last_gate": gate}
        if tech:
            fields.update({
                "last_price":       tech["price"],
                "last_adr_pct":     tech["adr_pct"],
                "last_rsi":         tech["rsi"],
                "last_vol_ratio":   tech["vol_ratio"],
                "last_above_ema50": tech["above_ema50"],
                "last_scanned":     checked_at,
            })
        # last_checked หมายถึง "เวลาที่ตรวจ halal ล่าสุด" ตาม convention เดิมของ
        # daily_screener.py — ไม่แตะตรงนี้ถ้า halal ไม่ได้ถูก re-check ในสคริปต์นี้
        # (dashboard client เป็นคนเขียน halal_status/purify_pct/last_checked ไปแล้ว
        # ก่อน dispatch มาที่นี่)

        update_universe_entry(universe_data, sym, **fields)
        results.append({"symbol": sym, "gate": gate, "has_data": tech is not None})

        if i < len(symbols) - 1:
            time.sleep(0.8)  # เว้นจังหวะกัน Yahoo throttle ชั่วคราว

    save_json(UNIVERSE_PATH, universe_data)

    passed   = sum(1 for r in results if r["gate"] == "PASSED")
    no_data  = sum(1 for r in results if not r["has_data"])
    summary  = ", ".join(f"{r['symbol']}={r['gate']}" for r in results)
    print(f"\n✅ เช็คเสร็จ {len(results)} ตัว — ผ่านทุก Gate {passed} ตัว, "
          f"no_data จริง {no_data} ตัว")
    print(f"   {summary}")

    commit_msg = f"universe: manual check via yfinance (GitHub Actions) ({len(results)} symbols, {passed} passed) [{now_bkk_str()}]"
    git_commit_push(commit_msg)


if __name__ == "__main__":
    main()
