"""Bot state in Upstash Redis (REST API), for running on Vercel where the
filesystem doesn't persist between calls.

Reads the connection from UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN,
or the KV_REST_API_URL / KV_REST_API_TOKEN names that the Vercel marketplace
integration sets.
"""

import json
import os

import requests

STATE_KEY = "rain-alert:state"
UPDATE_KEY_PREFIX = "rain-alert:update:"
UPDATE_DEDUP_SECONDS = 24 * 3600

DEFAULT_STATE = {
    "last_home_alert_time": None,
    "radar_size": None,
    "last_radar_alert_time": None,
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
    return state


def save_state(state):
    _command("SET", STATE_KEY, json.dumps(state, ensure_ascii=False))


def claim_update(update_id):
    """True the first time an update_id is seen (Telegram retries webhooks
    that time out, and we don't want to answer the same message twice)."""
    return _command(
        "SET", f"{UPDATE_KEY_PREFIX}{update_id}", "1", "NX", "EX", str(UPDATE_DEDUP_SECONDS)
    ) == "OK"
