# Stock Alert PRO — GitHub Actions Edition v2.0

ระบบแจ้งเตือนหุ้น/crypto อัตโนมัติบน GitHub Actions ส่งแจ้งเตือนผ่าน Telegram  
รองรับ **12 alert types** ในไฟล์เดียว ไม่มีค่าใช้จ่าย

---

## 📁 โครงสร้างไฟล์

```
repo/
├── alert_engine.py              ← Engine หลัก (12 alert types)
├── watchlist.json               ← รายการหุ้นและเงื่อนไข alert
├── state.json                   ← Cooldown state (auto-updated)
├── alert_log.json               ← ประวัติ alert (auto-updated)
├── dashboard_pro.html           ← จัดการ watchlist.json แบบ visual
├── .github/workflows/check.yml  ← GitHub Actions workflow
│
├── module_rsi.py                ← Module 1: RSI (standalone)
├── module_ma_cross.py           ← Module 2: MA Crossover (standalone)
├── module_candle.py             ← Module 3: Candle Pattern (standalone)
├── module_news.py               ← Module 4: News + แปลไทย (standalone)
├── module_position.py           ← Module 5: Position Sizing (standalone)
├── module_mtf.py                ← Module 6: Multi-Timeframe (standalone)
├── module_score.py              ← Module 7: Confidence Score (standalone)
└── module_backtest.py           ← Module 8: Backtest (standalone)
```

---

## 🚀 วิธี Deploy

### 1. สร้าง GitHub Repository

```bash
git init
git add .
git commit -m "feat: Stock Alert PRO v2.0"
git remote add origin https://github.com/USERNAME/REPO.git
git push -u origin main
```

### 2. ตั้งค่า GitHub Secrets

ไปที่ **repo → Settings → Secrets and variables → Actions → New repository secret**

| Secret Name | ค่า |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Token จาก @BotFather |
| `TELEGRAM_CHAT_ID` | Chat ID ของคุณ |

> หา Chat ID ได้จาก @userinfobot หรือ @getidsbot ใน Telegram

### 3. ตรวจสอบ GitHub Actions

ไปที่ **repo → Actions** — จะเห็น workflow `Stock Alert PRO` รันอัตโนมัติ

---

## 📋 Alert Types ที่รองรับ (12 types)

| Type | คำอธิบาย |
|---|---|
| `price_target` | แจ้งเตือนเมื่อราคาถึงเป้า |
| `percent_change` | แจ้งเตือนเมื่อราคาขึ้น/ลง X% |
| `volume_spike` | Volume พุ่งผิดปกติ X เท่า |
| `support_resistance` | ทะลุแนวรับ/ต้าน |
| `rsi` | RSI Oversold/Overbought |
| `ma_crossover` | EMA/SMA Golden Cross / Death Cross |
| `candle_pattern` | 14 Candle Patterns เช่น Hammer, Engulfing |
| `news_sentiment` | ข่าวล่าสุด + แปลไทย + Sentiment |
| `position_size` | คำนวณ position size (Fixed Risk/Kelly/ATR) |
| `mtf_alignment` | Multi-Timeframe trend alignment |
| `alert_score` | Confidence Score 0–100 จาก 7 indicators |
| `backtest_check` | Backtest rule ย้อนหลัง trigger ถ้าผ่าน win rate |

---

## ⏰ Cron Schedule

| เวลา UTC | เวลา ICT | ความถี่ | ครอบคลุม |
|---|---|---|---|
| `*/5 8-12 * * 1-5` | 15:00–19:55 | ทุก 5 นาที | US Pre-market |
| `*/5 13-20 * * 1-5` | 20:00–03:55+1 | ทุก 5 นาที | US Market Hours |
| `*/15 21-23,0-7 * * *` | 04:00–14:55 | ทุก 15 นาที | Crypto 24/7 |
| `0 1 * * *` | 08:00 | วันละครั้ง | Daily Summary |

---

## 💻 Standalone Modules (ใช้งานอิสระได้)

```bash
# ติดตั้ง dependency
pip install yfinance

# RSI
python3 module_rsi.py --symbol AAPL --condition oversold

# MA Crossover
python3 module_ma_cross.py --symbol BTC-USD --fast 9 --slow 21

# Candle Pattern
python3 module_candle.py --symbol TSLA --pattern all

# News + แปลไทย
python3 module_news.py --symbol NVDA --hours 24

# Position Sizing
python3 module_position.py --symbol AAPL --account 10000 --risk 2

# Multi-Timeframe
python3 module_mtf.py --symbol BTC-USD --timeframes 1h 4h 1d

# Confidence Score
python3 module_score.py --symbol AAPL --direction bullish

# Backtest
python3 module_backtest.py --symbol AAPL --rule rsi_oversold --days 180
```

---

## 🔧 จัดการ watchlist.json

ใช้ `dashboard_pro.html` — เปิดในเบราว์เซอร์โดยตรง (ไม่ต้อง server):

1. เปิด `dashboard_pro.html` ในเบราว์เซอร์
2. กด **📂 Load watchlist.json**
3. เพิ่ม/แก้ไข/ลบ alerts ตามต้องการ
4. กด **💾 Download JSON**
5. Push ไฟล์ใหม่ขึ้น GitHub

---

## ⚠️ Security Notes

- ห้าม hardcode Telegram token ในไฟล์ใดๆ
- ใช้ GitHub Secrets เท่านั้น
- `state.json` และ `alert_log.json` จะถูก commit อัตโนมัติโดย GitHub Actions

---

## 📊 GitHub Actions Free Tier

- **2,000 นาที/เดือน** สำหรับ public repo (ฟรีไม่จำกัด)
- **2,000 นาที/เดือน** สำหรับ private repo
- ประมาณการใช้งาน: ~800 นาที/เดือน (ปลอดภัย)
