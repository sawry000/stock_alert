#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════╗
║  MODULE 7: Alert Confidence Score — คะแนนความน่าเชื่อถือ alert  ║
║  รวมหลาย indicator มาให้คะแนน 0-100 ก่อนส่ง alert              ║
╚══════════════════════════════════════════════════════════════════╝

วิธีใช้ standalone:
    python3 module_score.py --symbol AAPL --direction bullish
    python3 module_score.py --symbol BTC-USD --direction bearish --min-score 70

วิธี import:
    from module_score import calculate_confidence_score, fetch_score_data
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
# Confidence Score รวมสัญญาณจากหลายแหล่ง:
#
# 1. RSI (15 คะแนน)       — Oversold/Overbought = โอกาสกลับตัว
# 2. MA Trend (20 คะแนน)  — ราคา vs EMA21, EMA21 vs EMA50
# 3. Volume (15 คะแนน)    — Volume สูงกว่า avg = confirmation
# 4. Momentum (15 คะแนน)  — Price change % และ direction
# 5. Volatility (10 คะแนน) — ATR เหมาะสม ไม่ volatile เกินไป
# 6. Support/Resistance (15 คะแนน) — ราคาใกล้แนวรับ/ต้าน
# 7. MTF Alignment (10 คะแนน) — timeframe ใหญ่กว่า align มั้ย
#
# Score 80-100: 🔥 สัญญาณแข็งมาก — น่าเชื่อถือสูง
# Score 65-79:  ✅ สัญญาณดี — ควรเทรด
# Score 50-64:  🟡 สัญญาณปานกลาง — เพิ่มความระมัดระวัง
# Score < 50:   ❌ สัญญาณอ่อน — ควรรอ
# ─────────────────────────────────────────────────────────────────────────────


def fetch_score_data(symbol: str, interval: str = "1d") -> dict | None:
    """
    ดึงข้อมูลทั้งหมดที่ต้องการสำหรับคำนวณ score

    Returns:
        dict: raw data สำหรับคำนวณ score
    """
    lookback_map = {
        "1m": "5d", "5m": "5d", "15m": "30d", "30m": "60d",
        "1h": "60d", "4h": "60d", "1d": "90d", "1wk": "2y",
    }
    fetch_period = lookback_map.get(interval, "90d")

    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period=fetch_period, interval=interval)

        if hist.empty or len(hist) < 50:
            print(f"  [{symbol}] ข้อมูลไม่พอสำหรับคำนวณ score")
            return None

        closes = list(hist["Close"].astype(float))
        highs = list(hist["High"].astype(float))
        lows = list(hist["Low"].astype(float))
        volumes = list(hist["Volume"].astype(float))

        price = closes[-1]
        prev_close = closes[-2]

        # ─── EMA ───
        def ema(prices, period):
            if len(prices) < period:
                return None
            e = sum(prices[:period]) / period
            k = 2 / (period + 1)
            for p in prices[period:]:
                e = p * k + e * (1 - k)
            return e

        ema9 = ema(closes, 9)
        ema21 = ema(closes, 21)
        ema50 = ema(closes, 50)

        # ─── RSI ───
        gains = [max(closes[i] - closes[i-1], 0) for i in range(1, len(closes))]
        losses = [max(closes[i-1] - closes[i], 0) for i in range(1, len(closes))]
        period = 14
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period
        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period-1) + gains[i]) / period
            avg_loss = (avg_loss * (period-1) + losses[i]) / period
        rsi = 100 - 100 / (1 + avg_gain / avg_loss) if avg_loss > 0 else 100.0

        # ─── Volume ───
        avg_volume_20 = sum(volumes[-21:-1]) / 20 if len(volumes) >= 21 else volumes[-1]
        current_volume = volumes[-1]
        volume_ratio = current_volume / avg_volume_20 if avg_volume_20 > 0 else 1

        # ─── ATR ───
        trs = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
               for i in range(1, len(closes))]
        atr = sum(trs[-14:]) / 14 if len(trs) >= 14 else 0
        atr_pct = (atr / price) * 100 if price > 0 else 0

        # ─── Momentum ───
        change_pct = ((price - prev_close) / prev_close) * 100 if prev_close > 0 else 0
        change_5d = ((price - closes[-6]) / closes[-6]) * 100 if len(closes) >= 6 and closes[-6] > 0 else 0

        # ─── S/R Levels (เรียบง่าย: 20-period high/low) ───
        high_20 = max(highs[-21:-1]) if len(highs) >= 21 else highs[-1]
        low_20 = min(lows[-21:-1]) if len(lows) >= 21 else lows[-1]
        dist_to_resistance = ((high_20 - price) / price) * 100 if price > 0 else 0
        dist_to_support = ((price - low_20) / price) * 100 if price > 0 else 0

        # ─── Higher TF trend ───
        # ใช้ EMA50 เป็น proxy สำหรับ higher timeframe
        higher_tf_bullish = price > ema50 if ema50 else None

        return {
            "symbol": symbol,
            "price": price,
            "prev_close": prev_close,
            "ema9": ema9,
            "ema21": ema21,
            "ema50": ema50,
            "rsi": round(rsi, 2),
            "volume": current_volume,
            "avg_volume_20": avg_volume_20,
            "volume_ratio": round(volume_ratio, 2),
            "atr": atr,
            "atr_pct": round(atr_pct, 2),
            "change_pct": round(change_pct, 2),
            "change_5d_pct": round(change_5d, 2),
            "high_20": round(high_20, 4),
            "low_20": round(low_20, 4),
            "dist_to_resistance_pct": round(dist_to_resistance, 2),
            "dist_to_support_pct": round(dist_to_support, 2),
            "higher_tf_bullish": higher_tf_bullish,
            "interval": interval,
            "bars": len(closes),
        }

    except Exception as e:
        print(f"  [{symbol}] Score data error: {e}")
        return None


