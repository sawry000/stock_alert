#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════╗
║  MODULE 3: Candle Pattern — ตรวจจับรูปแบบแท่งเทียน            ║
║  Doji, Hammer, Engulfing, 3 Consecutive, Shooting Star ฯลฯ     ║
╚══════════════════════════════════════════════════════════════════╝

วิธีใช้ standalone:
    python3 module_candle.py --symbol AAPL --pattern all
    python3 module_candle.py --symbol BTC-USD --interval 1h --pattern hammer

วิธี import:
    from module_candle import check_candle_pattern, fetch_candles, detect_all_patterns
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

try:
    import yfinance as yf
except ImportError:
    os.system("pip install yfinance --quiet --break-system-packages")
    import yfinance as yf

# ─── คำอธิบาย Patterns ───────────────────────────────────────────────────────
# Doji         = ราคาเปิด≈ปิด → ความลังเล market อาจกลับตัว
# Hammer       = ไส้ล่างยาว body เล็ก → Bullish reversal จากขาลง
# Shooting Star = ไส้บนยาว body เล็ก → Bearish reversal จากขาขึ้น
# Engulfing    = แท่งใหม่กลืนกินแท่งก่อน → สัญญาณ reversal แรง
# 3 Consecutive = 3 แท่งสีเดียวกันติดกัน → momentum แรง
# Marubozu     = body ยาว ไม่มีไส้ → momentum บริสุทธิ์
# Spinning Top = body เล็ก ไส้ยาวทั้งสองด้าน → ความลังเล
# ─────────────────────────────────────────────────────────────────────────────

CANDLE_DESCRIPTIONS_TH = {
    "doji": "Doji — ราคาเปิดเกือบเท่าปิด ตลาดลังเล อาจกลับตัว",
    "hammer": "Hammer 🔨 — ไส้ล่างยาว สัญญาณกลับตัวขึ้นจากขาลง",
    "inverted_hammer": "Inverted Hammer — ไส้บนยาว กลับตัวขึ้นที่ก้นตลาด",
    "shooting_star": "Shooting Star ⭐ — ไส้บนยาว สัญญาณกลับตัวลงจากขาขึ้น",
    "hanging_man": "Hanging Man — คล้าย Hammer แต่อยู่ปลายขาขึ้น → ระวัง",
    "bullish_engulfing": "Bullish Engulfing 🟢 — แท่งเขียวกลืนแท่งแดง → กลับตัวขึ้น",
    "bearish_engulfing": "Bearish Engulfing 🔴 — แท่งแดงกลืนแท่งเขียว → กลับตัวลง",
    "three_white_soldiers": "Three White Soldiers 🎖️ — 3 แท่งเขียวติดกัน momentum ขาขึ้นแรง",
    "three_black_crows": "Three Black Crows 🪶 — 3 แท่งแดงติดกัน momentum ขาลงแรง",
    "marubozu_bullish": "Bullish Marubozu 💚 — แท่งเขียวไม่มีไส้ ซื้อแรงมาก",
    "marubozu_bearish": "Bearish Marubozu 💔 — แท่งแดงไม่มีไส้ ขายแรงมาก",
    "spinning_top": "Spinning Top — body เล็ก ไส้ยาวสองด้าน ตลาดไม่แน่ใจ",
    "morning_star": "Morning Star 🌅 — 3 แท่ง: แดง-เล็ก-เขียว → กลับตัวขึ้น",
    "evening_star": "Evening Star 🌆 — 3 แท่ง: เขียว-เล็ก-แดง → กลับตัวลง",
}


