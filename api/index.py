import json
import os
import secrets

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

# The webhook runs outside GitHub Actions. Always read the latest scanner state
# from the public main branch rather than relying on a stale Vercel checkout.
os.environ["SDA_REMOTE_STATE"] = "1"

import telegram_dashboard as dashboard

app = FastAPI()


@app.get("/api/telegram", response_class=PlainTextResponse)
async def telegram_health():
    return "SDA Telegram webhook online"


@app.post("/api/telegram", response_class=PlainTextResponse)
async def telegram_webhook(request: Request):
    expected = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
    received = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not expected or not received or not secrets.compare_digest(received, expected):
        return PlainTextResponse("forbidden", status_code=403)

    try:
        body = await request.body()
        if not body or len(body) > 1024 * 1024:
            return PlainTextResponse("invalid body", status_code=400)
        payload = json.loads(body.decode("utf-8"))
        if isinstance(payload, dict) and payload.get("callback_query"):
            dashboard.handle_update(payload, {"offset": 0})
        return "ok"
    except Exception as exc:
        print(f"Telegram webhook error: {exc}")
        # Telegram should not endlessly redeliver a parsed but unsupported update.
        return "ok"

# Redeploy after webhook-secret changes so Vercel picks up the new environment value.