def calculate_confidence_score(data: dict, direction: str = "bullish") -> dict:
    """
    คำนวณ Confidence Score 0-100

    Args:
        data: dict จาก fetch_score_data()
        direction: "bullish" | "bearish"

    Returns:
        dict: {total_score, grade, breakdown, recommendation}
    """
    score = 0
    breakdown = {}
    is_bullish = direction == "bullish"

    price = data["price"]
    ema21 = data["ema21"] or price
    ema50 = data["ema50"] or price
    ema9 = data["ema9"] or price
    rsi = data["rsi"]

    # ─── 1. RSI (15 คะแนน) ───────────────────────────────────────────
    rsi_score = 0
    if is_bullish:
        if rsi <= 20:
            rsi_score = 15  # Extreme oversold = โอกาสทอง
        elif rsi <= 30:
            rsi_score = 12
        elif rsi <= 40:
            rsi_score = 8
        elif rsi <= 50:
            rsi_score = 4
        else:
            rsi_score = 0
        rsi_note = f"RSI={rsi:.1f} (Oversold={'ใช่' if rsi<=30 else 'ไม่'})"
    else:
        if rsi >= 80:
            rsi_score = 15
        elif rsi >= 70:
            rsi_score = 12
        elif rsi >= 60:
            rsi_score = 8
        elif rsi >= 50:
            rsi_score = 4
        else:
            rsi_score = 0
        rsi_note = f"RSI={rsi:.1f} (Overbought={'ใช่' if rsi>=70 else 'ไม่'})"

    score += rsi_score
    breakdown["RSI"] = {"score": rsi_score, "max": 15, "note": rsi_note}

    # ─── 2. MA Trend (20 คะแนน) ──────────────────────────────────────
    ma_score = 0
    if is_bullish:
        if price > ema21:
            ma_score += 7
        if ema21 > ema50:
            ma_score += 8
        if price > ema9 and ema9 > ema21:
            ma_score += 5  # triple alignment
    else:
        if price < ema21:
            ma_score += 7
        if ema21 < ema50:
            ma_score += 8
        if price < ema9 and ema9 < ema21:
            ma_score += 5

    score += ma_score
    alignment_str = "Bullish" if (price > ema21 and ema21 > ema50) else ("Bearish" if price < ema21 and ema21 < ema50 else "Mixed")
    breakdown["MA_Trend"] = {"score": ma_score, "max": 20, "note": f"EMA Alignment: {alignment_str}"}

    # ─── 3. Volume (15 คะแนน) ────────────────────────────────────────
    vol_ratio = data["volume_ratio"]
    vol_score = 0
    if vol_ratio >= 3.0:
        vol_score = 15
    elif vol_ratio >= 2.0:
        vol_score = 12
    elif vol_ratio >= 1.5:
        vol_score = 8
    elif vol_ratio >= 1.0:
        vol_score = 4
    else:
        vol_score = 0

    score += vol_score
    breakdown["Volume"] = {"score": vol_score, "max": 15, "note": f"Volume {vol_ratio:.1f}x ค่าเฉลี่ย"}

    # ─── 4. Momentum (15 คะแนน) ──────────────────────────────────────
    chg = data["change_pct"]
    chg5 = data["change_5d_pct"]
    mom_score = 0

    if is_bullish:
        if chg >= 3:
            mom_score += 8
        elif chg >= 1:
            mom_score += 5
        elif chg >= 0:
            mom_score += 2

        if chg5 >= 5:
            mom_score += 7
        elif chg5 >= 2:
            mom_score += 4
    else:
        if chg <= -3:
            mom_score += 8
        elif chg <= -1:
            mom_score += 5
        elif chg <= 0:
            mom_score += 2

        if chg5 <= -5:
            mom_score += 7
        elif chg5 <= -2:
            mom_score += 4

    mom_score = min(mom_score, 15)
    score += mom_score
    breakdown["Momentum"] = {"score": mom_score, "max": 15,
                              "note": f"1d: {chg:+.1f}%  5d: {chg5:+.1f}%"}

    # ─── 5. Volatility (10 คะแนน) ────────────────────────────────────
    atr_pct = data["atr_pct"]
    vol_score2 = 0
    # เหมาะสม: ATR 1-5% สำหรับ stock, ไม่ดีถ้า <0.5% (ไม่ขยับ) หรือ >10% (เสี่ยงสูง)
    if 1.0 <= atr_pct <= 4.0:
        vol_score2 = 10
    elif 0.5 <= atr_pct < 1.0 or 4.0 < atr_pct <= 7.0:
        vol_score2 = 6
    elif atr_pct < 0.5:
        vol_score2 = 2
    else:
        vol_score2 = 0  # volatile เกินไป

    score += vol_score2
    breakdown["Volatility"] = {"score": vol_score2, "max": 10,
                                "note": f"ATR={atr_pct:.1f}% ({'เหมาะสม' if vol_score2>=8 else 'ระวัง'})"}

    # ─── 6. Support/Resistance (15 คะแนน) ────────────────────────────
    sr_score = 0
    if is_bullish:
        # ใกล้ support = ดี, ไกล resistance = มี upside
        if data["dist_to_support_pct"] <= 2:
            sr_score += 10  # ใกล้ support มาก
        elif data["dist_to_support_pct"] <= 5:
            sr_score += 6
        if data["dist_to_resistance_pct"] >= 5:
            sr_score += 5  # มี upside room
    else:
        if data["dist_to_resistance_pct"] <= 2:
            sr_score += 10
        elif data["dist_to_resistance_pct"] <= 5:
            sr_score += 6
        if data["dist_to_support_pct"] >= 5:
            sr_score += 5

    sr_score = min(sr_score, 15)
    score += sr_score
    breakdown["Support_Resistance"] = {"score": sr_score, "max": 15,
        "note": f"H20=${data['high_20']:.3f}  L20=${data['low_20']:.3f}"}

    # ─── 7. MTF (10 คะแนน) ───────────────────────────────────────────
    mtf_score = 0
    htf_bullish = data.get("higher_tf_bullish")
    if htf_bullish is not None:
        if is_bullish and htf_bullish:
            mtf_score = 10
        elif not is_bullish and not htf_bullish:
            mtf_score = 10
        else:
            mtf_score = 0  # ขัดแย้งกัน

    score += mtf_score
    breakdown["MTF_Alignment"] = {"score": mtf_score, "max": 10,
        "note": f"Higher TF {'Bullish' if htf_bullish else 'Bearish'} vs direction {'Bullish' if is_bullish else 'Bearish'}"}

    # ─── Grade ────────────────────────────────────────────────────────
    total = min(score, 100)
    if total >= 80:
        grade = "A"
        grade_th = "🔥 สัญญาณแข็งมาก — น่าเชื่อถือสูง"
        recommendation = "เทรดได้ — ความเสี่ยงต่ำ"
    elif total >= 65:
        grade = "B"
        grade_th = "✅ สัญญาณดี — ควรเทรด"
        recommendation = "เทรดได้ — ตั้ง Stop เสมอ"
    elif total >= 50:
        grade = "C"
        grade_th = "🟡 สัญญาณปานกลาง — ระมัดระวัง"
        recommendation = "เทรดครึ่ง position — รอ confirmation เพิ่ม"
    else:
        grade = "D"
        grade_th = "❌ สัญญาณอ่อน — ควรรอ"
        recommendation = "ยังไม่ถึงเวลา — รอสัญญาณดีกว่า"

    return {
        "symbol": data["symbol"],
        "direction": direction,
        "total_score": total,
        "grade": grade,
        "grade_th": grade_th,
        "recommendation": recommendation,
        "breakdown": breakdown,
        "max_possible": 100,
    }