def fetch_candles(symbol: str, interval: str = "1d", count: int = 10) -> list[dict] | None:
    """
    ดึงข้อมูลแท่งเทียนล่าสุด

    Returns:
        list of candles: [{open, high, low, close, volume, body_pct, upper_wick, lower_wick, is_bullish}]
    """
    lookback_map = {
        "1m": "5d", "5m": "5d", "15m": "30d", "30m": "60d",
        "1h": "60d", "4h": "60d", "1d": "90d", "1wk": "2y",
    }
    fetch_period = lookback_map.get(interval, "90d")
    fetch_count = max(count, 15)

    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period=fetch_period, interval=interval)

        if hist.empty or len(hist) < 4:
            print(f"  [{symbol}] ข้อมูลแท่งเทียนไม่พอ")
            return None

        hist = hist.tail(fetch_count)
        candles = []

        for i, (_, row) in enumerate(hist.iterrows()):
            o = float(row["Open"])
            h = float(row["High"])
            l = float(row["Low"])
            c = float(row["Close"])
            v = float(row["Volume"])
            rng = h - l if h != l else 0.0001

            body = abs(c - o)
            upper_wick = h - max(o, c)
            lower_wick = min(o, c) - l

            candles.append({
                "open": o, "high": h, "low": l, "close": c, "volume": v,
                "body": body,
                "body_pct": body / rng * 100 if rng > 0 else 0,
                "upper_wick": upper_wick,
                "upper_wick_pct": upper_wick / rng * 100 if rng > 0 else 0,
                "lower_wick": lower_wick,
                "lower_wick_pct": lower_wick / rng * 100 if rng > 0 else 0,
                "range": rng,
                "is_bullish": c >= o,
                "change_pct": ((c - o) / o) * 100 if o != 0 else 0,
            })

        return candles

    except Exception as e:
        print(f"  [{symbol}] Candle fetch error: {e}")
        return None


