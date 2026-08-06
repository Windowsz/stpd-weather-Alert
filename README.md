# 🌧️ Rain Alert - แจ้งเตือนฝนล่วงหน้าผ่าน Telegram

โปรเจกต์นี้ใช้ Python สแกนพยากรณ์อากาศจาก [Open-Meteo API](https://open-meteo.com/) (ฟรี ไม่ต้องใช้ API Key)
เพื่อเช็คโอกาสเกิดฝนล่วงหน้า 1 ชั่วโมง และส่งข้อความแจ้งเตือนเข้า Telegram โดยอัตโนมัติทุก 5 นาที
ผ่าน GitHub Actions (บอทจะแจ้งเตือนแค่ครั้งเดียวต่อรอบพยากรณ์ 1 ชั่วโมง ไม่สแปมซ้ำแม้เช็คถี่)
นอกจากนี้ยังสามารถ **ถามพยากรณ์ฝนของสถานที่อื่นได้ทันทีผ่าน Telegram**
โดยการส่งลิงก์ Google Maps, พิกัดตรงๆ, หรือแชร์พิกัด เข้าไปในแชท บอทจะแกะพิกัดแล้วตอบกลับผลพยากรณ์ให้อัตโนมัติ

> ⚠️ หมายเหตุ: 5 นาทีคือ**ค่าต่ำสุดที่ GitHub Actions รองรับได้จริง** ตามเอกสารทางการของ GitHub
> (เคยลองตั้งทุก 1 นาทีแล้ว แต่ GitHub ไม่รันตามจริง — ปล่อยเงียบไปหลายนาทีโดยไม่มี run เกิดขึ้นเลย)
> และถึงจะตั้ง 5 นาที ก็ยังไม่การันตี exact timing 100% ช่วงที่ระบบ GitHub โหลดสูงอาจดีเลย์เพิ่มได้อีก
> ถ้าอยากได้คำตอบทันที ให้กด **Run workflow** เองแทนการรอ

## 📍 พิกัดบ้าน (ใช้เช็คพยากรณ์ตามตารางเวลา)

| ค่า | รายละเอียด |
| --- | --- |
| Latitude | `13.8628558` |
| Longitude | `100.4303806` |
| Timezone | `Asia/Bangkok` |

สามารถแก้ไขพิกัดได้ที่ตัวแปร `LATITUDE` และ `LONGITUDE` ในไฟล์ `main.py`

## ⚙️ เงื่อนไขการแจ้งเตือน

ระบบจะส่งข้อความแจ้งเตือนเมื่อข้อมูลพยากรณ์ล่วงหน้า 1 ชั่วโมง เข้าเงื่อนไขข้อใดข้อหนึ่งต่อไปนี้:

- โอกาสเกิดฝน (`precipitation_probability`) **มากกว่าหรือเท่ากับ 50%**
- ปริมาณฝนรวม (`precipitation` + `showers`) **มากกว่า 0.1 มม.**

## 🗺️ ถามพยากรณ์ฝนของสถานที่อื่นผ่าน Telegram (Google Maps Link)

นอกจากแจ้งเตือนอัตโนมัติของพิกัดบ้านแล้ว สามารถเช็คพยากรณ์ฝนของสถานที่ใดก็ได้ทันที โดย:

ส่งข้อมูลตำแหน่งแบบใดแบบหนึ่งต่อไปนี้เข้าไปในแชทกับ Bot ของคุณ:

- **ลิงก์ Google Maps** — เปิด Google Maps เลือกตำแหน่งที่ต้องการเช็ค กด **แชร์ (Share)** แล้วคัดลอกลิงก์มาส่ง
  (รองรับทั้งลิงก์เต็ม เช่น `https://www.google.com/maps/@13.75,100.50,15z` และลิงก์ย่อ เช่น
  `https://maps.app.goo.gl/xxxxx` — ยกเว้นลิงก์แชร์ "ร้าน/สถานที่" บางแบบที่ไม่มีพิกัดฝังอยู่เลย
  กรณีนี้บอทจะตอบกลับแจ้งให้ลองวิธีอื่นแทน)
- **พิกัดตรงๆ** — พิมพ์หรือวางพิกัดในรูปแบบ `lat, lon` เช่น `13.7605620, 100.5680219`
  ส่งเป็นข้อความธรรมดาได้เลย (ปนกับข้อความอื่นก็ยังจับได้ เช่น "เช็คฝนที่ 13.76, 100.56 หน่อย")
- **แชร์ตำแหน่งผ่าน Telegram** — กดปุ่ม 📎 แล้วเลือก **Location** เพื่อแชร์พิกัดปัจจุบันโดยตรง (แม่นยำที่สุด)

จากนั้น:
1. รอจนกว่า Workflow จะรันรอบถัดไป (สูงสุด ~5 นาที ตาม Cron แต่ GitHub อาจดีเลย์ได้ในบางช่วง)
   หรือกด **Run workflow** เพื่อให้ตอบกลับทันที
2. บอทจะตอบกลับข้อความพยากรณ์ฝนของพิกัดนั้น พร้อมลิงก์เปิดใน Google Maps

> หมายเหตุ: เพื่อความปลอดภัย บอทจะตอบกลับเฉพาะข้อความที่ส่งมาจากแชทที่ตรงกับ `TELEGRAM_CHAT_ID`
> ที่ตั้งค่าไว้ใน Secrets เท่านั้น ข้อความจากแชทอื่นจะถูกเพิกเฉย
>
> การอ่านข้อความใหม่ ๆ (Telegram `getUpdates`) ต้องใช้การจำ "ตำแหน่งข้อความล่าสุดที่อ่านแล้ว" (`last_update_id`)
> ซึ่งเก็บไว้ในไฟล์ `data/state.json` และ Workflow จะ commit ไฟล์นี้กลับเข้า Repository อัตโนมัติหลังรันทุกครั้ง
> (ต้องเปิดสิทธิ์ `contents: write` ให้ Workflow ซึ่งตั้งค่าไว้ให้แล้วใน `check-rain.yml`)

---

## 🚀 ขั้นตอนการตั้งค่า

### 1. สร้าง Telegram Bot ผ่าน @BotFather

1. เปิด Telegram แล้วค้นหา [@BotFather](https://t.me/BotFather)
2. พิมพ์คำสั่ง `/newbot` แล้วกดส่ง
3. ตั้งชื่อ Bot ตามที่ต้องการ (เช่น `My Rain Alert Bot`)
4. ตั้ง username ของ Bot (ต้องลงท้ายด้วย `bot` เช่น `my_rain_alert_bot`)
5. เมื่อสร้างสำเร็จ BotFather จะส่ง **Token** กลับมา หน้าตาประมาณนี้:
   ```
   123456789:AAExampleTokenXXXXXXXXXXXXXXXXXXXXX
   ```
   เก็บค่านี้ไว้ นี่คือค่า `TELEGRAM_BOT_TOKEN`

### 2. หา Chat ID ผ่าน @userinfobot

1. ค้นหา [@userinfobot](https://t.me/userinfobot) ใน Telegram
2. กด Start หรือพิมพ์อะไรก็ได้ส่งไปคุยกับ Bot
3. Bot จะตอบกลับข้อมูลของคุณ รวมถึง `Id` ซึ่งเป็นตัวเลข เช่น `987654321`
   เก็บค่านี้ไว้ นี่คือค่า `TELEGRAM_CHAT_ID`

> ⚠️ **สำคัญ:** หลังจากได้ Token และ Chat ID แล้ว ให้เปิดแชทกับ Bot ที่สร้างไว้ในขั้นตอนที่ 1
> แล้วกด **Start** หรือส่งข้อความอย่างน้อย 1 ครั้ง เพื่อให้ Bot สามารถส่งข้อความกลับมาหาคุณได้

### 3. นำโค้ดขึ้น GitHub Repository

1. สร้าง Repository ใหม่บน GitHub (Public หรือ Private ก็ได้)
2. อัปโหลดไฟล์ทั้งหมดในโปรเจกต์นี้ขึ้น Repository เช่น:
   ```bash
   git init
   git add .
   git commit -m "Initial commit: Rain Alert project"
   git branch -M main
   git remote add origin https://github.com/<your-username>/<your-repo>.git
   git push -u origin main
   ```

### 4. ตั้งค่า Secrets ใน GitHub Repository

1. เข้าไปที่หน้า Repository บน GitHub
2. ไปที่ **Settings** > **Secrets and variables** > **Actions**
3. กด **New repository secret** แล้วเพิ่ม Secrets ทั้ง 2 ตัวดังนี้:

   | Name | Value |
   | --- | --- |
   | `TELEGRAM_BOT_TOKEN` | Token ที่ได้จาก @BotFather |
   | `TELEGRAM_CHAT_ID` | Chat ID ที่ได้จาก @userinfobot |

4. กด **Add secret** เพื่อบันทึกแต่ละค่า

### 5. เปิดใช้งาน GitHub Actions

1. ไปที่แท็บ **Actions** ของ Repository
2. หากมีข้อความให้ยืนยันการเปิดใช้งาน Workflow ให้กด **I understand my workflows, go ahead and enable them**
3. เลือก Workflow ชื่อ **Rain Alert Automation**
4. ทดสอบรันด้วยตนเองโดยกด **Run workflow** (ปุ่มนี้มาจาก `workflow_dispatch`)
5. เมื่อรันสำเร็จ ระบบจะเริ่มทำงานอัตโนมัติทุก 5 นาที ตาม Cron `*/5 * * * *`
   (ไม่ต้องกังวลเรื่องแจ้งเตือนซ้ำ บอทแจ้งเตือนแค่ครั้งเดียวต่อรอบพยากรณ์ 1 ชั่วโมง)

---

## 🗂️ โครงสร้างโปรเจกต์

```
.
├── .github/
│   └── workflows/
│       └── check-rain.yml    # GitHub Actions Workflow
├── data/
│   └── state.json            # เก็บ last_update_id ของ Telegram (สร้างอัตโนมัติ)
├── main.py                   # สคริปต์หลักสำหรับเช็คพยากรณ์, ส่งแจ้งเตือน, และตอบคำถามพิกัด
├── requirements.txt          # รายการไลบรารีที่ต้องใช้
└── README.md
```

## 🧪 การทดสอบรันบนเครื่องตัวเอง (Local)

```bash
pip install -r requirements.txt

export TELEGRAM_BOT_TOKEN="your-bot-token"
export TELEGRAM_CHAT_ID="your-chat-id"

python main.py
```

## 📝 หมายเหตุ

- Open-Meteo API เป็นบริการฟรีและไม่ต้องใช้ API Key
- GitHub Actions บน Free Plan มีข้อจำกัดเรื่องความแม่นยำของเวลา Schedule (อาจดีเลย์ได้บ้างเล็กน้อย)
- หากต้องการปรับเงื่อนไขการแจ้งเตือน สามารถแก้ไขค่าคงที่ `RAIN_PROBABILITY_THRESHOLD`
  และ `RAIN_AMOUNT_THRESHOLD` ได้ในไฟล์ `main.py`
