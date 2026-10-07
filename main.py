#!/usr/bin/env python3
"""Rain Alert - checks Open-Meteo forecasts and sends Telegram alerts.

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

# A model "votes rain" when, anywhere in the look-ahead window, its rain amount
# reaches RAIN_AMOUNT_THRESHOLD and (if it reports one) its probability reaches
# RAIN_PROBABILITY_THRESHOLD. An alert needs MIN_MODEL_VOTES models to agree.
FORECAST_MODELS = ("ecmwf_ifs025", "icon_global", "gfs_global")
MIN_MODEL_VOTES = 2
WINDOW_HOURS = 3  # look-ahead window, starting 1 hour from now
ALERT_COOLDOWN_HOURS = 2  # don't re-alert for the same rain spell
RAIN_PROBABILITY_THRESHOLD = 40  # percent
RAIN_AMOUNT_THRESHOLD = 0.5  # mm per hour

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}"

GOOGLE_MAPS_SHORT_DOMAINS = ("goo.gl", "maps.app.goo.gl")
GOOGLE_MAPS_HOSTS = ("google.com", "maps.google.com", "goo.gl", "maps.app.goo.gl")
URL_PATTERN = re.compile(r"https?://\S+")
LATLON_AT_PATTERN = re.compile(r"@(-?\d+\.\d+),(-?\d+\.\d+)")
LATLON_VALUE_PATTERN = re.compile(r"^(-?\d+\.\d+),\s*(-?\d+\.\d+)")
LATLON_3D4D_PATTERN = re.compile(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)")
PLACE_CID_PATTERN = re.compile(r"!1s0x[0-9a-f]+:(0x[0-9a-f]+)")
PLAIN_LATLON_PATTERN = re.compile(r"(-?\d{1,2}\.\d{1,10})\s*,\s*(-?\d{1,3}\.\d{1,10})")

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "state.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("rain-alert")


def fetch_forecast(lat, lon):
    """Fetch hourly precipitation forecasts from several Open-Meteo models."""
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "precipitation_probability,precipitation,showers",
        "models": ",".join(FORECAST_MODELS),
        "timezone": TIMEZONE,
        "forecast_days": 2,  # 2 days so the window never runs off the end near midnight
    }

    logger.info("Fetching forecast from Open-Meteo (lat=%s, lon=%s)...", lat, lon)
    response = requests.get(OPEN_METEO_URL, params=params, timeout=15)
    response.raise_for_status()
    data = response.json()
    logger.info("Forecast fetched successfully.")
    return data


def get_window_start_index(times):
    """Index of the first hourly slot of the look-ahead window (1 hour from now,
    Bangkok time). Raises IndexError if the data doesn't cover it."""
    target = (datetime.now(BANGKOK_TZ) + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    target_str = target.strftime("%Y-%m-%dT%H:%M")
    if target_str not in times:
        raise IndexError(f"Slot {target_str} not found in hourly data")
    return times.index(target_str)


def format_display_time(time_str):
    """Format an Open-Meteo hourly timestamp as 24h Bangkok-local text."""
    dt = datetime.strptime(time_str, "%Y-%m-%dT%H:%M")
    return dt.strftime("%d/%m/%Y %H:%M น. (UTC+7)")


def is_home_location(lat, lon):
    """Check whether coordinates are close enough to count as the home location."""
    return abs(lat - LATITUDE) <= HOME_COORD_TOLERANCE and abs(lon - LONGITUDE) <= HOME_COORD_TOLERANCE


def _series(hourly, name, model):
    """Hourly list for a variable/model; Open-Meteo suffixes keys with the model name."""
    return hourly.get(f"{name}_{model}") or hourly.get(name) or []


def analyze_forecast(data, start_index, window_hours=WINDOW_HOURS):
    """Summarise each model's rain forecast over the look-ahead window and count
    how many models agree that rain is likely.

    Note: Open-Meteo's `precipitation` already includes showers, so showers are
    reported separately but never added on top."""
    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    end_index = start_index + window_hours
    if end_index > len(times):
        raise IndexError(
            f"Window {start_index}-{end_index} is out of range for hourly data "
            f"(length={len(times)})"
        )

    window_times = times[start_index:end_index]
    models = {}
    for model in FORECAST_MODELS:
        precip = _series(hourly, "precipitation", model)[start_index:end_index]
        prob = _series(hourly, "precipitation_probability", model)[start_index:end_index]
        showers = _series(hourly, "showers", model)[start_index:end_index]
        if not any(v is not None for v in precip):
            continue  # model has no data for this location/time

        peak_rain = max((v or 0) for v in precip)
        peak_slot = window_times[[(v or 0) for v in precip].index(peak_rain)]
        probs = [v for v in prob if v is not None]
        peak_prob = max(probs) if probs else None
        peak_showers = max((v or 0) for v in showers) if showers else 0

        votes_rain = peak_rain >= RAIN_AMOUNT_THRESHOLD and (
            peak_prob is None or peak_prob >= RAIN_PROBABILITY_THRESHOLD
        )
        models[model] = {
            "peak_rain": peak_rain,
            "peak_slot": peak_slot,
            "peak_prob": peak_prob,
            "peak_showers": peak_showers,
            "votes_rain": votes_rain,
        }

    votes = sum(1 for m in models.values() if m["votes_rain"])
    needed = min(MIN_MODEL_VOTES, len(models)) if models else MIN_MODEL_VOTES
    return {
        "window_start": window_times[0],
        "window_end": window_times[-1],
        "models": models,
        "votes": votes,
        "model_count": len(models),
        "triggered": bool(models) and votes >= needed,
    }


def _model_lines(analysis):
    """One line per model: peak rain / probability and whether it voted for rain."""
    lines = []
    for name, m in analysis["models"].items():
        prob = f"{m['peak_prob']:.0f}%" if m["peak_prob"] is not None else "-"
        mark = "☔" if m["votes_rain"] else "▫️"
        lines.append(f"{mark} `{name}`: {m['peak_rain']:.1f} มม./ชม., โอกาส {prob}")
    return "\n".join(lines)


def _window_text(analysis):
    start = format_display_time(analysis["window_start"])
    end = datetime.strptime(analysis["window_end"], "%Y-%m-%dT%H:%M").strftime("%H:%M น.")
    return f"{start} – {end}"


def build_alert_message(lat, lon, analysis):
    """Build a nicely formatted Markdown message for the scheduled home alert."""
    return (
        "🌧️ *แจ้งเตือนฝนตก* 🌧️\n\n"
        f"📍 *พิกัด:* `{lat}, {lon}` ({HOME_LABEL})\n"
        f"🕐 *ช่วงเวลา:* {_window_text(analysis)}\n\n"
        f"🗳️ *โมเดลที่เห็นตรงกันว่าฝนตก:* {analysis['votes']}/{analysis['model_count']}\n"
        f"{_model_lines(analysis)}\n\n"
        "_แนะนำให้เตรียมร่มหรือเสื้อกันฝนไว้ล่วงหน้า_"
    )


def build_query_reply_message(lat, lon, analysis):
    """Build a Markdown reply for an on-demand location query."""
    triggered = analysis["triggered"]
    status_emoji = "🌧️" if triggered else "🌤️"
    status_text = (
        f"*มีแนวโน้มฝนตกใน {WINDOW_HOURS} ชั่วโมงข้างหน้า!*"
        if triggered else f"ไม่มีแนวโน้มฝนตกใน {WINDOW_HOURS} ชั่วโมงข้างหน้า"
    )
    maps_link = f"https://www.google.com/maps?q={lat},{lon}"
    home_suffix = f" ({HOME_LABEL})" if is_home_location(lat, lon) else ""

    return (
        f"{status_emoji} *ผลการเช็คพยากรณ์ฝน*\n\n"
        f"📍 *พิกัด:* `{lat}, {lon}`{home_suffix}\n"
        f"🔗 [เปิดใน Google Maps]({maps_link})\n"
        f"🕐 *ช่วงเวลา:* {_window_text(analysis)}\n\n"
        f"🗳️ *โมเดลที่เห็นตรงกันว่าฝนตก:* {analysis['votes']}/{analysis['model_count']}\n"
        f"{_model_lines(analysis)}\n\n"
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
    """Load persisted state (last processed Telegram update_id, last home
    alert sent) from disk."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
                state.setdefault("last_update_id", None)
                state.setdefault("last_home_alert_time", None)
                return state
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read state file, starting fresh: %s", exc)
    return {"last_update_id": None, "last_home_alert_time": None}


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

    match = LATLON_3D4D_PATTERN.search(url)
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


def extract_plain_latlon(text):
    """Find a bare 'lat, lon' pair typed or pasted directly into the message
    (e.g. copied from a Google Maps pin: 13.7605620, 100.5680219)."""
    if not text:
        return None
    for lat_str, lon_str in PLAIN_LATLON_PATTERN.findall(text):
        lat, lon = float(lat_str), float(lon_str)
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return lat, lon
    return None


def extract_query_location(message):
    """Extract (lat, lon) from a Telegram message: a Google Maps link, a
    natively shared location, or a bare 'lat, lon' pair. Returns None if
    nothing usable was found."""
    location = message.get("location")
    if location:
        return location["latitude"], location["longitude"]

    text = message.get("text") or message.get("caption")

    maps_url = find_google_maps_url(text)
    if maps_url:
        coords = extract_latlon_from_maps_url(maps_url)
        if coords:
            return coords

    return extract_plain_latlon(text)


def is_in_cooldown(last_alert_slot, window_start):
    """True if the last alert was recent enough that this window is the same rain spell."""
    if not last_alert_slot:
        return False
    fmt = "%Y-%m-%dT%H:%M"
    elapsed = datetime.strptime(window_start, fmt) - datetime.strptime(last_alert_slot, fmt)
    return elapsed < timedelta(hours=ALERT_COOLDOWN_HOURS)


def check_home_alert(bot_token, chat_id, state):
    """Check the home location and send an alert if enough models expect rain
    in the next few hours.

    Alerts are spaced at least ALERT_COOLDOWN_HOURS apart (tracked in `state`),
    so running this frequently doesn't spam repeats for the same rain spell."""
    try:
        data = fetch_forecast(LATITUDE, LONGITUDE)
        index = get_window_start_index(data.get("hourly", {}).get("time", []))
        analysis = analyze_forecast(data, index)
    except (requests.RequestException, IndexError, KeyError) as exc:
        logger.error("Failed to fetch or parse forecast data: %s", exc)
        return

    for name, m in analysis["models"].items():
        logger.info(
            "Model %s -> peak_rain=%.2f mm, peak_prob=%s, votes_rain=%s",
            name, m["peak_rain"], m["peak_prob"], m["votes_rain"],
        )

    if not analysis["triggered"]:
        logger.info(
            "No alert needed (%d/%d models vote rain, need %d).",
            analysis["votes"], analysis["model_count"], MIN_MODEL_VOTES,
        )
        return

    if is_in_cooldown(state.get("last_home_alert_time"), analysis["window_start"]):
        logger.info(
            "Already alerted at %s (cooldown %dh), skipping duplicate.",
            state.get("last_home_alert_time"), ALERT_COOLDOWN_HOURS,
        )
        return

    logger.info(
        "Rain condition triggered! (%d/%d models). Sending alert...",
        analysis["votes"], analysis["model_count"],
    )

    message = build_alert_message(LATITUDE, LONGITUDE, analysis)

    try:
        send_telegram_message(bot_token, chat_id, message)
        state["last_home_alert_time"] = analysis["window_start"]
    except requests.RequestException as exc:
        logger.error("Failed to send Telegram alert: %s", exc)


def process_location_queries(bot_token, allowed_chat_id, state):
    """Check for new Telegram messages containing a Google Maps link (or a
    shared location) and reply with the rain forecast for that spot."""
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
            maps_url = find_google_maps_url(message.get("text") or message.get("caption"))
            if not maps_url:
                continue

            logger.info("Found a Google Maps link but could not extract coordinates: %s", maps_url)
            try:
                send_telegram_message(
                    bot_token, chat_id,
                    "⚠️ ขออภัยครับ ไม่สามารถแกะพิกัดจากลิงก์นี้ได้ "
                    "(ลิงก์ประเภทนี้ไม่มีพิกัดฝังอยู่โดยตรง มักเกิดกับลิงก์แชร์ร้าน/สถานที่จากแอปมือถือ)\n\n"
                    "ลองวิธีนี้แทนครับ:\n"
                    "• กดค้างบนตำแหน่งในแผนที่เพื่อปักหมุดเอง แล้วกด Share จะได้ลิงก์ที่มีพิกัดฝังอยู่\n"
                    "• หรือกด 📎 ใน Telegram แล้วเลือก Location เพื่อแชร์พิกัดโดยตรง (แม่นยำสุด)",
                )
            except requests.RequestException:
                pass
            continue

        lat, lon = coords
        logger.info("Location query received: lat=%s, lon=%s", lat, lon)

        try:
            data = fetch_forecast(lat, lon)
            index = get_window_start_index(data.get("hourly", {}).get("time", []))
            analysis = analyze_forecast(data, index)
            reply = build_query_reply_message(lat, lon, analysis)
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

    state["last_update_id"] = highest_update_id


def main():
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not bot_token or not chat_id:
        logger.error(
            "Missing required environment variables: "
            "TELEGRAM_BOT_TOKEN and/or TELEGRAM_CHAT_ID."
        )
        sys.exit(1)

    state = load_state()
    check_home_alert(bot_token, chat_id, state)
    process_location_queries(bot_token, chat_id, state)
    save_state(state)

    logger.info("Done.")


if __name__ == "__main__":
    main()
