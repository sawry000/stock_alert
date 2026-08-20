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
UNIVERSE_PATH  = BASE_DIR / "universe.json"
# Structural fix (race condition กับ daily-screener): alert_engine.py ไม่เขียน
# universe.json ตรงๆ อีกต่อไป — เขียนลงไฟล์แยกนี้แทน แล้วให้ daily_screener.py
# เป็นคน merge เข้า universe.json ตอน startup รอบถัดไป (ดู sync_universe_tech()
# และท้าย main() ด้านล่าง)
PATCH_PATH     = BASE_DIR / "universe_live_patch.json"

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

        # ADR (Average Daily Range 14 วัน) — คำนวณจาก ticker ที่สร้างไว้แล้ว
        # ใช้เป็น dynamic threshold สำหรับ percent_change alert
        # (ไม่ต้องเรียก yfinance เพิ่ม เพราะ ticker object ยังอยู่ใน scope นี้)
        adr_pct = 0.0
        try:
            hist_adr = ticker.history(period="20d", interval="1d")
            if len(hist_adr) >= 5:
                ranges  = ((hist_adr["High"] - hist_adr["Low"]) / hist_adr["Close"]) * 100
                adr_pct = float(ranges.tail(14).mean())
        except Exception:
            adr_pct = 0.0

        return {
            "price":      price,
            "prev_close": prev_close,
            "change_pct": change_pct,
            "volume":     today_vol,
            "avg_volume": avg_volume,
            "adr_pct":    adr_pct,
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
#  UNIVERSE.JSON SYNC — เติม Purify/Price/ADR/RSI/Vol/Gate ล่าสุดให้เฉพาะ symbol
#  ที่อยู่ใน watchlist ทุกครั้งที่ alert-engine รัน (ทุก 5 นาที) จะได้มีข้อมูล
#  สดใหม่ตลอดในหน้า Watchlist Manager โดยไม่ต้องรอ daily_screener.py รอบเช้า
#  หรือกด "Check Halal ที่เลือก" เอง
#
#  หมายเหตุ: ตัวที่ "ไม่ได้" อยู่ใน watchlist (universe อีก ~1800 ตัว) จะไม่ถูก
#  แตะเลยจากฟังก์ชันนี้ — เพื่อไม่ให้ alert-engine ที่ต้องรันเร็วทุก 5 นาที
#  ช้าลงจากการ scan universe ทั้งก้อน (นั่นเป็นหน้าที่ของ daily_screener.py)
# ══════════════════════════════════════════════════════════════════════════════

DEFAULT_UNI_CFG = {
    "max_purify_pct":      5.0,
    "min_adr_pct":         8.0,
    "min_price":           1.0,
    "min_avg_volume":  100000,
    "rsi_min":            35.0,
    "rsi_max":            72.0,
    "volume_ratio_min":    1.3,
    "require_above_ema50": True,
}


def _calc_adr(highs, lows, n=20):
    """สูตร Average Daily Range % เดียวกับ daily_screener.py/manual_check.py:
    (High-Low)/Low*100 เฉลี่ย n วันล่าสุด — ตั้งใจแยกจาก adr_pct ที่คำนวณใน
    fetch_quote() (ใช้ Close เป็นตัวหารและ lookback ต่างกัน) เพราะค่าที่จะโชว์
    ในคอลัมน์ ADR ของ Universe Manager/Watchlist ต้องเทียบกับ threshold
    min_adr_pct เดียวกับที่ daily_screener.py ใช้ ถ้าสูตรไม่ตรงกัน gate จะ
    ตัดสินผิดเพี้ยนไปจากที่ scan อัตโนมัติเห็น"""
    pairs = list(zip(highs[-n:], lows[-n:]))
    if not pairs:
        return 0.0
    ranges = [(h - l) / l * 100 for h, l in pairs if l > 0]
    return sum(ranges) / len(ranges) if ranges else 0.0


def _calc_gap_stats(opens, closes, n=60):
    """วัดความเสี่ยง "gap ข้ามคืน" ของหุ้นย้อนหลัง n วัน — ต่างจาก ADR ตรงที่
    ADR วัดการแกว่งตัว "ระหว่างวัน" (High-Low) แต่เคสที่พังหนักสุด (BLLN
    -34%, VPG -31%, IOVA -29%, BLZE -23%) ถือแค่ 0.03-0.99 วันก่อนโดน stop
    — สั้นขนาดนี้บ่งชี้ว่าเป็น "กระโดดข้ามคืน" (ราคาเปิดเช้าต่างจากราคาปิด
    เมื่อวานมาก) ไม่ใช่แค่แกว่งในวันเดียวกัน แต่ ADR วัดไม่ตรงกับกลไกนี้เลย
    วัด gap ตรงๆ แม่นกว่า: gap% = (Open วันนี้ - Close เมื่อวาน) / Close
    เมื่อวาน * 100 (ใช้ค่า absolute เพราะสนใจแค่ขนาดของการกระโดด ไม่สนทิศทาง)

    คืนค่า (max_gap_pct, p90_gap_pct):
      - max_gap_pct: gap ที่รุนแรงที่สุดที่เคยเกิดในช่วงย้อนหลัง — สะท้อน
        "worst case ที่เคยเกิดจริงกับหุ้นตัวนี้" (เช่น หุ้น biotech ที่เคย
        กระโดดแรงจากข่าว FDA ก็มีโอกาสเกิดซ้ำได้อีกในธรรมชาติของมัน)
      - p90_gap_pct: 90th percentile — ตัวแทน "วันแย่ทั่วไป" ที่ไม่ใช่แค่
        outlier ครั้งเดียว ทนทานกว่า max เวลาหุ้นมี 1 เหตุการณ์ผิดปกติจริงๆ
        ที่ไม่น่าเกิดซ้ำ (เช่น stock split ที่คำนวณ gap ผิดเพี้ยน)
    """
    if len(opens) < 2 or len(closes) < 2:
        return 0.0, 0.0
    m = min(len(opens), len(closes))
    opens_a  = opens[-m:]
    closes_a = closes[-m:]
    lookback = min(n, m - 1)
    gaps = []
    for i in range(m - lookback, m):
        cp = closes_a[i - 1]   # ราคาปิดของ "วันก่อนหน้า" เทียบกับ opens_a[i]
        if cp and cp > 0:
            gaps.append(abs(opens_a[i] - cp) / cp * 100)
    if not gaps:
        return 0.0, 0.0
    gaps.sort()
    max_gap = gaps[-1]
    p90_idx = max(0, int(len(gaps) * 0.9) - 1)
    p90_gap = gaps[p90_idx]
    return round(max_gap, 2), round(p90_gap, 2)


def _clean_num(v):
    """กัน NaN/Infinity หลุดเข้า JSON — json.dump ปกติจะ dump เป็น literal
    `NaN` ซึ่งไม่ใช่ JSON มาตรฐาน ทำให้ JSON.parse() ฝั่ง browser (dashboard)
    throw ตอนโหลดไฟล์ทั้งไฟล์ (ไม่ใช่แค่ field นั้น)"""
    if v is None:
        return None
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


def _uni_find_entry(universe_data, symbol):
    sym_u = symbol.upper()
    for e in universe_data.get("universe", []):
        s = (e if isinstance(e, str) else e.get("symbol", "")).upper()
        if s == sym_u:
            return None if isinstance(e, str) else e
    return None


def _uni_symbol_exists(universe_data, symbol):
    sym_u = symbol.upper()
    return any(
        (e if isinstance(e, str) else e.get("symbol", "")).upper() == sym_u
        for e in universe_data.get("universe", [])
    )


def build_earnings_alert_message(watchlist, universe_data, threshold_days):
    """
    สร้างข้อความแจ้งเตือนหุ้นใน watchlist ที่จะประกาศผลประกอบการภายใน
    threshold_days วันข้างหน้า (อ่าน next_earnings_date จาก universe.json ซึ่ง
    daily_screener.py เป็นคนดึงมาเก็บไว้) — คืนค่า None ถ้าไม่มีตัวไหนใกล้เลย
    """
    today    = now_utc().date()
    upcoming = []
    for stock in watchlist:
        sym   = stock.get("symbol", "")
        entry = _uni_find_entry(universe_data, sym)
        if not entry:
            continue
        earn_str = entry.get("next_earnings_date")
        if not earn_str:
            continue
        try:
            earn_date = datetime.fromisoformat(earn_str).date()
        except (ValueError, TypeError):
            continue
        days_left = (earn_date - today).days
        if 0 <= days_left <= threshold_days:
            upcoming.append((days_left, sym, earn_date))

    if not upcoming:
        return None

    upcoming.sort(key=lambda x: x[0])
    lines = [
        "📅 <b>ใกล้ประกาศผลประกอบการ</b>",
        f"🕐 {now_bkk_str()}",
        "",
        f"หุ้นใน Watchlist ที่จะประกาศผลภายใน {threshold_days} วัน:",
        "",
    ]
    for days_left, sym, earn_date in upcoming:
        when = "วันนี้!" if days_left == 0 else ("พรุ่งนี้" if days_left == 1 else f"อีก {days_left} วัน")
        lines.append(f"  📊 <b>{sym}</b> — {earn_date.strftime('%d/%m/%Y')} ({when})")
    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━━",
        "⚠️ ผลประกอบการมักทำให้ราคาผันผวนแรง — พิจารณาความเสี่ยงก่อนถือข้ามวันประกาศ",
    ]
    return "\n".join(lines)


def build_universe_earnings_digest_messages(universe_data, watchlist, threshold_days):
    """
    สรุปวันประกาศผลประกอบการของหุ้นทั้ง Universe (ไม่ใช่แค่ watchlist เหมือน
    build_earnings_alert_message) — สำหรับคนที่อยากเล่นตามช่วงประกาศผลโดยตรง
    ไม่จำกัดแค่หุ้นที่ติดตามอยู่แล้ว ตั้งใจให้ threshold กว้างกว่าเวอร์ชัน
    watchlist เพราะเป็นเครื่องมือ "หาโอกาสใหม่" ไม่ใช่แค่เตือนของที่ถืออยู่
    คืนค่าเป็น list เพราะจำนวนหุ้นทั้ง universe เยอะกว่ามาก (~1000+ ตัว) อาจมี
    หลายสิบตัวที่เข้าเงื่อนไขพร้อมกัน ต้องแบ่งหน้าเหมือนรายงานอื่นๆ
    """
    today    = now_utc().date()
    wl_syms  = {s.get("symbol", "").upper() for s in watchlist}
    upcoming = []
    for entry in universe_data.get("universe", []):
        if isinstance(entry, str):
            continue  # entry แบบ string เปล่าไม่เคยมี next_earnings_date อยู่แล้ว
        sym      = entry.get("symbol", "")
        earn_str = entry.get("next_earnings_date")
        if not sym or not earn_str:
            continue
        try:
            earn_date = datetime.fromisoformat(earn_str).date()
        except (ValueError, TypeError):
            continue
        days_left = (earn_date - today).days
        if 0 <= days_left <= threshold_days:
            in_wl = sym.upper() in wl_syms
            upcoming.append((days_left, sym, earn_date, in_wl))

    if not upcoming:
        return []

    upcoming.sort(key=lambda x: (x[0], x[1]))

    header = (
        "📅 <b>ปฏิทินประกาศผลประกอบการ — Universe</b>\n"
        f"🕐 {now_bkk_str()}\n\n"
        f"หุ้นทั้ง Universe ที่จะประกาศผลภายใน {threshold_days} วัน: {len(upcoming)} ตัว\n"
        "(⭐ = อยู่ใน Watchlist อยู่แล้ว)"
    )

    blocks = []
    for days_left, sym, earn_date, in_wl in upcoming:
        when = "วันนี้!" if days_left == 0 else ("พรุ่งนี้" if days_left == 1 else f"อีก {days_left} วัน")
        star = "⭐ " if in_wl else "  "
        blocks.append(f"{star}📊 <b>{sym}</b> — {earn_date.strftime('%d/%m/%Y')} ({when})")

    footer = (
        "\n\n━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "⚠️ ผลประกอบการมักทำให้ราคาผันผวนแรง — ตัวที่ยังไม่อยู่ใน Watchlist "
        "จะไม่มี BUY/SELL signal อัตโนมัติให้ ต้องติดตาม/เข้าเองถ้าสนใจเล่นรอบนี้"
    )

    # ── แบ่งหน้าถ้ายาวเกิน budget (เหมือนรายงานอื่นๆ) ──
    messages = []
    current_parts = [header]
    current_len   = len(header)
    for block in blocks:
        if current_len + len(block) + 1 > TELEGRAM_MSG_BUDGET:
            messages.append("\n".join(current_parts))
            current_parts = [block]
            current_len   = len(block)
        else:
            current_parts.append(block)
            current_len += len(block) + 1
    if current_parts:
        messages.append("\n".join(current_parts))

    messages[-1] += footer

    total = len(messages)
    if total > 1:
        for i in range(total):
            messages[i] += f"\n\n📄 หน้า {i + 1}/{total}"

    return messages


def build_trailing_stop_digest_message(log, state, today_str):
    """
    สรุปการเลื่อน Trailing Stop ของวันนี้ทั้งหมด (ไม่ว่าจะเคยแจ้งเตือน
    real-time ไปแล้วหรือไม่ — ดึงจาก log entry type="trailing_stop_moved"
    ที่บันทึกไว้ทุกครั้งที่มีการเลื่อน) กลุ่มตามหุ้น แสดงว่าวันนี้เลื่อนไปกี่ครั้ง
    และเลื่อนขึ้นมาสุทธิเท่าไหร่ (จากระดับแรกสุดของวันไปจนถึงระดับล่าสุด)
    คืนค่า None ถ้าวันนี้ไม่มีการเลื่อนเลยสักครั้ง
    """
    today_moves = [e for e in log
                   if e.get("type") == "trailing_stop_moved"
                   and (e.get("timestamp") or "").startswith(today_str)]
    if not today_moves:
        return None

    by_symbol = {}
    for e in today_moves:
        by_symbol.setdefault(e["symbol"], []).append(e)

    lines = [
        "📈 <b>สรุป Trailing Stop วันนี้</b>",
        f"🕐 {now_bkk_str()}",
        "",
        f"หุ้นที่มีการเลื่อน Stop ขึ้นวันนี้: {len(by_symbol)} ตัว",
        "",
    ]
    for sym, moves in sorted(by_symbol.items(), key=lambda kv: -len(kv[1])):
        moves.sort(key=lambda e: e["timestamp"])
        first_stop = moves[0]["value"]
        last_stop  = moves[-1]["value"]
        net_move_pct = (last_stop - first_stop) / first_stop * 100 if first_stop else 0
        cur_price = moves[-1].get("price")
        lines.append(
            f"  📊 <b>{sym}</b> — เลื่อน {len(moves)} ครั้ง  "
            f"${first_stop:.4f} → ${last_stop:.4f}  ({net_move_pct:+.2f}%)"
            + (f"  •  ราคาปัจจุบัน ${cur_price:.4f}" if cur_price else "")
        )
    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━━",
        "ℹ️ Stop เลื่อนขึ้นเพื่อล็อกกำไรที่มีอยู่ไว้บางส่วน ไม่ต้องทำอะไรเพิ่ม",
    ]
    return "\n".join(lines)


