# 🌧️ Rain Alert - แจ้งเตือนฝนล่วงหน้าผ่าน Telegram

โปรเจกต์นี้ใช้ Python สแกนพยากรณ์อากาศจาก [Open-Meteo API](https://open-meteo.com/) (ฟรี ไม่ต้องใช้ API Key)
เพื่อเช็คโอกาสเกิดฝนล่วงหน้า 1 ชั่วโมง และส่งข้อความแจ้งเตือนเข้า Telegram โดยอัตโนมัติทุก 30 นาที
ผ่าน GitHub Actions

## 📍 พิกัดที่ใช้เช็คพยากรณ์

| ค่า | รายละเอียด |
| --- | --- |
| Latitude | `13.866` |
| Longitude | `100.443` |
| Timezone | `Asia/Bangkok` |

สามารถแก้ไขพิกัดได้ที่ตัวแปร `LATITUDE` และ `LONGITUDE` ในไฟล์ `main.py`

## ⚙️ เงื่อนไขการแจ้งเตือน

ระบบจะส่งข้อความแจ้งเตือนเมื่อข้อมูลพยากรณ์ล่วงหน้า 1 ชั่วโมง เข้าเงื่อนไขข้อใดข้อหนึ่งต่อไปนี้:

- โอกาสเกิดฝน (`precipitation_probability`) **มากกว่าหรือเท่ากับ 50%**
- ปริมาณฝนรวม (`precipitation` + `showers`) **มากกว่า 0.1 มม.**

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
5. เมื่อรันสำเร็จ ระบบจะเริ่มทำงานอัตโนมัติทุก 30 นาที ตาม Cron `0,30 * * * *`

---

## 🗂️ โครงสร้างโปรเจกต์

```
.
├── .github/
│   └── workflows/
│       └── check-rain.yml    # GitHub Actions Workflow
├── main.py                   # สคริปต์หลักสำหรับเช็คพยากรณ์และส่งแจ้งเตือน
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
