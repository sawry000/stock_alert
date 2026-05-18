# 📊 Stock Price Alert System

ระบบ alert ราคาหุ้นอัตโนมัติ รันบน GitHub Actions → ส่งแจ้งเตือนเข้า Telegram

---

## 🗂️ โครงสร้างไฟล์

```
stock-alert-system/
├── .github/
│   └── workflows/
│       └── check.yml        ← GitHub Actions cron job
├── alert_engine.py          ← Python engine หลัก
├── watchlist.json           ← Config หุ้น + เงื่อนไข (แก้ไขได้เอง)
├── state.json               ← State tracking (auto-updated)
├── alert_log.json           ← Alert history log (auto-updated)
├── dashboard.html           ← Web UI ดู watchlist + log
└── README.md
```

---

## 🚀 วิธีติดตั้ง (ทำครั้งเดียว ~5 นาที)

### Step 1 — สร้าง GitHub Repository

1. ไปที่ https://github.com/new
2. ตั้งชื่อ repo เช่น `stock-alerts`
3. เลือก **Private** (ปลอดภัยกว่า)
4. กด **Create repository**
5. Upload ไฟล์ทั้งหมดขึ้นไป (drag & drop หรือใช้ git)

### Step 2 — ตั้ง Telegram Secrets

1. ไปที่ repo → **Settings** → **Secrets and variables** → **Actions**
2. กด **New repository secret** แล้วใส่:

| Secret Name | ค่า |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Token จาก @BotFather เช่น `7123456789:AAHxxxxxxx` |
| `TELEGRAM_CHAT_ID` | Chat ID ของคุณ เช่น `123456789` |

> วิธีหา Chat ID: ส่งข้อความหา bot ของคุณก่อน แล้วเปิด
> `https://api.telegram.org/bot{TOKEN}/getUpdates`
> ดูค่า `message.chat.id`

### Step 3 — เปิดใช้ GitHub Actions

1. ไปที่ repo → **Actions** tab
2. ถ้ามี popup ถามให้ enable → กด **I understand my workflows, go ahead and enable them**
3. ระบบจะรันอัตโนมัติตาม cron schedule ที่ตั้งไว้

### Step 4 — ทดสอบ manual run

1. ไปที่ **Actions** → **Stock Price Alert**
2. กด **Run workflow** → **Run workflow**
3. รอ 1-2 นาที ดู logs
4. เช็ค Telegram ว่าได้รับ message หรือไม่

---

## ⚙️ การตั้งค่า watchlist.json

### เพิ่มหุ้นใหม่

```json
{
  "symbol": "TSLA",
  "name": "Tesla Inc.",
  "market": "US",
  "timeframe": "1D",
  "enabled": true,
  "alerts": [
    {
      "id": "TSLA_BUY_200",
      "type": "price_target",
      "direction": "below_or_equal",
      "target_price": 200.00,
      "note": "Buy zone รับแนวต้านเก่า",
      "action": "BUY",
      "emoji": "📥",
      "cooldown_minutes": 240
    }
  ]
}
```

### ประเภท Alert ที่รองรับ

| type | คำอธิบาย | ฟิลด์ที่ต้องมี |
|---|---|---|
| `price_target` | ราคาถึงจุดที่กำหนด | `target_price`, `direction` |
| `percent_change` | ราคาขึ้น/ลง X% ใน 1 วัน | `threshold_pct`, `direction` |
| `volume_spike` | ปริมาณซื้อขายพุ่ง X เท่า | `multiplier` |
| `support_resistance` | ราคาทะลุแนวรับ/แนวต้าน | `level`, `direction` |

### direction options

- `price_target`: `"below_or_equal"` หรือ `"above_or_equal"`
- `percent_change`: `"down"` หรือ `"up"`
- `support_resistance`: `"break_below"` หรือ `"break_above"`

---

## 📱 ตัวอย่าง Telegram Message

```
📥 ALERT: ATER (Aterian Inc.)

💰 Price: $0.9750  📉 -2.50%
🎯 Target hit: $0.9800
⚡ Signal: BUY
⏱ Timeframe: 4H
📋 Note: EMA50 (4H) — Buy zone

📊 View on TradingView
🕐 2024-01-15T14:30:00Z
```

---

## 📊 Web Dashboard

เปิดไฟล์ `dashboard.html` ในเครื่องที่มี repo อยู่:

```bash
# Python simple server
python3 -m http.server 8080
# แล้วเปิด http://localhost:8080/dashboard.html
```

หรือ enable GitHub Pages:
- Settings → Pages → Source: main branch / root
- เข้าได้ที่ `https://yourusername.github.io/stock-alerts/dashboard.html`

---

## ⏱️ GitHub Actions Quota

| Plan | นาที/เดือน |
|---|---|
| Free | 2,000 |
| Pro | 3,000 |

ระบบนี้ใช้ประมาณ **~1,200 นาที/เดือน** (รันเฉพาะช่วง market hours)

---

## 🔧 Symbols ที่รองรับ

| ตลาด | ตัวอย่าง Symbol |
|---|---|
| US Stocks | `AAPL`, `TSLA`, `ATER`, `GPRO` |
| SET ไทย | `PTT.BK`, `ADVANC.BK`, `AOT.BK` |
| Crypto | `BTC-USD`, `ETH-USD`, `SOL-USD` |
| Forex | `EURUSD=X`, `USDJPY=X`, `USDTHB=X` |
