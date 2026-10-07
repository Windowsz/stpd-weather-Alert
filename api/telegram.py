"""Telegram webhook: answers location queries as soon as they arrive.
Register it once with setWebhook, passing secret_token=<TELEGRAM_WEBHOOK_SECRET>
(see README)."""

import json
import logging
import os
import sys
from http.server import BaseHTTPRequestHandler

# The shared modules live in the project root, one level up.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
import state_store  # noqa: E402
from webutil import secret_matches, send_json  # noqa: E402

logger = logging.getLogger("rain-alert")


def _first_delivery(update_id):
    """Dedup Telegram redeliveries; if Redis is down, answer anyway (a possible
    duplicate reply beats no reply)."""
    try:
        return state_store.claim_update(update_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not dedup update %s: %s", update_id, exc)
        return True


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        secret = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not secret_matches(secret, "TELEGRAM_WEBHOOK_SECRET"):
            send_json(self, 401, {"error": "unauthorized"})
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
            update = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            send_json(self, 400, {"error": "bad request"})
            return

        message = update.get("message") or update.get("edited_message")
        update_id = update.get("update_id")
        try:
            if message and update_id is not None and _first_delivery(update_id):
                main.handle_message(
                    os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"], message
                )
        except Exception:  # noqa: BLE001
            # Still answer 200: a non-2xx makes Telegram redeliver the same
            # update over and over.
            logger.exception("Failed to handle Telegram update %s", update_id)

        send_json(self, 200, {"ok": True})
