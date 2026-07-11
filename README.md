# SpeedZ Alert — Halal Stock Alert PRO (Autonomous Pipeline)

ระบบ screen หุ้น + แจ้งเตือนอัตโนมัติ รันเต็มรูปแบบบน **GitHub Actions** ไม่มีค่าใช้จ่าย
คัดกรองหุ้น **Halal** จาก universe ~1,065 ตัว → เข้า watchlist อัตโนมัติ → ยิงสัญญาณ BUY/SELL
ผ่าน **Telegram** (ข้อความภาษาไทย) พร้อมแดชบอร์ดจัดการแบบ visual

> 📊 สถานะปัจจุบัน: Universe **~1,065 ตัว** | Watchlist **~69/100 ตัว** (ตัวเลขจริงดูได้ที่ Telegram รายงานประจำวัน หรือ Universe Manager)

---

## 🧩 ภาพรวมสถาปัตยกรรม

ระบบแบ่งเป็น **2 job หลัก** ใน workflow เดียว (`.github/workflows/check.yml`) ทำงานแยกอิสระจากกัน:

```
┌─────────────────────────┐         ┌──────────────────────────┐
│  🔍 daily-screener        │         │  🚨 alert-engine            │
│  (daily_screener.py)     │         │  (alert_engine.py)        │
│  รันวันละ 1 ครั้ง          │         │  รันถี่ (~ทุก 15 นาที)      │
│  timeout 65 นาที          │         │  timeout 15 นาที           │
│                          │         │                            │
│  หา universe → watchlist  │         │  เช็ค watchlist → ยิง       │
│  ตัวใหม่ + เช็คตัวที่ควรออก │         │  BUY/SELL signal ผ่าน      │
└─────────────────────────┘         │  Telegram                 │
                                     └──────────────────────────┘
```

ควบคุมว่า job ไหนจะรันผ่าน input `run_mode` ตอน trigger (`workflow_dispatch`):

| run_mode | daily-screener รัน? | alert-engine รัน? |
|---|:---:|:---:|
| `alert_only` (default) | ❌ | ✅ |
| `screener_only` | ✅ | ❌ |
| `full_pipeline` | ✅ | ✅ |

**Trigger จาก Google Apps Script** (ภายนอก repo) — ปัจจุบันตั้งความถี่ไว้ที่ **~15 นาที**
พร้อมช่วง **07:00–09:00 ICT บล็อกไม่ให้ alert-engine รัน** (ให้เวลา daily-screener รันให้เสร็จก่อน
กันไม่ให้สอง job แย่ง push ไฟล์เดียวกันจนชนกัน — ดูหัวข้อ "หมายเหตุการทำงาน" ด้านล่าง)

---

## 📁 โครงสร้างไฟล์ (ของจริงในปัจจุบัน)

```
repo/
├── daily_screener.py            ← หา universe ใหม่เข้า watchlist + เช็คตัวที่ควรถอด
├── alert_engine.py              ← เช็ค watchlist ทุกตัว ยิง BUY/SELL ผ่าน Telegram
├── add_stock.py                 ← เพิ่มหุ้นเข้า watchlist แบบ manual (ใช้ Gemini AI เลือก template)
├── gemini_client.py             ← wrapper เรียก Gemini API (screener ใช้เลือก template หุ้น)
├── manual_check.py              ← สคริปต์ตรวจ/sync universe.json แบบ manual
│
├── watchlist.json               ← หุ้นที่ติดตามอยู่ + alert config ต่อตัว (auto-updated)
├── universe.json                ← หุ้นทั้งหมดที่ผ่าน Halal screen (auto-updated)
├── state.json                   ← cooldown / open position tracking (auto-updated)
├── alert_log.json               ← ประวัติ alert ที่ยิงไปแล้ว (auto-updated, เก็บ 500 รายการล่าสุด)
├── screener_log.json            ← ประวัติการ scan ของ daily_screener.py
├── screener_state.json          ← state ภายในของ screener (auto-updated)
│
├── dashboard_pro.html           ← แดชบอร์ดหลัก: Watchlist Manager + Universe Manager
├── dashboard.html               ← แดชบอร์ดรุ่นเก่า/สำรอง
├── universe_manager.html        ← แดชบอร์ดจัดการ universe แบบแยก (เวอร์ชันย่อย)
├── portfolio_dashboard.html     ← แดชบอร์ดสรุป portfolio/position
│
├── .github/workflows/
│   ├── check.yml                ← workflow หลัก (2 job: daily-screener, alert-engine)
│   └── manual_check.yml         ← workflow แยกสำหรับรัน manual_check.py
│
└── module_*.py                  ← โมดูลคำนวณแบบ standalone (ดูหัวข้อด้านล่าง)
    ├── module_rsi.py
    ├── module_ma_cross.py
    ├── module_candle.py
    ├── module_news.py
    ├── module_position.py
    ├── module_mtf.py
    ├── module_score.py
    └── module_backtest.py
```

