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
import telegram_dashboard_compact as compact
import main as scanner
import position_action_v20

# market_data.json contains full transaction history and can become too large for
# reliable remote dashboard reads. The scanner also publishes a compact snapshot
# containing the latest per-token analysis only. Prefer it for all dashboard views.
_original_dashboard_load = dashboard.load

def _dashboard_load(path, default):
    if path == "market_data.json" and os.environ.get("SDA_REMOTE_STATE") == "1":
        compact_market = _original_dashboard_load("market_analysis.json", {"tokens": {}})
        if isinstance(compact_market, dict) and compact_market.get("tokens"):
            return compact_market
    return _original_dashboard_load(path, default)

dashboard.load = _dashboard_load

# portfolio_data.json can lag the live wallet after a sale. Make the wallet
# authoritative for CURRENT positions before any dashboard/position-action code
# consumes the portfolio. Realized history remains untouched and is still read
# from the trade ledger.
_original_load_with_market = dashboard.load

def _dashboard_load_synced(path, default):
    data = _original_load_with_market(path, default)
    if path == "portfolio_data.json" and isinstance(data, dict):
        wallet = _original_dashboard_load("wallet_data.json", {})
        return compact._sync_current_to_wallet(wallet, data)
    return data

dashboard.load = _dashboard_load_synced

# Keep the existing dashboard/menu implementation, but replace only the main
# wallet renderer with the compact merged Wallet + Portfolio version.
def _compact_wallet_portfolio(wallet, portfolio, md, ws, meta):
    return compact.merge_wallet_portfolio(wallet, portfolio, scanner)

dashboard._merge_wallet_portfolio = _compact_wallet_portfolio

# Position Action V19: mirror the paper engine emergency-loss guard.
# This is display-only; the actual paper exit is enforced in run_scanner_v18.sh.
_original_position_recommendations = dashboard._position_recommendations

def _position_recommendations_v19(md, ws, meta, portfolio):
    rows = _original_position_recommendations(md, ws, meta, portfolio)
    tokens = md.get("tokens", {}) if isinstance(md, dict) else {}
    current = portfolio.get("current", {}) if isinstance(portfolio, dict) else {}
    for row in rows:
        token = row.get("token")
        pf = current.get(token, {}) if isinstance(current, dict) else {}
        pnl = dashboard.engine.num(pf.get("unrealized_pnl_pct"))
        token_data = tokens.get(token, {}) or {}
        analysis = dashboard._analysis(token_data)
        try:
            s = dashboard.engine.score(token, analysis, ws) if analysis else {"confidence": 0, "m1h": 0, "net_1h": 0}
        except Exception:
            s = {"confidence": 0, "m1h": 0, "net_1h": 0}
        score = dashboard.engine.num(s.get("confidence"))
        m1h = dashboard.engine.num(s.get("m1h"))
        flow1 = dashboard.engine.num(s.get("net_1h"))
        if pf.get("cost_sda") is not None and pf.get("unrealized_pnl_pct") is not None:
            if pnl <= -15 and score < 35 and m1h < 0 and flow1 < 0:
                row["action"] = "EMERGENCY SELL"
    return sorted(rows, key=lambda x: ({"EMERGENCY SELL": -1, "SELL / EXIT": 0, "PARTIAL SELL": 1, "HOLD / TRAIL": 2, "HOLD / WATCH": 3, "HOLD / NO COST BASIS": 4}.get(x.get("action"), 9), -dashboard.engine.num(x.get("score"))))

dashboard._position_recommendations = _position_recommendations_v19

# V20 shared policy keeps the same emergency rule in every dashboard path
# and gives EMERGENCY SELL its dedicated 🚨 icon.
position_action_v20.patch_dashboard(dashboard)

# Add a manual hourly-report refresh button without changing the existing GUI.
_original_menu_keyboard = dashboard.menu_keyboard

def _menu_keyboard_with_refresh():
    keyboard = _original_menu_keyboard()
    rows = keyboard.get("inline_keyboard", [])
    rows.append([{"text": "🔄 Refresh", "callback_data": "REFRESH"}])
    return keyboard

dashboard.menu_keyboard = _menu_keyboard_with_refresh

