"""Bot state in Upstash Redis (REST API), for running on Vercel where the
filesystem doesn't persist between calls.

Reads the connection from UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN,
or the KV_REST_API_URL / KV_REST_API_TOKEN names that the Vercel marketplace
integration sets.
"""

import base64
import json
import os

import requests

STATE_KEY = "rain-alert:state"
UPDATE_KEY_PREFIX = "rain-alert:update:"
UPDATE_DEDUP_SECONDS = 24 * 3600
RADAR_KEY_PREFIX = "rain-alert:radar:"
RADAR_CACHE_TTL_SECONDS = 6 * 3600
# Raw bytes per Redis key. Base64 stays under Upstash's ~1 MB command limit.
RADAR_CACHE_CHUNK = 480_000
# Phetchabun and Trang are ~55 MB; storing those would blow the function time.
RADAR_CACHE_MAX_BYTES = 18_000_000

DEFAULT_STATE = {
    "last_home_alert_time": None,
    "last_radar_alert_time": None,
    "radar_digest": None,
    "radar_changed_at": None,
}


def _config():
    url = os.environ.get("UPSTASH_REDIS_REST_URL") or os.environ.get("KV_REST_API_URL")
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN") or os.environ.get("KV_REST_API_TOKEN")
    if not url or not token:
        raise RuntimeError(
            "Missing Redis config: set UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN"
        )
    return url.rstrip("/"), token


def _command(*args):
    """Run one Redis command through the Upstash REST API and return its result."""
    url, token = _config()
    response = requests.post(
        url, json=list(args), headers={"Authorization": f"Bearer {token}"}, timeout=10
    )
    response.raise_for_status()
    body = response.json()
    if "error" in body:
        raise RuntimeError(f"Redis error: {body['error']}")
    return body.get("result")


def load_state():
    raw = _command("GET", STATE_KEY)
    state = dict(DEFAULT_STATE)
    if raw:
        state.update(json.loads(raw))
    state.pop("radar_size", None)  # replaced by radar_digest
    return state


def save_state(state):
    _command("SET", STATE_KEY, json.dumps(state, ensure_ascii=False))


def _pipeline(commands):
    """Run several Redis commands in one request. Returns a list of results."""
    url, token = _config()
    response = requests.post(
        f"{url}/pipeline",
        json=commands,
        headers={"Authorization": f"Bearer {token}"},
        timeout=20,
    )
    response.raise_for_status()
    body = response.json()
    results = []
    for item in body:
        if isinstance(item, dict) and "error" in item:
            raise RuntimeError(f"Redis error: {item['error']}")
        results.append(item.get("result") if isinstance(item, dict) else item)
    return results


def radar_cache_get(site_id, fingerprint):
    """GIF bytes stored for this fingerprint, or None.

    None also means Redis is not configured (local runs and GitHub Actions),
    so the caller just downloads the file.
    """
    try:
        _config()
    except RuntimeError:
        return None
    prefix = f"{RADAR_KEY_PREFIX}{site_id}:"
    try:
        meta, count = _pipeline([
            ["GET", prefix + "meta"],
            ["GET", prefix + "n"],
        ])
    except (RuntimeError, requests.RequestException, ValueError):
        return None
    if meta != fingerprint or not count:
        return None
    try:
        n = int(count)
        chunks = _pipeline([["GET", f"{prefix}{i}"] for i in range(n)])
    except (RuntimeError, requests.RequestException, ValueError):
        return None
    if not chunks or any(chunk is None for chunk in chunks):
        return None
    try:
        return b"".join(base64.b64decode(chunk) for chunk in chunks)
    except (ValueError, TypeError):
        return None


def radar_cache_put(site_id, fingerprint, data):
    """Store a GIF until `fingerprint` changes. No-op without Redis."""
    try:
        _config()
    except RuntimeError:
        return
    prefix = f"{RADAR_KEY_PREFIX}{site_id}:"
    pieces = [data[i:i + RADAR_CACHE_CHUNK] for i in range(0, len(data), RADAR_CACHE_CHUNK)]
    commands = []
    for index, piece in enumerate(pieces):
        encoded = base64.b64encode(piece).decode("ascii")
        commands.append(["SET", f"{prefix}{index}", encoded, "EX", str(RADAR_CACHE_TTL_SECONDS)])
    commands.append(["SET", prefix + "n", str(len(pieces)), "EX", str(RADAR_CACHE_TTL_SECONDS)])
    commands.append(["SET", prefix + "meta", fingerprint, "EX", str(RADAR_CACHE_TTL_SECONDS)])
    # Drop chunks left over from a larger previous copy of this station.
    old = _command("GET", prefix + "n")
    try:
        old_n = int(old) if old else 0
    except ValueError:
        old_n = 0
    for index in range(len(pieces), old_n):
        commands.append(["DEL", f"{prefix}{index}"])
    # A few keys per request so a 15 MB GIF stays under the REST body limit.
    for start in range(0, len(commands), 4):
        _pipeline(commands[start:start + 4])


def claim_update(update_id):
    """True the first time an update_id is seen (Telegram retries webhooks
    that time out, and we don't want to answer the same message twice)."""
    return _command(
        "SET", f"{UPDATE_KEY_PREFIX}{update_id}", "1", "NX", "EX", str(UPDATE_DEDUP_SECONDS)
    ) == "OK"