---

## 🔍 daily_screener.py — ทำอะไรบ้าง

```
universe.json (~1,065 ตัว)
      ↓
 [GATE 1] Halal Check
      ↓ ✅ HALAL only
 [GATE 2] Purify% Filter (default ≤ 5%)
      ↓ ✅
 [GATE 3] Liquidity — Volume + Price > $1
      ↓ ✅
 [GATE 4] ADR > threshold% (default 8%)
      ↓ ✅
 [GATE 5] Trend — Price > EMA50
      ↓ ✅
 [GATE 6] Momentum — RSI 40–70 + Volume ≥ 1.5x เฉลี่ย
      ↓ ✅ ผ่านครบทุก gate
 Gemini AI เลือก Template ที่เหมาะสม → เพิ่มเข้า watchlist.json
```

**พร้อมกันนั้น** จะเช็คหุ้นทุกตัวที่อยู่ใน watchlist อยู่แล้วว่าควรถอดออกไหม (PHASE 1: Removal Check):
- Halal status เปลี่ยนเป็น NOT HALAL
- Purify% เกิน threshold
- ADR ลดลงต่ำกว่า 5% ต่อเนื่อง ≥ 3 วัน
- Death Cross + Volume หดตัว

หุ้นที่ถูกถอดจะกลับไปกอง universe.json รอโอกาสใหม่ ไม่หายไปไหน

> ⚠️ **ใช้เวลานาน** — สแกนหุ้นเป็นพันตัว รันได้นานถึง ~47–65 นาที นี่คือเหตุผลที่ต้องบล็อก
> alert-engine ไม่ให้รันพร้อมกัน (ดูหัวข้อหมายเหตุด้านล่าง)

---

## 🚨 alert_engine.py — ทำอะไรบ้าง

วิ่งลูปหุ้นทุกตัวใน watchlist (~69 ตัว) เช็คทุก alert ที่ตั้งไว้ต่อตัว แล้วยิง Telegram เมื่อเงื่อนไขผ่าน

### Alert types ที่ใช้งานจริงตอนนี้

| Type | Action | คำอธิบาย |
|---|---|---|
| `rsi` | BUY | RSI Oversold |
| `ma_crossover` | BUY / SELL | Golden Cross (BUY) / Death Cross (SELL) |
| `alert_score` | BUY / SELL | Confidence Score รวมจากหลาย indicator |
| `mtf_alignment` | BUY | แนวโน้มสอดคล้องกันหลาย timeframe (1h/4h/1d) |
| `volume_spike` | BUY | Volume พุ่งผิดปกติ (2-3 เท่าเฉลี่ย) |
| `percent_change` | BUY / SELL | ราคาขึ้น/ลงเกิน threshold ที่ตั้งไว้ |
| `support_resistance` | SELL | ราคาหลุดแนวรับ (ใช้เป็น Stop Loss หลัก) |

### Multi-Layer Signal Gate (สำหรับ BUY)

