#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════╗
║  MODULE 6: Multi-Timeframe (MTF) — ยืนยันสัญญาณหลาย Timeframe ║
║  ป้องกัน false signal จาก intraday noise                        ║
╚══════════════════════════════════════════════════════════════════╝

วิธีใช้ standalone:
    python3 module_mtf.py --symbol AAPL
    python3 module_mtf.py --symbol BTC-USD --primary 1h --confirm 4h 1d
    python3 module_mtf.py --symbol TSLA --min-alignment 2

วิธี import:
    from module_mtf import check_mtf_alignment, fetch_mtf_data
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
# MTF Analysis = ดู trend จากหลาย timeframe พร้อมกัน
#
# หลักการ: "Trade in the direction of the bigger timeframe"
# - เปิด trade เฉพาะเมื่อ timeframe ใหญ่กว่า Align กับทิศทางที่ต้องการ
# - ตัวอย่าง: สัญญาณ BUY บน 15m ต้องการให้ 1h และ 4h ก็เป็น Bullish
#
# Trend วิเคราะห์จาก:
#   - ราคาอยู่เหนือ/ใต้ EMA21
#   - EMA21 อยู่เหนือ/ใต้ EMA50
#   - RSI ฝั่ง Bullish/Bearish (>50 / <50)
#
# Score ยิ่งสูง = ยิ่ง Align = ยิ่งน่าเชื่อถือ
# ─────────────────────────────────────────────────────────────────────────────

TIMEFRAME_LABELS_TH = {
    "1m": "1 นาที", "5m": "5 นาที", "15m": "15 นาที", "30m": "30 นาที",
    "1h": "1 ชั่วโมง", "4h": "4 ชั่วโมง", "1d": "รายวัน", "1wk": "รายสัปดาห์",
}

TIMEFRAME_HIERARCHY = ["1m", "5m", "15m", "30m", "1h", "4h", "1d", "1wk"]


def calculate_ema(prices: list[float], period: int) -> float | None:
    """คำนวณ EMA ล่าสุด"""
    if len(prices) < period:
        return None
    ema = sum(prices[:period]) / period
    k = 2 / (period + 1)
    for p in prices[period:]:
        ema = p * k + ema * (1 - k)
    return ema


