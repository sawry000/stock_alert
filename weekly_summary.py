#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
weekly_summary.py — สรุปผลลัพธ์กลยุทธ์รายสัปดาห์ ส่งเข้า Telegram

วิเคราะห์เทรดที่ปิดแล้วจาก alert_log.json (เฉพาะที่มี pnl_pct/entry_price
บันทึกไว้จริง — ดู alert_engine.py calc_position_size()/log.append()) แยก
เป็น "7 วันล่าสุด" กับ "ทั้งหมดตั้งแต่เริ่มเก็บข้อมูล" เพื่อดูว่ากลยุทธ์ที่
ปรับไปช่วยจริงมั้ย โดยไม่ต้องเข้าไปเปิดแดชบอร์ดเอง (หน้า Strategy Analytics
ใน dashboard_pro.html ทำแบบเดียวกันนี้แต่เป็น interactive — สคริปต์นี้ทำ
สรุปสั้นๆ ส่ง Telegram อัตโนมัติทุกสัปดาห์แทน)

รันโดย GitHub Actions workflow: weekly_summary.yml (cron ทุกวันจันทร์ 08:00 ICT)
รันเองก็ได้: python3 weekly_summary.py
"""

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).parent
LOG_PATH = BASE_DIR / "alert_log.json"

LOOKBACK_DAYS = 7   # ช่วงเวลาที่สรุป — ปรับได้ผ่าน env WEEKLY_LOOKBACK_DAYS


# ══════════════════════════════════════════════════════════════════════════════
#  พื้นฐาน (คัดลอกรูปแบบเดียวกับ alert_engine.py/daily_screener.py — แต่ละ
#  สคริปต์ในโปรเจกต์นี้ตั้งใจให้ standalone รันได้เดี่ยวๆ ไม่ผูก import ข้ามไฟล์)
# ══════════════════════════════════════════════════════════════════════════════

def load_json(path, default):
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return default
    return default


def now_utc():
    return datetime.now(timezone.utc)


def now_bkk_str():
    bkk = now_utc() + timedelta(hours=7)
    return bkk.strftime("%d/%m/%Y %H:%M ICT")


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
                if result.get("ok"):
                    return True
                print(f"  [Telegram] API ตอบ error: {result}")
        except urllib.error.URLError as e:
            print(f"  [Telegram] Attempt {attempt+1} failed: {e}")
        if attempt < 2:
            time.sleep(3)
    return False


# ══════════════════════════════════════════════════════════════════════════════
#  วิเคราะห์
# ══════════════════════════════════════════════════════════════════════════════

def parse_ts(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def closed_trades_from_log(log):
    """เฉพาะ SELL ที่มี pnl_pct + entry_price บันทึกไว้จริง (position ที่เปิด
    อยู่จริงตอน SELL — กันนับเทรดเก่าก่อนมี tracking หรือ SELL ที่ไม่มี
    position เปิดอยู่เลยตอนนั้น)"""
    return [
        e for e in log
        if e.get("action") == "SELL"
        and e.get("pnl_pct") is not None
        and e.get("entry_price") is not None
    ]


def calc_stats(trades):
    if not trades:
        return None
    wins   = [t for t in trades if t["pnl_pct"] > 0]
    losses = [t for t in trades if t["pnl_pct"] <= 0]
    win_rate = len(wins) / len(trades) * 100
    avg_win  = sum(t["pnl_pct"] for t in wins) / len(wins) if wins else None
    avg_loss = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else None
    expectancy = sum(t["pnl_pct"] for t in trades) / len(trades)
    breakeven = None
    if avg_win is not None and avg_loss is not None and (avg_win - avg_loss) != 0:
        breakeven = (-avg_loss) / (avg_win - avg_loss) * 100
    rr_vals = [t["rr_ratio"] for t in trades if t.get("rr_ratio") is not None]
    avg_rr = sum(rr_vals) / len(rr_vals) if rr_vals else None
    return {
        "n": len(trades), "wins": len(wins), "losses": len(losses),
        "win_rate": win_rate, "avg_win": avg_win, "avg_loss": avg_loss,
        "expectancy": expectancy, "breakeven": breakeven, "avg_rr": avg_rr,
    }


def group_by(trades, key_fn, top_n=3):
    groups = {}
    for t in trades:
        k = key_fn(t) or "unknown"
        groups.setdefault(k, []).append(t)
    rows = []
    for k, v in groups.items():
        wins = [t for t in v if t["pnl_pct"] > 0]
        rows.append({
            "key": k, "n": len(v),
            "win_rate": len(wins) / len(v) * 100,
            "avg_pnl": sum(t["pnl_pct"] for t in v) / len(v),
        })
    rows.sort(key=lambda r: -r["n"])
    return rows[:top_n]


ALERT_TYPE_TH = {
    "rsi":                 "RSI Oversold",
    "ma_crossover":        "MA Crossover",
    "alert_score":         "Confidence Score",
    "mtf_alignment":       "MTF Alignment",
    "volume_spike":        "Volume Spike",
    "percent_change":      "ราคาเปลี่ยนแปลงเกิน threshold",
    "support_resistance":  "Stop-Loss (แนวรับ-แนวต้าน)",
    "take_profit":         "🎯 Take Profit",
    "trailing_stop":       "📈 Trailing Stop",
    "stagnant_exit":       "⏳ Stagnant Exit",
}


def label(k):
    if k == "unknown":
        return "ไม่ทราบ (position เก่าก่อนมี tracking นี้)"
    return ALERT_TYPE_TH.get(k, k)


def fmt_pct(n):
    if n is None:
        return "-"
    return f"{'+' if n >= 0 else ''}{n:.2f}%"


# ══════════════════════════════════════════════════════════════════════════════
#  สร้างข้อความ Telegram
# ══════════════════════════════════════════════════════════════════════════════

def build_section(title, stats):
    if stats is None:
        return f"<b>{title}</b>\n  ยังไม่มีเทรดที่ปิดในช่วงนี้\n"
    wr_icon = "🟢" if stats["win_rate"] >= 50 else ("🟡" if stats["win_rate"] >= 35 else "🔴")
    lines = [
        f"<b>{title}</b>",
        f"  {wr_icon} Win Rate: <b>{stats['win_rate']:.1f}%</b>  ({stats['wins']}✅ / {stats['losses']}❌ จาก {stats['n']} เทรด)",
        f"  ชนะเฉลี่ย: <b>{fmt_pct(stats['avg_win'])}</b>   แพ้เฉลี่ย: <b>{fmt_pct(stats['avg_loss'])}</b>",
        f"  Expectancy/เทรด: <b>{fmt_pct(stats['expectancy'])}</b>",
    ]
    if stats["breakeven"] is not None:
        gap = stats["win_rate"] - stats["breakeven"]
        gap_icon = "✅" if gap >= 0 else "⚠️"
        lines.append(f"  Win Rate ที่ต้องการ (Breakeven): {stats['breakeven']:.1f}%  {gap_icon} ({'เกิน' if gap>=0 else 'ขาด'} {abs(gap):.1f} จุด)")
    if stats["avg_rr"] is not None:
        lines.append(f"  R:R เฉลี่ย: 1:{stats['avg_rr']:.2f}")
    return "\n".join(lines) + "\n"


def build_breakdown(title, rows):
    if not rows:
        return ""
    lines = [f"<b>{title}</b>"]
    for r in rows:
        wr_icon = "🟢" if r["win_rate"] >= 50 else ("🟡" if r["win_rate"] >= 35 else "🔴")
        lines.append(f"  {wr_icon} {label(r['key'])}: {r['win_rate']:.0f}% win  ({fmt_pct(r['avg_pnl'])} เฉลี่ย, n={r['n']})")
    return "\n".join(lines) + "\n"


def build_message(week_stats, alltime_stats, week_by_exit, week_by_entry, lookback_days):
    lines = [
        "📊 <b>สรุปกลยุทธ์รายสัปดาห์</b>",
        f"🕐 {now_bkk_str()}  •  ย้อนหลัง {lookback_days} วัน",
        "━━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        build_section(f"📅 {lookback_days} วันล่าสุด", week_stats).rstrip("\n"),
        "",
        build_section("📈 ทั้งหมดตั้งแต่เริ่มเก็บข้อมูล", alltime_stats).rstrip("\n"),
    ]
    if week_by_exit:
        lines += ["", "━━━━━━━━━━━━━━━━━━━━━━━━━", build_breakdown("🚪 ออกเพราะอะไรบ้าง (สัปดาห์นี้)", week_by_exit).rstrip("\n")]
    if week_by_entry:
        lines += ["", build_breakdown("🚀 เข้าเพราะอะไรบ้าง (สัปดาห์นี้)", week_by_entry).rstrip("\n")]

    if week_stats and alltime_stats:
        diff = week_stats["win_rate"] - alltime_stats["win_rate"]
        if diff >= 5:
            lines += ["", "✅ Win Rate สัปดาห์นี้ดีขึ้นกว่าค่าเฉลี่ยสะสม — กลยุทธ์ที่ปรับไปเริ่มเห็นผล"]
        elif diff <= -5:
            lines += ["", "⚠️ Win Rate สัปดาห์นี้แย่กว่าค่าเฉลี่ยสะสม — ควรดูรายละเอียดใน Strategy Analytics ในแดชบอร์ด"]

    lines += ["", "🔎 ดูรายละเอียดเต็มได้ที่หน้า Strategy Analytics ในแดชบอร์ด"]
    msg = "\n".join(lines)

    # กัน Telegram limit 4096 ตัวอักษร (เผื่อ buffer เหมือน alert_engine.py)
    if len(msg) > 3800:
        msg = msg[:3800] + "\n\n… (ตัดเพื่อไม่เกินลิมิต Telegram — ดูฉบับเต็มในแดชบอร์ด)"
    return msg


def main():
    lookback_days = int(os.environ.get("WEEKLY_LOOKBACK_DAYS", str(LOOKBACK_DAYS)))
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    log = load_json(LOG_PATH, [])
    if not isinstance(log, list):
        print("⚠️ alert_log.json อ่านไม่ได้หรือ format ผิด — ข้าม")
        return

    closed = closed_trades_from_log(log)
    cutoff = now_utc() - timedelta(days=lookback_days)
    week_trades = [t for t in closed if (parse_ts(t.get("timestamp", "")) or now_utc()) >= cutoff]

    week_stats    = calc_stats(week_trades)
    alltime_stats = calc_stats(closed)
    week_by_exit  = group_by(week_trades, lambda t: t.get("type"))
    week_by_entry = group_by(week_trades, lambda t: t.get("entry_alert_type"))

    print(f"[{now_bkk_str()}] เทรดที่ปิดทั้งหมด: {len(closed)}  |  {lookback_days} วันล่าสุด: {len(week_trades)}")
    if week_stats:
        print(f"  Win rate สัปดาห์นี้: {week_stats['win_rate']:.1f}%  Expectancy: {fmt_pct(week_stats['expectancy'])}")

    if not token or not chat_id:
        print("⚠️ ไม่มี TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID — ข้ามการส่ง (พิมพ์ผลไว้ใน log เท่านั้น)")
        return

    msg = build_message(week_stats, alltime_stats, week_by_exit, week_by_entry, lookback_days)
    ok = send_telegram(token, chat_id, msg)
    print(f"[Weekly Summary] {'✅ ส่งสำเร็จ' if ok else '❌ ส่งไม่สำเร็จ'}")


if __name__ == "__main__":
    main()
