"""Keep real-wallet portfolio state consistent with the latest valid wallet snapshot.

This is READ ONLY: it never sends orders and never changes realized ledger P/L.
"""
import json
import os
from copy import deepcopy
from datetime import datetime, timezone

WALLET_FILE = "wallet_data.json"
PORTFOLIO_FILE = "portfolio_data.json"


def _num(value, default=0.0):
    try:
        return default if value is None else float(value)
    except (TypeError, ValueError):
        return default


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def _save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _holding_maps(wallet):
    by_address = {}
    by_symbol = {}
    for holding in wallet.get("holdings", []) or []:
        if not isinstance(holding, dict):
            continue
        amount = _num(holding.get("amount"))
        if amount < 0:
            continue
        address = str(holding.get("address") or "").strip().lower()
        symbol = str(holding.get("symbol") or "").strip().upper()
        if address:
            by_address[address] = holding
        if symbol:
            by_symbol[symbol] = holding
    return by_address, by_symbol


def sync():
    wallet = _load(WALLET_FILE, {})
    portfolio = _load(PORTFOLIO_FILE, {})
    if not isinstance(wallet, dict) or not isinstance(portfolio, dict):
        return False

    # Never mutate the portfolio from a failed/stale wallet refresh. The scanner
    # can temporarily receive HTTP 429 from the explorer; an empty snapshot must
    # not be interpreted as "the wallet is empty".
    if wallet.get("stale") or wallet.get("error"):
        print("Wallet snapshot is stale/errored; portfolio sync skipped.")
        return False

    current = portfolio.get("current")
    if not isinstance(current, dict):
        return False

    by_address, by_symbol = _holding_maps(wallet)
    result = deepcopy(portfolio)
    new_current = {}
    total_cost = 0.0
    total_pnl = 0.0

    for key, position in current.items():
        if not isinstance(position, dict):
            continue
        address = str(position.get("address") or key).strip().lower()
        symbol = str(position.get("symbol") or "").strip().upper()
        holding = by_address.get(address) or (by_symbol.get(symbol) if symbol else None)
        if holding is None:
            # Fresh wallet snapshot and token is genuinely absent: it is no
            # longer an open real position. Keep the historical trade ledger;
            # only remove it from current holdings.
            continue

        wallet_amount = _num(holding.get("amount"))
        old_amount = _num(position.get("amount"))
        old_cost = _num(position.get("cost_sda"))
        if old_amount > 0:
            remaining_cost = old_cost * max(0.0, min(1.0, wallet_amount / old_amount))
        else:
            remaining_cost = old_cost

        value = holding.get("value_sda")
        if value is None:
            price = _num(holding.get("price_sda"))
            value = wallet_amount * price if price > 0 else None

        position["amount"] = wallet_amount
        position["cost_sda"] = remaining_cost
        if value is not None:
            value = _num(value)
            pnl = value - remaining_cost
            position["value_sda"] = value
            position["unrealized_pnl_sda"] = pnl
            position["unrealized_pnl_pct"] = pnl / remaining_cost * 100 if remaining_cost > 0 else 0.0
        elif remaining_cost > 0:
            # Do not invent a market value when no valid price is available.
            position["unrealized_pnl_sda"] = None
            position["unrealized_pnl_pct"] = None

        position["wallet_synced_at"] = datetime.now(timezone.utc).isoformat()
        new_current[key] = position
        total_cost += remaining_cost
        total_pnl += _num(position.get("unrealized_pnl_sda"))

    result["current"] = new_current
    result["open_cost_sda"] = total_cost
    result["open_pnl_sda"] = total_pnl
    result["open_unrealized_pnl_sda"] = total_pnl
    result["wallet_sync_at"] = datetime.now(timezone.utc).isoformat()
    _save(PORTFOLIO_FILE, result)
    print(f"Wallet portfolio sync: {len(new_current)} open positions, cost={total_cost:.2f} SDA, P/L={total_pnl:+.2f} SDA")
    return True


if __name__ == "__main__":
    sync()