def detect_all_patterns(candles: list[dict]) -> dict[str, bool]:
    """
    ตรวจจับ pattern ทั้งหมดจากแท่งเทียนล่าสุด

    Returns:
        dict: {pattern_name: True/False}
    """
    if len(candles) < 3:
        return {}

    c0 = candles[-1]   # แท่งปัจจุบัน
    c1 = candles[-2]   # แท่งก่อน
    c2 = candles[-3]   # แท่งก่อนก่อน

    patterns = {}

    # ─── 1 แท่ง ───────────────────────────────────────────────────────
    # Doji: body < 10% ของ range
    patterns["doji"] = c0["body_pct"] < 10

    # Spinning Top: body 10-30%, ไส้ยาวทั้งสองด้าน
    patterns["spinning_top"] = (
        10 <= c0["body_pct"] <= 30 and
        c0["upper_wick_pct"] >= 20 and
        c0["lower_wick_pct"] >= 20
    )

    # Hammer: ไส้ล่าง >= 2x body, ไส้บน < 20%, body > 10%
    patterns["hammer"] = (
        c0["lower_wick"] >= c0["body"] * 2 and
        c0["upper_wick_pct"] < 20 and
        c0["body_pct"] >= 10 and
        not c0["is_bullish"]  # มักเป็นแดงในช่วงขาลง
    )

    # Inverted Hammer: ไส้บน >= 2x body, ไส้ล่าง < 20%
    patterns["inverted_hammer"] = (
        c0["upper_wick"] >= c0["body"] * 2 and
        c0["lower_wick_pct"] < 20 and
        c0["body_pct"] >= 10
    )

    # Shooting Star: เหมือน Inverted Hammer แต่เกิดที่ขาขึ้น (แท่งก่อนขึ้น)
    patterns["shooting_star"] = (
        c0["upper_wick"] >= c0["body"] * 2 and
        c0["lower_wick_pct"] < 20 and
        c0["body_pct"] >= 10 and
        c1["is_bullish"]  # แท่งก่อนขึ้น
    )

    # Hanging Man: เหมือน Hammer แต่เกิดที่ขาขึ้น
    patterns["hanging_man"] = (
        c0["lower_wick"] >= c0["body"] * 2 and
        c0["upper_wick_pct"] < 20 and
        c0["body_pct"] >= 10 and
        c1["is_bullish"]
    )

    # Marubozu Bullish: แท่งเขียว body > 90% ของ range
    patterns["marubozu_bullish"] = c0["is_bullish"] and c0["body_pct"] >= 90

    # Marubozu Bearish: แท่งแดง body > 90%
    patterns["marubozu_bearish"] = not c0["is_bullish"] and c0["body_pct"] >= 90

    # ─── 2 แท่ง ───────────────────────────────────────────────────────
    # Bullish Engulfing: แดง→เขียวใหญ่กว่า
    patterns["bullish_engulfing"] = (
        not c1["is_bullish"] and
        c0["is_bullish"] and
        c0["open"] < c1["close"] and
        c0["close"] > c1["open"] and
        c0["body"] > c1["body"]
    )

    # Bearish Engulfing: เขียว→แดงใหญ่กว่า
    patterns["bearish_engulfing"] = (
        c1["is_bullish"] and
        not c0["is_bullish"] and
        c0["open"] > c1["close"] and
        c0["close"] < c1["open"] and
        c0["body"] > c1["body"]
    )

    # ─── 3 แท่ง ───────────────────────────────────────────────────────
    # Three White Soldiers: 3 เขียวติดกัน แต่ละแท่งปิดสูงกว่าก่อน
    patterns["three_white_soldiers"] = (
        c2["is_bullish"] and c1["is_bullish"] and c0["is_bullish"] and
        c1["close"] > c2["close"] and
        c0["close"] > c1["close"] and
        c2["body_pct"] >= 50 and c1["body_pct"] >= 50 and c0["body_pct"] >= 50
    )

    # Three Black Crows: 3 แดงติดกัน
    patterns["three_black_crows"] = (
        not c2["is_bullish"] and not c1["is_bullish"] and not c0["is_bullish"] and
        c1["close"] < c2["close"] and
        c0["close"] < c1["close"] and
        c2["body_pct"] >= 50 and c1["body_pct"] >= 50 and c0["body_pct"] >= 50
    )

    # Morning Star: แดง(ใหญ่) + doji/เล็ก + เขียว(ใหญ่)
    patterns["morning_star"] = (
        not c2["is_bullish"] and c2["body_pct"] >= 50 and
        c1["body_pct"] <= 30 and  # แท่งกลางเล็ก
        c0["is_bullish"] and c0["body_pct"] >= 50 and
        c0["close"] > (c2["open"] + c2["close"]) / 2  # ปิดเกิน 50% ของแท่งแรก
    )

    # Evening Star: เขียว(ใหญ่) + doji/เล็ก + แดง(ใหญ่)
    patterns["evening_star"] = (
        c2["is_bullish"] and c2["body_pct"] >= 50 and
        c1["body_pct"] <= 30 and
        not c0["is_bullish"] and c0["body_pct"] >= 50 and
        c0["close"] < (c2["open"] + c2["close"]) / 2
    )

    return patterns


def check_candle_pattern(alert: dict, candles: list[dict]) -> tuple[bool, list[str]]:
    """
    เช็กเงื่อนไข candle pattern alert

    alert fields:
        type: "candle_pattern"
        patterns: list ของ pattern ที่ต้องการ เช่น ["hammer", "bullish_engulfing"]
                  หรือ "all" เพื่อ detect ทุก pattern
        match_any: True = trigger ถ้าพบ pattern ใดๆ (default=True)
                   False = trigger ต่อเมื่อพบทุก pattern ที่กำหนด
        interval: timeframe
        count: จำนวนแท่งที่ต้องการ (default=10)

    Returns:
        (triggered: bool, found_patterns: list[str])
    """
    target_patterns = alert.get("patterns", ["hammer", "bullish_engulfing", "morning_star"])
    match_any = alert.get("match_any", True)

    all_patterns = detect_all_patterns(candles)
    found = [p for p in target_patterns if all_patterns.get(p, False)]

    if match_any:
        triggered = len(found) > 0
    else:
        triggered = len(found) == len(target_patterns)

    return triggered, found


