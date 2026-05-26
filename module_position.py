#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════╗
║  MODULE 5: Position Sizing — คำนวณขนาดการเทรดที่เหมาะสม        ║
║  Fixed Risk %, Kelly Criterion, ATR-based Stop Loss             ║
╚══════════════════════════════════════════════════════════════════╝

วิธีใช้ standalone:
    python3 module_position.py --symbol AAPL --account 10000 --risk 2
    python3 module_position.py --symbol BTC-USD --account 5000 --entry 65000 --stop 63000
    python3 module_position.py --symbol TSLA --account 50000 --method kelly --win-rate 0.6

วิธี import:
    from module_position import calculate_position, fetch_atr
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
# Fixed Risk: ขาดทุนสูงสุด X% ของพอร์ตต่อ trade
#   ขนาด position = (พอร์ต × risk%) / (entry - stop_loss)
#
# ATR Stop: ใช้ Average True Range ตั้ง stop แบบ dynamic ตาม volatility
#   Stop = Entry - (ATR × multiplier)  → ป้องกันไม่ให้ถูก stop ก่อนเวลา
#
# Kelly Criterion: คำนวณ % พอร์ตที่ควรเทรดจาก win rate และ R:R
#   Kelly% = W - (1-W)/R  (W=win rate, R=reward/risk ratio)
#   แนะนำใช้ Half-Kelly เพื่อความปลอดภัย
#
# Risk Reward Ratio: ควรมี R:R อย่างน้อย 1:2 (เสี่ยง 1 ได้ 2)
# ─────────────────────────────────────────────────────────────────────────────


