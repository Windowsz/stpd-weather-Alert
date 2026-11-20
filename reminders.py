"""Personal reminders delivered by the same 5-minute cron as the rain check.

On Vercel each reminder is one field in a Redis hash, so a cron rewrite of
the weather state cannot wipe it. Without Redis (a local run) they live in
the state dict the caller already saves.
"""

import json
import re
from datetime import datetime, timedelta

import state_store

MAX_REMINDERS = 30
MAX_TEXT = 200

_MONTHS = {
    "ม.ค.": 1, "มกราคม": 1,
    "ก.พ.": 2, "กุมภาพันธ์": 2,
    "มี.ค.": 3, "มีนาคม": 3,
    "เม.ย.": 4, "เมษายน": 4,
    "พ.ค.": 5, "พฤษภาคม": 5,
    "มิ.ย.": 6, "มิถุนายน": 6,
    "ก.ค.": 7, "กรกฎาคม": 7,
    "ส.ค.": 8, "สิงหาคม": 8,
    "ก.ย.": 9, "กันยายน": 9,
    "ต.ค.": 10, "ตุลาคม": 10,
    "พ.ย.": 11, "พฤศจิกายน": 11,
    "ธ.ค.": 12, "ธันวาคม": 12,
}
_TIME_RE = re.compile(r"^(\d{1,2})[:.](\d{2})\s*(?:น\.?)?$")
_DATE_RE = re.compile(r"^(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?$")
_CANCEL_RE = re.compile(r"^(?:ลบเตือน|ยกเลิกเตือน)\s+(\d+)\s*$")
_LIST_COMMANDS = {"เตือน", "เตือนความจำ", "รายการเตือน", "/remind", "/reminders", "/เตือน"}


def handle_text(text, state, now):
    """Reply text if `text` is a reminder command, else None.

    `now` is an aware Bangkok datetime. `state` is the weather state dict;
    it is only written when Redis is not configured.
    """
    command = _normalize(text)
    if command is None:
        return None
    if command in _LIST_COMMANDS:
        return _list_text(_load(state), now)
    cancel = _CANCEL_RE.match(command)
    if cancel:
        return _cancel(state, int(cancel.group(1)), now)
    if command.startswith("เตือน ") or command.startswith("/remind "):
        rest = command.split(None, 1)[1]
        return _add(state, rest, now)
    return None


def due_reminders(state, now):
    """Reminders whose time has arrived, earliest first."""
    ready = []
    for item in _load(state):
        when = datetime.fromisoformat(item["at"])
        if when <= now:
            ready.append(item)
    ready.sort(key=lambda item: item["at"])
    return ready


def forget(state, ids):
    """Drop reminders that were delivered."""
    if not ids:
        return
    if state_store.reminders_enabled():
        state_store.reminders_delete(ids)
        return
    if state is None:
        return
    gone = set(ids)
    state["reminders"] = [item for item in state.get("reminders") or [] if item["id"] not in gone]


def _add(state, rest, now):
    try:
        when, text, rolled = _parse_when(rest, now)
    except ValueError as exc:
        return str(exc)
    items = _load(state)
    if len(items) >= MAX_REMINDERS:
        return f"จำไว้ครบ {MAX_REMINDERS} รายการแล้ว ลบอันเก่าก่อนด้วย ลบเตือน 1"
    item = {"id": _new_id(items), "at": when.isoformat(timespec="minutes"), "text": text}
    try:
        _store(state, item)
    except ValueError as exc:
        return str(exc)
    if rolled == "time":
        lead = "เวลานี้ผ่านไปแล้ว เลยจำไว้"
    elif rolled == "year":
        lead = "วันนั้นของปีนี้ผ่านไปแล้ว เลยจำไว้ปีหน้า"
    else:
        lead = "จำไว้แล้ว"
    return f"{lead} {_format_when(when, now)}\n{text}"


def _cancel(state, index, now):
    items = sorted(_load(state), key=lambda item: item["at"])
    if index < 1 or index > len(items):
        if not items:
            return "ยังไม่มีเตือนความจำ"
        return f"ไม่มีรายการที่ {index} ส่ง รายการเตือน เพื่อดูเลข"
    item = items[index - 1]
    forget(state, [item["id"]])
    when = datetime.fromisoformat(item["at"])
    return f"ลบแล้ว {_format_when(when, now)} — {item['text']}"


def _list_text(items, now):
    if not items:
        return (
            "ยังไม่มีเตือนความจำ\n"
            "ตั้งด้วย เตือน 18:00 รับพัสดุ หรือ เตือน พรุ่งนี้ 9:00 ประชุม"
        )
    lines = ["เตือนความจำที่จำไว้"]
    for index, item in enumerate(sorted(items, key=lambda item: item["at"]), start=1):
        when = datetime.fromisoformat(item["at"])
        lines.append(f"{index}. {_format_when(when, now)} — {item['text']}")
    lines.append("ลบด้วย ลบเตือน 1")
    return "\n".join(lines)


