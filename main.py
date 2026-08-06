#!/usr/bin/env python3
"""Rain Alert - checks Open-Meteo forecast and sends a Telegram alert if rain is likely soon."""

import logging
import os
import sys

import requests

LATITUDE = 13.866
LONGITUDE = 100.443
TIMEZONE = "Asia/Bangkok"
FORECAST_INDEX = 1  # 1 hour ahead in the hourly array

RAIN_PROBABILITY_THRESHOLD = 50  # percent
RAIN_AMOUNT_THRESHOLD = 0.1  # mm

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("rain-alert")


def fetch_forecast():
    """Fetch hourly precipitation forecast from Open-Meteo."""
    params = {
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "hourly": "precipitation_probability,precipitation,showers",
        "timezone": TIMEZONE,
        "forecast_days": 1,
    }

    logger.info(
        "Fetching forecast from Open-Meteo (lat=%s, lon=%s, tz=%s)...",
        LATITUDE, LONGITUDE, TIMEZONE,
    )
    response = requests.get(OPEN_METEO_URL, params=params, timeout=15)
    response.raise_for_status()
    data = response.json()
    logger.info("Forecast fetched successfully.")
    return data


def extract_forecast_point(data, index):
    """Extract a single hourly forecast point at the given index."""
    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    probabilities = hourly.get("precipitation_probability", [])
    precipitations = hourly.get("precipitation", [])
    showers = hourly.get("showers", [])

    if index >= len(times):
        raise IndexError(
            f"Requested index {index} is out of range for hourly data "
            f"(length={len(times)})"
        )

    return {
        "time": times[index],
        "precipitation_probability": probabilities[index],
        "precipitation": precipitations[index],
        "showers": showers[index],
    }


def should_alert(forecast_point):
    """Determine whether the rain conditions warrant an alert."""
    probability = forecast_point["precipitation_probability"] or 0
    precipitation = forecast_point["precipitation"] or 0
    showers = forecast_point["showers"] or 0
    total_rain = precipitation + showers

    triggered = probability >= RAIN_PROBABILITY_THRESHOLD or total_rain > RAIN_AMOUNT_THRESHOLD
    return triggered, probability, total_rain


def build_message(forecast_point, total_rain):
    """Build a nicely formatted Markdown message for Telegram."""
    return (
        "🌧️ *แจ้งเตือนฝนตก* 🌧️\n\n"
        f"📍 *พิกัด:* `{LATITUDE}, {LONGITUDE}`\n"
        f"🕐 *ช่วงเวลา:* `{forecast_point['time']}`\n\n"
        f"☔️ *โอกาสเกิดฝน:* `{forecast_point['precipitation_probability']}%`\n"
        f"💧 *ปริมาณฝน:* `{forecast_point['precipitation']} มม.`\n"
        f"🌦️ *Showers:* `{forecast_point['showers']} มม.`\n"
        f"📊 *รวมปริมาณน้ำฝน:* `{total_rain:.2f} มม.`\n\n"
        "_แนะนำให้เตรียมร่มหรือเสื้อกันฝนไว้ล่วงหน้า_"
    )


def send_telegram_message(bot_token, chat_id, message):
    """Send a Markdown-formatted message to a Telegram chat."""
    url = TELEGRAM_API_URL.format(token=bot_token)
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown",
    }

    logger.info("Sending alert message to Telegram chat_id=%s...", chat_id)
    response = requests.post(url, data=payload, timeout=15)
    response.raise_for_status()
    logger.info("Telegram message sent successfully.")
    return response.json()


def main():
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not bot_token or not chat_id:
        logger.error(
            "Missing required environment variables: "
            "TELEGRAM_BOT_TOKEN and/or TELEGRAM_CHAT_ID."
        )
        sys.exit(1)

    try:
        data = fetch_forecast()
        forecast_point = extract_forecast_point(data, FORECAST_INDEX)
    except (requests.RequestException, IndexError, KeyError) as exc:
        logger.error("Failed to fetch or parse forecast data: %s", exc)
        sys.exit(1)

    logger.info(
        "Forecast at %s -> probability=%s%%, precipitation=%s mm, showers=%s mm",
        forecast_point["time"],
        forecast_point["precipitation_probability"],
        forecast_point["precipitation"],
        forecast_point["showers"],
    )

    triggered, probability, total_rain = should_alert(forecast_point)

    if not triggered:
        logger.info(
            "No alert needed (probability=%s%%, total_rain=%.2f mm below thresholds).",
            probability, total_rain,
        )
        return

    logger.info(
        "Rain condition triggered! (probability=%s%%, total_rain=%.2f mm). Preparing alert...",
        probability, total_rain,
    )

    message = build_message(forecast_point, total_rain)

    try:
        send_telegram_message(bot_token, chat_id, message)
    except requests.RequestException as exc:
        logger.error("Failed to send Telegram message: %s", exc)
        sys.exit(1)

    logger.info("Done.")


if __name__ == "__main__":
    main()
