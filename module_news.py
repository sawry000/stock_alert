#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════╗
║  MODULE 4: News Sentiment — ข่าวหุ้นแปลภาษาไทย + วิเคราะห์    ║
║  ดึงข่าวจาก yfinance → แปล → วิเคราะห์ sentiment → แจ้งเตือน  ║
╚══════════════════════════════════════════════════════════════════╝

วิธีใช้ standalone:
    python3 module_news.py --symbol AAPL --max 5
    python3 module_news.py --symbol BTC-USD --sentiment positive --hours 24

วิธี import:
    from module_news import fetch_news, check_news_sentiment, build_news_message
"""

import argparse
import json
import os
import re
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

try:
    import yfinance as yf
except ImportError:
    os.system("pip install yfinance --quiet --break-system-packages")
    import yfinance as yf

# ─── คำอธิบาย ────────────────────────────────────────────────────────────────
# Module นี้ดึงข่าวล่าสุดจาก yfinance (Yahoo Finance)
# แล้วส่งไปแปลเป็นภาษาไทยผ่าน MyMemory API (ฟรี ไม่ต้อง key)
# วิเคราะห์ sentiment จาก keyword ใน headline
# ─────────────────────────────────────────────────────────────────────────────

# ─── Sentiment Keywords ───────────────────────────────────────────────────────
POSITIVE_KEYWORDS = [
    "surge", "soar", "rally", "gain", "rise", "jump", "climb", "beat",
    "record", "profit", "growth", "bullish", "upgrade", "buy", "strong",
    "positive", "better", "exceed", "outperform", "boost", "expand",
    "breakthrough", "acquisition", "partnership", "dividend", "buyback",
    "สูงขึ้น", "เพิ่ม", "กำไร", "บวก", "แข็งค่า",
]

NEGATIVE_KEYWORDS = [
    "plunge", "crash", "drop", "fall", "decline", "loss", "miss", "weak",
    "bearish", "downgrade", "sell", "warning", "risk", "cut", "layoff",
    "lawsuit", "investigation", "fraud", "bankruptcy", "recall", "fine",
    "penalty", "debt", "concern", "worry", "disappoint", "worse",
    "ลดลง", "ร่วง", "ขาดทุน", "ลบ", "อ่อนค่า",
]


def translate_to_thai(text: str) -> str:
    """
    แปลข้อความเป็นภาษาไทยโดยใช้ MyMemory API (ฟรี ไม่ต้อง key)
    ถ้าไม่สำเร็จ return ข้อความเดิม
    """
    if not text or len(text.strip()) < 3:
        return text

    try:
        # Trim ให้สั้นลงถ้ายาวเกิน 400 char (MyMemory limit)
        text_trimmed = text[:400]
        encoded = urllib.parse.quote(text_trimmed)
        url = f"https://api.mymemory.translated.net/get?q={encoded}&langpair=en|th"

        req = urllib.request.Request(url, headers={"User-Agent": "StockAlertBot/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
            translated = data.get("responseData", {}).get("translatedText", "")
            # ตรวจว่าแปลสำเร็จจริง (ไม่ใช่ error message)
            if translated and "INVALID" not in translated.upper() and len(translated) > 3:
                return translated
    except Exception:
        pass  # Fallback: return original

    return text


def analyze_sentiment(text: str) -> dict:
    """
    วิเคราะห์ sentiment จาก text

    Returns:
        dict: {score: int, label: str, positive_hits: list, negative_hits: list}
    """
    text_lower = text.lower()
    pos_hits = [k for k in POSITIVE_KEYWORDS if k in text_lower]
    neg_hits = [k for k in NEGATIVE_KEYWORDS if k in text_lower]

    score = len(pos_hits) - len(neg_hits)

    if score >= 2:
        label = "positive"
    elif score <= -2:
        label = "negative"
    elif score == 1:
        label = "slightly_positive"
    elif score == -1:
        label = "slightly_negative"
    else:
        label = "neutral"

    return {
        "score": score,
        "label": label,
        "positive_hits": pos_hits[:5],
        "negative_hits": neg_hits[:5],
    }


def fetch_news(
    symbol: str,
    max_news: int = 5,
    hours_back: int = 24,
    translate: bool = True,
) -> list[dict]:
    """
    ดึงข่าวล่าสุดสำหรับ symbol

    Args:
        symbol: Ticker เช่น AAPL
        max_news: จำนวนข่าวสูงสุด
        hours_back: ดึงข่าวย้อนหลังกี่ชั่วโมง (0 = ทุกข่าว)
        translate: แปลภาษาไทยหรือไม่

    Returns:
        list[dict]: รายการข่าว พร้อม sentiment และการแปล
    """
    try:
        ticker = yf.Ticker(symbol)
        raw_news = ticker.news

        if not raw_news:
            print(f"  [{symbol}] ไม่พบข่าว")
            return []

        now_ts = datetime.now(timezone.utc).timestamp()
        cutoff_ts = now_ts - (hours_back * 3600) if hours_back > 0 else 0

        results = []
        for item in raw_news:
            # ดึงข้อมูล (structure ต่างกันตาม yfinance version)
            content = item.get("content", item)
            title = (
                content.get("title") or
                item.get("title") or
                ""
            )
            pub_ts = (
                content.get("pubDate") or
                item.get("providerPublishTime") or
                item.get("published_at") or
                0
            )
            link = (
                content.get("canonicalUrl", {}).get("url") or
                item.get("link") or
                item.get("url") or
                ""
            )
            publisher = (
                content.get("provider", {}).get("displayName") or
                item.get("publisher") or
                "Yahoo Finance"
            )
            summary = (
                content.get("summary") or
                item.get("summary") or
                ""
            )

            if not title:
                continue

            # กรองตามเวลา
            try:
                if isinstance(pub_ts, str):
                    pub_dt = datetime.fromisoformat(pub_ts.replace("Z", "+00:00"))
                    pub_ts_val = pub_dt.timestamp()
                else:
                    pub_ts_val = float(pub_ts)
            except Exception:
                pub_ts_val = 0

            if hours_back > 0 and pub_ts_val > 0 and pub_ts_val < cutoff_ts:
                continue

            # Sentiment
            full_text = f"{title} {summary}"
            sentiment = analyze_sentiment(full_text)

            # แปลภาษาไทย
            title_th = translate_to_thai(title) if translate else title
            summary_th = ""
            if translate and summary:
                summary_th = translate_to_thai(summary[:300])

            # Format เวลา
            pub_str = ""
            if pub_ts_val > 0:
                pub_dt = datetime.fromtimestamp(pub_ts_val, tz=timezone.utc)
                # ใช้ offset เป็น Asia/Bangkok (UTC+7)
                bkk_offset = timedelta(hours=7)
                bkk_dt = pub_dt + bkk_offset
                pub_str = bkk_dt.strftime("%d/%m %H:%M") + " (ICT)"

            results.append({
                "title": title,
                "title_th": title_th,
                "summary": summary[:200] if summary else "",
                "summary_th": summary_th[:200] if summary_th else "",
                "publisher": publisher,
                "link": link,
                "published_at": pub_str,
                "published_ts": pub_ts_val,
                "sentiment": sentiment,
            })

            if len(results) >= max_news:
                break

        return results

    except Exception as e:
        print(f"  [{symbol}] News fetch error: {e}")
        return []


def check_news_sentiment(alert: dict, news_list: list[dict]) -> tuple[bool, list[dict]]:
    """
    เช็กเงื่อนไข news sentiment alert

    alert fields:
        type: "news_sentiment"
        condition: "any" | "positive" | "negative" | "strong_positive" | "strong_negative"
                   | "high_volume" (ข่าวเยอะมาก)
        min_news: จำนวนข่าวขั้นต่ำ (default=1)
        hours_back: ย้อนหลังกี่ชั่วโมง (default=24)
        sentiment_threshold: score ขั้นต่ำ (default=2 สำหรับ positive)

    Returns:
        (triggered: bool, matching_news: list[dict])
    """
    condition = alert.get("condition", "any")
    min_news = alert.get("min_news", 1)
    threshold = alert.get("sentiment_threshold", 2)

    if not news_list:
        return False, []

    if condition == "any":
        matching = news_list
    elif condition == "positive":
        matching = [n for n in news_list if n["sentiment"]["score"] > 0]
    elif condition == "negative":
        matching = [n for n in news_list if n["sentiment"]["score"] < 0]
    elif condition == "strong_positive":
        matching = [n for n in news_list if n["sentiment"]["score"] >= threshold]
    elif condition == "strong_negative":
        matching = [n for n in news_list if n["sentiment"]["score"] <= -threshold]
    elif condition == "high_volume":
        matching = news_list if len(news_list) >= alert.get("min_news", 3) else []
    else:
        matching = []

    triggered = len(matching) >= min_news
    return triggered, matching[:5]


def build_news_message(symbol: str, name: str, news_list: list[dict], alert: dict) -> str:
    """สร้าง Telegram message สำหรับ news sentiment alert"""
    emoji = alert.get("emoji", "📰")
    note = alert.get("note", "")
    condition = alert.get("condition", "any")

    condition_th = {
        "any": "ข่าวใหม่",
        "positive": "ข่าวบวก",
        "negative": "ข่าวลบ",
        "strong_positive": "ข่าวบวกแรง",
        "strong_negative": "ข่าวลบแรง",
        "high_volume": "ข่าวเยอะมาก",
    }

    sentiment_emoji = {
        "positive": "🟢", "slightly_positive": "🟡",
        "negative": "🔴", "slightly_negative": "🟠", "neutral": "⚪",
    }

    lines = [
        f"{emoji} <b>NEWS ALERT: {symbol}</b> ({name})",
        f"📋 เงื่อนไข: {condition_th.get(condition, condition)}",
        f"📰 พบ {len(news_list)} ข่าว",
        "",
    ]

    for i, n in enumerate(news_list[:4], 1):
        s_emoji = sentiment_emoji.get(n["sentiment"]["label"], "⚪")
        title_display = n["title_th"] if n["title_th"] != n["title"] else n["title"]
        lines.append(f"{i}. {s_emoji} <b>{title_display}</b>")

        if n.get("summary_th"):
            lines.append(f"   📝 {n['summary_th'][:100]}...")

        if n.get("link"):
            lines.append(f"   🔗 <a href='{n['link']}'>{n['publisher']}</a>  {n['published_at']}")
        else:
            lines.append(f"   📰 {n['publisher']}  {n['published_at']}")

        lines.append("")

    if note:
        lines.append(f"📋 Note: {note}")

    lines.extend([
        f"📊 <a href='https://www.tradingview.com/chart/?symbol={symbol}'>ดูบน TradingView</a>",
        f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
    ])

    return "\n".join(lines)


# ─── Standalone Runner ────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="ดึงและวิเคราะห์ข่าวหุ้น แปลภาษาไทย",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
ตัวอย่าง:
  python3 module_news.py --symbol AAPL
  python3 module_news.py --symbol BTC-USD --max 10 --hours 48
  python3 module_news.py --symbol TSLA --sentiment negative --no-translate
        """
    )
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--max", type=int, default=5, help="จำนวนข่าวสูงสุด (default: 5)")
    parser.add_argument("--hours", type=int, default=24, help="ย้อนหลังกี่ชั่วโมง (0=ทั้งหมด)")
    parser.add_argument("--sentiment",
                        choices=["any","positive","negative","strong_positive","strong_negative"],
                        default="any")
    parser.add_argument("--no-translate", action="store_true", help="ไม่แปลภาษาไทย")
    parser.add_argument("--json", action="store_true")

    args = parser.parse_args()

    print(f"\n📰 กำลังดึงข่าวสำหรับ {args.symbol} (ย้อนหลัง {args.hours}h)...")
    if not args.no_translate:
        print("   🔄 จะแปลภาษาไทย (ใช้เวลาสักครู่...)")

    news = fetch_news(
        args.symbol,
        max_news=args.max,
        hours_back=args.hours,
        translate=not args.no_translate,
    )

    if args.json:
        print(json.dumps(news, ensure_ascii=False, indent=2))
        return

    if not news:
        print(f"\n  ไม่พบข่าวใน {args.hours}h ที่ผ่านมา")
        return

    print(f"\n{'='*65}")
    print(f"  พบ {len(news)} ข่าว")
    print(f"{'='*65}")

    sentiment_icon = {"positive":"🟢","slightly_positive":"🟡","negative":"🔴","slightly_negative":"🟠","neutral":"⚪"}

    for i, n in enumerate(news, 1):
        si = sentiment_icon.get(n["sentiment"]["label"], "⚪")
        print(f"\n  {i}. {si} [{n['sentiment']['label'].upper()}] score={n['sentiment']['score']:+d}")
        print(f"     EN: {n['title']}")
        if n["title_th"] != n["title"]:
            print(f"     TH: {n['title_th']}")
        print(f"     📰 {n['publisher']}  |  {n['published_at']}")
        if n["sentiment"]["positive_hits"]:
            print(f"     ➕ {', '.join(n['sentiment']['positive_hits'])}")
        if n["sentiment"]["negative_hits"]:
            print(f"     ➖ {', '.join(n['sentiment']['negative_hits'])}")

    # Check alert
    alert = {"condition": args.sentiment, "min_news": 1}
    triggered, matching = check_news_sentiment(alert, news)
    print(f"\n{'='*65}")
    print(f"  เงื่อนไข [{args.sentiment}]: {'✅ TRIGGERED' if triggered else '⬜ Not triggered'}")
    print(f"  ตรงเงื่อนไข: {len(matching)}/{len(news)} ข่าว")
    print()


if __name__ == "__main__":
    main()