ก่อนยิง BUY ต้องผ่านครบทุกชั้น:
1. **Macro Gate** — ระงับถ้า SPY < -1% หรือ BTC < -3% ในวันนั้น (`get_macro_context()`)
2. **Position Gate** — ถ้ามี position เปิดอยู่แล้ว จะ**ไม่เขียนทับ** entry price/เวลาเดิม (ยัง
   ส่ง alert แจ้งเตือนได้ตามปกติ แต่ข้อมูล P&L ที่ track ไว้จะอ้างอิงจากการซื้อครั้งแรกเสมอ)
3. **Cooldown Gate** — ทั้ง cooldown ต่อ alert_id และ re-entry cooldown ต่อหุ้น (ตั้งค่าได้ต่อตัว)
4. **Conviction Gate** — ให้คะแนน 4 มิติ (Trend / Momentum / Volume / Volatility) ต้องผ่านขั้นต่ำ
   ตามที่ตั้งไว้ ยิ่งผ่านครบ 4/4 = "🔥 TOP SIGNAL"

### Position Tracking + รายงานสรุป

ทุกครั้งที่ BUY สำเร็จ (และไม่มี position ค้างอยู่) จะบันทึก `open_entry` / `open_time` /
`open_peak` / `open_stop` / `open_target` ไว้ใน `state.json` — SELL จะเคลียร์ค่าพวกนี้ทิ้ง

ทุกวันตอน **UTC hour 2 (~09:00–09:59 ICT)** — เวลาแรกหลังช่วงบล็อกเช้าจบ — จะส่ง 2 รายงาน:
- **Daily Summary** — สรุปภาพรวม watchlist วันนั้น (ขึ้น/ลงแรงสุด 5 ตัว)
- **Position Status** — สรุป P&L ของทุก position ที่เปิดอยู่ (% กำไร-ขาดทุน, ระยะห่างจาก
  SL/TP, ถือมากี่วัน, คำแนะนำ next-step แบบ rule-based)

ตั้งเวลาได้ที่ `watchlist.json → settings.daily_summary_hour_utc` / `position_status_hour_utc`
(หน่วยเป็น UTC — ต้องเลี่ยงช่วงที่บล็อก alert-engine ไว้)

---

## 🖥️ dashboard_pro.html — แดชบอร์ดหลัก

เปิดตรงในเบราว์เซอร์ได้เลย ไม่ต้องมี server, sync ข้อมูลผ่าน GitHub API (ต้องใส่ PAT เอง)

**Watchlist Manager**
- รายชื่อหุ้น — ดู/แก้ไข/ลบ alert ต่อตัว
- กลุ่มหุ้น (Sector Flow) — จัดกลุ่มหุ้นใน watchlist ตาม Sector/Industry จริง เรียงตาม
  % เปลี่ยนแปลงถ่วงน้ำหนักด้วยมูลค่าซื้อขาย ($-weighted) พร้อมราคา/%/เวลาอัปเดตล่าสุดต่อตัว
  และ dropdown แก้กลุ่มเองได้

**Universe Manager**
- รายชื่อ — ดูหุ้นทั้งหมดใน universe (~1,065 ตัว) พร้อมสถานะว่าอยู่ใน watchlist หรือยัง
- เพิ่มหุ้น / เพิ่มหลายตัว (bulk)
- Filter Config — ปรับเกณฑ์ gate ของ screener
- กลุ่มหุ้น (Sector Flow) — เหมือน Watchlist Manager แต่ scope ครอบคลุมทั้ง universe

**Portfolio Health / Alert Log / State-Cooldown** — ดูสถานะรวมของระบบและประวัติ alert

---

## 🚀 วิธี Deploy

### 1. ตั้งค่า GitHub Secrets

**repo → Settings → Secrets and variables → Actions → New repository secret**

| Secret | ใช้ทำอะไร | จำเป็น? |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Token จาก @BotFather | ✅ ต้องมี |
| `TELEGRAM_CHAT_ID` | Chat ID ปลายทาง (หา ได้จาก @userinfobot) | ✅ ต้องมี |
| `GEMINI_API_KEY` | ให้ screener เลือก template หุ้นอัตโนมัติ | ⚙️ optional |

### 2. Trigger workflow

