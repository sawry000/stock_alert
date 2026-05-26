#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════╗
║  MODULE 1: RSI Alert — ตรวจสอบ RSI Oversold / Overbought   ║
║  ใช้งานอิสระ หรือ import เข้า alert_engine.py ก็ได้        ║
╚══════════════════════════════════════════════════════════════╝

วิธีใช้ standalone:
    python3 module_rsi.py --symbol AAPL --period 14 --oversold 30 --overbought 70

วิธี import:
    from module_rsi import check_rsi, fetch_rsi
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
    import yf as yf

# ─── คำอธิบาย: RSI (Relative Strength Index) ────────────────────────────────
# RSI คือ momentum indicator วัดความแข็งแรงของราคา ค่าอยู่ระหว่าง 0-100
# RSI < 30 = Oversold (ราคาถูกขายมากเกินไป → โอกาสกลับตัวขึ้น)
# RSI > 70 = Overbought (ราคาถูกซื้อมากเกินไป → โอกาสกลับตัวลง)
# RSI < 20 = Extreme Oversold (โอกาสดีมาก แต่ระวัง downtrend แรง)
# RSI > 80 = Extreme Overbought (ระวังการขายทำกำไร)
# ─────────────────────────────────────────────────────────────────────────────


def calculate_rsi(closes: list[float], period: int = 14) -> float | None:
    """
    คำนวณ RSI จาก list ราคาปิด
    คืนค่า RSI ล่าสุด หรือ None ถ้าข้อมูลไม่พอ

    Args:
        closes: รายการราคาปิด (เรียงจากเก่าไปใหม่)
        period: ช่วง RSI (default=14 ตาม Wilder's standard)
    """
    if len(closes) < period + 1:
        return None

    gains = []
    losses = []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))

    # Wilder's smoothing (EMA-like)
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return round(rsi, 2)


def fetch_rsi(symbol: str, period: int = 14, interval: str = "1d") -> dict | None:
    """
    ดึงข้อมูลราคาและคำนวณ RSI สำหรับ symbol ที่กำหนด

    Args:
        symbol: ticker เช่น AAPL, BTC-USD, PTT.BK
        period: ช่วง RSI (default=14)
        interval: timeframe เช่น 1m, 5m, 15m, 1h, 1d, 1wk

    Returns:
        dict: {rsi, price, prev_rsi, signal, interval, period}
        None: ถ้าดึงข้อมูลไม่ได้
    """
    # ต้องการข้อมูลอย่างน้อย period*3 แท่งเพื่อให้ RSI แม่นยำ
    lookback_map = {
        "1m": "5d",
        "5m": "5d",
        "15m": "60d",
        "30m": "60d",
        "1h": "60d",
        "4h": "60d",
        "1d": "90d",
        "1wk": "2y",
    }
    fetch_period = lookback_map.get(interval, "90d")

    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period=fetch_period, interval=interval)

        if hist.empty or len(hist) < period + 2:
            print(f"  [{symbol}] ข้อมูลไม่พอสำหรับคำนวณ RSI (ต้องการ {period+2} แท่ง ได้ {len(hist)})")
            return None

        closes = list(hist["Close"].astype(float))
        current_rsi = calculate_rsi(closes, period)
        prev_rsi = calculate_rsi(closes[:-1], period)

        if current_rsi is None:
            return None

        # วิเคราะห์ signal
        signal = "neutral"
        if current_rsi <= 20:
            signal = "extreme_oversold"
        elif current_rsi <= 30:
            signal = "oversold"
        elif current_rsi >= 80:
            signal = "extreme_overbought"
        elif current_rsi >= 70:
            signal = "overbought"

        # ตรวจ divergence (RSI กำลัง turn)
        rsi_turning_up = prev_rsi is not None and current_rsi > prev_rsi and current_rsi < 40
        rsi_turning_down = prev_rsi is not None and current_rsi < prev_rsi and current_rsi > 60

        return {
            "symbol": symbol,
            "rsi": current_rsi,
            "prev_rsi": prev_rsi,
            "price": float(hist["Close"].iloc[-1]),
            "signal": signal,
            "rsi_turning_up": rsi_turning_up,
            "rsi_turning_down": rsi_turning_down,
            "interval": interval,
            "period": period,
            "bars_used": len(closes),
        }
    except Exception as e:
        print(f"  [{symbol}] RSI fetch error: {e}")
        return None


def check_rsi(alert: dict, rsi_data: dict) -> tuple[bool, float]:
    """
    เช็กเงื่อนไข RSI alert

    alert fields:
        type: "rsi"
        condition: "oversold" | "overbought" | "extreme_oversold" | "extreme_overbought"
                   | "below" | "above" | "turning_up" | "turning_down"
        threshold: ค่า RSI ที่กำหนด (ใช้กับ condition below/above)
        oversold_level: ค่า RSI ที่ถือว่า oversold (default=30)
        overbought_level: ค่า RSI ที่ถือว่า overbought (default=70)
        period: ช่วง RSI (default=14)
        interval: timeframe (default="1d")

    Returns:
        (triggered: bool, rsi_value: float)
    """
    rsi = rsi_data["rsi"]
    condition = alert.get("condition", "oversold")
    oversold_lvl = alert.get("oversold_level", 30)
    overbought_lvl = alert.get("overbought_level", 70)
    threshold = alert.get("threshold", None)

    if condition == "oversold":
        return rsi <= oversold_lvl, rsi
    elif condition == "overbought":
        return rsi >= overbought_lvl, rsi
    elif condition == "extreme_oversold":
        return rsi <= alert.get("extreme_level", 20), rsi
    elif condition == "extreme_overbought":
        return rsi >= alert.get("extreme_level", 80), rsi
    elif condition == "below" and threshold is not None:
        return rsi <= threshold, rsi
    elif condition == "above" and threshold is not None:
        return rsi >= threshold, rsi
    elif condition == "turning_up":
        return rsi_data.get("rsi_turning_up", False), rsi
    elif condition == "turning_down":
        return rsi_data.get("rsi_turning_down", False), rsi

    return False, rsi