def fetch_atr(symbol: str, period: int = 14, interval: str = "1d") -> dict | None:
    """
    คำนวณ ATR (Average True Range) — วัด volatility ของราคา

    Args:
        symbol: Ticker
        period: ATR period (default=14)
        interval: timeframe

    Returns:
        dict: {atr, atr_pct, price, suggested_stop_pct}
    """
    lookback_map = {
        "1m": "5d", "5m": "5d", "15m": "30d", "30m": "60d",
        "1h": "60d", "4h": "60d", "1d": "90d", "1wk": "2y",
    }
    fetch_period = lookback_map.get(interval, "90d")

    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period=fetch_period, interval=interval)

        if hist.empty or len(hist) < period + 2:
            return None

        highs = list(hist["High"].astype(float))
        lows = list(hist["Low"].astype(float))
        closes = list(hist["Close"].astype(float))

        true_ranges = []
        for i in range(1, len(closes)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
            true_ranges.append(tr)

        # Wilder's ATR
        atr = sum(true_ranges[:period]) / period
        for tr in true_ranges[period:]:
            atr = (atr * (period - 1) + tr) / period

        price = closes[-1]
        atr_pct = (atr / price) * 100 if price > 0 else 0

        return {
            "atr": round(atr, 4),
            "atr_pct": round(atr_pct, 2),
            "price": round(price, 4),
            "suggested_stop_1x": round(price - atr, 4),
            "suggested_stop_2x": round(price - atr * 2, 4),
            "suggested_stop_3x": round(price - atr * 3, 4),
            "interval": interval,
            "period": period,
        }
    except Exception as e:
        print(f"  [{symbol}] ATR error: {e}")
        return None


def calculate_position(
    account_size: float,
    entry_price: float,
    stop_loss: float,
    target_price: float | None = None,
    risk_pct: float = 2.0,
    method: str = "fixed_risk",
    win_rate: float = 0.5,
    commission_pct: float = 0.1,
) -> dict:
    """
    คำนวณขนาด position ที่เหมาะสม

    Args:
        account_size: ขนาดพอร์ต (บาท หรือ USD)
        entry_price: ราคาเข้า
        stop_loss: ราคา stop loss
        target_price: ราคาเป้าหมาย
        risk_pct: % พอร์ตที่ยอมขาดทุนต่อ trade
        method: "fixed_risk" | "kelly" | "half_kelly"
        win_rate: อัตราชนะ (0.0-1.0) สำหรับ Kelly
        commission_pct: ค่า commission % ต่อด้าน

    Returns:
        dict: ผลลัพธ์การคำนวณทั้งหมด
    """
    if entry_price <= 0 or stop_loss <= 0:
        return {"error": "ราคาต้องมากกว่า 0"}

    risk_per_share = abs(entry_price - stop_loss)
    if risk_per_share <= 0:
        return {"error": "Entry และ Stop Loss ต้องต่างกัน"}

    risk_amount = account_size * (risk_pct / 100)

    # Fixed Risk method
    shares_fixed = risk_amount / risk_per_share
    position_value_fixed = shares_fixed * entry_price

    # Kelly Criterion
    rr_ratio = 0
    shares_kelly = 0
    position_value_kelly = 0
    kelly_pct = 0

    if target_price and target_price != entry_price:
        reward_per_share = abs(target_price - entry_price)
        rr_ratio = reward_per_share / risk_per_share

        if win_rate > 0 and rr_ratio > 0:
            kelly_pct = win_rate - (1 - win_rate) / rr_ratio
            kelly_pct = max(0, min(kelly_pct, 0.25))  # cap ที่ 25%
            half_kelly_pct = kelly_pct / 2

            kelly_amount = account_size * (kelly_pct if method == "kelly" else half_kelly_pct)
            shares_kelly = kelly_amount / entry_price
            position_value_kelly = kelly_amount

    # เลือก method
    if method in ("kelly", "half_kelly") and shares_kelly > 0:
        shares = shares_kelly
        position_value = position_value_kelly
        used_pct = (position_value / account_size) * 100
    else:
        shares = shares_fixed
        position_value = position_value_fixed
        used_pct = (position_value / account_size) * 100

    # คำนวณผลลัพธ์
    max_loss = shares * risk_per_share
    commission_cost = position_value * (commission_pct / 100) * 2  # เข้า + ออก

    result = {
        "method": method,
        "account_size": account_size,
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "target_price": target_price,
        "risk_pct": risk_pct,
        "risk_amount": round(risk_amount, 2),
        "risk_per_share": round(risk_per_share, 4),
        "shares": round(shares, 2),
        "shares_int": int(shares),  # ปัดลงเพื่อความปลอดภัย
        "position_value": round(position_value, 2),
        "position_pct_of_account": round(used_pct, 1),
        "max_loss": round(max_loss, 2),
        "commission_cost": round(commission_cost, 2),
        "total_risk_with_commission": round(max_loss + commission_cost, 2),
        "rr_ratio": round(rr_ratio, 2) if rr_ratio > 0 else None,
        "kelly_pct": round(kelly_pct * 100, 1) if kelly_pct > 0 else None,
        "half_kelly_pct": round(kelly_pct * 50, 1) if kelly_pct > 0 else None,
    }

    if target_price:
        potential_profit = shares * abs(target_price - entry_price) - commission_cost
        result["potential_profit"] = round(potential_profit, 2)
        result["profit_on_account_pct"] = round((potential_profit / account_size) * 100, 2)

    # คำแนะนำ
    warnings = []
    if used_pct > 20:
        warnings.append("⚠️ Position ใหญ่มาก (>20% พอร์ต) — เสี่ยงสูง")
    if rr_ratio > 0 and rr_ratio < 1.5:
        warnings.append("⚠️ R:R ต่ำกว่า 1:1.5 — ควรปรับ Target")
    if used_pct > 50:
        warnings.append("🚨 DANGER: ใช้พอร์ตมากกว่า 50% — อันตรายมาก!")

    if not warnings:
        warnings.append("✅ ขนาด position อยู่ในเกณฑ์ปลอดภัย")

    result["warnings"] = warnings
    return result


def build_position_message(symbol: str, name: str, pos: dict, alert: dict) -> str:
    """สร้าง Telegram message พร้อมข้อมูล position sizing"""
    emoji = alert.get("emoji", "💼")
    note = alert.get("note", "")

    method_th = {
        "fixed_risk": "Fixed Risk",
        "kelly": "Kelly Criterion",
        "half_kelly": "Half-Kelly (แนะนำ)",
    }

    direction_icon = "📥" if pos.get("entry_price", 0) < pos.get("stop_loss", 0) else "📥"
    rr = pos.get("rr_ratio")
    rr_str = f"{rr:.1f}" if rr else "N/A"

    lines = [
        f"{emoji} <b>POSITION SIZE: {symbol}</b> ({name})",
        "",
        f"💼 วิธีคำนวณ: <b>{method_th.get(pos['method'], pos['method'])}</b>",
        f"💰 Entry: <b>${pos['entry_price']:.4f}</b>",
        f"🛑 Stop Loss: <b>${pos['stop_loss']:.4f}</b>",
    ]

    if pos.get("target_price"):
        lines.append(f"🎯 Target: <b>${pos['target_price']:.4f}</b>")

    lines.extend([
        "",
        f"📊 ผลการคำนวณ:",
        f"  • ขนาด: <b>{pos['shares_int']:,} หุ้น</b> (${pos['position_value']:,.2f})",
        f"  • % พอร์ต: <b>{pos['position_pct_of_account']:.1f}%</b>",
        f"  • ขาดทุนสูงสุด: <b>${pos['max_loss']:,.2f}</b> ({pos['risk_pct']}% พอร์ต)",
        f"  • R:R Ratio: <b>1:{rr_str}</b>",
    ])

    if pos.get("potential_profit"):
        lines.append(f"  • กำไรเป้าหมาย: <b>${pos['potential_profit']:,.2f}</b> ({pos['profit_on_account_pct']}% พอร์ต)")

    lines.append("")
    for w in pos.get("warnings", []):
        lines.append(w)

    if note:
        lines.append(f"\n📋 Note: {note}")

    lines.extend([
        "",
        f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
    ])

    return "\n".join(lines)


# ─── Standalone Runner ────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="คำนวณขนาดการเทรดที่เหมาะสม",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
ตัวอย่าง:
  python3 module_position.py --symbol AAPL --account 10000 --entry 150 --stop 145 --target 165 --risk 2
  python3 module_position.py --symbol BTC-USD --account 5000 --entry 65000 --stop 63000 --target 70000 --method kelly --win-rate 0.55
  python3 module_position.py --symbol AAPL --account 10000 --atr-stop  (ใช้ ATR คำนวณ stop)
        """
    )
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--account", type=float, required=True, help="ขนาดพอร์ต (USD/THB)")
    parser.add_argument("--entry", type=float, help="ราคาเข้า (ถ้าไม่ระบุจะดึง current price)")
    parser.add_argument("--stop", type=float, help="ราคา Stop Loss")
    parser.add_argument("--target", type=float, help="ราคาเป้าหมาย")
    parser.add_argument("--risk", type=float, default=2.0, help="% พอร์ตที่ยอมขาดทุน (default: 2)")
    parser.add_argument("--method", default="fixed_risk",
                        choices=["fixed_risk","kelly","half_kelly"])
    parser.add_argument("--win-rate", type=float, default=0.55, help="อัตราชนะ 0.0-1.0 (default: 0.55)")
    parser.add_argument("--commission", type=float, default=0.1, help="ค่า commission % (default: 0.1)")
    parser.add_argument("--atr-stop", action="store_true", help="คำนวณ stop จาก ATR")
    parser.add_argument("--atr-mult", type=float, default=2.0, help="ATR multiplier (default: 2)")
    parser.add_argument("--interval", default="1d")
    parser.add_argument("--json", action="store_true")

    args = parser.parse_args()

    entry = args.entry
    stop = args.stop

    # ดึง current price ถ้าไม่ระบุ
    if not entry:
        print(f"🔍 ดึงราคาปัจจุบัน {args.symbol}...")
        atr_data = fetch_atr(args.symbol, interval=args.interval)
        if atr_data:
            entry = atr_data["price"]
            print(f"  ราคาปัจจุบัน: ${entry:.4f}")

    if not entry:
        print("❌ ระบุ --entry หรือมีข้อมูลราคา")
        sys.exit(1)

    # คำนวณ ATR stop ถ้าต้องการ
    if args.atr_stop or not stop:
        print(f"📊 กำลังคำนวณ ATR({14}) สำหรับ {args.symbol}...")
        atr_data = fetch_atr(args.symbol, interval=args.interval)
        if atr_data:
            stop = entry - (atr_data["atr"] * args.atr_mult)
            print(f"  ATR={atr_data['atr']:.4f}  Stop={stop:.4f} ({args.atr_mult}x ATR)")
        else:
            print("❌ ไม่สามารถคำนวณ ATR ได้")
            if not stop:
                sys.exit(1)

    if not stop:
        print("❌ ระบุ --stop หรือใช้ --atr-stop")
        sys.exit(1)

    result = calculate_position(
        account_size=args.account,
        entry_price=entry,
        stop_loss=stop,
        target_price=args.target,
        risk_pct=args.risk,
        method=args.method,
        win_rate=args.win_rate,
        commission_pct=args.commission,
    )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    print(f"\n{'='*60}")
    print(f"  POSITION SIZING: {args.symbol}")
    print(f"{'='*60}")
    print(f"  พอร์ต       : ${args.account:,.2f}")
    print(f"  Entry       : ${entry:.4f}")
    print(f"  Stop Loss   : ${stop:.4f}")
    if args.target:
        print(f"  Target      : ${args.target:.4f}")
    print(f"  Risk/trade  : {args.risk}%  (${result['risk_amount']:,.2f})")
    print(f"  วิธีคำนวณ  : {args.method}")
    print(f"{'─'*60}")
    print(f"  จำนวนหุ้น  : {result['shares_int']:,}")
    print(f"  มูลค่า pos  : ${result['position_value']:,.2f} ({result['position_pct_of_account']:.1f}% พอร์ต)")
    print(f"  ขาดทุนสูงสุด: ${result['max_loss']:,.2f}")
    if result.get("rr_ratio"):
        print(f"  R:R Ratio   : 1:{result['rr_ratio']:.1f}")
    if result.get("potential_profit"):
        print(f"  กำไรเป้าหมาย: ${result['potential_profit']:,.2f} ({result['profit_on_account_pct']:.1f}% พอร์ต)")
    if result.get("kelly_pct"):
        print(f"  Kelly %     : {result['kelly_pct']:.1f}%  Half-Kelly: {result['half_kelly_pct']:.1f}%")
    print(f"{'─'*60}")
    for w in result.get("warnings", []):
        print(f"  {w}")
    print()


if __name__ == "__main__":
    main()
