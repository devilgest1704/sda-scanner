import json
import os
import urllib.error
import urllib.request

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

app = FastAPI()


def _authorized(request: Request) -> bool:
    expected = os.environ.get("CRON_SECRET", "")
    if not expected:
        # Keep the existing cron-job.org configuration working. If CRON_SECRET
        # is configured later, the endpoint becomes protected automatically.
        return True
    received = request.headers.get("Authorization", "")
    return received == f"Bearer {expected}"


def _dispatch() -> tuple[bool, str]:
    token = os.environ.get("GITHUB_DISPATCH_TOKEN", "")
    if not token:
        return False, "missing GITHUB_DISPATCH_TOKEN"
    url = "https://api.github.com/repos/devilgest1704/sda-scanner/actions/workflows/scanner_v18.yml/dispatches"
    payload = json.dumps({
        "ref": "main",
        "inputs": {
            "run_reason": "Cronjob hourly",
            "send_report": "true",
        },
    }).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return response.status in (200, 201, 204), f"GitHub dispatch HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return False, f"GitHub dispatch HTTP {exc.code}: {body[:300]}"
    except Exception as exc:
        return False, str(exc)


@app.get("/api/hourly", response_class=PlainTextResponse)
async def hourly(request: Request):
    if not _authorized(request):
        return PlainTextResponse("Unauthorized", status_code=401)
    ok, message = _dispatch()
    return PlainTextResponse(message, status_code=200 if ok else 502)