ไปที่ **Actions → Halal Stock Alert PRO — Autonomous Pipeline → Run workflow**
เลือก `run_mode` ตามต้องการ (ปกติปล่อยให้ Google Apps Script ยิงอัตโนมัติทุก ~15 นาที)

---

## ⚠️ หมายเหตุการทำงาน (สำคัญ อ่านก่อนแก้ค่า schedule)

- **daily-screener กับ alert-engine เขียนไฟล์ทับซ้อนกันได้** (`universe.json` ใช้ร่วมกันทั้งคู่)
  ถ้า trigger ถี่เกินไปหรือรันพร้อมกันบ่อย จะเกิด push conflict บ่อยขึ้น — ปัจจุบันแก้ด้วย 2 ทาง:
  1. ตั้ง `concurrency group` แยกกันต่อ job (กันรันซ้อนกับตัวเอง)
  2. **บล็อกไม่ให้ alert-engine รันช่วง 07:00–09:00 ICT** ที่ Apps Script (ให้เวลา screener
     รันคนเดียวจนเสร็จ) — ถ้าจะขยับช่วงเวลานี้ ต้องปรับ `daily_summary_hour_utc` /
     `position_status_hour_utc` ใน `watchlist.json` ให้เลี่ยงช่วงบล็อกด้วย ไม่งั้นรายงาน
     ทั้งสองจะไม่มีวันได้ส่งเลย (เคยเกิดปัญหานี้มาแล้ว — ตั้งชนกับช่วงบล็อกพอดี)
- **alert_engine.py มี try/except ครอบทั้งลูปเช็คหุ้นและส่วนสร้างรายงาน** — หุ้นตัวไหน error
  จะถูกข้ามไปตัวถัดไป ไม่ทำให้ทั้ง run ล่มและ state หายทั้งหมด
- **Re-entry บนหุ้นที่ถือ position อยู่แล้วจะไม่เขียนทับ entry เดิม** — ป้องกันไม่ให้ % กำไร-ขาดทุน
  ในรายงาน Position Status ผิดเพี้ยนเวลามี BUY signal ยิงซ้ำระหว่างที่ยังถือของเดิมอยู่

---

## 💻 Standalone Modules (ใช้แยกอิสระได้ นอกเหนือจาก pipeline หลัก)

```bash
pip install yfinance

python3 module_rsi.py --symbol AAPL --condition oversold
python3 module_ma_cross.py --symbol BTC-USD --fast 9 --slow 21
python3 module_candle.py --symbol TSLA --pattern all
python3 module_news.py --symbol NVDA --hours 24
python3 module_position.py --symbol AAPL --account 10000 --risk 2
python3 module_mtf.py --symbol BTC-USD --timeframes 1h 4h 1d
python3 module_score.py --symbol AAPL --direction bullish
python3 module_backtest.py --symbol AAPL --rule rsi_oversold --days 180
```

โมดูลพวกนี้เป็นเครื่องมือคำนวณแบบเดี่ยวๆ ไม่ได้ผูกกับ pipeline หลัก (`daily_screener.py` /
`alert_engine.py` มี logic คำนวณของตัวเองแยกต่างหาก) เผื่อไว้สำหรับเทส/วิเคราะห์หุ้นตัวใดตัวหนึ่ง
แบบ manual โดยไม่ต้องรันทั้งระบบ

---

## 🔧 Security Notes

- ห้าม hardcode Telegram token / Gemini key ในไฟล์ใดๆ — ใช้ GitHub Secrets เท่านั้น
- `state.json`, `alert_log.json`, `universe.json`, `watchlist.json` ถูก commit อัตโนมัติโดย
  GitHub Actions bot (`github-actions[bot]`) — อย่าแก้ไฟล์พวกนี้มือระหว่างที่ workflow กำลังรันอยู่
  เพราะจะชนกับ auto-commit ได้
- Dashboard (`dashboard_pro.html`) ต้องใส่ GitHub PAT เพื่อ sync ข้อมูล — เก็บไว้ใน
  `localStorage` ของเบราว์เซอร์ตัวเอง ไม่ได้ส่งไปที่ไหนอื่น
