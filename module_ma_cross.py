#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════╗
║  MODULE 2: MA Crossover — EMA/SMA Golden Cross / Death Cross    ║
║  ตรวจจับ Moving Average crossover สัญญาณ trend เปลี่ยนทิศ     ║
╚══════════════════════════════════════════════════════════════════╝

วิธีใช้ standalone:
    python3 module_ma_cross.py --symbol AAPL --fast 9 --slow 21 --ma-type EMA
    python3 module_ma_cross.py --symbol BTC-USD --fast 20 --slow 50 --interval 1h

วิธี import:
    from module_ma_cross import check_ma_cross, fetch_ma_data
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

# ─── คำอธิบาย ────────────────────────────────────────────────────────────────
# Golden Cross = MA เร็วข้าม MA ช้าขึ้น → สัญญาณ Bullish (เริ่มขาขึ้น)
# Death Cross  = MA เร็วข้าม MA ช้าลง  → สัญญาณ Bearish (เริ่มขาลง)
#
# คู่ MA ยอดนิยม:
#   EMA 9/21  — Scalping / Intraday (ไว)
#   EMA 20/50 — Swing Trading (กลาง)
#   SMA 50/200 — Position Trading (ช้า แต่แม่น)
#   EMA 12/26 — MACD basis
# ─────────────────────────────────────────────────────────────────────────────


def calculate_sma(prices: list[float], period: int) -> list[float]:
    """คำนวณ Simple Moving Average"""
    smas = []
    for i in range(len(prices)):
        if i < period - 1:
            smas.append(None)
        else:
            smas.append(sum(prices[i - period + 1: i + 1]) / period)
    return smas


def calculate_ema(prices: list[float], period: int) -> list[float]:
    """คำนวณ Exponential Moving Average (Wilder's EMA)"""
    emas = [None] * (period - 1)
    # Seed: ค่าแรกใช้ SMA
    seed = sum(prices[:period]) / period
    emas.append(seed)
    k = 2 / (period + 1)
    for price in prices[period:]:
        emas.append(price * k + emas[-1] * (1 - k))
    return emas


def fetch_ma_data(
    symbol: str,
    fast_period: int = 9,
    slow_period: int = 21,
    ma_type: str = "EMA",
    interval: str = "1d",
) -> dict | None:
    """
    ดึงข้อมูลและคำนวณ MA crossover

    Args:
        symbol: Ticker
        fast_period: ช่วง MA เร็ว (default=9)
        slow_period: ช่วง MA ช้า (default=21)
        ma_type: "EMA" หรือ "SMA"
        interval: timeframe

    Returns:
        dict: ข้อมูล MA และสัญญาณ cross
    """
    # ต้องการข้อมูลอย่างน้อย slow_period * 3
    lookback_map = {
        "1m": "5d", "5m": "5d", "15m": "30d", "30m": "60d",
        "1h": "60d", "4h": "60d", "1d": "180d", "1wk": "3y",
    }
    fetch_period = lookback_map.get(interval, "180d")
    min_bars = slow_period * 3

    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period=fetch_period, interval=interval)

        if hist.empty or len(hist) < min_bars:
            print(f"  [{symbol}] ข้อมูลไม่พอ (ต้องการ {min_bars} แท่ง ได้ {len(hist)})")
            return None

        closes = list(hist["Close"].astype(float))
        ma_func = calculate_ema if ma_type.upper() == "EMA" else calculate_sma

        fast_ma = ma_func(closes, fast_period)
        slow_ma = ma_func(closes, slow_period)

        # ดึงค่า 3 จุดล่าสุดที่ทั้งคู่มีค่า
        valid_pairs = [
            (f, s) for f, s in zip(fast_ma, slow_ma)
            if f is not None and s is not None
        ]

        if len(valid_pairs) < 2:
            return None

        curr_fast, curr_slow = valid_pairs[-1]
        prev_fast, prev_slow = valid_pairs[-2]

        # คำนวณ gap % (ระยะห่างระหว่าง MA)
        gap_pct = ((curr_fast - curr_slow) / curr_slow) * 100 if curr_slow != 0 else 0

        # ตรวจ cross
        golden_cross = prev_fast <= prev_slow and curr_fast > curr_slow
        death_cross = prev_fast >= prev_slow and curr_fast < curr_slow

        # ตรวจ trend (fast อยู่เหนือ/ใต้ slow)
        trend = "bullish" if curr_fast > curr_slow else "bearish"

        # Momentum: fast MA ขึ้น/ลง
        fast_prev2, fast_prev1 = valid_pairs[-3][0] if len(valid_pairs) >= 3 else prev_fast, prev_fast
        fast_accelerating = curr_fast > prev_fast > fast_prev2

        return {
            "symbol": symbol,
            "ma_type": ma_type.upper(),
            "fast_period": fast_period,
            "slow_period": slow_period,
            "fast_ma": round(curr_fast, 4),
            "slow_ma": round(curr_slow, 4),
            "prev_fast_ma": round(prev_fast, 4),
            "prev_slow_ma": round(prev_slow, 4),
            "gap_pct": round(gap_pct, 3),
            "golden_cross": golden_cross,
            "death_cross": death_cross,
            "trend": trend,
            "fast_accelerating": fast_accelerating,
            "price": round(closes[-1], 4),
            "interval": interval,
        }

    except Exception as e:
        print(f"  [{symbol}] MA fetch error: {e}")
        return None