def _uni_update_entry(universe_data, symbol, **fields):
    """merge field เข้า entry เดิม ไม่ทับทั้ง object — เหมือน update_universe_entry()
    ใน daily_screener.py/manual_check.py ทุกประการ (คนละไฟล์แต่ต้อง behavior
    ตรงกัน กันข้อมูลที่ scan ไว้ก่อนหน้าหายเวลามาจากอีก process หนึ่งเขียนทับ)

    หมายเหตุ (หลัง structural fix universe.json): ฟังก์ชันนี้ไม่ถูกเรียกใช้จาก
    ที่ไหนใน alert_engine.py แล้ว (sync_universe_tech() เปลี่ยนไปเขียนลง
    _patch_update_entry()/universe_live_patch.json แทน) เหลือไว้เผื่อมีจุดอื่น
    ในอนาคตต้องแก้ universe.json ตรงๆ จริงๆ (ไม่ใช่ tech-sync data)"""
    sym_u = symbol.upper()
    for i, e in enumerate(universe_data.get("universe", [])):
        s = (e if isinstance(e, str) else e.get("symbol", "")).upper()
        if s != sym_u:
            continue
        if isinstance(e, str):
            base = {"symbol": symbol.upper(), "name": symbol.upper(), "added_at": now_str()}
            base.update(fields)
            universe_data["universe"][i] = base
        else:
            e.update(fields)
        return True
    return False


def _uni_eval_gate(entry, price, adr_pct, avg_volume, rsi, vol_ratio, above_ema50, cfg):
    """ตรรกะเดียวกับ eval_gate() ใน manual_check.py / run_gates() ใน
    daily_screener.py / uniEvalGate() ใน dashboard_pro.html — คัดลอกมาแบบย่อ
    เพื่อไม่ต้อง import ข้ามไฟล์ (alert_engine.py ต้องรันแบบ standalone เร็วๆ
    ทุก 5 นาที ไม่อยากผูก dependency เพิ่ม)"""
    halal_status = (entry or {}).get("halal_status") or "UNKNOWN"
    purify       = (entry or {}).get("purify_pct")

    if halal_status != "HALAL":
        return "halal"
    if purify is not None and purify > cfg["max_purify_pct"]:
        return "purify"
    if price is None or adr_pct is None or avg_volume is None:
        return "no_data"
    if price < cfg["min_price"] or avg_volume < cfg["min_avg_volume"]:
        return "liquidity"
    if adr_pct < cfg["min_adr_pct"]:
        return "adr"
    if cfg["require_above_ema50"] and not above_ema50:
        return "trend"
    if not (cfg["rsi_min"] <= rsi <= cfg["rsi_max"]) or vol_ratio < cfg["volume_ratio_min"]:
        return "momentum"
    return "PASSED"


def _patch_update_entry(patch_data, symbol, **fields):
    """เขียน field ลง universe_live_patch.json (key = symbol) แทนที่จะเขียนลง
    universe.json ตรงๆ — โครงสร้าง flat dict {"SYMBOL": {field: value, ...}}
    ไม่มี list/index ให้ conflict เวลามีสอง process เขียนพร้อมกัน (แค่ key
    คนละตัวกันก็ merge กันได้เองไม่มีปัญหา ต่างจาก universe.json ที่เป็น list
    ต้องหา index ก่อนแก้ ถ้าสอง process หา/แก้ list พร้อมกันคือช่องให้ conflict)"""
    fields["last_scanned"] = fields.get("last_scanned", now_str())
    patch_data.setdefault(symbol.upper(), {}).update(fields)


def sync_universe_tech(universe_data, patch_data, uni_cfg, symbol, quote):
    """คำนวณ last_price/last_adr_pct/last_rsi/last_vol_ratio/last_above_ema50/
    last_gate/last_scanned ของ symbol นี้ แล้วเขียนลง patch_data (ในหน่วยความจำ
    — ตัวเรียกต้อง save_json(PATCH_PATH, patch_data) เองตอนจบ main())

    Structural fix: เดิมฟังก์ชันนี้เขียนลง universe_data (= universe.json) ตรงๆ
    ทำให้ชนกับ daily_screener.py ที่เขียนไฟล์เดียวกันพร้อมกันได้ (race condition
    ต้องพึ่ง retry+rebase ประคองอาการ) ตอนนี้เขียนลง universe_live_patch.json
    แยกไฟล์แทน แล้วให้ daily_screener.py เป็นคน merge เข้า universe.json ตอน
    startup รอบถัดไป — universe_data ยังใช้อ่านอย่างเดียว (หา entry เดิม/gate)
    ไม่เขียนอะไรกลับเข้าไปอีกแล้ว

    ตั้งใจไม่ throw ออกไปนอกฟังก์ชันนี้เลย — ถ้า sync พลาดของตัวใดตัวหนึ่ง
    (เช่น history ว่าง/network แว้บ) ให้ log แล้วข้าม ไม่ทำให้ alert-engine
    ทั้ง run ล้มเพราะ sync ซึ่งเป็นแค่ "bonus feature" ไม่ใช่งานหลัก (งานหลัก
    คือเช็ค alert ยิง Telegram)"""
    if not _uni_symbol_exists(universe_data, symbol):
        # symbol อยู่ใน watchlist แต่ไม่เคยผ่าน screener มาก่อนเลย (เพิ่มเอง
        # ผ่าน "เพิ่มหุ้น" โดยไม่ผ่าน universe) — ข้าม ไม่สร้าง entry ใหม่ที่นี่
        # เพราะไม่รู้ halal_status/purify_pct จริง จะทำให้ gate เพี้ยน
        return

    try:
        hist = fetch_history(symbol, period="90d", interval="1d")
        if hist is None or hist.empty:
            _patch_update_entry(patch_data, symbol, last_gate="no_data")
            return

        hist = hist.dropna(subset=["Close", "High", "Low"])
        if len(hist) < 20:
            _patch_update_entry(patch_data, symbol, last_gate="no_data")
            return

        closes = hist["Close"].tolist()
        highs  = hist["High"].tolist()
        lows   = hist["Low"].tolist()

        price   = quote["price"]
        adr_pct = _calc_adr(highs, lows, 20)

        ema50_list  = _calc_ema(closes, 50)
        ema50       = next((v for v in reversed(ema50_list) if v is not None), None)
        above_ema50 = (price > ema50) if ema50 else False

        rsi_list = _calc_rsi(closes, 14)
        rsi      = next((v for v in reversed(rsi_list) if v is not None), 50.0)

        avg_volume = quote.get("avg_volume") or 0
        today_vol  = quote.get("volume") or 0
        vol_ratio  = (today_vol / avg_volume) if avg_volume > 0 else 1.0
        # change_pct มีอยู่แล้วใน quote (คำนวณตอน fetch_quote() ไม่ต้องยิง
        # yfinance เพิ่ม) — ใช้ต่อเพื่อเขียนกลับ last_change_pct/last_dollar_volume
        # ทำให้ Sector Flow ใน dashboard อัปเดตทุกครั้งที่ alert-engine รัน
        # (ถี่กว่า daily-screener มาก) แทนที่จะรอ scan รอบใหญ่วันละ 1-2 ครั้ง
        change_pct = quote.get("change_pct") or 0.0

        entry = _uni_find_entry(universe_data, symbol)
        gate  = _uni_eval_gate(entry, price, adr_pct, avg_volume, rsi, vol_ratio, above_ema50, uni_cfg)

        _patch_update_entry(
            patch_data, symbol,
            last_price       = _clean_num(round(price, 4)),
            last_adr_pct     = _clean_num(round(adr_pct, 2)),
            last_rsi         = _clean_num(round(rsi, 1)),
            last_vol_ratio   = _clean_num(round(vol_ratio, 2)),
            last_above_ema50 = bool(above_ema50),
            last_gate        = gate,
            last_change_pct     = _clean_num(round(change_pct, 2)),
            last_dollar_volume   = _clean_num(round(price * today_vol, 2)),
        )
    except Exception as e:
        print(f"  [{symbol}] universe_live_patch.json sync error (ข้าม ไม่กระทบ alert check): {e}")


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
    pct           = quote["change_pct"]
    direction     = alert.get("direction", "down")
    base_threshold = alert.get("threshold_pct", 5.0)

    # Dynamic threshold: max(base, 1.5 × ADR) — กัน false positive บนหุ้น high-ADR
    # หุ้น ADR 8%: threshold จริง = max(5%, 12%) = 12%
    # หุ้น ADR 5%: threshold จริง = max(5%, 7.5%) = 7.5%
    # ถ้า adr_pct = 0 (ดึงไม่ได้) → fallback ใช้ base_threshold เดิม
    adr_pct = quote.get("adr_pct", 0.0)
    if adr_pct >= 1.0:  # มีค่า ADR จริง
        dynamic_threshold = max(base_threshold, 1.5 * adr_pct)
    else:
        dynamic_threshold = base_threshold

    if direction == "down" and pct <= -dynamic_threshold:
        return True, pct, dynamic_threshold
    if direction == "up"   and pct >= dynamic_threshold:
        return True, pct, dynamic_threshold
    return False, pct, dynamic_threshold


# ── Tier 1: Support / Resistance ─────────────────────────────────────────────
def check_support_resistance(alert, quote, symbol=None, dynamic_stop=None):
    price     = quote["price"]
    level     = alert.get("level", 0)
    direction = alert.get("direction", "break_below")
    # FIX: ก่อนหน้านี้ level ต้องตั้งค่าเองใน watchlist.json เท่านั้น (ปกติเป็น 0
    # ตลอด เพราะไม่มีจุดไหนในโค้ดเขียนค่ากลับไปที่ watchlist.json เลย) ทำให้
    # ตัวเลข "🛑 Stop: $X" ที่โชว์ในข้อความ BUY เป็นแค่คำแนะนำให้ผู้ใช้ไปตั้งเอง
    # ไม่เคยเป็น auto-sell จริง — บางหุ้นเลยถือขาดทุนได้นานเป็นเดือนโดยไม่มีอะไร
    # มาห้าม (มีแค่ Death Cross / percent_change ที่ยังทำงานอยู่จริง)
    # แก้โดยถ้า level ไม่ได้ตั้งไว้ (0) แต่มี dynamic_stop จาก open_stop ที่
    # คำนวณไว้ตอน BUY (เก็บใน state.json) ให้ใช้ค่านั้นแทนโดยอัตโนมัติ — ถ้า
    # ผู้ใช้ตั้ง level เองไว้ใน watchlist.json ค่านั้นยังชนะเสมอ (ไม่ทับ)
    if level <= 0 and dynamic_stop:
        level     = dynamic_stop
        direction = "break_below"  # open_stop เป็น stop-loss เสมอ ทิศทางเดียว
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
#  CONVICTION GATE — ด่านสุดท้ายก่อนปล่อย BUY (ทุก alert type ต้องผ่าน)
#
#  แทนที่ Layer 2+3 เดิม (Volatility Gate + AND Signal Gate) ที่ถูกถอดออกไป
#  ตอนเปลี่ยนเป็น Tiered System — ออกแบบให้ "เร็วแต่ไม่มั่ว":
#  ไม่บังคับ AND ทุกตัวเหมือนเดิม (ช้า) แต่ให้คะแนน 4 มิติ แล้วต้องผ่าน
#  อย่างน้อย 3/4 ถึงปล่อยสัญญาณ — ใช้ history ที่ต้องดึงอยู่แล้วจาก signal
#  เดิม ไม่เพิ่ม API call ใหม่ (ยกเว้นกรณี signal เดิมไม่ได้ดึง history มา)
# ══════════════════════════════════════════════════════════════════════════════