def check_alert_score(alert: dict, data: dict) -> tuple[bool, dict]:
    """
    เช็กว่า score ถึง threshold มั้ย

    alert fields:
        type: "alert_score"
        direction: "bullish" | "bearish"
        min_score: คะแนนขั้นต่ำ (default=65)
        interval: timeframe

    Returns:
        (triggered: bool, score_result: dict)
    """
    direction = alert.get("direction", "bullish")
    min_score = alert.get("min_score", 65)

    score_result = calculate_confidence_score(data, direction)
    triggered = score_result["total_score"] >= min_score
    return triggered, score_result


def build_score_message(symbol: str, name: str, score_result: dict, alert: dict) -> str:
    """สร้าง Telegram message สำหรับ confidence score alert"""
    emoji = alert.get("emoji", "🎯")
    note = alert.get("note", "")
    total = score_result["total_score"]
    grade = score_result["grade"]

    # สร้าง progress bar
    filled = int(total / 10)
    bar = "█" * filled + "░" * (10 - filled)

    lines = [
        f"{emoji} <b>SCORE ALERT: {symbol}</b> ({name})",
        "",
        f"🎯 Confidence Score: <b>{total}/100</b>  [{bar}]",
        f"📊 Grade: <b>{grade}</b>  {score_result['grade_th']}",
        f"💡 แนะนำ: {score_result['recommendation']}",
        f"📈 Direction: <b>{score_result['direction'].upper()}</b>",
        "",
        "📋 รายละเอียดคะแนน:",
    ]

    for component, info in score_result["breakdown"].items():
        comp_bar = "█" * info["score"] + "░" * (info["max"] - info["score"])
        comp_pct = int((info["score"] / info["max"]) * 100)
        lines.append(
            f"  • {component}: <b>{info['score']}/{info['max']}</b> ({comp_pct}%)  {info['note']}"
        )

    if note:
        lines.append(f"\n📝 Note: {note}")

    lines.extend([
        "",
        f"📊 <a href='https://www.tradingview.com/chart/?symbol={symbol}'>ดูบน TradingView</a>",
        f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
    ])

    return "\n".join(lines)


