#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════╗
║  MODULE 8: Backtest — ทดสอบ alert rule ย้อนหลัง                ║
║  คำนวณ win rate, avg return, max drawdown จาก historical data   ║
╚══════════════════════════════════════════════════════════════════╝

วิธีใช้ standalone:
    python3 module_backtest.py --symbol AAPL --rule rsi_oversold --days 180
    python3 module_backtest.py --symbol BTC-USD --rule golden_cross --days 365
    python3 module_backtest.py --symbol TSLA --rule volume_spike --hold-days 5

วิธี import:
    from module_backtest import run_backtest, BACKTEST_RULES
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta

try:
    import yfinance as yf
except ImportError:
    os.system("pip install yfinance --quiet --break-system-packages")
    import yfinance as yf

# ─── คำอธิบาย ────────────────────────────────────────────────────────────────
# Backtest = จำลองว่าถ้าใช้ rule นี้ย้อนหลัง X วัน จะได้ผลอย่างไร
#
# วิธีทำงาน:
# 1. ดึงข้อมูลย้อนหลัง (ตาม --days)
# 2. สแกนทุกวันว่า rule trigger มั้ย
# 3. เมื่อ trigger → จำลองซื้อ (entry)
# 4. ถือครอง X วัน (--hold-days) แล้วขาย (exit)
# 5. คำนวณ win rate, avg return, max loss, best trade
#
# Rules ที่รองรับ:
#   rsi_oversold     — RSI < 30
#   rsi_overbought   — RSI > 70
#   golden_cross     — EMA9 ข้าม EMA21 ขึ้น
#   death_cross      — EMA9 ข้าม EMA21 ลง
#   volume_spike     — Volume > 2x avg
#   price_breakout   — ราคาทำ high ใหม่ 20 วัน
#   price_breakdown  — ราคาทำ low ใหม่ 20 วัน
#   hammer           — Candle Hammer pattern
#   three_soldiers   — Three White Soldiers
# ─────────────────────────────────────────────────────────────────────────────

BACKTEST_RULES = {
    "rsi_oversold": "RSI < 30 (Oversold) — ซื้อเมื่อ oversold, ถือ N วัน",
    "rsi_overbought": "RSI > 70 (Overbought) — Short/ขายเมื่อ overbought",
    "golden_cross": "Golden Cross EMA9/EMA21 — ซื้อเมื่อ EMA9 ข้ามขึ้น",
    "death_cross": "Death Cross EMA9/EMA21 — Short/ขายเมื่อ EMA9 ข้ามลง",
    "volume_spike": "Volume Spike >2x — ซื้อเมื่อ volume พุ่งขึ้น",
    "price_breakout": "Price Breakout 20D High — ซื้อเมื่อทำ high ใหม่",
    "price_breakdown": "Price Breakdown 20D Low — Short/ขายเมื่อทำ low ใหม่",
    "hammer": "Hammer Candle — ซื้อเมื่อเจอ hammer pattern",
    "three_soldiers": "Three White Soldiers — ซื้อเมื่อเจอ 3 เขียวติดกัน",
}


def _calc_ema(prices: list[float], period: int) -> list[float | None]:
    result = [None] * (period - 1)
    seed = sum(prices[:period]) / period
    result.append(seed)
    k = 2 / (period + 1)
    for p in prices[period:]:
        result.append(p * k + result[-1] * (1 - k))
    return result


def _calc_rsi(closes: list[float], period: int = 14) -> list[float | None]:
    result = [None] * period
    gains = [max(closes[i] - closes[i-1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i-1] - closes[i], 0) for i in range(1, len(closes))]
    if len(gains) < period:
        return result
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    rsi_val = 100 - 100 / (1 + avg_g / avg_l) if avg_l > 0 else 100.0
    result.append(rsi_val)
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period-1) + gains[i]) / period
        avg_l = (avg_l * (period-1) + losses[i]) / period
        rsi_val = 100 - 100 / (1 + avg_g / avg_l) if avg_l > 0 else 100.0
        result.append(rsi_val)
    return result


def _detect_hammer(o, h, l, c) -> bool:
    rng = h - l
    if rng <= 0:
        return False
    body = abs(c - o)
    lower_wick = min(o, c) - l
    upper_wick = h - max(o, c)
    return (
        lower_wick >= body * 2 and
        (upper_wick / rng) < 0.2 and
        (body / rng) >= 0.1
    )


