"""Small HTTP helpers shared by the Vercel functions in api/."""

import hmac
import json
import os


def secret_matches(provided, env_name):
    """Constant-time check of a request secret against an environment variable.
    Fails closed if the variable isn't set."""
    expected = os.environ.get(env_name)
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided.encode(), expected.encode())


def send_json(handler, status, payload):
    body = json.dumps(payload, ensure_ascii=False).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)