# ─── Standalone Runner ────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="คำนวณ Alert Confidence Score 0-100",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
ตัวอย่าง:
  python3 module_score.py --symbol AAPL --direction bullish
  python3 module_score.py --symbol BTC-USD --direction bearish --min-score 70
  python3 module_score.py --symbol TSLA --interval 1h
        """
    )
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--direction", default="bullish", choices=["bullish","bearish"])
    parser.add_argument("--min-score", type=int, default=65)
    parser.add_argument("--interval", default="1d",
                        choices=["1m","5m","15m","30m","1h","4h","1d","1wk"])
    parser.add_argument("--json", action="store_true")

    args = parser.parse_args()

    print(f"\n🎯 กำลังคำนวณ Confidence Score สำหรับ {args.symbol} ({args.direction.upper()})...")
    data = fetch_score_data(args.symbol, interval=args.interval)

    if not data:
        print("❌ ดึงข้อมูลไม่สำเร็จ")
        sys.exit(1)

    result = calculate_confidence_score(data, direction=args.direction)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    total = result["total_score"]
    filled = int(total / 5)
    bar = "█" * filled + "░" * (20 - filled)

    print(f"\n{'='*65}")
    print(f"  {args.symbol}  |  Direction: {args.direction.upper()}")
    print(f"  Score: {total}/100  Grade: {result['grade']}")
    print(f"  [{bar}]")
    print(f"  {result['grade_th']}")
    print(f"  แนะนำ: {result['recommendation']}")
    print(f"{'─'*65}")
    print("  รายละเอียด:")
    for comp, info in result["breakdown"].items():
        pct = int((info["score"] / info["max"]) * 100)
        stars = "⭐" * (info["score"] // (info["max"] // 5) if info["max"] > 0 else 0)
        print(f"  [{comp:25s}] {info['score']:2d}/{info['max']} ({pct:3d}%)  {info['note']}")
    print(f"{'='*65}")
    print(f"  เงื่อนไข (min_score={args.min_score}): {'✅ TRIGGERED' if total >= args.min_score else '⬜ Not triggered'}")
    print()


if __name__ == "__main__":
    main()