def run_backtest(
    symbol: str,
    rule: str,
    days: int = 180,
    hold_days: int = 5,
    rsi_period: int = 14,
    rsi_threshold: float = 30,
    volume_multiplier: float = 2.0,
    interval: str = "1d",
) -> dict:
    """
    รัน backtest สำหรับ rule ที่กำหนด

    Args:
        symbol: Ticker
        rule: rule name (จาก BACKTEST_RULES)
        days: จำนวนวันที่ต้องการ backtest
        hold_days: ถือครองกี่วันก่อนขาย
        rsi_period: RSI period
        rsi_threshold: RSI threshold สำหรับ oversold/overbought
        volume_multiplier: volume spike multiplier
        interval: timeframe

    Returns:
        dict: ผลลัพธ์ backtest ทั้งหมด
    """
    if rule not in BACKTEST_RULES:
        return {"error": f"Rule '{rule}' ไม่รองรับ รองรับ: {list(BACKTEST_RULES.keys())}"}

    # คำนวณ fetch period รวม warmup bars
    warmup = max(rsi_period + 5, 55)  # warmup สำหรับคำนวณ indicators
    total_days = days + warmup + hold_days + 30

    fetch_period = f"{min(total_days, 730)}d"

    try:
        print(f"  [{symbol}] ดึงข้อมูล {fetch_period}...")
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period=fetch_period, interval=interval)

        if hist.empty or len(hist) < warmup + hold_days + 5:
            return {"error": "ข้อมูลไม่พอสำหรับ backtest"}

        opens  = list(hist["Open"].astype(float))
        highs  = list(hist["High"].astype(float))
        lows   = list(hist["Low"].astype(float))
        closes = list(hist["Close"].astype(float))
        vols   = list(hist["Volume"].astype(float))
        dates  = list(hist.index)

        n = len(closes)

        # ─── Compute indicators ───────────────────────────────────────
        rsi_series = _calc_rsi(closes, rsi_period)
        ema9_series = _calc_ema(closes, 9)
        ema21_series = _calc_ema(closes, 21)

        avg_vol_20 = []
        for i in range(n):
            if i < 20:
                avg_vol_20.append(None)
            else:
                avg_vol_20.append(sum(vols[i-20:i]) / 20)

        high_20 = []
        low_20  = []
        for i in range(n):
            if i < 20:
                high_20.append(None)
                low_20.append(None)
            else:
                high_20.append(max(highs[i-20:i]))
                low_20.append(min(lows[i-20:i]))

        # ─── Scan for signals ─────────────────────────────────────────
        # ใช้เฉพาะ N วันล่าสุดที่ต้องการ backtest
        cutoff_idx = max(0, n - days - hold_days)
        end_idx = n - hold_days  # ต้องมีข้อมูลพอสำหรับ hold_days

        trades = []
        last_signal_idx = -999  # ป้องกัน signal ชนกัน

        for i in range(max(cutoff_idx, warmup), end_idx):
            # cooldown: ห้าม signal ถี่เกินไป
            if i - last_signal_idx < max(hold_days, 3):
                continue

            triggered = False
            rsi = rsi_series[i] if i < len(rsi_series) else None
            ema9 = ema9_series[i] if i < len(ema9_series) else None
            ema9_prev = ema9_series[i-1] if i > 0 and i-1 < len(ema9_series) else None
            ema21 = ema21_series[i] if i < len(ema21_series) else None
            ema21_prev = ema21_series[i-1] if i > 0 and i-1 < len(ema21_series) else None
            avg_v = avg_vol_20[i]

            if rule == "rsi_oversold":
                triggered = rsi is not None and rsi <= rsi_threshold

            elif rule == "rsi_overbought":
                triggered = rsi is not None and rsi >= (100 - rsi_threshold)

            elif rule == "golden_cross":
                triggered = (
                    ema9 is not None and ema9_prev is not None and
                    ema21 is not None and ema21_prev is not None and
                    ema9_prev <= ema21_prev and ema9 > ema21
                )

            elif rule == "death_cross":
                triggered = (
                    ema9 is not None and ema9_prev is not None and
                    ema21 is not None and ema21_prev is not None and
                    ema9_prev >= ema21_prev and ema9 < ema21
                )

            elif rule == "volume_spike":
                triggered = avg_v is not None and vols[i] >= avg_v * volume_multiplier

            elif rule == "price_breakout":
                triggered = high_20[i] is not None and closes[i] > high_20[i]

            elif rule == "price_breakdown":
                triggered = low_20[i] is not None and closes[i] < low_20[i]

            elif rule == "hammer":
                triggered = _detect_hammer(opens[i], highs[i], lows[i], closes[i])

            elif rule == "three_soldiers":
                if i >= 2:
                    c0 = {"o": opens[i], "c": closes[i]}
                    c1 = {"o": opens[i-1], "c": closes[i-1]}
                    c2 = {"o": opens[i-2], "c": closes[i-2]}
                    triggered = (
                        c0["c"] > c0["o"] and c1["c"] > c1["o"] and c2["c"] > c2["o"] and
                        c0["c"] > c1["c"] > c2["c"]
                    )

            if triggered:
                entry_price = closes[i]
                exit_idx = min(i + hold_days, n - 1)
                exit_price = closes[exit_idx]

                # สำหรับ short rules (death_cross, rsi_overbought, price_breakdown)
                is_short = rule in ("death_cross", "rsi_overbought", "price_breakdown")
                if is_short:
                    pnl_pct = ((entry_price - exit_price) / entry_price) * 100
                else:
                    pnl_pct = ((exit_price - entry_price) / entry_price) * 100

                # Max drawdown during hold period
                hold_closes = closes[i:exit_idx+1]
                if is_short:
                    max_adverse = max(hold_closes) if hold_closes else exit_price
                    max_dd = ((max_adverse - entry_price) / entry_price) * 100
                else:
                    min_adverse = min(hold_closes) if hold_closes else exit_price
                    max_dd = ((entry_price - min_adverse) / entry_price) * 100

                # Format date
                try:
                    dt = dates[i]
                    if hasattr(dt, 'strftime'):
                        date_str = dt.strftime("%Y-%m-%d")
                    else:
                        date_str = str(dt)[:10]
                except Exception:
                    date_str = f"bar_{i}"

                trades.append({
                    "date": date_str,
                    "entry": round(entry_price, 4),
                    "exit": round(exit_price, 4),
                    "hold_days": hold_days,
                    "pnl_pct": round(pnl_pct, 2),
                    "max_drawdown_pct": round(max_dd, 2),
                    "win": pnl_pct > 0,
                    "is_short": is_short,
                })
                last_signal_idx = i

        # ─── Statistics ───────────────────────────────────────────────
        if not trades:
            return {
                "symbol": symbol, "rule": rule, "days": days, "hold_days": hold_days,
                "total_trades": 0,
                "error": "ไม่พบ signal เลยใน period นี้",
                "rule_description": BACKTEST_RULES.get(rule, rule),
            }

        wins = [t for t in trades if t["win"]]
        losses = [t for t in trades if not t["win"]]
        pnls = [t["pnl_pct"] for t in trades]
        dds = [t["max_drawdown_pct"] for t in trades]

        win_rate = len(wins) / len(trades) * 100
        avg_return = sum(pnls) / len(pnls)
        avg_win = sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0
        best_trade = max(pnls)
        worst_trade = min(pnls)
        max_dd_overall = max(dds) if dds else 0
        profit_factor = abs(avg_win * len(wins)) / abs(avg_loss * len(losses)) if losses and avg_loss != 0 else float("inf")
        total_return = sum(pnls)  # สมมติ 1% per trade compounded แบบง่าย

        # Rating
        if win_rate >= 60 and avg_return >= 2:
            rating = "🔥 Excellent — rule นี้ work มากในอดีต"
        elif win_rate >= 50 and avg_return >= 1:
            rating = "✅ Good — rule นี้ work พอใช้"
        elif win_rate >= 45:
            rating = "🟡 Fair — ควรปรับ parameter หรือเพิ่ม filter"
        else:
            rating = "❌ Poor — rule นี้ไม่ work กับ symbol นี้"

        return {
            "symbol": symbol,
            "rule": rule,
            "rule_description": BACKTEST_RULES.get(rule, rule),
            "interval": interval,
            "backtest_days": days,
            "hold_days": hold_days,
            "total_trades": len(trades),
            "win_trades": len(wins),
            "loss_trades": len(losses),
            "win_rate_pct": round(win_rate, 1),
            "avg_return_pct": round(avg_return, 2),
            "avg_win_pct": round(avg_win, 2),
            "avg_loss_pct": round(avg_loss, 2),
            "best_trade_pct": round(best_trade, 2),
            "worst_trade_pct": round(worst_trade, 2),
            "max_drawdown_pct": round(max_dd_overall, 2),
            "profit_factor": round(min(profit_factor, 999), 2),
            "total_return_pct": round(total_return, 2),
            "rating": rating,
            "recent_trades": trades[-10:],  # 10 trades ล่าสุด
        }

    except Exception as e:
        return {"error": f"Backtest error: {e}"}


