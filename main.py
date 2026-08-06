#!/usr/bin/env python3
"""Rain Alert - checks Open-Meteo forecast and sends Telegram alerts.

Two features:
1. Scheduled home-location check: alerts if rain is likely in the next hour.
2. On-demand query: if the user sends a Google Maps link (or shares a
   location) in the Telegram chat, the bot replies with the rain forecast
   for that spot.
"""

import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import requests

# Home location used for the scheduled alert.
LATITUDE = 13.8628558
LONGITUDE = 100.4303806
TIMEZONE = "Asia/Bangkok"
BANGKOK_TZ = ZoneInfo(TIMEZONE)
HOME_LABEL = "บ้าน นนทบุรี"
HOME_COORD_TOLERANCE = 0.001  # ~110m, for matching a shared pin to home

RAIN_PROBABILITY_THRESHOLD = 50  # percent
RAIN_AMOUNT_THRESHOLD = 0.1  # mm

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}"

GOOGLE_MAPS_SHORT_DOMAINS = ("goo.gl", "maps.app.goo.gl")
GOOGLE_MAPS_HOSTS = ("google.com", "maps.google.com", "goo.gl", "maps.app.goo.gl")
URL_PATTERN = re.compile(r"https?://\S+")
LATLON_AT_PATTERN = re.compile(r"@(-?\d+\.\d+),(-?\d+\.\d+)")
LATLON_VALUE_PATTERN = re.compile(r"^(-?\d+\.\d+),\s*(-?\d+\.\d+)")

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "state.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("rain-alert")


def fetch_forecast(lat, lon):
    """Fetch hourly precipitation forecast from Open-Meteo for a given point."""
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "precipitation_probability,precipitation,showers",
        "timezone": TIMEZONE,
        "forecast_days": 1,
    }

    logger.info("Fetching forecast from Open-Meteo (lat=%s, lon=%s)...", lat, lon)
    response = requests.get(OPEN_METEO_URL, params=params, timeout=15)
    response.raise_for_status()
    data = response.json()
    logger.info("Forecast fetched successfully.")
    return data


