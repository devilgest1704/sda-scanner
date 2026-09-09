import json
import os
import secrets
from http.server import BaseHTTPRequestHandler

# The webhook runs outside GitHub Actions. Always read the latest scanner state
# from the public main branch rather than relying on a stale Vercel checkout.
os.environ["SDA_REMOTE_STATE"] = "1"

import telegram_dashboard as dashboard


class handler(BaseHTTPRequestHandler):
    def _reply(self, status=200, body="ok"):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        expected = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
        received = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not expected or not received or not secrets.compare_digest(received, expected):
            self._reply(403, "forbidden")
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 1024 * 1024:
                self._reply(400, "invalid body")
                return
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if isinstance(payload, dict) and payload.get("callback_query"):
                dashboard.handle_update(payload, {"offset": 0})
            self._reply(200, "ok")
        except Exception as exc:
            print(f"Telegram webhook error: {exc}")
            # Return 200 after parsing a Telegram update so Telegram does not
            # repeatedly redeliver a malformed/unsupported callback forever.
            self._reply(200, "ok")

    def do_GET(self):
        self._reply(200, "SDA Telegram webhook online")
