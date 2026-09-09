#!/usr/bin/env python3
import os
import secrets
import sys
import requests


token = os.environ.get("TELEGRAM_TOKEN")
webhook_url = os.environ.get("TELEGRAM_WEBHOOK_URL")
secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET")

if not token or not webhook_url:
    print("Required: TELEGRAM_TOKEN and TELEGRAM_WEBHOOK_URL")
    sys.exit(1)

if not secret:
    secret = secrets.token_urlsafe(32)
    print("Generated TELEGRAM_WEBHOOK_SECRET:", secret)
    print("Set the same value in Vercel before running this setup again.")
    sys.exit(1)

r = requests.post(
    f"https://api.telegram.org/bot{token}/setWebhook",
    json={
        "url": webhook_url,
        "secret_token": secret,
        "allowed_updates": ["callback_query"],
        "drop_pending_updates": False,
    },
    timeout=30,
)

print("Telegram API status:", r.status_code)
try:
    print("Telegram API response:", r.json())
except ValueError:
    print("Telegram API response:", r.text[:1000])

r.raise_for_status()