def check_ma_cross(alert: dict, ma_data: dict) -> tuple[bool, str]:
    """
    เช็กเงื่อนไข MA crossover alert

    alert fields:
        type: "ma_crossover"
        condition: "golden_cross" | "death_cross" | "above_both" | "below_both"
                   | "gap_expanding" | "trend_bullish" | "trend_bearish"
        fast_period: ช่วง MA เร็ว (default=9)
        slow_period: ช่วง MA ช้า (default=21)
        ma_type: "EMA" หรือ "SMA"
        interval: timeframe (default="1d")

    Returns:
        (triggered: bool, description: str)
    """
    condition = alert.get("condition", "golden_cross")

    if condition == "golden_cross":
        triggered = ma_data["golden_cross"]
    elif condition == "death_cross":
        triggered = ma_data["death_cross"]
    elif condition == "above_both":
        triggered = ma_data["price"] > ma_data["fast_ma"] and ma_data["price"] > ma_data["slow_ma"]
    elif condition == "below_both":
        triggered = ma_data["price"] < ma_data["fast_ma"] and ma_data["price"] < ma_data["slow_ma"]
    elif condition == "trend_bullish":
        triggered = ma_data["trend"] == "bullish"
    elif condition == "trend_bearish":
        triggered = ma_data["trend"] == "bearish"
    elif condition == "gap_expanding":
        curr_gap = abs(ma_data["gap_pct"])
        prev_gap = abs(((ma_data["prev_fast_ma"] - ma_data["prev_slow_ma"]) / ma_data["prev_slow_ma"]) * 100)
        triggered = curr_gap > prev_gap
    else:
        triggered = False

    desc = f"{ma_data['ma_type']}{ma_data['fast_period']}/{ma_data['slow_period']}"
    return triggered, desc