def get_next_hour_index(times):
    """Find the hourly array index matching exactly 1 hour from now (Bangkok time)."""
    target = (datetime.now(BANGKOK_TZ) + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    target_str = target.strftime("%Y-%m-%dT%H:%M")
    if target_str in times:
        return times.index(target_str)
    return min(1, len(times) - 1)


def format_display_time(time_str):
    """Format an Open-Meteo hourly timestamp as 24h Bangkok-local text."""
    dt = datetime.strptime(time_str, "%Y-%m-%dT%H:%M")
    return dt.strftime("%d/%m/%Y %H:%M น. (UTC+7)")


def is_home_location(lat, lon):
    """Check whether coordinates are close enough to count as the home location."""
    return abs(lat - LATITUDE) <= HOME_COORD_TOLERANCE and abs(lon - LONGITUDE) <= HOME_COORD_TOLERANCE


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


def build_alert_message(lat, lon, forecast_point, total_rain):
    """Build a nicely formatted Markdown message for the scheduled home alert."""
    return (
        "🌧️ *แจ้งเตือนฝนตก* 🌧️\n\n"
        f"📍 *พิกัด:* `{lat}, {lon}` ({HOME_LABEL})\n"
        f"🕐 *ช่วงเวลา:* {format_display_time(forecast_point['time'])}\n\n"
        f"☔️ *โอกาสเกิดฝน:* `{forecast_point['precipitation_probability']}%`\n"
        f"💧 *ปริมาณฝน:* `{forecast_point['precipitation']} มม.`\n"
        f"🌦️ *Showers:* `{forecast_point['showers']} มม.`\n"
        f"📊 *รวมปริมาณน้ำฝน:* `{total_rain:.2f} มม.`\n\n"
        "_แนะนำให้เตรียมร่มหรือเสื้อกันฝนไว้ล่วงหน้า_"
    )


def build_query_reply_message(lat, lon, forecast_point, total_rain, triggered):
    """Build a Markdown reply for an on-demand location query."""
    status_emoji = "🌧️" if triggered else "🌤️"
    status_text = "*มีแนวโน้มฝนตกในชั่วโมงหน้า!*" if triggered else "ไม่มีแนวโน้มฝนตกในชั่วโมงหน้า"
    maps_link = f"https://www.google.com/maps?q={lat},{lon}"
    home_suffix = f" ({HOME_LABEL})" if is_home_location(lat, lon) else ""

    return (
        f"{status_emoji} *ผลการเช็คพยากรณ์ฝน*\n\n"
        f"📍 *พิกัด:* `{lat}, {lon}`{home_suffix}\n"
        f"🔗 [เปิดใน Google Maps]({maps_link})\n"
        f"🕐 *ช่วงเวลา:* {format_display_time(forecast_point['time'])}\n\n"
        f"☔️ *โอกาสเกิดฝน:* `{forecast_point['precipitation_probability']}%`\n"
        f"💧 *ปริมาณฝน:* `{forecast_point['precipitation']} มม.`\n"
        f"🌦️ *Showers:* `{forecast_point['showers']} มม.`\n"
        f"📊 *รวมปริมาณน้ำฝน:* `{total_rain:.2f} มม.`\n\n"
        f"{status_text}"
    )


def send_telegram_message(bot_token, chat_id, message):
    """Send a Markdown-formatted message to a Telegram chat."""
    url = f"{TELEGRAM_API_BASE.format(token=bot_token)}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }

    logger.info("Sending message to Telegram chat_id=%s...", chat_id)
    response = requests.post(url, data=payload, timeout=15)
    response.raise_for_status()
    logger.info("Telegram message sent successfully.")
    return response.json()


def get_telegram_updates(bot_token, offset=None):
    """Fetch pending updates (messages) sent to the bot."""
    url = f"{TELEGRAM_API_BASE.format(token=bot_token)}/getUpdates"
    params = {"timeout": 0}
    if offset is not None:
        params["offset"] = offset

    response = requests.get(url, params=params, timeout=15)
    response.raise_for_status()
    return response.json().get("result", [])


def load_state():
    """Load the last processed Telegram update_id from disk."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read state file, starting fresh: %s", exc)
    return {"last_update_id": None}


def save_state(state):
    """Persist the last processed Telegram update_id to disk."""
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def resolve_short_url(url):
    """Follow redirects to resolve a shortened Google Maps link (goo.gl)."""
    try:
        response = requests.head(url, allow_redirects=True, timeout=10)
        if response.url and response.url != url:
            return response.url
        response = requests.get(url, allow_redirects=True, timeout=10, stream=True)
        response.close()
        return response.url
    except requests.RequestException as exc:
        logger.warning("Could not resolve shortened URL %s: %s", url, exc)
        return url


def extract_latlon_from_maps_url(url):
    """Extract (lat, lon) from a Google Maps URL, resolving short links first."""
    parsed = urlparse(url)

    if any(domain in parsed.netloc for domain in GOOGLE_MAPS_SHORT_DOMAINS):
        url = resolve_short_url(url)
        parsed = urlparse(url)

    match = LATLON_AT_PATTERN.search(url)
    if match:
        return float(match.group(1)), float(match.group(2))

    query_params = parse_qs(parsed.query)
    for key in ("q", "query", "ll", "destination"):
        for value in query_params.get(key, []):
            m = LATLON_VALUE_PATTERN.match(value.strip())
            if m:
                return float(m.group(1)), float(m.group(2))

    return None


def find_google_maps_url(text):
    """Find the first Google Maps URL inside a chunk of message text."""
    if not text:
        return None
    for url in URL_PATTERN.findall(text):
        if any(host in url for host in GOOGLE_MAPS_HOSTS):
            return url
    return None


def extract_query_location(message):
    """Extract (lat, lon) from a Telegram message: a Google Maps link or a
    natively shared location. Returns None if nothing usable was found."""
    location = message.get("location")
    if location:
        return location["latitude"], location["longitude"]

    text = message.get("text") or message.get("caption")
    maps_url = find_google_maps_url(text)
    if not maps_url:
        return None

    return extract_latlon_from_maps_url(maps_url)


def check_home_alert(bot_token, chat_id):
    """Check the home location and send an alert if rain is likely soon."""
    try:
        data = fetch_forecast(LATITUDE, LONGITUDE)
        index = get_next_hour_index(data.get("hourly", {}).get("time", []))
        forecast_point = extract_forecast_point(data, index)
    except (requests.RequestException, IndexError, KeyError) as exc:
        logger.error("Failed to fetch or parse forecast data: %s", exc)
        return

    logger.info(
        "Home forecast at %s -> probability=%s%%, precipitation=%s mm, showers=%s mm",
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
        "Rain condition triggered! (probability=%s%%, total_rain=%.2f mm). Sending alert...",
        probability, total_rain,
    )

    message = build_alert_message(LATITUDE, LONGITUDE, forecast_point, total_rain)

    try:
        send_telegram_message(bot_token, chat_id, message)
    except requests.RequestException as exc:
        logger.error("Failed to send Telegram alert: %s", exc)


def process_location_queries(bot_token, allowed_chat_id):
    """Check for new Telegram messages containing a Google Maps link (or a
    shared location) and reply with the rain forecast for that spot."""
    state = load_state()

    try:
        updates = get_telegram_updates(bot_token, offset=state.get("last_update_id"))
    except requests.RequestException as exc:
        logger.warning("Could not fetch Telegram updates: %s", exc)
        return

    if not updates:
        logger.info("No new Telegram messages to process.")
        return

    logger.info("Received %d new Telegram update(s).", len(updates))
    highest_update_id = state.get("last_update_id") or 0

    for update in updates:
        highest_update_id = max(highest_update_id, update["update_id"] + 1)

        message = update.get("message") or update.get("edited_message")
        if not message:
            continue

        chat_id = message.get("chat", {}).get("id")
        if str(chat_id) != str(allowed_chat_id):
            logger.info("Ignoring message from unrecognized chat_id=%s.", chat_id)
            continue

        try:
            coords = extract_query_location(message)
        except Exception as exc:  # noqa: BLE001 - keep processing other updates
            logger.warning("Failed to parse location from message: %s", exc)
            coords = None

        if not coords:
            continue

        lat, lon = coords
        logger.info("Location query received: lat=%s, lon=%s", lat, lon)

        try:
            data = fetch_forecast(lat, lon)
            index = get_next_hour_index(data.get("hourly", {}).get("time", []))
            forecast_point = extract_forecast_point(data, index)
            triggered, _, total_rain = should_alert(forecast_point)
            reply = build_query_reply_message(lat, lon, forecast_point, total_rain, triggered)
            send_telegram_message(bot_token, chat_id, reply)
        except (requests.RequestException, IndexError, KeyError) as exc:
            logger.error("Failed to answer location query: %s", exc)
            try:
                send_telegram_message(
                    bot_token, chat_id,
                    "⚠️ ไม่สามารถดึงข้อมูลพยากรณ์อากาศสำหรับพิกัดนี้ได้ กรุณาลองใหม่อีกครั้ง",
                )
            except requests.RequestException:
                pass

    save_state({"last_update_id": highest_update_id})


def main():
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not bot_token or not chat_id:
        logger.error(
            "Missing required environment variables: "
            "TELEGRAM_BOT_TOKEN and/or TELEGRAM_CHAT_ID."
        )
        sys.exit(1)

    check_home_alert(bot_token, chat_id)
    process_location_queries(bot_token, chat_id)

    logger.info("Done.")


if __name__ == "__main__":
    main()
