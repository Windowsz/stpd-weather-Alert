"""Scheduled home-location rain check. Call it every 5 minutes from an
external scheduler (e.g. cron-job.org) with the header
    Authorization: Bearer <CRON_SECRET>
"""

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


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        auth = self.headers.get("Authorization", "")
        token = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
        if not secret_matches(token, "CRON_SECRET"):
            send_json(self, 401, {"error": "unauthorized"})
            return

        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if not bot_token or not chat_id:
            send_json(self, 500, {"error": "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set"})
            return

        try:
            state = state_store.load_state()
            main.check_home_alert(bot_token, chat_id, state)
            state_store.save_state(state)
        except Exception:  # noqa: BLE001 - report instead of a bare 500
            logger.exception("Home check failed")
            send_json(self, 500, {"error": "check failed, see function logs"})
            return

        send_json(self, 200, {"ok": True, "state": state})

    do_POST = do_GET