def build_candle_message(symbol: str, name: str, candles: list[dict], found_patterns: list[str], alert: dict) -> str:
    """สร้าง Telegram message สำหรับ candle pattern alert"""
    note = alert.get("note", "")
    action = alert.get("action", "")
    emoji = alert.get("emoji", "🕯️")
    c0 = candles[-1]
    price = c0["close"]
    change = c0["change_pct"]
    arrow = "📈" if change >= 0 else "📉"
    sign = "+" if change >= 0 else ""

    # รวม description ภาษาไทย
    pattern_lines = []
    for p in found_patterns:
        desc = CANDLE_DESCRIPTIONS_TH.get(p, p)
        pattern_lines.append(f"  • {desc}")

    pattern_text = "\n".join(pattern_lines) if pattern_lines else "  • ไม่พบ pattern"

    lines = [
        f"{emoji} <b>CANDLE ALERT: {symbol}</b> ({name})",
        "",
        f"🕯️ Pattern ที่พบ:",
        pattern_text,
        "",
        f"💰 ราคา: <b>${price:.4f}</b>  {arrow} {sign}{change:.2f}%",
        f"📊 แท่งปัจจุบัน: Body={c0['body_pct']:.0f}%  ↑Wick={c0['upper_wick_pct']:.0f}%  ↓Wick={c0['lower_wick_pct']:.0f}%",
        f"⏱ Timeframe: {alert.get('interval','1d')}",
    ]

    if action:
        lines.append(f"🎯 Action: <b>{action}</b>")
    if note:
        lines.append(f"📋 Note: {note}")

    lines.extend([
        "",
        f"📊 <a href='https://www.tradingview.com/chart/?symbol={symbol}'>ดูบน TradingView</a>",
        f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
    ])

    return "\n".join(lines)


# ─── Standalone Runner ────────────────────────────────────────────────────────

def main():
    all_pattern_names = list(CANDLE_DESCRIPTIONS_TH.keys())

    parser = argparse.ArgumentParser(
        description="ตรวจจับรูปแบบแท่งเทียน (Candlestick Patterns)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Patterns ที่รองรับ:
  {chr(10).join(f'  {k}: {v}' for k, v in CANDLE_DESCRIPTIONS_TH.items())}

ตัวอย่าง:
  python3 module_candle.py --symbol AAPL --pattern all
  python3 module_candle.py --symbol BTC-USD --pattern hammer --interval 1h
  python3 module_candle.py --symbol TSLA --pattern bullish_engulfing three_white_soldiers
        """
    )
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--pattern", nargs="+", default=["all"],
                        help="Pattern ที่ต้องการ หรือ 'all' เพื่อดูทุก pattern")
    parser.add_argument("--interval", default="1d",
                        choices=["1m","5m","15m","30m","1h","4h","1d","1wk"])
    parser.add_argument("--count", type=int, default=10, help="จำนวนแท่งย้อนหลัง")
    parser.add_argument("--json", action="store_true")

    args = parser.parse_args()
    target = all_pattern_names if "all" in args.pattern else args.pattern

    print(f"\n🕯️ กำลังวิเคราะห์ Candlestick Pattern สำหรับ {args.symbol} ({args.interval})...")
    candles = fetch_candles(args.symbol, interval=args.interval, count=args.count)

    if not candles:
        print("❌ ดึงข้อมูลไม่สำเร็จ")
        sys.exit(1)

    all_p = detect_all_patterns(candles)

    if args.json:
        print(json.dumps({"candles": candles[-3:], "patterns": all_p}, ensure_ascii=False, indent=2))
        return

    print(f"\n{'='*60}")
    print(f"  Symbol: {args.symbol}  |  แท่งปัจจุบัน: {'🟢' if candles[-1]['is_bullish'] else '🔴'}")
    print(f"  O={candles[-1]['open']:.4f}  H={candles[-1]['high']:.4f}  L={candles[-1]['low']:.4f}  C={candles[-1]['close']:.4f}")
    print(f"{'='*60}")
    print("\n  Pattern Detection:")
    for p in target:
        found = all_p.get(p, False)
        icon = "✅" if found else "⬜"
        desc = CANDLE_DESCRIPTIONS_TH.get(p, p)
        print(f"  {icon} {p}: {desc}")
    print()


if __name__ == "__main__":
    main()