def conviction_gate(symbol, quote, alert_history_cache=None):
    """
    ให้คะแนนหลายมิติจากข้อมูลราคาล่าสุด แล้วตัดสินว่าควรปล่อย BUY หรือไม่
    Returns: (passed: bool, score: int, detail: dict)

    มิติที่เช็ก:
      1. Trend      — ราคา > EMA21 (1d)               กัน buy ขาลง
      2. Mom        — RSI(14) อยู่ระหว่าง 35-75         กัน buy จุด exhaustion
      3. Volume     — Volume วันนี้ >= 1.1x ค่าเฉลี่ย    กัน buy ตอนไม่มีคนเล่น
      4. Vol%       — ATR% ไม่ผิดปกติเกิน 2.5x ของปกติ   กัน chase ตอน volatility พุ่ง
      5. Long Trend — ราคา > EMA200 (Daily)             กัน buy สวนเทรนด์ใหญ่
                       (ข้ามมิตินี้ถ้าหุ้นมีประวัติเทรดไม่พอ เช่นเพิ่ง IPO — ไม่ลงโทษ
                       เพราะข้อมูลขาด ไม่ใช่เพราะเทรนด์จริงไม่ดี — ใช้ EMA200 แทน
                       EMA800 เพราะ EMA800 ต้องการข้อมูลย้อนหลัง ~3.2 ปี ซึ่งหุ้น
                       GROWTH/เพิ่ง IPO ในระบบนี้จำนวนมากไม่มีประวัติยาวขนาดนั้น)

    ก่อนถึง 5 มิติด้านบน มี HARD GATE แยกต่างหาก (ไม่ใช่ soft-scoring):
      0. ADR Extreme Ceiling — ADR ต้องไม่เกิน MAX_ADR_PCT_HARD (veto เฉพาะ
                       เคสสุดโต่งจริงๆ) — ADR ระดับปกติของหุ้น momentum/small-cap
                       ในระบบนี้ (median ~8%, 92% ของ watchlist เกิน 6%) ไม่ควร
                       ถูกกันไม่ให้ซื้อเลย ความเสี่ยงจาก ADR สูงจัดการด้วยการ
                       "ลดขนาด position" แทน (ดู calc_position_size()) ไม่ใช่ห้าม
                       เข้าไม้ตั้งแต่ต้น — เดิมเคย hard-block ที่ 6% แล้วพบว่ากัน
                       หุ้นเกือบทั้ง watchlist ออกไปโดยไม่ตั้งใจ

    Threshold ปรับตามจำนวนมิติที่เช็กได้จริงในรอบนั้น (4 หรือ 5) — ต้องผ่าน
    อย่างน้อย "ทั้งหมด - 1" มิติเสมอ (3/4 เดิม หรือ 4/5 ถ้ามี EMA200 ด้วย)
    """
    hist = alert_history_cache
    if hist is None:
        hist = fetch_history(symbol, period="90d", interval="1d")

    if hist is None or len(hist) < 25:
        # ข้อมูลไม่พอให้เช็ก — ปล่อยผ่านแบบ neutral (ไม่ block เพราะข้อมูลขาด)
        return True, 4, {"note": "ข้อมูลไม่พอสำหรับ conviction check — ปล่อยผ่าน", "total_dims": 4}

    closes  = list(hist["Close"].astype(float))
    highs   = list(hist["High"].astype(float))
    lows    = list(hist["Low"].astype(float))
    volumes = list(hist["Volume"].astype(float))
    price   = quote["price"]

    detail = {}
    score  = 0

    # ── HARD GATE: ADR สุดโต่งจริงๆ เท่านั้นที่ veto (>MAX_ADR_PCT_HARD) —
    # ระดับ "สูงกว่าปกติ" (6-20%) ปล่อยผ่านตามปกติ แล้วไปลดขนาด position แทน
    # ที่ calc_position_size() ดู comment เต็มที่ MAX_ADR_PCT_HARD ด้านบนไฟล์
    adr_pct_now = _calc_adr(highs, lows, 20)
    if adr_pct_now > MAX_ADR_PCT_HARD:
        detail["adr_gate"] = {"pass": False, "adr_pct": round(adr_pct_now, 2), "max_allowed": MAX_ADR_PCT_HARD}
        detail["total_dims"] = 4
        detail["veto_reason"] = f"ADR {adr_pct_now:.1f}% เกิน {MAX_ADR_PCT_HARD}% (สุดโต่งเกินจะรับความเสี่ยงได้แม้ลด size แล้ว)"
        return False, 0, detail
    detail["adr_gate"] = {"pass": True, "adr_pct": round(adr_pct_now, 2), "max_allowed": MAX_ADR_PCT_HARD}

    # ── 1. Trend: ราคา > EMA21 ────────────────────────────────────
    ema21_list = _calc_ema(closes, 21)
    ema21      = next((v for v in reversed(ema21_list) if v is not None), None)
    trend_pass = (ema21 is not None) and (price > ema21)
    detail["trend"] = {"pass": trend_pass, "price": round(price, 2),
                       "ema21": round(ema21, 2) if ema21 else None}
    if trend_pass:
        score += 1

    # ── 2. Momentum: RSI ไม่สุดโต่ง ──────────────────────────────
    rsi_list = _calc_rsi(closes, 14)
    rsi      = next((v for v in reversed(rsi_list) if v is not None), 50.0)
    mom_pass = 35 <= rsi <= 75
    detail["momentum"] = {"pass": mom_pass, "rsi": round(rsi, 1)}
    if mom_pass:
        score += 1

    # ── 3. Volume: วันนี้ >= 1.1x ค่าเฉลี่ย ──────────────────────
    avg_vol   = sum(volumes[-21:-1]) / 20 if len(volumes) >= 21 else (volumes[-1] or 1)
    today_vol = quote.get("volume") or volumes[-1]
    vol_ratio = today_vol / avg_vol if avg_vol > 0 else 1.0
    vol_pass  = vol_ratio >= 1.1
    detail["volume"] = {"pass": vol_pass, "ratio": round(vol_ratio, 2)}
    if vol_pass:
        score += 1

    # ── 4. Volatility sanity: ATR วันนี้ไม่ผิดปกติเกิน 2.5x ATR เฉลี่ย ──
    atr_now = _calc_atr(highs, lows, closes, 14)
    # ATR baseline: คำนวณจากช่วงก่อนหน้า (offset 5 วัน) เทียบ relative spike
    atr_base = _calc_atr(highs[:-5], lows[:-5], closes[:-5], 14) if len(closes) >= 30 else atr_now
    atr_spike = (atr_now / atr_base) if (atr_base and atr_base > 0) else 1.0
    vola_pass = atr_spike <= 2.5
    detail["volatility"] = {"pass": vola_pass, "atr_spike_ratio": round(atr_spike, 2)}
    if vola_pass:
        score += 1

    # ── 5. Long Trend: ราคา > EMA200 (Daily) ────────────────────────
    # ต้อง fetch history แยกยาวกว่าเดิม (90d ไม่พอ) — เรียกเฉพาะตอนใกล้จะยิง
    # BUY จริงเท่านั้น (conviction_gate ไม่ได้ถูกเรียกทุกหุ้นทุกรอบ) ไม่กระทบ
    # ภาระ API หนักเกินไป
    total_dims  = 4
    long_hist   = fetch_history(symbol, period="2y", interval="1d")
    if long_hist is not None and len(long_hist) >= 210:
        long_closes = list(long_hist["Close"].astype(float))
        ema200_list = _calc_ema(long_closes, 200)
        ema200      = next((v for v in reversed(ema200_list) if v is not None), None)
        long_trend_pass = (ema200 is not None) and (price > ema200)
        detail["long_trend"] = {"pass": long_trend_pass, "price": round(price, 2),
                                 "ema200": round(ema200, 2) if ema200 else None,
                                 "bars_available": len(long_closes)}
        if long_trend_pass:
            score += 1
        total_dims = 5
    else:
        # ข้อมูลไม่พอ (หุ้นเพิ่ง IPO/ประวัติสั้นกว่า ~210 วันเทรด) — ข้ามมิตินี้
        # ไปเฉยๆ ไม่ให้คะแนนและไม่หักคะแนน แค่ลดตัวหารกลับไปเป็น 4 มิติเหมือนเดิม
        bars = len(long_hist) if long_hist is not None else 0
        detail["long_trend"] = {"pass": None, "note": f"ข้อมูลไม่พอ ({bars} วัน < 210) — ข้ามมิตินี้"}

    detail["total_dims"] = total_dims
    # อัปเดต: บังคับให้มิติ Volume ต้องผ่านเสมอ ไม่ใช่แค่มิติใดมิติหนึ่งจาก
    # "ทั้งหมด - 1" — วิเคราะห์ trade log ย้อนหลังพบว่าสัญญาณที่ Volume❌ (คนไม่
    # ค่อยเล่นตอนเข้า) มีแนวโน้มราคานิ่งแล้วเด้งกลับก่อนถึง target บ่อยกว่า
    # มิติอื่น เดิม gate ปล่อยให้ Volume เป็นมิติเดียวที่ fail ได้โดยยังผ่าน
    # เกณฑ์ 3/4 — ตอนนี้ต้องผ่านทั้ง Volume และคะแนนรวมตามเกณฑ์เดิม
    passed = detail["volume"]["pass"] and score >= max(3, total_dims - 1)
    return passed, score, detail


# ══════════════════════════════════════════════════════════════════════════════
#  POSITION SIZE CALCULATOR  (account_size default = 100 USD)
# ══════════════════════════════════════════════════════════════════════════════

# ── R:R / Kelly tuning constants ────────────────────────────────────────────
# วิเคราะห์ trade log ย้อนหลัง (47 เทรดที่ปิดจริง ณ 4 ส.ค. 69) พบว่า stop
# กว้างสุด 15% (ATR*2) ชนกับ target คงที่ 8% ทำให้แพ้เฉลี่ย -15.9% > ชนะเฉลี่ย
# +11.1% (R:R เฉลี่ย <1) ทั้งที่ win rate 29.8% ต้องการ R:R อย่างน้อย ~1.4:1
# ถึงจะเสมอทุน — คุมสองจุดนี้ใหม่:
#   1) ลด ATR multiplier 2x → 1.5x และ cap 15% → 10% (แพ้แต่ละไม้เจ็บน้อยลง)
#   2) บังคับ target ให้ห่างจาก stop อย่างน้อย MIN_RR เท่าเสมอ โดยใช้ target_pct
#      ที่ตั้งไว้ใน watchlist (ปกติ 8%) เป็น "พื้น" ไม่ใช่ค่าตายตัว — ถ้า stop
#      กว้างกว่านั้นจน R:R ต่ำกว่า MIN_RR จะขยาย target ขึ้นให้พอแทน
ATR_STOP_MULTIPLIER = 1.5
ATR_STOP_CAP_PCT     = 10.0
MIN_RR_RATIO         = 1.5
# วิเคราะห์เทรดที่ปิดหลัง deploy R:R fix (19 ส.ค. 69) พบว่าเทรดที่แพ้หนักสุด
# ทั้งหมด (BLLN -34%, VPG -31%, IOVA -29%, BLZE -23%) ล้วนเป็นหุ้นที่ ADR
# (ค่าเฉลี่ยการแกว่งตัวรายวัน) สูง 6.5-9.75% อยู่แล้วเป็นปกติ — เมื่อ ADR
# ปกติสูงใกล้เคียงหรือเกิน stop cap (10%) วันแย่วันเดียว/ช่องว่างราคาข้าม
# คืน (gap) ก็ทะลุ stop ไปได้ไกลกว่าที่ตั้งใจมาก เพราะระบบนี้ยิงแจ้งเตือนให้
# คนกดขายเอง ไม่ใช่ auto-trade ทันที (มี delay จากรอบเช็ค ~15 นาที + เวลาที่
# คนเห็นข้อความแล้วไปกดขายจริง) จึงกันได้แค่ "ไม่เข้าไม้ตั้งแต่ต้น" กับหุ้น
# ที่แกว่งแรงขนาดนี้ ไม่ใช่ไปไล่ตัดที่ stop-loss เพราะ stop-loss เจอราคาที่
# ทะลุไปแล้วเท่านั้น ตัดไม่ทันจริงๆ
#
# ── รอบแรกเคยตั้ง hard gate ที่ 6% แล้วพบว่าพลาด: 92% ของ watchlist จริง
# (median ADR 8.21%) มี ADR เกิน 6% เป็นปกติ เพราะระบบนี้ตั้งใจคัดหุ้น
# momentum/small-cap ซึ่งแกว่งแรงเป็นธรรมชาติของมันอยู่แล้ว hard gate ที่ 6%
# เท่ากับปิดโอกาสซื้อเกือบทั้งหมด ไม่ใช่ทางแก้ที่ถูกต้อง ──
#
# ── รอบสอง: เปลี่ยนจาก hard gate เป็น position-size scaling แต่ตั้ง
# ADR_SIZE_REF_PCT ไว้ที่ 6.0 เหมือนเดิม ก็ยังพลาดอยู่ดี — daily_screener.py
# เองมี min_adr_pct=8.0 เป็น "เกณฑ์ขั้นต่ำที่จะเข้า watchlist ได้เลย" (ตั้งใจ
# คัดหุ้นแกว่งแรงตั้งแต่ต้นทาง) ตั้ง reference ไว้ต่ำกว่าเกณฑ์เข้า watchdog
# เอง แปลว่าแทบทุกตัวที่เข้ามาได้จะโดนลด size ตั้งแต่วันแรก เช็คจริงมีแค่
# 6/77 ตัว (8%) เท่านั้นที่ได้ budget เต็ม — ไม่ตรงเจตนา
#
# นอกจากนี้ยังพบว่า ADR ของหุ้นที่พังหนักสุด 4 ตัว (BLLN 6.56%, VPG 8.49%,
# IOVA 8.02%, BLZE 9.75%) อยู่ใกล้ค่ากลาง (median 8.21%) ของทั้ง watchlist
# พอดี ไม่ใช่ตัวที่ ADR ผิดปกติ/สุดโต่งเลย — แปลว่า ADR อย่างเดียวแยกไม่ออก
# จริงๆ ว่าตัวไหนจะ "ปลอดภัย" ตัวไหนจะ "ระเบิด" ในหมู่หุ้นกลุ่มนี้ (ทุกตัว
# แกว่งแรงพอๆ กันหมดโดยธรรมชาติของกลยุทธ์) ไม่มี threshold ไหนที่ทั้งกัน
# เคสแบบนี้ได้ครบ "และ" ปล่อยให้ตัวส่วนใหญ่ได้ไม้เต็มไปพร้อมกัน — เลือกทาง
# ตั้ง reference ให้สอดคล้องกับปรัชญาการคัดหุ้นของ screener เอง (ส่วนใหญ่
# ได้ไม้เต็ม เน้นกันเฉพาะ tail ที่ ADR สูงกว่าปกติของ universe นี้จริงๆ)
# มากกว่าจะพยายามกันเคสเฉพาะจุดที่ ADR แยกไม่ออกอยู่ดี
#
# แนวทางสุดท้าย: แยกเป็น 2 ชั้น
#   1) MAX_ADR_PCT_HARD — veto เฉพาะเคสสุดโต่งจริงๆ (>p99 ของ universe จริง
#      ซึ่งมักเป็น data error/penny stock ผิดปกติ ไม่ใช่ momentum play จริง)
#   2) ADR_SIZE_REF_PCT — ตั้งไว้เหนือ min_adr_pct ของ screener (8.0) พอ
#      สมควร (~83% ของ watchlist ปัจจุบันได้ไม้เต็ม, เหลือ tail ที่แกว่ง
#      แรงกว่าชาวบ้านจริงๆ ~17% ที่โดนลด) ต่ำกว่านี้ไม่ลด size สูงกว่านี้ลด
#      ตามสัดส่วน (ดู calc_position_size()) — ยอมรับว่าจะไม่ช่วยกันเคสแบบ
#      BLLN/VPG/IOVA/BLZE เพิ่มเติม (อยู่ในช่วง "ปกติ" ของ universe นี้พอดี)
#      เพราะ R:R gate + hard stop cap ที่ทำไปรอบก่อนคือด่านหลักที่ดูแลเคส
#      แบบนั้นอยู่แล้ว ส่วนนี้เก็บไว้กันเฉพาะ tail ที่สุดโต่งจริงๆ เท่านั้น
MAX_ADR_PCT_HARD      = 25.0
ADR_SIZE_REF_PCT      = 11.0
#
# ── รอบสาม: ADR (ไม่ว่าตั้ง threshold เท่าไหร่) วัด "การแกว่งตัวระหว่างวัน"
# ไม่ใช่ "การกระโดดข้ามคืน" ซึ่งเป็นกลไกจริงที่ทำให้ BLLN/VPG/IOVA/BLZE พัง
# (ถือแค่ 0.03-0.99 วันก่อนโดน stop — สั้นขนาดนี้บ่งชี้ชัดว่าเป็น gap ข้าม
# คืน ไม่ใช่แกว่งในวันเดียวกัน) เพิ่มสัญญาณที่ตรงประเด็นกว่า: เช็คประวัติ
# gap ข้ามคืนจริงของหุ้นนั้นย้อนหลัง (ดู _calc_gap_stats()) แล้วลด size
# เพิ่มเติมถ้าหุ้นเคยกระโดดข้ามคืนแรงมาก่อน (สะท้อนพฤติกรรมเฉพาะตัวของหุ้น
# นั้น เช่น biotech ที่มีข่าว FDA เป็นระยะ มักกระโดดซ้ำได้อีก) — ใช้ค่า factor
# ที่ "เข้มกว่า" ระหว่าง ADR-based กับ gap-based เสมอ (min ของสองค่า) เพราะ
# ทั้งสองแหล่งความเสี่ยงเป็นอิสระต่อกัน ป้องกันได้ทั้งคู่ไม่ทับซ้อนกัน
GAP_LOOKBACK_DAYS     = 60
GAP_SIZE_REF_PCT      = 15.0   # ถ้าหุ้นไม่เคย gap ข้ามคืนเกิน 15% ในช่วง 60 วัน ไม่ลด
# win rate เฉลี่ยจริงจาก trade log ยุคหลังแก้บั๊ก stop-loss (35.3%) — ใช้เป็น
# ค่า default สำหรับ Kelly แทนการเดา 50/50 (มองโลกในแง่ร้ายกว่าจะปลอดภัยกว่า
# ถ้าจะพลาดควรพลาดไปทาง "แนะนำ size เล็กเกินไป" ไม่ใช่ "ใหญ่เกินไป")
DEFAULT_WIN_RATE = 0.35


