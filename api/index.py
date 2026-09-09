import json
import os
import secrets
import urllib.error
import urllib.request

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

# The webhook runs outside GitHub Actions. Always read the latest scanner state
# from the public main branch rather than relying on a stale Vercel checkout.
os.environ["SDA_REMOTE_STATE"] = "1"

import telegram_dashboard as dashboard

app = FastAPI()


def _cron_authorized(request: Request) -> bool:
    expected = os.environ.get("CRON_SECRET", "")
    received = request.headers.get("Authorization", "")
    if not expected or not received:
        return False
    return secrets.compare_digest(received, f"Bearer {expected}")


def _dispatch_scanner() -> tuple[bool, str]:
    token = os.environ.get("GITHUB_DISPATCH_TOKEN", "")
    if not token:
        return False, "missing GITHUB_DISPATCH_TOKEN"

    url = "https://api.github.com/repos/devilgest1704/sda-scanner/actions/workflows/scanner_v18.yml/dispatches"
    payload = json.dumps({
        "ref": "main",
        "inputs": {"run_reason": "external_watchdog"},
    }).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "sda-scanner-watchdog",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            if response.status == 204:
                return True, "scanner dispatched"
            return False, f"GitHub returned HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        return False, f"GitHub returned HTTP {exc.code}"
    except Exception as exc:
        return False, f"dispatch error: {type(exc).__name__}"


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


@app.get("/api/watchdog", response_class=PlainTextResponse)
async def external_watchdog(request: Request):
    """External 5-minute watchdog endpoint.

    A scheduler outside GitHub (for example cron-job.org) can call this endpoint.
    It authenticates with CRON_SECRET and dispatches scanner_v18.yml through GitHub.
    The GitHub scheduled watchdog remains as a fallback.
    """
    if not _cron_authorized(request):
        return PlainTextResponse("forbidden", status_code=403)

    ok, message = _dispatch_scanner()
    if ok:
        return PlainTextResponse("ok: scanner dispatched")
    return PlainTextResponse(f"watchdog error: {message}", status_code=502)

# Redeploy after webhook-secret changes so Vercel picks up the new environment value.