# Paper Trading opens directly on the current paper-trading status.
# Statistics remains the detailed view with current positions + trade history.
_original_handle_update = dashboard.handle_update
def _handle_update_with_actions(update, state=None):
    cb = update.get("callback_query") or {}
    data = cb.get("data")

    if data == "REFRESH":
        state = state if state is not None else {"offset": 0}
        state["offset"] = max(
            int(state.get("offset", 0)),
            int(update.get("update_id", 0)) + 1,
        )
        msg = cb.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        message_id = msg.get("message_id")
        configured_chat = os.environ.get("CHAT_ID")

        if configured_chat and str(chat_id) != str(configured_chat):
            dashboard.answer_callback(cb.get("id"), "Unauthorized")
            return state

        dashboard.answer_callback(cb.get("id"), "Hourly report spouštím…")
        ok, message = _dispatch_hourly_report()
        if ok:
            dashboard.edit(
                chat_id,
                message_id,
                "🔄 HOURLY REPORT\n\nPožadavek byl odeslán.\nNový dashboard přijde za okamžik.",
                dashboard.menu_keyboard(),
            )
        else:
            dashboard.edit(
                chat_id,
                message_id,
                f"❌ Refresh se nepodařil\n\n{message}",
                dashboard.menu_keyboard(),
            )
        return state

    if data != "PAPER":
        return _original_handle_update(update, state)

    state = state if state is not None else {"offset": 0}
    state["offset"] = max(
        int(state.get("offset", 0)),
        int(update.get("update_id", 0)) + 1,
    )
    msg = cb.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")
    configured_chat = os.environ.get("CHAT_ID")

    if configured_chat and str(chat_id) != str(configured_chat):
        dashboard.answer_callback(cb.get("id"), "Unauthorized")
        return state

    dashboard.answer_callback(cb.get("id"))
    dashboard.edit(
        chat_id,
        message_id,
        dashboard.paper_report(),
        dashboard.paper_menu_keyboard(),
    )
    return state

dashboard.handle_update = _handle_update_with_actions

app = FastAPI()


def _cron_authorized(request: Request) -> bool:
    expected = os.environ.get("CRON_SECRET", "")
    received = request.headers.get("Authorization", "")
    if not expected or not received:
        return False
    return secrets.compare_digest(received, f"Bearer {expected}")


def _dispatch_workflow(workflow_file: str, run_reason: str) -> tuple[bool, str]:
    token = os.environ.get("GITHUB_DISPATCH_TOKEN", "")
    if not token:
        return False, "missing GITHUB_DISPATCH_TOKEN"

    url = (
        "https://api.github.com/repos/devilgest1704/sda-scanner/actions/"
        f"workflows/{workflow_file}/dispatches"
    )
    payload = json.dumps({
        "ref": "main",
        "inputs": {"run_reason": run_reason},
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
                return True, f"{workflow_file} dispatched"
            return False, f"GitHub returned HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        return False, f"GitHub returned HTTP {exc.code}"
    except Exception as exc:
        return False, f"dispatch error: {type(exc).__name__}"


def _dispatch_scanner() -> tuple[bool, str]:
    return _dispatch_workflow("scanner_v18.yml", "external_watchdog")


def _dispatch_hourly_report() -> tuple[bool, str]:
    return _dispatch_workflow("telegram_hourly.yml", "manual_refresh")


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
        return "ok"


@app.get("/api/watchdog", response_class=PlainTextResponse)
async def external_watchdog(request: Request):
    """External 5-minute watchdog endpoint."""
    if not _cron_authorized(request):
        return PlainTextResponse("forbidden", status_code=403)

    ok, message = _dispatch_scanner()
    if ok:
        return PlainTextResponse("ok: scanner dispatched")
    return PlainTextResponse(f"watchdog error: {message}", status_code=502)


@app.get("/api/hourly", response_class=PlainTextResponse)
async def external_hourly_report(request: Request):
    """External hourly Telegram report trigger."""
    if not _cron_authorized(request):
        return PlainTextResponse("forbidden", status_code=403)

    ok, message = _dispatch_hourly_report()
    if ok:
        return PlainTextResponse("ok: hourly report dispatched")
    return PlainTextResponse(f"hourly error: {message}", status_code=502)


# Vercel deploy trigger: 2026-09-10 16:45 UTC
