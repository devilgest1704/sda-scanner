import os

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


@app.get("/api/hourly", response_class=PlainTextResponse)
async def hourly(request: Request):
    if not _authorized(request):
        return PlainTextResponse("Unauthorized", status_code=401)
    # Automatic hourly Telegram dashboard pushes are disabled; external cron calls become no-ops.
    return PlainTextResponse("Automatic Telegram dashboard reports are disabled; use the interactive menu.",
                             status_code=200)