def calc_position_size(pos_cfg, symbol, entry_price=None):
    """
    คำนวณ position size จาก budget $100/หุ้น
    รองรับ fractional shares (crypto/ETF) และ whole shares (หุ้นทั่วไป)

    อัปเดต: เพิ่ม R:R gate (บังคับ target ห่างจาก stop อย่างน้อย MIN_RR เท่า)
    และพอร์ต Kelly criterion + คำเตือน R:R มาจาก module_position.py (เดิมมีโค้ด
    ชุดนี้อยู่แล้วแต่ไม่เคยถูกเรียกใช้จริงจากที่นี่ — ทำให้มี R:R warning สอง
    มาตรฐานคนละที่ ตอนนี้รวมเป็นจุดเดียว)

    อัปเดต 2: ลด budget ($) ตามสัดส่วน ADR แทนการห้ามซื้อหุ้น ADR สูงไปเลย
    (ดู comment เต็มที่ ADR_SIZE_REF_PCT ด้านบนไฟล์ — hard gate ที่เคยลองทำ
    บล็อค 92% ของ watchlist โดยไม่ตั้งใจ เพราะ ADR สูงเป็นเรื่องปกติของหุ้น
    momentum ในระบบนี้) หุ้น ADR สูงยังซื้อได้เต็มที่ตามสัญญาณเดิม แค่ขนาด
    ไม้เล็กลงตามความเสี่ยง — ถ้า gap ทะลุ stop จริง ความเสียหายเป็น $ จะเล็ก
    ลงตามสัดส่วนไปด้วย ไม่ใช่เสียเต็ม $100 เหมือนหุ้นเสี่ยงต่ำ

    อัปเดต 3: เพิ่มการลด budget ตามประวัติ "gap ข้ามคืน" จริงของหุ้น (แยก
    จาก ADR) เพราะเคสที่พังหนักสุดถือแค่ต่ำกว่า 1 วัน บ่งชี้ว่าเป็นกลไก gap
    ไม่ใช่แค่แกว่งระหว่างวันซึ่ง ADR วัดไม่ตรง — ดู comment เต็มที่
    GAP_SIZE_REF_PCT ด้านบนไฟล์
    """
    account_base = pos_cfg.get("account_size", 100)   # ← default $100 (ก่อนปรับตาม ADR/gap)
    risk_pct   = pos_cfg.get("risk_pct",    2.0)
    stop_pct   = pos_cfg.get("stop_pct",    None)
    target_pct = pos_cfg.get("target_pct",  None)
    win_rate   = pos_cfg.get("win_rate",    DEFAULT_WIN_RATE)

    if entry_price is None:
        q = fetch_quote(symbol)
        entry_price = q["price"] if q else 0
    if entry_price <= 0:
        return None

    atr_val = None
    adr_pct_val = None
    max_gap_pct = None
    p90_gap_pct = None
    adr_size_factor = 1.0
    gap_size_factor = 1.0
    if stop_pct is None:
        hist = fetch_history(symbol, period="90d", interval="1d")
        if hist is not None and len(hist) >= 16:
            highs  = list(hist["High"].astype(float))
            lows   = list(hist["Low"].astype(float))
            opens  = list(hist["Open"].astype(float))
            closes = list(hist["Close"].astype(float))
            atr_val = _calc_atr(highs, lows, closes, 14)
            if atr_val:
                raw_stop_pct = (atr_val * ATR_STOP_MULTIPLIER / entry_price) * 100
                stop_pct     = min(raw_stop_pct, ATR_STOP_CAP_PCT)
            else:
                stop_pct = 5.0
            # ── ลด budget ตามสัดส่วน ADR (ใช้ highs/lows ชุดเดียวกับ ATR ไม่
            # ต้องยิง fetch_history ซ้ำ) — ADR <= ADR_SIZE_REF_PCT ไม่ลดเลย
            # (factor=1.0) ADR ยิ่งสูงกว่านั้น factor ยิ่งเล็กลงเป็นสัดส่วนผกผัน
            adr_pct_val = _calc_adr(highs, lows, 20)
            if adr_pct_val and adr_pct_val > ADR_SIZE_REF_PCT:
                adr_size_factor = max(0.15, ADR_SIZE_REF_PCT / adr_pct_val)  # ไม่ให้เล็กจนต่ำกว่า 15% ของ budget เดิม
            # ── ลด budget ตามประวัติ gap ข้ามคืนจริง (สัญญาณคนละตัวจาก ADR) ──
            max_gap_pct, p90_gap_pct = _calc_gap_stats(opens, closes, GAP_LOOKBACK_DAYS)
            if max_gap_pct and max_gap_pct > GAP_SIZE_REF_PCT:
                gap_size_factor = max(0.15, GAP_SIZE_REF_PCT / max_gap_pct)
        else:
            stop_pct = 5.0

    # ใช้ค่า factor ที่ "เข้มกว่า" เสมอระหว่าง ADR-based กับ gap-based —
    # ทั้งสองแหล่งความเสี่ยงเป็นอิสระต่อกัน หุ้นอาจ ADR ปกติแต่เคย gap แรง
    # มาก่อนก็ได้ (หรือกลับกัน) ป้องกันได้ครบทั้งสองทาง
    size_factor = min(adr_size_factor, gap_size_factor)
    account = round(account_base * size_factor, 2)

    stop_price  = entry_price * (1 - stop_pct / 100)
    risk_per_sh = entry_price - stop_price

    # คำนวณจำนวนหุ้นจาก budget ที่ปรับตาม ADR แล้ว
    shares_frac = account / entry_price
    shares_int  = math.floor(shares_frac)
    is_frac     = shares_int == 0   # ราคาสูงกว่า budget → ต้อง fractional
    disp_shares = round(shares_frac, 6) if is_frac else shares_int
    pos_value   = round(disp_shares * entry_price, 2)
    pos_pct     = round(pos_value / account_base * 100, 1) if account_base > 0 else 0
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
        "account_base":  account_base,
        "adr_pct":       round(adr_pct_val, 2) if adr_pct_val is not None else None,
        "adr_size_factor": round(adr_size_factor, 3),
        "max_gap_pct":   max_gap_pct,
        "p90_gap_pct":   p90_gap_pct,
        "gap_size_factor": round(gap_size_factor, 3),
        "size_factor":   round(size_factor, 3),
        # บอกว่าตัวไหน "เข้ม" กว่าจนเป็นตัวกำหนด size_factor สุดท้าย เอาไว้
        # โชว์ในข้อความ Telegram ให้คนอ่านเข้าใจว่าทำไม size ถึงเล็กลง
        "size_limited_by": ("gap" if gap_size_factor < adr_size_factor
                             else ("adr" if adr_size_factor < 1.0 else None)),
        "atr":           round(atr_val, 4) if atr_val else None,
    }

    if target_pct:
        # ── บังคับ R:R ขั้นต่ำ ────────────────────────────────────────────
        # target_pct ที่ตั้งไว้ใน watchlist (ปกติ 8%) เป็น "พื้น" เท่านั้น —
        # ถ้า stop_pct ที่คำนวณได้กว้างจนทำให้ R:R < MIN_RR_RATIO จะขยาย
        # target ขึ้นให้ R:R แตะ MIN_RR_RATIO พอดี (ไม่ลด stop เพิ่ม เพราะ
        # stop มาจาก ATR สะท้อนความผันผวนจริงของหุ้นตัวนั้น)
        effective_target_pct = max(target_pct, round(stop_pct * MIN_RR_RATIO, 2))
        tp = entry_price * (1 + effective_target_pct / 100)
        rr = effective_target_pct / stop_pct if stop_pct > 0 else 0
        profit_usd = round(disp_shares * (tp - entry_price), 2)
        result["target"]          = round(tp, 4)
        result["target_pct"]      = effective_target_pct
        result["target_pct_base"] = target_pct   # ค่าดั้งเดิมจาก watchlist เผื่ออยาก debug
        result["rr_ratio"]        = round(rr, 2)
        result["target_usd"]      = profit_usd
        result["rr_adjusted"]     = effective_target_pct > target_pct

        # ── Kelly criterion (informational เท่านั้น — ไม่ได้ใช้ปรับขนาด
        # position จริง เพราะระบบนี้ตั้งใจใช้ budget คงที่ $100/หุ้นเพื่อให้
        # เทียบผลลัพธ์ระหว่างหุ้นได้ตรงไปตรงมา — Kelly% ไว้ดูเป็น sanity
        # check ว่าขนาดที่ควรเสี่ยงจริงๆ ห่างจาก budget คงที่แค่ไหน) ──
        if win_rate > 0 and rr > 0:
            kelly_pct = win_rate - (1 - win_rate) / rr
            kelly_pct = max(0.0, min(kelly_pct, 0.25))  # cap ที่ 25% กัน over-leverage
            result["kelly_pct"]      = round(kelly_pct * 100, 1) if kelly_pct > 0 else 0.0
            result["half_kelly_pct"] = round(kelly_pct * 50, 1) if kelly_pct > 0 else 0.0
        else:
            result["kelly_pct"]      = 0.0
            result["half_kelly_pct"] = 0.0

        warnings = []
        if pos_pct > 20:
            warnings.append("⚠️ Position ใหญ่มาก (>20% พอร์ต) — เสี่ยงสูง")
        if rr < MIN_RR_RATIO:
            # ปกติไม่ควรเกิดขึ้นแล้วเพราะบังคับ R:R ไว้ข้างบน — เหลือไว้เป็น
            # safety net เผื่อมี edge case (เช่น stop_pct <= 0)
            warnings.append(f"⚠️ R:R ต่ำกว่า 1:{MIN_RR_RATIO} — ควรทบทวน")
        if pos_pct > 50:
            warnings.append("🚨 DANGER: ใช้พอร์ตมากกว่า 50% — อันตรายมาก!")
        if not warnings:
            warnings.append("✅ ขนาด position อยู่ในเกณฑ์ปลอดภัย")
        result["warnings"] = warnings

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
    reason_th = {"adr": "ADR สูง", "gap": "เคย gap ข้ามคืนแรง"}.get(pos.get("size_limited_by"), "ความผันผวนสูง")
    budget_line = f"📦 Position (งบ ${pos['account']:.0f}):"
    if pos.get("size_factor", 1.0) < 0.999:
        budget_line = f"📦 Position (งบ ${pos['account']:.0f} — ลดจาก ${pos['account_base']:.0f} เพราะ{reason_th}):"
    lines    = [
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━━",
        budget_line,
        f"  • ซื้อ: <b>{sh_str}</b>",
        f"  • ใช้เงิน: <b>${pos['pos_value']:,.2f}</b>",
        f"  🛑 Stop: <b>${pos['stop']:.4f}</b>  (-{pos['stop_pct']:.1f}%)",
    ]
    if pos.get("adr_pct") is not None and pos.get("size_factor", 1.0) < 0.999:
        detail_bits = []
        if pos.get("adr_size_factor", 1.0) < 0.999:
            detail_bits.append(f"ADR {pos['adr_pct']:.1f}%")
        if pos.get("gap_size_factor", 1.0) < 0.999 and pos.get("max_gap_pct"):
            detail_bits.append(f"เคย gap ข้ามคืนสูงสุด {pos['max_gap_pct']:.1f}% ใน {GAP_LOOKBACK_DAYS} วันหลัง")
        detail_txt = " และ ".join(detail_bits) if detail_bits else "ความผันผวนสูง"
        lines.append(f"  ⚡ {detail_txt} → ลด size เหลือ {pos['size_factor']*100:.0f}% ของ budget เต็ม")
    if pos.get("atr"):
        lines.append(f"  • ATR(14): ${pos['atr']:.4f}")
    if pos.get("target"):
        lines.append(f"  🎯 Target: <b>${pos['target']:.4f}</b>  (+{pos['target_pct']:.1f}%)  R:R=1:{pos['rr_ratio']:.1f}")
        if pos.get("rr_adjusted"):
            lines.append(f"  ℹ️ ขยาย target จาก +{pos['target_pct_base']:.1f}% เพื่อคุม R:R ≥1:{MIN_RR_RATIO}")
        if pos.get("target_usd"):
            lines.append(f"  • กำไรถ้าถึง Target: ~+${pos['target_usd']:.2f}")
    lines.append(f"  ⚠️ เสี่ยงขาดทุน: -${pos['actual_risk']:.2f} ถ้า SL โดน")
    if pos.get("half_kelly_pct") is not None and pos.get("target"):
        if pos["half_kelly_pct"] > 0:
            lines.append(f"  📐 Kelly (win rate {DEFAULT_WIN_RATE*100:.0f}%): แนะนำ ~{pos['half_kelly_pct']:.1f}% พอร์ต (half-Kelly)")
        else:
            lines.append(f"  📐 Kelly (win rate {DEFAULT_WIN_RATE*100:.0f}%): ยังไม่มี positive edge ที่ R:R นี้ — ระวังเป็นพิเศษ")
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
    _emoji, symbol, name, price, pct, arrow, sign, tv = _header(stock, quote, "🛑")

    reason_th = {
        "sl_break":      "🛑 หลุด Stop Loss / แนวรับสำคัญ",
        "death_cross":   "💀 Death Cross — EMA9 ตัดลงใต้ EMA21",
        "pct_drop":      f"📉 ราคาลง {abs(pct):.1f}% วันเดียว",
        "score_bear":    "🔴 Confidence Score ขาลงสูง",
        "take_profit":   "🎯 ถึงเป้าหมายกำไร (Take Profit) ตามแผน",
        "trailing_stop": "📈 หลุด Trailing Stop — ล็อกกำไรที่สะสมมาไว้",
        "stagnant_exit": "⏳ ถือมานานเกินไปโดยราคาไม่ไปไหน — ปิดคืนทุนไปเข้าไม้อื่น",
    }.get(reason, reason)

    # FIX: เดิมทุก reason (ทั้งขาดทุนจริงและกำไรตามแผน) ใช้ header "🛑 SELL
    # SIGNAL" กับ action steps "ขายออกทันที — อย่ารอ / อย่า average down"
    # เหมือนกันหมด — พอเป็นเคส take_profit หรือ trailing_stop ที่จริงๆ คือ
    # ปิดสถานะได้กำไรตามแผน คำเตือนโทนนี้เข้ากับบริบทไม่ได้เลย (เหมือนกำลัง
    # เตือนภัยทั้งที่เป็นข่าวดี) แยกโทนข้อความเป็น 2 แบบตามความหมายจริงแทน
    is_profit_exit = reason in ("take_profit", "trailing_stop")
    is_stagnant_exit = reason == "stagnant_exit"

    if is_profit_exit:
        header_line  = f"✅ <b>ปิดสถานะทำกำไร: {symbol}</b> ({name})"
        action_lines = [
            "📌 <b>สิ่งที่ควรทำ:</b>",
            "  1️⃣ ล็อกกำไรตามแผนเรียบร้อยแล้ว",
            "  2️⃣ ถ้ายังไม่ได้ auto-trade ให้ขายในโบรกเกอร์ตามสัญญาณทันที",
            "  3️⃣ รอ BUY signal ใหม่ก่อน re-entry เหมือนเดิม",
        ]
    elif is_stagnant_exit:
        header_line  = f"⏳ <b>ปิดสถานะ (แช่แข็งนานเกินไป): {symbol}</b> ({name})"
        action_lines = [
            "📌 <b>สิ่งที่ควรทำ:</b>",
            "  1️⃣ ปิดสถานะเพื่อคืนทุนไปหาโอกาสอื่น ไม่ใช่เพราะขาดทุนหนัก",
            "  2️⃣ ถ้ายังไม่ได้ auto-trade ให้ปิดในโบรกเกอร์ตามสัญญาณ",
            "  3️⃣ รอ BUY signal ใหม่ก่อน re-entry เหมือนเดิม",
        ]
    else:
        header_line  = f"🛑 <b>SELL SIGNAL: {symbol}</b> ({name})"
        action_lines = [
            "📌 <b>สิ่งที่ควรทำ:</b>",
            "  1️⃣ ขายออกทันที — อย่ารอ",
            "  2️⃣ อย่า average down",
            "  3️⃣ รอ BUY signal ใหม่ก่อน re-entry",
        ]

    lines = [
        header_line,
        "",
        f"⚡ {reason_th}",
        f"💰 ราคา: <b>${price:.4f}</b>  {arrow} {sign}{pct:.2f}%",
    ]
    if detail:
        lines.append(f"📋 {detail}")
    lines += [
        "",
        *action_lines,
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


def build_daily_summary_messages(watchlist, quotes_cache, universe_data):
    """
    รายงานสรุปประจำวัน: หุ้นขึ้น/ลงแรงสุด + กลุ่มหุ้น (Sector Flow) แบบ
    $-weighted (สูตรเดียวกับ tab Sector Flow บน dashboard_pro.html) + รายชื่อ
    ทั้งหมดพร้อม tag ว่าอยู่ sector ไหนและ sector นั้น %เปลี่ยนแปลงเท่าไหร่
    ต่อท้ายแต่ละหุ้น — คืนค่าเป็น list เพราะเนื้อหายาวขึ้นจากเดิมมาก อาจต้อง
    แบ่งหลายข้อความถ้าเกิน budget ของ Telegram (เหมือน Position Status)
    """
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

    # ── จัดกลุ่มตาม sector (last_sector จาก universe.json ที่ daily_screener.py
    # เขียนไว้) แล้วเฉลี่ย % เปลี่ยนแปลงแบบถ่วงน้ำหนักด้วยมูลค่าซื้อขาย
    # (price × volume) เหมือน renderSectorFlow() บน dashboard เป๊ะ — หุ้นที่
    # ไม่เคยถูก scan จะตกไปกอง "อื่นๆ / ยังไม่จัดกลุ่ม" ──
    sector_of = {}
    groups = {}
    for stock in watchlist:
        sym = stock["symbol"]
        q = quotes_cache.get(sym)
        if not q:
            continue
        entry  = _uni_find_entry(universe_data, sym)
        sector = (entry.get("last_sector") if entry else None) or "อื่นๆ / ยังไม่จัดกลุ่ม"
        if sector == "Unknown":
            sector = "อื่นๆ / ยังไม่จัดกลุ่ม"
        sector_of[sym] = sector
        dv = (q["price"] * q["volume"]) if q.get("volume") else 0
        groups.setdefault(sector, []).append((q["change_pct"], dv))

    sector_avg = {}
    for sector, changes in groups.items():
        weightable = [(c, dv) for c, dv in changes if dv and dv > 0]
        if weightable:
            sum_dv = sum(dv for _, dv in weightable)
            avg = sum(c * dv for c, dv in weightable) / sum_dv
        else:
            avg = sum(c for c, _ in changes) / len(changes)
        up   = len([c for c, _ in changes if c >= 0])
        down = len(changes) - up
        sector_avg[sector] = {"avg": avg, "up": up, "down": down, "n": len(changes)}

    sorted_sectors = sorted(sector_avg.items(), key=lambda kv: -kv[1]["avg"])

    header_lines = [
        "<b>📊 สรุปประจำวัน — Stock Alert Pro v3.1</b>",
        f"🕐 {now_bkk_str()}", "",
        f"ขึ้น: {len(gainers)} ตัว  |  ลง: {len(losers)} ตัว  |  ดูอยู่: {len(watchlist)} ตัว",
        "", "━━━━━━━━━━━━━━━━━━━━━━━━━",
        "<b>🔥 ขึ้นแรงสุด 5 ตัว:</b>",
    ]
    for sym, p, chg in gainers[:5]:
        header_lines.append(f"  📈 <b>{sym}</b> ${p:.2f}  +{chg:.1f}%")
    header_lines += ["", "<b>💧 ลงแรงสุด 5 ตัว:</b>"]
    for sym, p, chg in losers[:5]:
        header_lines.append(f"  📉 <b>{sym}</b> ${p:.2f}  {chg:.1f}%")

    header_lines += ["", "━━━━━━━━━━━━━━━━━━━━━━━━━", "<b>🌐 กลุ่มหุ้น (Sector Flow):</b>"]
    for sector, info in sorted_sectors:
        arr = "📈" if info["avg"] >= 0 else "📉"
        sgn = "+" if info["avg"] >= 0 else ""
        header_lines.append(
            f"  {arr} <b>{sector}</b>  {sgn}{info['avg']:.2f}%  "
            f"({info['n']} ตัว — ขึ้น {info['up']}/ลง {info['down']})"
        )

    header_lines += ["", "━━━━━━━━━━━━━━━━━━━━━━━━━", "<b>📋 รายชื่อทั้งหมด:</b>"]
    header = "\n".join(header_lines)

    blocks = []
    for stock in watchlist:
        sym = stock["symbol"]
        q   = quotes_cache.get(sym)
        if not q:
            blocks.append(f"  • {sym} — ไม่มีข้อมูล")
            continue
        arr    = "📈" if q["change_pct"] >= 0 else "📉"
        sgn    = "+" if q["change_pct"] >= 0 else ""
        sector = sector_of.get(sym, "อื่นๆ / ยังไม่จัดกลุ่ม")
        sec    = sector_avg.get(sector, {"avg": 0.0})
        sec_arr = "📈" if sec["avg"] >= 0 else "📉"
        sec_sgn = "+" if sec["avg"] >= 0 else ""
        blocks.append(
            f"  • <b>{sym}</b> ${q['price']:.2f}  {arr} {sgn}{q['change_pct']:.1f}%  "
            f"<i>[{sector} {sec_arr}{sec_sgn}{sec['avg']:.1f}%]</i>"
        )

    footer = (
        "\n\n━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "💡 กฎสำคัญ: ตั้ง Stop Loss ทุกครั้ง | งบ $100/หุ้น\n"
        f"🤖 Stock Alert Pro v3.1  •  {now_bkk_str()}"
    )

    # ── แบ่งเป็นหลายข้อความถ้ายาวเกิน budget (join ด้วย "\n" เดี่ยว รักษา
    # รูปแบบรายการหุ้นบรรทัดเดียวต่อตัวแบบเดิม ไม่ใช่บล็อกเว้นบรรทัดแบบ
    # Position Status) ──
    messages = []
    current_parts = [header]
    current_len   = len(header)
    for block in blocks:
        if current_len + len(block) + 1 > TELEGRAM_MSG_BUDGET:
            messages.append("\n".join(current_parts))
            current_parts = [block]
            current_len   = len(block)
        else:
            current_parts.append(block)
            current_len += len(block) + 1
    if current_parts:
        messages.append("\n".join(current_parts))

    messages[-1] += footer

    total = len(messages)
    if total > 1:
        for i in range(total):
            messages[i] += f"\n\n📄 หน้า {i + 1}/{total}"

    return messages


# ══════════════════════════════════════════════════════════════════════════════
#  POSITION STATUS REPORT — สรุป P&L ของ position ที่ยังเปิดอยู่ (ยังไม่มี SELL)
#
#  อ่านจาก state[symbol]["open_entry"/"open_time"/"open_stop"/"open_target"/
#  "open_peak"/"open_conviction"] ที่ถูกบันทึกไว้ตอน BUY fire แล้วเทียบกับ
#  ราคาปัจจุบันใน quotes_cache เพื่อสรุปว่าตอนนี้ +/- กี่% และให้คำแนะนำ
#  เชิงกฎ risk-management ทั่วไป (ไม่ใช่คำแนะนำการลงทุนเฉพาะบุคคล)
# ══════════════════════════════════════════════════════════════════════════════

TELEGRAM_MSG_BUDGET = 3500   # เผื่อ buffer จากลิมิตจริง 4096 ตัวอักษรของ Telegram


def _position_next_step(pnl_pct, dist_to_stop_pct, dist_to_target_pct,
                         drawdown_from_peak_pct, days_held):
    """
    ให้คำแนะนำ "ทำอย่างไรต่อ" แบบ rule-based ตามหลัก risk-management ทั่วไป
    เรียงตามความสำคัญ (เช็กเงื่อนไขที่อันตราย/เร่งด่วนที่สุดก่อน)
    """
    if dist_to_stop_pct is not None and dist_to_stop_pct <= 3:
        return "⚠️ ใกล้ชน Stop Loss มาก — เตรียมใจทำตามแผนเดิมถ้าหลุด อย่าย้าย SL หนี"
    if dist_to_target_pct is not None and dist_to_target_pct <= 3:
        return "🎯 ใกล้ถึง Target แล้ว — พิจารณาล็อกกำไรบางส่วน หรือเลื่อน SL ขึ้นมาที่ทุน"
    if pnl_pct > 0 and drawdown_from_peak_pct is not None and drawdown_from_peak_pct >= 5:
        return (f"📉 ราคาลงจากจุดสูงสุด {drawdown_from_peak_pct:.1f}% แล้ว "
                f"— พิจารณาเลื่อน Stop ตามราคาขึ้นมา (trailing stop) เพื่อล็อกกำไรที่มีอยู่")
    if pnl_pct <= -8:
        return "🔴 ขาดทุนหนักใกล้โซนเสี่ยง — ห้าม average down, รอ SL เดิมทำงานตามแผน"
    if days_held is not None and days_held >= 10 and -3 <= pnl_pct <= 3:
        return f"⏳ ถือมา {days_held:.0f} วันแล้วราคายังไม่ไปไหน — ทบทวนว่าเหตุผลตอนเข้าซื้อยังจริงอยู่มั้ย"
    if pnl_pct > 0:
        return "👀 กำไรอยู่ — ถือต่อตามแผน รอ Target หรือเลื่อน SL ตามราคาเป็นระยะ"
    return "👀 ถือต่อตามแผนเดิม รอ Target หรือ Stop ทำงาน — ยังไม่ถึงจุดต้องตัดสินใจ"


def build_position_status_messages(state, watchlist, quotes_cache):
    """
    สร้างรายการข้อความ (list of str) สรุปสถานะ position ที่เปิดอยู่ทั้งหมด
    แบ่งเป็นหลายข้อความถ้ายาวเกิน budget ของ Telegram
    คืนค่า [] ถ้าไม่มี position เปิดอยู่เลย
    """
    watch_syms = {s["symbol"] for s in watchlist}
    rows = []

    for symbol, sym_state in state.items():
        if symbol.startswith("__") or not isinstance(sym_state, dict):
            continue
        entry = sym_state.get("open_entry")
        if not entry:
            continue
        # กันข้อมูลค้าง: ถ้ามี last_sell_at ใหม่กว่า open_time ให้ถือว่าปิดไปแล้ว
        open_time  = sym_state.get("open_time")
        sell_at    = sym_state.get("last_sell_at")
        if open_time and sell_at and minutes_since(sell_at) < minutes_since(open_time):
            continue
        if symbol not in watch_syms:
            continue

        quote = quotes_cache.get(symbol)
        if not quote:
            continue
        current = quote["price"]

        # ── อัปเดต peak (ราคาสูงสุดตั้งแต่เข้าซื้อ) เพื่อคำนวณ drawdown ──
        peak = max(sym_state.get("open_peak", entry) or entry, current)
        state[symbol]["open_peak"] = peak

        pnl_pct        = (current - entry) / entry * 100
        drawdown_pct   = (peak - current) / peak * 100 if peak > 0 else 0
        stop           = sym_state.get("open_stop")
        target         = sym_state.get("open_target")
        dist_to_stop   = ((current - stop) / current * 100) if stop else None
        dist_to_target = ((target - current) / current * 100) if target else None
        days_held      = (minutes_since(open_time) / 1440) if open_time else None
        conviction       = sym_state.get("open_conviction")
        conviction_total = sym_state.get("open_conviction_total", 4)  # เก่าก่อนมี EMA200 = 4 เสมอ

        rows.append({
            "symbol": symbol, "entry": entry, "current": current,
            "pnl_pct": pnl_pct, "drawdown_pct": drawdown_pct,
            "stop": stop, "target": target,
            "dist_to_stop": dist_to_stop, "dist_to_target": dist_to_target,
            "days_held": days_held, "conviction": conviction,
            "conviction_total": conviction_total,
        })

    if not rows:
        return []

    # เรียงจากขาดทุนมากสุดไปกำไรมากสุด — ให้ตัวที่ต้องระวังก่อนขึ้นก่อน
    rows.sort(key=lambda r: r["pnl_pct"])

    winners = sum(1 for r in rows if r["pnl_pct"] >= 0)
    losers  = len(rows) - winners
    avg_pnl = sum(r["pnl_pct"] for r in rows) / len(rows)

    header = (
        "<b>📦 สรุปสถานะ Position ที่เปิดอยู่</b>\n"
        f"🕐 {now_bkk_str()}\n\n"
        f"เปิดอยู่ทั้งหมด: <b>{len(rows)}</b> ตัว  |  "
        f"🟢 กำไร {winners}  |  🔴 ขาดทุน {losers}  |  "
        f"เฉลี่ย {avg_pnl:+.2f}%\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━"
    )

    blocks = []
    for r in rows:
        icon = "🟢" if r["pnl_pct"] >= 0 else "🔴"
        conv_txt = f"  •  Conviction เดิม {r['conviction']}/{r['conviction_total']}" if r["conviction"] else ""
        days_txt = f"{r['days_held']:.1f} วัน" if r["days_held"] is not None else "ไม่ทราบ"
        stop_txt = f"${r['stop']:.4f}" if r["stop"] else "—"
        tgt_txt  = f"${r['target']:.4f}" if r["target"] else "—"
        next_step = _position_next_step(
            r["pnl_pct"], r["dist_to_stop"], r["dist_to_target"],
            r["drawdown_pct"], r["days_held"]
        )
        block = (
            f"{icon} <b>{r['symbol']}</b>  <b>{r['pnl_pct']:+.2f}%</b>{conv_txt}\n"
            f"  • เข้าซื้อ ${r['entry']:.4f} → ปัจจุบัน ${r['current']:.4f}"
            f"  (ถือมา {days_txt})\n"
            f"  • 🛑 Stop {stop_txt}  •  🎯 Target {tgt_txt}\n"
            f"  • ➡️ {next_step}"
        )
        blocks.append(block)

    # ── แบ่งเป็นหลายข้อความถ้ายาวเกิน budget ──
    messages  = []
    current_parts = [header]
    current_len   = len(header)
    for block in blocks:
        if current_len + len(block) + 2 > TELEGRAM_MSG_BUDGET:
            messages.append("\n\n".join(current_parts))
            current_parts = [block]
            current_len   = len(block)
        else:
            current_parts.append(block)
            current_len += len(block) + 2

    if current_parts:
        messages.append("\n\n".join(current_parts))

    footer = ("\n\n━━━━━━━━━━━━━━━━━━━━━━━━━\n"
              "ℹ️ คำแนะนำเป็นแนวทางทั่วไปจากกฎ Risk Management "
              "ไม่ใช่คำแนะนำการลงทุนเฉพาะบุคคล")
    messages[-1] += footer

    total = len(messages)
    if total > 1:
        for i in range(total):
            messages[i] += f"\n\n📄 หน้า {i + 1}/{total}"

    return messages


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

    # ── Universe.json sync setup (ดูรายละเอียดที่ sync_universe_tech()) ──────
    # universe_data โหลดมาใช้ "อ่านอย่างเดียว" ตอนนี้ (หา entry เดิม/eval gate)
    # ไม่เขียนกลับเข้า universe.json อีกแล้ว — เขียนลง patch_data/PATCH_PATH
    # แทน (structural fix ของ race condition กับ daily_screener.py)
    universe_data = load_json(UNIVERSE_PATH, {"settings": {}, "universe": []})
    if not isinstance(universe_data.get("universe"), list):
        universe_data["universe"] = []
    uni_cfg = {**DEFAULT_UNI_CFG, **(universe_data.get("settings") or {})}
    patch_data = load_json(PATCH_PATH, {})
    if not isinstance(patch_data, dict):
        patch_data = {}

    # ── Macro Gate ────────────────────────────────────────────────────
    print(f"\n[{now_str()}] ── MACRO CONTEXT CHECK ──")
    market_down, btc_down, spy_chg, btc_chg = get_macro_context()

    print(f"\n[{now_str()}] เริ่ม alert check — {len(watchlist)} symbols")

    for stock in watchlist:
        try:
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

            # เติมข้อมูล Purify/Price/ADR/RSI/Vol/Gate ล่าสุดเข้า
            # universe_live_patch.json ให้ symbol นี้ (ใช้ quote ที่เพิ่งดึงมา
            # ไม่ fetch price ซ้ำ — แค่เพิ่ม history call เดียวสำหรับ RSI/EMA50
            # ที่ quote เดิมไม่มี) — daily_screener.py จะ merge เข้า
            # universe.json จริงตอน startup รอบถัดไป
            sync_universe_tech(universe_data, patch_data, uni_cfg, symbol, quote)

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

            # ── Reminder cooldown สำหรับ position ที่เปิดอยู่แล้ว ───────
            # ลด noise: ถ้ามี position เปิดอยู่แล้ว (open_entry มีค่า) ไม่ต้องส่ง
            # Telegram แจ้ง BUY ซ้ำถี่ๆ — cooldown ปกติของแต่ละ alert (15-240
            # นาที) สั้นกว่าที่ควร ทำให้หุ้นที่ถืออยู่แล้วได้แจ้งเตือนซ้ำได้
            # หลายรอบต่อวัน จำกัดไว้ที่ประมาณวันละ 1 ครั้งแทน (ปรับได้ต่อหุ้น
            # ผ่าน position_reminder_cooldown_minutes) — ไม่กระทบ BUY ครั้งแรก
            # ที่ยังไม่มี position เปิดอยู่ ยิงได้ตามปกติทันที
            has_open_position = bool(sym_state.get("open_entry"))
            reminder_cd       = stock.get("position_reminder_cooldown_minutes", 1200)  # ~20 ชม.
            last_reminder_at  = sym_state.get("last_buy_reminder_at", "")
            in_reminder_cd    = bool(last_reminder_at) and minutes_since(last_reminder_at) < reminder_cd

            # ══════════════════════════════════════════════════════════════
            #  POSITION MANAGEMENT — Trailing Stop + Take Profit
            #  ทำงานอัตโนมัติทุกรอบสำหรับหุ้นที่มี position เปิดอยู่ ไม่ขึ้นกับ
            #  alert ที่ตั้งไว้ใน watchlist.json เลย — เป็น risk management
            #  ระดับ position (ราคาต้นทุน/peak/stop) ไม่ใช่ signal จาก indicator
            #  ต้องทำงาน "ก่อน" ลูปเช็ก alert ด้านล่าง เพราะ support_resistance
            #  alert (ใช้เป็น auto Stop Loss หลัก) จะอ่าน open_stop ที่เพิ่ง
            #  เลื่อนขึ้นในรอบนี้ทันที ไม่ต้องรอรอบถัดไป
            # ══════════════════════════════════════════════════════════════
            if has_open_position:
                entry     = sym_state.get("open_entry")
                peak_prev = sym_state.get("open_peak") or entry
                peak_now  = max(peak_prev, price)
                if peak_now != peak_prev:
                    sym_state["open_peak"] = peak_now

                # ── Trailing Stop: เลื่อน stop ขึ้นตามราคาสูงสุดที่เคยขึ้นไป
                # (ratchet ทางเดียว ไม่มีวันลดลง) เพื่อล็อกกำไรที่มีอยู่ไว้บางส่วน
                # แทนที่จะปล่อยให้กำไรไหลกลับมาเป็นขาดทุน (เช่นเคสที่เจอมาก่อน:
                # AGL เคย +16% แต่สุดท้ายปิดที่ -9.32% เพราะไม่มีกลไกนี้มาก่อน)
                # ค่า default: ต้องกำไรอย่างน้อย 8% จากทุนก่อนถึงจะเริ่ม trail
                # แล้วเว้นระยะ 5% จากจุดสูงสุด — ปรับได้ต่อหุ้นผ่าน
                # trailing_stop_activate_pct / trailing_stop_trail_pct
                trail_enabled = stock.get("trailing_stop_enabled", True)
                activate_pct  = stock.get("trailing_stop_activate_pct", 8.0)
                trail_pct     = stock.get("trailing_stop_trail_pct", 5.0)
                # threshold กันแจ้งเตือนถี่เกินไป — แจ้ง real-time เฉพาะตอนที่
                # stop เลื่อนขึ้นสะสม >= 2% จากระดับที่เคยแจ้งไปครั้งล่าสุด
                # (ไม่ใช่เทียบกับระดับก่อนหน้าทันที เพราะถ้าเทียบแบบนั้นการขยับ
                # ทีละนิดต่อเนื่องจะไม่มีวันถึง threshold เลยแม้สะสมมาไกลแล้ว)
                notify_threshold_pct = stock.get("trailing_stop_notify_threshold_pct", 2.0)
                if trail_enabled and entry:
                    profit_from_entry_pct = (peak_now - entry) / entry * 100
                    if profit_from_entry_pct >= activate_pct:
                        trail_candidate = peak_now * (1 - trail_pct / 100)
                        cur_stop = sym_state.get("open_stop")
                        if not cur_stop or trail_candidate > cur_stop:
                            sym_state["open_stop"] = trail_candidate
                            # ตั้ง flag ไว้บอกว่า stop ตอนนี้คือ trailing stop
                            # ไม่ใช่ static stop เดิมจากตอน BUY แล้ว — ใช้ตัดสิน
                            # ข้อความตอน SELL ทำงานจริง (ดูจุดที่เรียก
                            # build_sell_message กับ atype == "support_resistance")
                            sym_state["open_stop_is_trailing"] = True
                            print(f"  [Trailing Stop] {symbol}: เลื่อน stop ขึ้นเป็น "
                                  f"${trail_candidate:.4f} (peak=${peak_now:.4f}, "
                                  f"กำไรจากทุน {profit_from_entry_pct:+.1f}%)")

                            # ── บันทึก log ทุกครั้งที่เลื่อน (ไม่ว่าจะแจ้งเตือน
                            # real-time หรือไม่) ไว้ใช้สร้างสรุปประจำวันทีหลัง ──
                            log.append({
                                "timestamp": now_str(), "symbol": symbol,
                                "alert_id": f"{symbol}_TRAIL_ADJUST",
                                "type": "trailing_stop_moved", "action": "ADJUST",
                                "price": price, "change_pct": quote["change_pct"],
                                "value": round(trail_candidate, 4),
                                "entry_price": entry, "pnl_pct": None,
                                "days_held": None, "entry_alert_type": None,
                                "conviction_score": None, "conviction_total": None, "rr_ratio": None,
                            })

                            # ── แจ้งเตือน real-time เฉพาะตอนขยับมีนัยสำคัญพอ ──
                            last_notified = sym_state.get("open_trailing_last_notified_stop")
                            if last_notified:
                                move_since_notified_pct = (trail_candidate - last_notified) / last_notified * 100
                            else:
                                move_since_notified_pct = None  # ครั้งแรกที่ trailing เริ่มทำงาน — แจ้งเสมอ
                            should_notify = (last_notified is None) or (move_since_notified_pct >= notify_threshold_pct)
                            if should_notify:
                                trail_msg = (
                                    f"📈 <b>Trailing Stop เลื่อนขึ้น: {symbol}</b>\n"
                                    f"🕐 {now_bkk_str()}\n\n"
                                    f"ราคาปัจจุบัน: <b>${price:.4f}</b>  (จุดสูงสุด ${peak_now:.4f})\n"
                                    f"กำไรจากทุน: <b>{profit_from_entry_pct:+.1f}%</b>\n"
                                    f"🛑 Stop เลื่อนขึ้นเป็น: <b>${trail_candidate:.4f}</b>\n\n"
                                    f"ℹ️ ป้องกันกำไรที่มีอยู่ไว้บางส่วน — ไม่ต้องทำอะไรเพิ่ม ระบบดูแลให้อัตโนมัติ"
                                )
                                if send_telegram(token, chat_id, trail_msg):
                                    sym_state["open_trailing_last_notified_stop"] = trail_candidate
                                    fired_count += 1

                # ── Take Profit: ราคาถึงเป้าหมายที่ตั้งไว้ตอน BUY -> ขายทันที ──
                # แยกจาก support_resistance เพราะ TP เป็นการ "ขายเพราะได้กำไรตาม
                # แผน" ไม่ใช่การหลุดแนวรับ ข้อความและ log ควรบอกเหตุผลต่างกันชัดเจน
                tp_enabled = stock.get("take_profit_enabled", True)
                target     = sym_state.get("open_target")
                if tp_enabled and target and entry and price >= target:
                    tp_pnl_pct = (price - entry) / entry * 100
                    open_time_val = sym_state.get("open_time")
                    days_held = minutes_since(open_time_val) / 1440 if open_time_val else None
                    detail_txt = f"เป้าหมาย ${target:.4f} (กำไร {tp_pnl_pct:+.1f}%)"
                    tp_msg = build_sell_message(stock, quote, "take_profit", detail_txt)
                    days_held_txt = f"  •  ถือมา {days_held:.1f} วัน" if days_held is not None else ""
                    pnl_line = (
                        f"\n━━━━━━━━━━━━━━━━━━━━━━━━━"
                        f"\n📊 <b>ผลลัพธ์ position นี้:</b> 🟢 กำไร  <b>{tp_pnl_pct:+.2f}%</b>"
                        f"\n  • เข้าซื้อ ${entry:.4f} → ปิดที่ ${price:.4f}{days_held_txt}"
                    )
                    tp_msg = tp_msg.replace("\n\n📊 <a href=", pnl_line + "\n\n📊 <a href=", 1)
                    tp_success = send_telegram(token, chat_id, tp_msg)
                    print(f"  [Take Profit] {symbol}: "
                          f"{'✅ ส่งสำเร็จ' if tp_success else '❌ ส่งไม่สำเร็จ'} — {tp_pnl_pct:+.2f}%")
                    if tp_success:
                        fired_count += 1
                        entry_type_val = sym_state.get("open_alert_type")
                        # ดึง conviction/R:R ที่บันทึกไว้ตอน BUY กลับมาใส่ log ก่อน
                        # state ถูกเคลียร์ด้านล่าง (แก้บั๊กเดียวกับจุด SELL หลัก)
                        conv_score_val = sym_state.get("open_conviction")
                        conv_total_val = sym_state.get("open_conviction_total")
                        rr_ratio_val   = sym_state.get("open_rr_ratio")
                        sym_state["last_sell_at"] = now_str()
                        log.append({
                            "timestamp": now_str(), "symbol": symbol,
                            "alert_id": f"{symbol}_TAKE_PROFIT", "type": "take_profit",
                            "action": "SELL", "price": price,
                            "change_pct": quote["change_pct"], "value": target,
                            "entry_price": entry, "pnl_pct": round(tp_pnl_pct, 4),
                            "days_held": round(days_held, 2) if days_held is not None else None,
                            "entry_alert_type": entry_type_val,
                            "conviction_score": conv_score_val, "conviction_total": conv_total_val,
                            "rr_ratio": rr_ratio_val,
                        })
                        for _k in ("open_entry", "open_time", "open_peak", "open_stop",
                                   "open_target", "open_conviction", "open_conviction_total", "open_rr_ratio",
                                   "open_alert_type", "open_stop_is_trailing",
                                   "open_trailing_last_notified_stop", "last_buy_reminder_at"):
                            sym_state.pop(_k, None)
                        # position ปิดไปแล้ว — ข้าม alert loop ที่เหลือของหุ้นตัวนี้
                        # ในรอบนี้ ไปหุ้นตัวถัดไปเลย (กัน SELL/BUY ซ้ำซ้อนในรอบเดียวกัน)
                        continue

                # ── Stagnant Exit: position แช่แข็งนานเกินไปโดยราคาไม่ไปไหน ──
                # เดิม _position_next_step() มีข้อความเตือนแบบนี้อยู่แล้วใน Position
                # Status digest แต่เป็นแค่ข้อความแจ้ง ไม่เคยขายจริง — วิเคราะห์ trade
                # log ย้อนหลังพบเทรดที่ปล่อยให้ถือ 22-46 วันก่อนโดน exit แบบเจ็บหนัก
                # (COHR -28.6%, PSIX -31.8%, OPTX -51.5%, RIOT -33.1%) เพราะรอสัญญาณ
                # ที่ตอบสนองช้าอย่าง ma_crossover/percent_change — เพิ่ม auto-exit
                # ตัดจบเองถ้าแช่แข็งนานเกินไปในโซนใกล้ทุน (ไม่ใช่ทั้งกำไรหนักหรือ
                # ขาดทุนหนัก ซึ่งกรณีนั้นมี take_profit/stop-loss คอยจัดการอยู่แล้ว)
                # เพื่อคืน capital ไปเข้าไม้อื่นที่มีโอกาสมากกว่า
                stagnant_enabled = stock.get("stagnant_exit_enabled", True)
                stagnant_days    = stock.get("stagnant_exit_days", 15)
                stagnant_band    = stock.get("stagnant_exit_band_pct", 3.0)
                if stagnant_enabled and entry and sym_state.get("open_time"):
                    days_open    = minutes_since(sym_state["open_time"]) / 1440
                    stagnant_pnl = (price - entry) / entry * 100
                    if days_open >= stagnant_days and abs(stagnant_pnl) <= stagnant_band:
                        detail_txt = (f"ถือมา {days_open:.0f} วัน ราคายังอยู่ในช่วง "
                                      f"±{stagnant_band:.0f}% ({stagnant_pnl:+.1f}%)")
                        st_msg = build_sell_message(stock, quote, "stagnant_exit", detail_txt)
                        days_held_txt = f"  •  ถือมา {days_open:.1f} วัน"
                        pnl_icon = "🟢 กำไร" if stagnant_pnl >= 0 else "🔴 ขาดทุน"
                        pnl_line = (
                            f"\n━━━━━━━━━━━━━━━━━━━━━━━━━"
                            f"\n📊 <b>ผลลัพธ์ position นี้:</b> {pnl_icon}  <b>{stagnant_pnl:+.2f}%</b>"
                            f"\n  • เข้าซื้อ ${entry:.4f} → ปิดที่ ${price:.4f}{days_held_txt}"
                        )
                        st_msg = st_msg.replace("\n\n📊 <a href=", pnl_line + "\n\n📊 <a href=", 1)
                        st_success = send_telegram(token, chat_id, st_msg)
                        print(f"  [Stagnant Exit] {symbol}: "
                              f"{'✅ ส่งสำเร็จ' if st_success else '❌ ส่งไม่สำเร็จ'} "
                              f"— {stagnant_pnl:+.2f}% หลังถือ {days_open:.0f} วัน")
                        if st_success:
                            fired_count += 1
                            entry_type_val = sym_state.get("open_alert_type")
                            conv_score_val = sym_state.get("open_conviction")
                            conv_total_val = sym_state.get("open_conviction_total")
                            rr_ratio_val   = sym_state.get("open_rr_ratio")
                            sym_state["last_sell_at"] = now_str()
                            log.append({
                                "timestamp": now_str(), "symbol": symbol,
                                "alert_id": f"{symbol}_STAGNANT_EXIT", "type": "stagnant_exit",
                                "action": "SELL", "price": price,
                                "change_pct": quote["change_pct"], "value": round(days_open, 1),
                                "entry_price": entry, "pnl_pct": round(stagnant_pnl, 4),
                                "days_held": round(days_open, 2),
                                "entry_alert_type": entry_type_val,
                                "conviction_score": conv_score_val, "conviction_total": conv_total_val,
                                "rr_ratio": rr_ratio_val,
                            })
                            for _k in ("open_entry", "open_time", "open_peak", "open_stop",
                                       "open_target", "open_conviction", "open_conviction_total", "open_rr_ratio",
                                       "open_alert_type", "open_stop_is_trailing",
                                       "open_trailing_last_notified_stop", "last_buy_reminder_at"):
                                sym_state.pop(_k, None)
                            continue

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
                # ── Reminder cooldown (มี position เปิดอยู่แล้ว — ลด noise) ──
                if action == "BUY" and has_open_position and in_reminder_cd:
                    rem = reminder_cd - minutes_since(last_reminder_at)
                    print(f"  [{alert_id}] มี position เปิดอยู่แล้ว — เตือนซ้ำอีกใน {rem/60:.1f} ชม.")
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
                    triggered, tval, used_threshold = check_percent_change(alert, quote)
                    if triggered:
                        adr_info = f" | ADR {quote.get('adr_pct', 0):.1f}%" if quote.get("adr_pct", 0) >= 1.0 else ""
                        detail = f"เปลี่ยนแปลง {tval:+.2f}% (threshold {used_threshold:.1f}%{adr_info})"
                        if action == "BUY":
                            pos = calc_position_size(pos_cfg, symbol, price)
                            msg = build_buy_message(stock, quote, atype, detail, pos)
                        elif action == "SELL":
                            msg = build_sell_message(stock, quote, "pct_drop", detail)
                        else:
                            msg = build_info_message(stock, quote, atype, detail)

                elif atype == "support_resistance":
                    triggered, tval, level = check_support_resistance(
                        alert, quote, symbol, dynamic_stop=sym_state.get("open_stop"))
                    if triggered:
                        lvl_str = f"${level:.4f}" if level else "auto"
                        detail  = f"ราคา {alert.get('direction','').replace('_',' ')} แนวระดับ {lvl_str}"
                        if action == "BUY":
                            pos = calc_position_size(pos_cfg, symbol, price)
                            msg = build_buy_message(stock, quote, atype, detail, pos)
                        elif action == "SELL":
                            # ถ้า stop ที่หลุดคือ trailing stop (เลื่อนขึ้นมาแล้ว)
                            # ใช้ข้อความ "Trailing Stop" แทนที่จะขึ้นทั่วไปว่า
                            # "Stop Loss" เฉยๆ — ให้รู้ชัดว่าเคยกำไรมาก่อนแล้ว
                            # เพิ่งโดนล็อกกำไรบางส่วนออกมา ไม่ใช่ stop-loss ตั้งต้น
                            sell_reason = "trailing_stop" if sym_state.get("open_stop_is_trailing") else "sl_break"
                            msg = build_sell_message(stock, quote, sell_reason, detail)
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

                # ── CONVICTION GATE — ด่านสุดท้ายเฉพาะ BUY (รวมศูนย์ทุก type) ──
                # ทุก BUY signal (ไม่ว่าจะมาจาก RSI/MTF/Score/Volume/%Change ฯลฯ)
                # ต้องผ่านอย่างน้อย 3/4 มิติก่อนปล่อยจริง — กัน false signal แบบ
                # SNDK (ราคาลงหนักแต่ MTF ยัง bullish) โดยไม่ต้องดึง API เพิ่ม
                # ── BUY: เก็บ conviction score + R:R ไว้ใช้ตอน log.append ด้านล่าง ──
                # (เดิมคำนวณแล้วใส่แค่ในข้อความ Telegram อย่างเดียว ไม่เคยถูก
                # บันทึกแบบมีโครงสร้างใน alert_log.json เลย ทำให้ย้อนวิเคราะห์
                # ไม่ได้ว่าสัญญาณ conviction เต็ม 5/5 win rate สูงกว่า 3/5 จริงไหม)
                buy_conv_score = None
                buy_conv_total = None
                buy_rr_ratio   = None
                if action == "BUY":
                    # ── R:R Gate — บล็อค BUY ที่ไม่มีข้อมูล position/stop เลย ──
                    # (fetch_quote/fetch_history ล้มเหลว) เพราะไม่มี stop ให้
                    # ยึด ไม่ควรปล่อยสัญญาณแบบไม่มีแผนออกให้คนตามซื้อ
                    if pos is None:
                        print(f"  [{alert_id}] 🚫 R:R Gate FAIL — คำนวณ position size ไม่ได้ (ไม่มีข้อมูลราคา/ATR)")
                        continue
                    # safety net: calc_position_size บังคับ R:R ≥ MIN_RR_RATIO ไว้
                    # แล้วตั้งแต่ต้นทาง กรณีนี้ไม่ควรเกิด เว้นแต่ target_pct ไม่ได้
                    # ตั้งไว้ใน watchlist เลย (pos.get("target") จะเป็น None)
                    if pos.get("target") and pos.get("rr_ratio", 0) < MIN_RR_RATIO:
                        print(f"  [{alert_id}] 🚫 R:R Gate FAIL — R:R={pos.get('rr_ratio')} ต่ำกว่า 1:{MIN_RR_RATIO}")
                        continue

                    conv_pass, conv_score, conv_detail = conviction_gate(symbol, quote)
                    total_dims = conv_detail.get("total_dims", 4)
                    if not conv_pass:
                        failed_dims = [k for k, v in conv_detail.items()
                                       if isinstance(v, dict) and v.get("pass") is False]
                        print(f"  [{alert_id}] 🚫 Conviction Gate FAIL "
                              f"({conv_score}/{total_dims}) — ไม่ผ่าน: {', '.join(failed_dims)}")
                        continue
                    print(f"  [{alert_id}] ✅ Conviction Gate PASS ({conv_score}/{total_dims})")
                    buy_conv_score = conv_score
                    buy_conv_total = total_dims
                    buy_rr_ratio   = pos.get("rr_ratio")
                    # แนบผล Conviction Score เข้า message ก่อนส่งจริง
                    dims_th = {"trend": "Trend", "momentum": "Momentum",
                               "volume": "Volume", "volatility": "Volatility",
                               "long_trend": "EMA200"}
                    dim_marks = " ".join(
                        f"{dims_th.get(k,k)}{'✅' if v.get('pass') else ('➖' if v.get('pass') is None else '❌')}"
                        for k, v in conv_detail.items() if isinstance(v, dict)
                    )
                    if conv_score >= total_dims:
                        # ── สัญญาณเต็มพลัง ทุกมิติผ่านครบ — ทำให้เด่นขึ้นมาเพื่อให้สังเกตง่าย ──
                        conv_line = (f"\n⭐⭐⭐ <b>Conviction: {conv_score}/{total_dims} — สัญญาณเต็มพลัง</b> ⭐⭐⭐"
                                     f"\n🎯 ({dim_marks})")
                        msg = f"🔥🌟 <b>TOP SIGNAL ({conv_score}/{total_dims})</b> 🌟🔥\n" + msg
                    else:
                        conv_line = f"\n🎯 Conviction: {conv_score}/{total_dims}  ({dim_marks})"
                    msg = msg.replace("\n\n📊 <a href=", conv_line + "\n\n📊 <a href=", 1)

                # ── SELL: แนบผลลัพธ์ P&L ของ position ที่กำลังจะปิด (ถ้ามี open_entry) ──
                # เก็บไว้ในตัวแปรนี้เพื่อใช้ตอน log.append ด้านล่างด้วย (ก่อนหน้านี้
                # คำนวณแค่สำหรับใส่ในข้อความ Telegram แล้วทิ้ง ไม่เคยถูกบันทึกแบบ
                # มีโครงสร้างใน alert_log.json เลย — ทำให้สร้างหน้าสรุปกำไร-ขาดทุน/
                # ประวัติการเทรดย้อนหลังไม่ได้ ต้องแก้ตรงนี้ก่อนถึงจะมีข้อมูลป้อนหน้า
                # Portfolio ใหม่ได้)
                closed_entry_price = None
                closed_pnl_pct     = None
                closed_days_held   = None
                closed_entry_type  = None
                closed_conv_score  = None
                closed_conv_total  = None
                closed_rr_ratio    = None
                if action == "SELL":
                    prior_state = state.get(symbol, {})
                    entry_price = prior_state.get("open_entry")
                    if entry_price:
                        pnl_pct = (price - entry_price) / entry_price * 100
                        days_held_txt = ""
                        days_held = None
                        if prior_state.get("open_time"):
                            days_held = minutes_since(prior_state["open_time"]) / 1440
                            days_held_txt = f"  •  ถือมา {days_held:.1f} วัน"
                        result_icon = "🟢 กำไร" if pnl_pct >= 0 else "🔴 ขาดทุน"
                        pnl_line = (
                            f"\n━━━━━━━━━━━━━━━━━━━━━━━━━"
                            f"\n📊 <b>ผลลัพธ์ position นี้:</b> {result_icon}  "
                            f"<b>{pnl_pct:+.2f}%</b>"
                            f"\n  • เข้าซื้อ ${entry_price:.4f} → ปิดที่ ${price:.4f}{days_held_txt}"
                        )
                        msg = msg.replace("\n\n📊 <a href=", pnl_line + "\n\n📊 <a href=", 1)
                        closed_entry_price = entry_price
                        closed_pnl_pct     = round(pnl_pct, 4)
                        closed_days_held   = round(days_held, 2) if days_held is not None else None
                        closed_entry_type  = prior_state.get("open_alert_type")
                        # ดึง conviction score/R:R ที่บันทึกไว้ตอน BUY กลับมาใส่
                        # log ตอนปิดด้วย (แก้บั๊ก: เดิม field พวกนี้มีแค่ฝั่ง BUY
                        # log entry เท่านั้น ฝั่ง SELL ไม่เคยมีข้อมูลเลย)
                        closed_conv_score  = prior_state.get("open_conviction")
                        closed_conv_total  = prior_state.get("open_conviction_total")
                        closed_rr_ratio    = prior_state.get("open_rr_ratio")

                print(f"  [{alert_id}] ✅ TRIGGERED! ส่ง Telegram...")
                success = send_telegram(token, chat_id, msg)

                if success:
                    state.setdefault(symbol, {})[alert_id] = {"last_fired": now_str()}

                    # ── Track BUY/SELL timestamps (สำหรับ re-entry + sell cooldown) ──
                    if action == "BUY":
                        state[symbol]["last_buy_at"] = now_str()
                        # เก็บเวลาที่เพิ่งแจ้ง BUY ไปล่าสุด ไม่ว่าจะเป็นครั้งแรก
                        # หรือแจ้งซ้ำ — ให้ reminder cooldown ด้านบนใช้เทียบรอบถัดไป
                        state[symbol]["last_buy_reminder_at"] = now_str()
                        # FIX: gate ที่ comment ด้านบนตั้งใจไว้ตั้งแต่แรก ("2. ไม่มี
                        # open position สำหรับหุ้นตัวนั้น") แต่ไม่เคยถูกเขียนโค้ดจริง
                        # — ก่อนหน้านี้ทุกครั้งที่ BUY ยิงซ้ำขณะ position เดิมยัง
                        # เปิดอยู่ (ไม่ว่าจะเพราะ re-entry cooldown หมดตามปกติ หรือ
                        # เพราะ state.json หลุดจาก push fail แล้วทำให้ cooldown
                        # เข้าใจผิดว่ายังไม่เคยยิง) จะเขียนทับ open_entry/open_time/
                        # open_peak ด้วยราคา/เวลาล่าสุดทันที ทำให้ % กำไร-ขาดทุน
                        # และจำนวนวันที่ถือ ในรายงาน "สรุปสถานะ position"
                        # (build_position_status_messages) ผิดเพี้ยนจากความเป็นจริง
                        # — แก้โดยบันทึกค่าพวกนี้แค่ตอน "เปิด position ใหม่จริงๆ"
                        # (ยังไม่มี open_entry ค้างอยู่) เท่านั้น ถ้ามี position
                        # เปิดอยู่แล้ว ยังส่ง Telegram แจ้งเตือนตามปกติ (เผื่ออยากรู้
                        # ว่ามีสัญญาณ BUY อีกรอบ) แค่ไม่ไปยุ่งกับตัวเลข entry เดิม
                        if not state[symbol].get("open_entry"):
                            state[symbol]["open_entry"]            = price
                            state[symbol]["open_time"]             = now_str()
                            state[symbol]["open_peak"]             = price
                            state[symbol]["open_stop"]             = pos["stop"] if pos else None
                            state[symbol]["open_target"]           = pos.get("target") if pos else None
                            state[symbol]["open_conviction"]       = conv_score
                            state[symbol]["open_conviction_total"] = total_dims
                            # เก็บ R:R ตอนเข้าไว้ด้วย (เหมือน open_conviction) เพื่อ
                            # ดึงกลับมาใส่ log ตอนปิด position จริง — เดิมมีบั๊ก:
                            # SELL log entry ไม่เคยมี conviction_score/rr_ratio เลย
                            # (field มีในสคีมาแต่ไม่เคยถูกเติมค่าให้ฝั่ง SELL) ทำให้
                            # ฟีเจอร์ "breakdown ตาม conviction" ในแดชบอร์ดไม่มีข้อมูล
                            state[symbol]["open_rr_ratio"]         = pos.get("rr_ratio") if pos else None
                            state[symbol]["open_alert_type"]       = atype
                        else:
                            print(f"  [{alert_id}] ℹ️ มี position เปิดอยู่แล้ว "
                                  f"(entry เดิม ${state[symbol]['open_entry']:.4f}) "
                                  f"— ส่ง alert ตามปกติแต่ไม่เขียนทับ entry เดิม")
                    elif action == "SELL":
                        state[symbol]["last_sell_at"] = now_str()
                        # ── ปิด position: เคลียร์ open_* ทั้งหมดไม่ให้ P&L ค้าง ──
                        for _k in ("open_entry", "open_time", "open_peak", "open_stop",
                                   "open_target", "open_conviction", "open_conviction_total", "open_rr_ratio",
                                   "open_alert_type", "open_stop_is_trailing",
                                   "open_trailing_last_notified_stop", "last_buy_reminder_at"):
                            state[symbol].pop(_k, None)



                    log.append({
                        "timestamp":  now_str(),
                        "symbol":     symbol,
                        "alert_id":   alert_id,
                        "type":       atype,
                        "action":     action,
                        "price":      price,
                        "change_pct": quote["change_pct"],
                        "value":      tval,
                        # ── ข้อมูลผลลัพธ์เทรด (เฉพาะ SELL ที่มี position จริง) ──
                        "entry_price":      closed_entry_price,
                        "pnl_pct":          closed_pnl_pct,
                        "days_held":        closed_days_held,
                        "entry_alert_type": closed_entry_type,
                        # ── ข้อมูลคุณภาพสัญญาณตอนเข้า — โผล่ได้ทั้ง BUY (จาก
                        # buy_conv_score ที่เพิ่งผ่าน gate รอบนี้) และ SELL (จาก
                        # closed_conv_score ที่ดึงย้อนมาจาก state ตอน BUY เดิม)
                        # แก้บั๊ก: เดิม SELL ได้ None เสมอเพราะใช้ buy_conv_score
                        # ตัวเดียวซึ่งมีค่าเฉพาะตอน action=="BUY" ในรอบเดียวกันเท่านั้น
                        "conviction_score": buy_conv_score if action == "BUY" else closed_conv_score,
                        "conviction_total": buy_conv_total if action == "BUY" else closed_conv_total,
                        "rr_ratio":         buy_rr_ratio if action == "BUY" else closed_rr_ratio,
                    })
                    fired_count += 1
                    print(f"  [{alert_id}] ✅ ส่งสำเร็จ")
                else:
                    print(f"  [{alert_id}] ❌ ส่งไม่สำเร็จ")

            time.sleep(0.5)
        except Exception as e:
            # FIX: ก่อนหน้านี้ loop นี้ไม่มี try/except เลย — ถ้าหุ้นตัวไหน
            # ตัวหนึ่ง throw exception ไม่ว่าจะจุดไหนก็ตาม (เช่น field ที่
            # ขาดหาย, ข้อมูลราคาผิดปกติ, bug ใน check_* ฟังก์ชันใดก็ได้)
            # ทั้ง run จะ crash ทันที ทำให้ save_json(state) ไม่ถูกเรียก
            # (state ของหุ้นที่ผ่านไปแล้วก่อนหน้าในลูปเดียวกันหายหมด แม้จะ
            # ยิง Telegram ไปแล้วก็ตาม) และโค้ด Daily Summary / Position
            # Status ที่อยู่ท้ายฟังก์ชัน main() หลังลูปนี้ก็ไม่ถูกรันเลย —
            # แก้โดยดัก exception ต่อหุ้นแต่ละตัว บันทึก log แล้วข้ามไป
            # หุ้นตัวถัดไปแทน ไม่ให้ 1 ตัวที่มีปัญหาทำให้ทั้ง run ล่ม
            _err_sym = stock.get("symbol", "?")
            print(f"  [{_err_sym}] ⚠️ ERROR ไม่คาดคิดระหว่างประมวลผล — ข้ามไปหุ้นตัวถัดไป: {e}")
            continue

    # ── Daily Summary ─────────────────────────────────────────────────
    summary_hour  = settings.get("daily_summary_hour_utc", 1)
    current_hour  = now_utc().hour
    summary_state = state.get("__daily_summary__", {})
    last_summary  = summary_state.get("last_sent", "")
    today_str     = now_utc().strftime("%Y-%m-%d")

    if (current_hour == summary_hour
            and (not last_summary or not last_summary.startswith(today_str))
            and quotes_cache):
        try:
            print("\n[Daily Summary] กำลังส่ง...")
            summary_msgs = build_daily_summary_messages(watchlist, quotes_cache, universe_data)
            all_ok = True
            for i, smsg in enumerate(summary_msgs):
                ok = send_telegram(token, chat_id, smsg)
                all_ok = all_ok and ok
                if i < len(summary_msgs) - 1:
                    time.sleep(0.5)
            if all_ok:
                state["__daily_summary__"] = {"last_sent": now_str()}
                print(f"[Daily Summary] ✅ ส่งสำเร็จ ({len(summary_msgs)} ข้อความ)")
            else:
                print("[Daily Summary] ❌ ส่งไม่สำเร็จบางข้อความ")
        except Exception as e:
            # FIX: กันเหตุการณ์แบบเดียวกับ per-stock loop ด้านบน — ถ้า
            # build_daily_summary()/send_telegram() พังด้วยเหตุผลอะไรก็ตาม
            # (เช่น ข้อมูลราคาผิดรูปแบบ) จะไม่ทำให้ save_json() ท้ายฟังก์ชัน
            # ไม่ถูกเรียก และจะได้ log ไว้เห็นสาเหตุ แทนที่จะ silent fail
            print(f"[Daily Summary] ⚠️ ERROR ไม่คาดคิด — ข้ามรอบนี้ไป: {e}")

    # ── Position Status Report — สรุป P&L ของ position ที่เปิดอยู่ ──────
    status_hour  = settings.get("position_status_hour_utc", summary_hour)
    status_state = state.get("__position_status__", {})
    last_status  = status_state.get("last_sent", "")

    if (current_hour == status_hour
            and (not last_status or not last_status.startswith(today_str))
            and quotes_cache):
        try:
            print("\n[Position Status] กำลังสรุป...")
            status_msgs = build_position_status_messages(state, watchlist, quotes_cache)
            if status_msgs:
                all_ok = True
                for i, smsg in enumerate(status_msgs):
                    ok = send_telegram(token, chat_id, smsg)
                    all_ok = all_ok and ok
                    if i < len(status_msgs) - 1:
                        time.sleep(0.5)
                if all_ok:
                    state["__position_status__"] = {"last_sent": now_str()}
                    print(f"[Position Status] ✅ ส่งสำเร็จ ({len(status_msgs)} ข้อความ)")
                else:
                    print("[Position Status] ❌ ส่งไม่สำเร็จบางข้อความ")
            else:
                print("[Position Status] ไม่มี position เปิดอยู่ — ข้าม")
                state["__position_status__"] = {"last_sent": now_str()}
        except Exception as e:
            # FIX: เหตุผลเดียวกับ Daily Summary ด้านบน — กัน exception จาก
            # build_position_status_messages() (เช่น ถ้าหุ้นตัวไหนมี
            # open_entry ค้างแต่ไม่มีราคาปัจจุบันใน quotes_cache) ไม่ให้ทำ
            # save_json() ท้ายฟังก์ชันไม่ถูกเรียก
            print(f"[Position Status] ⚠️ ERROR ไม่คาดคิด — ข้ามรอบนี้ไป: {e}")

    # ── Earnings Alert — เตือนหุ้นใน watchlist ที่ใกล้ประกาศผลประกอบการ ──
    earn_hour    = settings.get("earnings_alert_hour_utc", summary_hour)
    earn_days    = settings.get("earnings_alert_threshold_days", 3)
    earn_state   = state.get("__earnings_alert__", {})
    last_earn    = earn_state.get("last_sent", "")

    if (current_hour == earn_hour
            and (not last_earn or not last_earn.startswith(today_str))):
        try:
            print("\n[Earnings Alert] กำลังเช็ค...")
            earn_msg = build_earnings_alert_message(watchlist, universe_data, earn_days)
            if earn_msg:
                if send_telegram(token, chat_id, earn_msg):
                    state["__earnings_alert__"] = {"last_sent": now_str()}
                    print("[Earnings Alert] ✅ ส่งสำเร็จ")
                else:
                    print("[Earnings Alert] ❌ ส่งไม่สำเร็จ")
            else:
                print("[Earnings Alert] ไม่มีหุ้นใกล้ประกาศผล — ข้าม")
                state["__earnings_alert__"] = {"last_sent": now_str()}
        except Exception as e:
            # FIX: เหตุผลเดียวกับ Daily Summary/Position Status ด้านบน — กัน
            # exception จาก build_earnings_alert_message() (เช่น next_earnings_date
            # ที่เก็บไว้ผิดรูปแบบ) ไม่ให้ทำ save_json() ท้ายฟังก์ชันไม่ถูกเรียก
            print(f"[Earnings Alert] ⚠️ ERROR ไม่คาดคิด — ข้ามรอบนี้ไป: {e}")

    # ── Universe Earnings Digest — ปฏิทินประกาศผลทั้ง Universe (ไม่จำกัดแค่
    # watchlist) สำหรับคนที่อยากเล่นตามช่วงประกาศผลโดยตรง เป็นรายงานแยกจาก
    # Earnings Alert ด้านบน — threshold กว้างกว่า (default 5 วัน) เพราะเป็น
    # เครื่องมือ "หาโอกาสใหม่" ไม่ใช่แค่เตือนของที่ถืออยู่แล้ว ──
    uni_earn_hour  = settings.get("universe_earnings_digest_hour_utc", summary_hour)
    uni_earn_days  = settings.get("universe_earnings_digest_threshold_days", 5)
    uni_earn_state = state.get("__universe_earnings_digest__", {})
    last_uni_earn  = uni_earn_state.get("last_sent", "")

    if (current_hour == uni_earn_hour
            and (not last_uni_earn or not last_uni_earn.startswith(today_str))):
        try:
            print("\n[Universe Earnings Digest] กำลังเช็ค...")
            uni_earn_msgs = build_universe_earnings_digest_messages(universe_data, watchlist, uni_earn_days)
            if uni_earn_msgs:
                all_ok = True
                for i, uemsg in enumerate(uni_earn_msgs):
                    ok = send_telegram(token, chat_id, uemsg)
                    all_ok = all_ok and ok
                    if i < len(uni_earn_msgs) - 1:
                        time.sleep(0.5)
                if all_ok:
                    state["__universe_earnings_digest__"] = {"last_sent": now_str()}
                    print(f"[Universe Earnings Digest] ✅ ส่งสำเร็จ ({len(uni_earn_msgs)} ข้อความ)")
                else:
                    print("[Universe Earnings Digest] ❌ ส่งไม่สำเร็จบางข้อความ")
            else:
                print("[Universe Earnings Digest] ไม่มีหุ้นใกล้ประกาศผลใน Universe — ข้าม")
                state["__universe_earnings_digest__"] = {"last_sent": now_str()}
        except Exception as e:
            # FIX: เหตุผลเดียวกับรายงานอื่นๆ ด้านบน — กัน exception ไม่ให้ทำ
            # save_json() ท้ายฟังก์ชันไม่ถูกเรียก
            print(f"[Universe Earnings Digest] ⚠️ ERROR ไม่คาดคิด — ข้ามรอบนี้ไป: {e}")

    # ── Trailing Stop Digest — สรุปวันละ 1 ครั้งว่าวันนี้เลื่อน stop ไปกี่ครั้ง ──
    trail_digest_hour  = settings.get("trailing_stop_digest_hour_utc", summary_hour)
    trail_digest_state = state.get("__trailing_stop_digest__", {})
    last_trail_digest  = trail_digest_state.get("last_sent", "")

    if (current_hour == trail_digest_hour
            and (not last_trail_digest or not last_trail_digest.startswith(today_str))):
        try:
            print("\n[Trailing Stop Digest] กำลังสรุป...")
            digest_msg = build_trailing_stop_digest_message(log, state, today_str)
            if digest_msg:
                if send_telegram(token, chat_id, digest_msg):
                    state["__trailing_stop_digest__"] = {"last_sent": now_str()}
                    print("[Trailing Stop Digest] ✅ ส่งสำเร็จ")
                else:
                    print("[Trailing Stop Digest] ❌ ส่งไม่สำเร็จ")
            else:
                print("[Trailing Stop Digest] วันนี้ไม่มีการเลื่อน stop เลย — ข้าม")
                state["__trailing_stop_digest__"] = {"last_sent": now_str()}
        except Exception as e:
            # FIX: เหตุผลเดียวกับรายงานอื่นๆ ด้านบน — กัน exception จาก
            # build_trailing_stop_digest_message() ไม่ให้ทำ save_json() ท้าย
            # ฟังก์ชันไม่ถูกเรียก
            print(f"[Trailing Stop Digest] ⚠️ ERROR ไม่คาดคิด — ข้ามรอบนี้ไป: {e}")

    save_json(STATE_PATH, state)
    save_json(LOG_PATH, log[-500:])
    # Structural fix: ไม่ save_json(UNIVERSE_PATH, universe_data) ตรงๆ อีกแล้ว
    # (นั่นคือต้นตอ race condition กับ daily_screener.py) — เขียนลง patch file
    # แยกแทน daily_screener.py จะ merge เข้า universe.json จริงตอน startup
    save_json(PATCH_PATH, patch_data)
    print(f"\n[{now_str()}] เสร็จสิ้น — fire {fired_count} alert(s)")


if __name__ == "__main__":
    main()