def _load(state):
    if state_store.reminders_enabled():
        return state_store.reminders_all()
    return list((state or {}).get("reminders") or [])


def _store(state, item):
    if state_store.reminders_enabled():
        state_store.reminders_put(item["id"], item)
        return
    if state is None:
        raise ValueError("ยังบันทึกเตือนความจำไม่ได้")
    state.setdefault("reminders", []).append(item)


def _new_id(items):
    used = {item["id"] for item in items}
    number = 1
    while str(number) in used:
        number += 1
    return str(number)


def _normalize(text):
    if not text:
        return None
    cleaned = text.strip()
    cleaned = re.sub(r"^(/[^\s@]+)@\S+", r"\1", cleaned)
    if cleaned.startswith("/เตือน"):
        cleaned = "เตือน" + cleaned[len("/เตือน"):]
    if cleaned in _LIST_COMMANDS or _CANCEL_RE.match(cleaned):
        return cleaned
    if cleaned.startswith("เตือน ") or cleaned.startswith("/remind "):
        return cleaned
    return None


def _parse_when(rest, now):
    """(aware datetime, text, rolled_to_tomorrow). ValueError message is Thai."""
    tokens = rest.split()
    if not tokens:
        raise ValueError("ใส่เวลาและข้อความ เช่น เตือน 18:00 รับพัสดุ")
    index = 0
    tomorrow = False
    if tokens[0] == "พรุ่งนี้":
        tomorrow = True
        index = 1
    date_parts = None
    if index < len(tokens):
        date_parts = _parse_date(tokens[index])
        if date_parts:
            index += 1
        elif index + 1 < len(tokens) and tokens[index].isdigit() and _MONTHS.get(tokens[index + 1]):
            date_parts = (int(tokens[index]), _MONTHS[tokens[index + 1]], None)
            index += 2
    if tomorrow and date_parts:
        raise ValueError("ใช้ พรุ่งนี้ หรือใส่วันที่ อย่างใดอย่างหนึ่ง")
    time_hm = _parse_time(tokens[index]) if index < len(tokens) else None
    if not time_hm:
        raise ValueError("ใส่เวลาแบบ 18:00 เช่น เตือน 18:00 รับพัสดุ")
    index += 1
    text = re.sub(r"^น\.?\s+", "", " ".join(tokens[index:]).strip())
    if not text:
        raise ValueError("ใส่ด้วยว่าให้เตือนเรื่องอะไร เช่น เตือน 18:00 รับพัสดุ")
    if len(text) > MAX_TEXT:
        raise ValueError(f"ข้อความยาวเกิน {MAX_TEXT} ตัวอักษร")
    hour, minute = time_hm
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("เวลาไม่ถูกต้อง ใช้ 0:00 ถึง 23:59")

    rolled = None
    if date_parts:
        day, month, year = date_parts
        year = year if year else now.year
        if year < 100:
            year += 2000
        try:
            when = datetime(year, month, day, hour, minute, tzinfo=now.tzinfo)
        except ValueError:
            raise ValueError("วันที่ไม่ถูกต้อง") from None
        if when <= now and date_parts[2] is None and (when.month, when.day) < (now.month, now.day):
            try:
                when = when.replace(year=year + 1)
            except ValueError:
                raise ValueError("วันที่ไม่ถูกต้อง") from None
            rolled = "year"
        if when <= now:
            raise ValueError("เวลานี้ผ่านไปแล้ว ใส่วันที่ใหม่")
    elif tomorrow:
        day = now + timedelta(days=1)
        when = datetime(day.year, day.month, day.day, hour, minute, tzinfo=now.tzinfo)
    else:
        when = datetime(now.year, now.month, now.day, hour, minute, tzinfo=now.tzinfo)
        if when <= now:
            when += timedelta(days=1)
            rolled = "time"
    return when, text, rolled


def _parse_time(token):
    match = _TIME_RE.match(token)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _parse_date(token):
    match = _DATE_RE.match(token)
    if not match:
        return None
    year = int(match.group(3)) if match.group(3) else None
    return int(match.group(1)), int(match.group(2)), year


def _format_when(when, now):
    if when.date() == now.date():
        day = "วันนี้"
    elif when.date() == (now + timedelta(days=1)).date():
        day = "พรุ่งนี้"
    else:
        day = when.strftime("%d/%m/%Y")
    return f"{day} {when.strftime('%H:%M')} น."