def build_backtest_message(symbol: str, name: str, result: dict, alert: dict) -> str:
    """สร้าง Telegram message สำหรับ backtest result"""
    emoji = alert.get("emoji", "📈")

    if "error" in result:
        return f"❌ Backtest {symbol}: {result['error']}"

    win_bar = "█" * int(result["win_rate_pct"] / 10) + "░" * (10 - int(result["win_rate_pct"] / 10))

    lines = [
        f"{emoji} <b>BACKTEST RESULT: {symbol}</b> ({name})",
        "",
        f"📋 Rule: <b>{result['rule']}</b>",
        f"📝 {result['rule_description']}",
        f"📅 ทดสอบย้อนหลัง: <b>{result['backtest_days']} วัน</b>  ถือ: {result['hold_days']} วัน",
        "",
        f"📊 ผลลัพธ์:",
        f"  • จำนวน trades: <b>{result['total_trades']}</b>  (ชนะ {result['win_trades']} แพ้ {result['loss_trades']})",
        f"  • Win Rate: <b>{result['win_rate_pct']:.1f}%</b>  [{win_bar}]",
        f"  • Avg Return: <b>{result['avg_return_pct']:+.2f}%</b>/trade",
        f"  • Best: <b>{result['best_trade_pct']:+.2f}%</b>  |  Worst: <b>{result['worst_trade_pct']:+.2f}%</b>",
        f"  • Max Drawdown: <b>{result['max_drawdown_pct']:.2f}%</b>",
        f"  • Profit Factor: <b>{result['profit_factor']:.2f}</b>",
        "",
        f"⭐ Rating: {result['rating']}",
    ]

    lines.extend([
        "",
        f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
    ])

    return "\n".join(lines)