def build_ma_message(symbol: str, name: str, ma_data: dict, alert: dict) -> str:
    """สร้าง Telegram message สำหรับ MA Crossover alert"""
    condition = alert.get("condition", "golden_cross")
    note = alert.get("note", "")
    action = alert.get("action", "")

    emoji_map = {
        "golden_cross": "🌟",
        "death_cross": "💀",
        "above_both": "🚀",
        "below_both": "🔻",
        "trend_bullish": "📈",
        "trend_bearish": "📉",
        "gap_expanding": "↔️",
    }
    emoji = alert.get("emoji", emoji_map.get(condition, "🔔"))

    condition_th = {
        "golden_cross": "Golden Cross — สัญญาณขาขึ้น",
        "death_cross": "Death Cross — สัญญาณขาลง",
        "above_both": "ราคาอยู่เหนือ MA ทั้งคู่",
        "below_both": "ราคาต่ำกว่า MA ทั้งคู่",
        "trend_bullish": "Trend กำลังขาขึ้น",
        "trend_bearish": "Trend กำลังขาลง",
        "gap_expanding": "ช่องว่าง MA กำลังขยาย",
    }
    cond_desc = condition_th.get(condition, condition)

    ma_label = f"{ma_data['ma_type']}{ma_data['fast_period']}/{ma_data['slow_period']}"
    trend_emoji = "🟢" if ma_data["trend"] == "bullish" else "🔴"

    lines = [
        f"{emoji} <b>MA CROSS ALERT: {symbol}</b> ({name})",
        "",
        f"⚡ สัญญาณ: <b>{cond_desc}</b>",
        f"💰 ราคา: <b>${ma_data['price']:.4f}</b>",
        f"📊 {ma_label}:",
        f"  • Fast MA{ma_data['fast_period']}: <b>${ma_data['fast_ma']:.4f}</b>",
        f"  • Slow MA{ma_data['slow_period']}: <b>${ma_data['slow_ma']:.4f}</b>",
        f"  • Gap: <b>{ma_data['gap_pct']:+.2f}%</b>",
        f"{trend_emoji} Trend: <b>{ma_data['trend'].upper()}</b>",
        f"⏱ Timeframe: {ma_data['interval']}",
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
    parser = argparse.ArgumentParser(
        description="ตรวจสอบ MA Crossover ของหุ้น/crypto",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
ตัวอย่าง:
  python3 module_ma_cross.py --symbol AAPL
  python3 module_ma_cross.py --symbol BTC-USD --fast 12 --slow 26 --ma-type EMA --interval 1h
  python3 module_ma_cross.py --symbol TSLA --fast 50 --slow 200 --ma-type SMA
        """
    )
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--fast", type=int, default=9, help="Fast MA period (default: 9)")
    parser.add_argument("--slow", type=int, default=21, help="Slow MA period (default: 21)")
    parser.add_argument("--ma-type", default="EMA", choices=["EMA","SMA"])
    parser.add_argument("--interval", default="1d",
                        choices=["1m","5m","15m","30m","1h","4h","1d","1wk"])
    parser.add_argument("--condition", default="golden_cross",
                        choices=["golden_cross","death_cross","above_both","below_both",
                                 "trend_bullish","trend_bearish","gap_expanding"])
    parser.add_argument("--json", action="store_true")

    args = parser.parse_args()

    print(f"\n🔍 กำลังคำนวณ {args.ma_type}{args.fast}/{args.slow} สำหรับ {args.symbol}...")
    ma_data = fetch_ma_data(
        args.symbol,
        fast_period=args.fast,
        slow_period=args.slow,
        ma_type=args.ma_type,
        interval=args.interval,
    )

    if not ma_data:
        print("❌ ดึงข้อมูลไม่สำเร็จ")
        sys.exit(1)

    if args.json:
        print(json.dumps(ma_data, ensure_ascii=False, indent=2))
        return

    trend_icon = "🟢 BULLISH" if ma_data["trend"] == "bullish" else "🔴 BEARISH"
    print(f"\n{'='*55}")
    print(f"  Symbol     : {args.symbol}")
    print(f"  Price      : ${ma_data['price']:.4f}")
    print(f"  {args.ma_type}{args.fast} (Fast)  : ${ma_data['fast_ma']:.4f}")
    print(f"  {args.ma_type}{args.slow} (Slow)  : ${ma_data['slow_ma']:.4f}")
    print(f"  Gap        : {ma_data['gap_pct']:+.3f}%")
    print(f"  Trend      : {trend_icon}")
    print(f"  Golden ✨  : {ma_data['golden_cross']}")
    print(f"  Death 💀   : {ma_data['death_cross']}")
    print(f"{'='*55}")

    alert = {"condition": args.condition}
    triggered, desc = check_ma_cross(alert, ma_data)
    status = "✅ TRIGGERED" if triggered else "⬜ Not triggered"
    print(f"\n  เงื่อนไข [{args.condition}]: {status}")
    print()


if __name__ == "__main__":
    main()