def calculate_rsi_simple(closes: list[float], period: int = 14) -> float | None:
    """คำนวณ RSI แบบเร็ว"""
    if len(closes) < period + 1:
        return None
    gains = [max(closes[i] - closes[i-1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i-1] - closes[i], 0) for i in range(1, len(closes))]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period-1) + gains[i]) / period
        avg_loss = (avg_loss * (period-1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    return round(100 - 100 / (1 + avg_gain / avg_loss), 2)


def analyze_single_timeframe(symbol: str, interval: str) -> dict | None:
    """
    วิเคราะห์ trend ของ timeframe เดียว

    Returns:
        dict: {trend, score, price, ema21, ema50, rsi, signals}
    """
    lookback_map = {
        "1m": "5d", "5m": "5d", "15m": "30d", "30m": "60d",
        "1h": "60d", "4h": "60d", "1d": "180d", "1wk": "3y",
    }
    fetch_period = lookback_map.get(interval, "90d")

    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period=fetch_period, interval=interval)

        if hist.empty or len(hist) < 55:
            return None

        closes = list(hist["Close"].astype(float))
        price = closes[-1]

        ema21 = calculate_ema(closes, 21)
        ema50 = calculate_ema(closes, 50)
        rsi = calculate_rsi_simple(closes)

        if ema21 is None or ema50 is None:
            return None

        # Scoring: แต่ละเงื่อนไขได้ 1 คะแนน Bullish (+1) หรือ Bearish (-1)
        signals = {}
        score = 0

        # 1. ราคา vs EMA21
        if price > ema21:
            signals["price_vs_ema21"] = {"value": "เหนือ EMA21", "bullish": True}
            score += 1
        else:
            signals["price_vs_ema21"] = {"value": "ใต้ EMA21", "bullish": False}
            score -= 1

        # 2. EMA21 vs EMA50 (trend direction)
        if ema21 > ema50:
            signals["ema_alignment"] = {"value": "EMA21 > EMA50 (Bullish Alignment)", "bullish": True}
            score += 1
        else:
            signals["ema_alignment"] = {"value": "EMA21 < EMA50 (Bearish Alignment)", "bullish": False}
            score -= 1

        # 3. RSI ฝั่ง Bullish/Bearish
        if rsi is not None:
            if rsi > 50:
                signals["rsi"] = {"value": f"RSI {rsi:.1f} > 50 (Bullish momentum)", "bullish": True}
                score += 1
            else:
                signals["rsi"] = {"value": f"RSI {rsi:.1f} < 50 (Bearish momentum)", "bullish": False}
                score -= 1

        # 4. Price momentum (ปิดสูงกว่า 5 แท่งก่อน)
        if len(closes) >= 6:
            price_5ago = closes[-6]
            if price > price_5ago:
                signals["momentum"] = {"value": "ราคาสูงกว่า 5 แท่งก่อน", "bullish": True}
                score += 1
            else:
                signals["momentum"] = {"value": "ราคาต่ำกว่า 5 แท่งก่อน", "bullish": False}
                score -= 1

        # สรุป trend
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

        return {
            "interval": interval,
            "interval_label": TIMEFRAME_LABELS_TH.get(interval, interval),
            "trend": trend,
            "score": score,
            "max_score": 4,
            "price": round(price, 4),
            "ema21": round(ema21, 4),
            "ema50": round(ema50, 4),
            "rsi": rsi,
            "signals": signals,
        }

    except Exception as e:
        print(f"  [{symbol}][{interval}] MTF error: {e}")
        return None


def fetch_mtf_data(
    symbol: str,
    timeframes: list[str] | None = None,
) -> dict:
    """
    ดึงและวิเคราะห์ MTF data สำหรับหลาย timeframe

    Args:
        symbol: Ticker
        timeframes: list ของ timeframe เช่น ["15m", "1h", "4h", "1d"]

    Returns:
        dict: {timeframes: dict, overall_alignment, bullish_count, bearish_count}
    """
    if timeframes is None:
        timeframes = ["1h", "4h", "1d"]

    results = {}
    for tf in timeframes:
        print(f"  [{symbol}][{tf}] วิเคราะห์ trend...")
        data = analyze_single_timeframe(symbol, tf)
        if data:
            results[tf] = data

    if not results:
        return {"timeframes": {}, "overall_alignment": "unknown", "bullish_count": 0, "bearish_count": 0}

    bullish_count = sum(1 for d in results.values() if "bullish" in d["trend"])
    bearish_count = sum(1 for d in results.values() if "bearish" in d["trend"])
    neutral_count = len(results) - bullish_count - bearish_count

    total = len(results)
    if bullish_count == total:
        alignment = "strong_bullish_all"
    elif bullish_count >= total * 0.75:
        alignment = "mostly_bullish"
    elif bearish_count == total:
        alignment = "strong_bearish_all"
    elif bearish_count >= total * 0.75:
        alignment = "mostly_bearish"
    elif bullish_count > bearish_count:
        alignment = "leaning_bullish"
    elif bearish_count > bullish_count:
        alignment = "leaning_bearish"
    else:
        alignment = "mixed"

    return {
        "symbol": symbol,
        "timeframes": results,
        "overall_alignment": alignment,
        "bullish_count": bullish_count,
        "bearish_count": bearish_count,
        "neutral_count": neutral_count,
        "total_tf": total,
    }


def check_mtf_alignment(alert: dict, mtf_data: dict) -> tuple[bool, str]:
    """
    เช็กเงื่อนไข MTF alignment alert

    alert fields:
        type: "mtf_alignment"
        required_alignment: "bullish" | "bearish" | "strong_bullish_all" | "strong_bearish_all"
                            | "mostly_bullish" | "mostly_bearish"
        min_bullish: จำนวน timeframe ขั้นต่ำที่ต้อง Bullish (default=2)
        min_bearish: จำนวน timeframe ขั้นต่ำที่ต้อง Bearish (default=2)
        timeframes: list ของ timeframe ที่ต้องการ
        interval: primary timeframe

    Returns:
        (triggered: bool, alignment_description: str)
    """
    condition = alert.get("required_alignment", "mostly_bullish")
    min_bullish = alert.get("min_bullish", 2)
    min_bearish = alert.get("min_bearish", 2)
    alignment = mtf_data.get("overall_alignment", "mixed")

    if condition in ("bullish", "mostly_bullish", "leaning_bullish"):
        triggered = mtf_data.get("bullish_count", 0) >= min_bullish
    elif condition in ("bearish", "mostly_bearish", "leaning_bearish"):
        triggered = mtf_data.get("bearish_count", 0) >= min_bearish
    elif condition == "strong_bullish_all":
        triggered = alignment == "strong_bullish_all"
    elif condition == "strong_bearish_all":
        triggered = alignment == "strong_bearish_all"
    else:
        triggered = alignment == condition

    return triggered, alignment


def build_mtf_message(symbol: str, name: str, mtf_data: dict, alert: dict) -> str:
    """สร้าง Telegram message สำหรับ MTF alignment alert"""
    emoji = alert.get("emoji", "📡")
    note = alert.get("note", "")
    alignment = mtf_data.get("overall_alignment", "mixed")

    alignment_th = {
        "strong_bullish_all": "🟢🟢🟢 ทุก TF Bullish — สัญญาณแข็งแกร่งมาก!",
        "mostly_bullish": "🟢🟢 ส่วนใหญ่ Bullish — แนวโน้มขาขึ้น",
        "leaning_bullish": "🟡🟢 เอนไปทาง Bullish",
        "strong_bearish_all": "🔴🔴🔴 ทุก TF Bearish — ระวังขาลง!",
        "mostly_bearish": "🔴🔴 ส่วนใหญ่ Bearish — แนวโน้มขาลง",
        "leaning_bearish": "🟡🔴 เอนไปทาง Bearish",
        "mixed": "⚪ Mixed — ทิศทางไม่ชัดเจน",
        "unknown": "❓ ไม่มีข้อมูล",
    }

    trend_icon = {
        "strong_bullish": "🟢🟢", "bullish": "🟢", "neutral": "⚪",
        "bearish": "🔴", "strong_bearish": "🔴🔴",
    }

    lines = [
        f"{emoji} <b>MTF ALERT: {symbol}</b> ({name})",
        "",
        f"📡 Alignment: <b>{alignment_th.get(alignment, alignment)}</b>",
        f"📊 Bullish TF: {mtf_data.get('bullish_count',0)}/{mtf_data.get('total_tf',0)}  |  Bearish: {mtf_data.get('bearish_count',0)}/{mtf_data.get('total_tf',0)}",
        "",
        "⏱ รายละเอียดแต่ละ Timeframe:",
    ]

    for tf, data in mtf_data.get("timeframes", {}).items():
        ti = trend_icon.get(data["trend"], "⚪")
        score_bar = "█" * max(0, data["score"]) + "░" * max(0, -data["score"])
        lines.append(
            f"  {ti} <b>{data['interval_label']}</b>: {data['trend'].replace('_',' ').upper()}"
            f"  RSI={data['rsi']:.0f}"
        )

    if note:
        lines.append(f"\n📋 Note: {note}")

    lines.extend([
        "",
        f"📊 <a href='https://www.tradingview.com/chart/?symbol={symbol}'>ดูบน TradingView</a>",
        f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
    ])

    return "\n".join(lines)


# ─── Standalone Runner ────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="วิเคราะห์ Multi-Timeframe trend alignment",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
ตัวอย่าง:
  python3 module_mtf.py --symbol AAPL
  python3 module_mtf.py --symbol BTC-USD --timeframes 15m 1h 4h 1d
  python3 module_mtf.py --symbol TSLA --alignment mostly_bullish --min-bullish 3
        """
    )
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--timeframes", nargs="+", default=["1h","4h","1d"],
                        help="Timeframes ที่ต้องการ (default: 1h 4h 1d)")
    parser.add_argument("--alignment", default="mostly_bullish",
                        choices=["bullish","bearish","mostly_bullish","mostly_bearish",
                                 "strong_bullish_all","strong_bearish_all"])
    parser.add_argument("--min-bullish", type=int, default=2)
    parser.add_argument("--min-bearish", type=int, default=2)
    parser.add_argument("--json", action="store_true")

    args = parser.parse_args()

    print(f"\n📡 กำลังวิเคราะห์ MTF สำหรับ {args.symbol}...")
    print(f"   Timeframes: {', '.join(args.timeframes)}")

    mtf_data = fetch_mtf_data(args.symbol, timeframes=args.timeframes)

    if args.json:
        print(json.dumps(mtf_data, ensure_ascii=False, indent=2))
        return

    print(f"\n{'='*65}")
    print(f"  Symbol: {args.symbol}  |  Alignment: {mtf_data['overall_alignment'].upper()}")
    print(f"  Bullish: {mtf_data['bullish_count']}/{mtf_data['total_tf']}  |  Bearish: {mtf_data['bearish_count']}/{mtf_data['total_tf']}")
    print(f"{'='*65}")

    trend_icons = {"strong_bullish":"🟢🟢","bullish":"🟢","neutral":"⚪","bearish":"🔴","strong_bearish":"🔴🔴"}

    for tf, data in mtf_data["timeframes"].items():
        ti = trend_icons.get(data["trend"], "⚪")
        print(f"\n  {ti} [{tf}] {data['trend'].upper()}  (score: {data['score']:+d}/{data['max_score']})")
        print(f"     Price={data['price']:.4f}  EMA21={data['ema21']:.4f}  EMA50={data['ema50']:.4f}  RSI={data['rsi']:.1f}")
        for sig, info in data["signals"].items():
            icon = "✅" if info["bullish"] else "❌"
            print(f"     {icon} {sig}: {info['value']}")

    alert = {"required_alignment": args.alignment, "min_bullish": args.min_bullish, "min_bearish": args.min_bearish}
    triggered, desc = check_mtf_alignment(alert, mtf_data)
    print(f"\n{'='*65}")
    print(f"  เงื่อนไข [{args.alignment}]: {'✅ TRIGGERED' if triggered else '⬜ Not triggered'}")
    print()


if __name__ == "__main__":
    main()