# ─── Standalone Runner ────────────────────────────────────────────────────────

def main():
    rule_list = "\n  ".join(f"{k}: {v}" for k, v in BACKTEST_RULES.items())

    parser = argparse.ArgumentParser(
        description="Backtest alert rule ย้อนหลัง",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Rules ที่รองรับ:
  {rule_list}

ตัวอย่าง:
  python3 module_backtest.py --symbol AAPL --rule rsi_oversold --days 365
  python3 module_backtest.py --symbol BTC-USD --rule golden_cross --days 180 --hold-days 7
  python3 module_backtest.py --symbol TSLA --rule volume_spike --hold-days 3 --vol-mult 3.0
        """
    )
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--rule", required=True, choices=list(BACKTEST_RULES.keys()))
    parser.add_argument("--days", type=int, default=180, help="วัน backtest (default: 180)")
    parser.add_argument("--hold-days", type=int, default=5, help="ถือครองกี่วัน (default: 5)")
    parser.add_argument("--rsi-period", type=int, default=14)
    parser.add_argument("--rsi-threshold", type=float, default=30)
    parser.add_argument("--vol-mult", type=float, default=2.0, help="Volume multiplier (default: 2.0)")
    parser.add_argument("--interval", default="1d",
                        choices=["1m","5m","15m","30m","1h","4h","1d","1wk"])
    parser.add_argument("--json", action="store_true")

    args = parser.parse_args()

    print(f"\n🔬 กำลัง Backtest [{args.rule}] สำหรับ {args.symbol}...")
    print(f"   ย้อนหลัง: {args.days} วัน  |  ถือ: {args.hold_days} วัน\n")

    result = run_backtest(
        symbol=args.symbol,
        rule=args.rule,
        days=args.days,
        hold_days=args.hold_days,
        rsi_period=args.rsi_period,
        rsi_threshold=args.rsi_threshold,
        volume_multiplier=args.vol_mult,
        interval=args.interval,
    )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if "error" in result:
        print(f"❌ {result['error']}")
        return

    print(f"{'='*65}")
    print(f"  BACKTEST: {args.symbol}  Rule: {args.rule}")
    print(f"  {result['rule_description']}")
    print(f"{'─'*65}")
    print(f"  ทดสอบ {result['total_trades']} trades ใน {result['backtest_days']} วัน")
    print(f"  Win Rate   : {result['win_rate_pct']:.1f}%  ({result['win_trades']}W/{result['loss_trades']}L)")
    print(f"  Avg Return : {result['avg_return_pct']:+.2f}% / trade")
    print(f"  Avg Win    : {result['avg_win_pct']:+.2f}%  |  Avg Loss: {result['avg_loss_pct']:+.2f}%")
    print(f"  Best Trade : {result['best_trade_pct']:+.2f}%  |  Worst: {result['worst_trade_pct']:+.2f}%")
    print(f"  Max DD     : {result['max_drawdown_pct']:.2f}%")
    print(f"  Profit Fac : {result['profit_factor']:.2f}")
    print(f"{'─'*65}")
    print(f"  {result['rating']}")
    print(f"{'─'*65}")
    print(f"  10 Trades ล่าสุด:")
    for t in result["recent_trades"]:
        icon = "✅" if t["win"] else "❌"
        print(f"    {icon} {t['date']}  Entry=${t['entry']:.3f} → Exit=${t['exit']:.3f}  PnL={t['pnl_pct']:+.2f}%  DD={t['max_drawdown_pct']:.1f}%")
    print()


if __name__ == "__main__":
    main()