def build_rsi_message(symbol: str, name: str, rsi_data: dict, alert: dict) -> str:
    """สร้าง Telegram message สำหรับ RSI alert"""
    rsi = rsi_data["rsi"]
    prev_rsi = rsi_data.get("prev_rsi")
    price = rsi_data["price"]
    signal = rsi_data["signal"]
    interval = rsi_data["interval"]
    condition = alert.get("condition", "oversold")
    note = alert.get("note", "")
    action = alert.get("action", "")

    # emoji mapping
    signal_emoji = {
        "extreme_oversold": "🔥",
        "oversold": "📉",
        "extreme_overbought": "🌡️",
        "overbought": "📈",
        "neutral": "⚪",
    }
    emoji = alert.get("emoji", signal_emoji.get(signal, "🔔"))

    # ลูกศร RSI
    rsi_arrow = ""
    if prev_rsi:
        rsi_arrow = "↑" if rsi > prev_rsi else "↓"

    # คำอธิบาย signal ภาษาไทย
    signal_th = {
        "extreme_oversold": "Extreme Oversold — โอกาสซื้อสูงมาก",
        "oversold": "Oversold — แนวโน้มกลับตัวขึ้น",
        "extreme_overbought": "Extreme Overbought — ระวังการปรับฐาน",
        "overbought": "Overbought — พิจารณาขายทำกำไร",
        "neutral": "Neutral",
    }
    signal_desc = signal_th.get(signal, signal)

    lines = [
        f"{emoji} <b>RSI ALERT: {symbol}</b> ({name})",
        "",
        f"📊 RSI({rsi_data['period']}): <b>{rsi:.1f}</b> {rsi_arrow}",
        f"💰 ราคา: <b>${price:.4f}</b>",
        f"⚡ สัญญาณ: <b>{signal_desc}</b>",
        f"⏱ Timeframe: {interval}",
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
        description="ตรวจสอบ RSI ของหุ้น/crypto แล้วแสดงผล",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
ตัวอย่างการใช้งาน:
  python3 module_rsi.py --symbol AAPL
  python3 module_rsi.py --symbol BTC-USD --interval 1h --period 14
  python3 module_rsi.py --symbol PTT.BK --oversold 35 --overbought 65
  python3 module_rsi.py --symbol TSLA --condition extreme_oversold
        """
    )
    parser.add_argument("--symbol", required=True, help="Ticker เช่น AAPL, BTC-USD, PTT.BK")
    parser.add_argument("--interval", default="1d",
                        choices=["1m","5m","15m","30m","1h","4h","1d","1wk"],
                        help="Timeframe (default: 1d)")
    parser.add_argument("--period", type=int, default=14, help="RSI period (default: 14)")
    parser.add_argument("--oversold", type=float, default=30, help="ระดับ Oversold (default: 30)")
    parser.add_argument("--overbought", type=float, default=70, help="ระดับ Overbought (default: 70)")
    parser.add_argument("--condition",
                        choices=["oversold","overbought","extreme_oversold","extreme_overbought","turning_up","turning_down"],
                        default="oversold",
                        help="เงื่อนไขที่ต้องการเช็ก")
    parser.add_argument("--json", action="store_true", help="แสดงผลเป็น JSON")

    args = parser.parse_args()

    print(f"\n🔍 กำลังดึงข้อมูล RSI สำหรับ {args.symbol} ({args.interval})...")
    rsi_data = fetch_rsi(args.symbol, period=args.period, interval=args.interval)

    if not rsi_data:
        print("❌ ดึงข้อมูลไม่สำเร็จ")
        sys.exit(1)

    if args.json:
        print(json.dumps(rsi_data, ensure_ascii=False, indent=2))
        return

    print(f"\n{'='*50}")
    print(f"  Symbol  : {args.symbol}")
    print(f"  Price   : ${rsi_data['price']:.4f}")
    print(f"  RSI({args.period}) : {rsi_data['rsi']:.2f}  (ก่อนหน้า: {rsi_data.get('prev_rsi', 'N/A')})")
    print(f"  Signal  : {rsi_data['signal']}")
    print(f"  Turning ↑: {rsi_data['rsi_turning_up']}  Turning ↓: {rsi_data['rsi_turning_down']}")
    print(f"{'='*50}")

    # ทดสอบ check_rsi
    alert = {
        "condition": args.condition,
        "oversold_level": args.oversold,
        "overbought_level": args.overbought,
    }
    triggered, val = check_rsi(alert, rsi_data)
    status = "✅ TRIGGERED" if triggered else "⬜ Not triggered"
    print(f"\n  เงื่อนไข [{args.condition}]: {status}  (RSI={val:.2f})")
    print()


if __name__ == "__main__":
    main()
