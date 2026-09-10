"""Keep real-wallet portfolio state consistent with the latest valid wallet snapshot.

READ ONLY: this never sends orders. Wallet balances are authoritative for open
quantity; the P/L cost basis is reconstructed from the real wallet trade ledger
so a partial sell cannot leave the original full cost attached to the remainder.
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


def _trade_amount(trade):
    """Return human token quantity from the ledger's SDA volume/price pair."""
    volume = _num(trade.get("cost_sda") if str(trade.get("side")).upper() == "BUY" else trade.get("proceeds_sda"))
    price = _num(trade.get("price_sda"))
    if volume > 0 and price > 0:
        return volume / price
    return 0.0


def _ledger_open_costs(trades):
    """FIFO-rebuild open amount/cost from the actual wallet trade ledger."""
    lots = {}
    for src in trades or []:
        if not isinstance(src, dict):
            continue
        token = str(src.get("token") or "").strip().lower()
        side = str(src.get("side") or "").strip().upper()
        qty = _trade_amount(src)
        if not token or qty <= 0 or side not in {"BUY", "SELL"}:
            continue
        if side == "BUY":
            lots.setdefault(token, []).append({"amount": qty, "cost": max(0.0, _num(src.get("cost_sda")))})
            continue
        remaining = qty
        queue = lots.setdefault(token, [])
        while remaining > 1e-12 and queue:
            lot = queue[0]
            take = min(remaining, lot["amount"])
            unit_cost = lot["cost"] / lot["amount"] if lot["amount"] > 0 else 0.0
            lot["amount"] -= take
            lot["cost"] = max(0.0, lot["cost"] - take * unit_cost)
            remaining -= take
            if lot["amount"] <= 1e-12:
                queue.pop(0)

    result = {}
    for token, queue in lots.items():
        amount = sum(_num(x.get("amount")) for x in queue)
        cost = sum(_num(x.get("cost")) for x in queue)
        if amount > 1e-12:
            result[token] = {"amount": amount, "cost_sda": cost, "lots": queue}
    return result


def sync():
    wallet = _load(WALLET_FILE, {})
    portfolio = _load(PORTFOLIO_FILE, {})
    if not isinstance(wallet, dict) or not isinstance(portfolio, dict):
        return False

    # A failed/stale explorer refresh must never turn a known wallet into an
    # empty wallet or alter portfolio accounting.
    if wallet.get("stale") or wallet.get("error"):
        print("Wallet snapshot is stale/errored; portfolio sync skipped.")
        return False

    current = portfolio.get("current")
    if not isinstance(current, dict):
        return False

    by_address, by_symbol = _holding_maps(wallet)
    ledger = _ledger_open_costs(portfolio.get("trades", []))
    result = deepcopy(portfolio)
    new_current = {}
    total_cost = 0.0
    total_pnl = 0.0
    synced_at = datetime.now(timezone.utc).isoformat()

    for key, original in current.items():
        if not isinstance(original, dict):
            continue
        position = deepcopy(original)
        address = str(position.get("address") or key).strip().lower()
        symbol = str(position.get("symbol") or "").strip().upper()
        holding = by_address.get(address) or (by_symbol.get(symbol) if symbol else None)
        if holding is None:
            # Successful wallet snapshot: genuinely absent token is no longer open.
            continue

        wallet_amount = _num(holding.get("amount"))
        token = address if address in ledger else None
        if token is None and symbol:
            for k, p in ledger.items():
                if str(position.get("symbol") or "").upper() == symbol:
                    token = k
                    break

        ledger_position = ledger.get(token) if token else None
        if ledger_position and ledger_position["amount"] > 0:
            ledger_amount = ledger_position["amount"]
            ledger_cost = ledger_position["cost_sda"]
            ratio = max(0.0, min(1.0, wallet_amount / ledger_amount))
            remaining_cost = ledger_cost * ratio
            position["lots"] = deepcopy(ledger_position["lots"])
        else:
            # No matching trade history: keep an existing cost only when the
            # wallet quantity has not changed; otherwise cost basis is unknown.
            old_amount = _num(position.get("amount"))
            old_cost = position.get("cost_sda")
            remaining_cost = _num(old_cost) if old_cost is not None and abs(wallet_amount - old_amount) < 1e-12 else None

        value = holding.get("value_sda")
        if value is None:
            price = _num(holding.get("price_sda"))
            value = wallet_amount * price if price > 0 else None

        position["amount"] = wallet_amount
        position["cost_sda"] = remaining_cost
        position["wallet_synced_at"] = synced_at
        position["cost_basis_status"] = "DETECTED" if remaining_cost is not None else "UNKNOWN"

        if value is not None:
            value = _num(value)
            position["value_sda"] = value
            if remaining_cost is not None:
                pnl = value - remaining_cost
                position["unrealized_pnl_sda"] = pnl
                position["unrealized_pnl_pct"] = pnl / remaining_cost * 100 if remaining_cost > 0 else 0.0
            else:
                position["unrealized_pnl_sda"] = None
                position["unrealized_pnl_pct"] = None
        else:
            position["value_sda"] = None
            position["unrealized_pnl_sda"] = None
            position["unrealized_pnl_pct"] = None

        new_current[key] = position
        if remaining_cost is not None:
            total_cost += remaining_cost
            total_pnl += _num(position.get("unrealized_pnl_sda"))

    result["current"] = new_current
    result["open_cost_sda"] = total_cost
    result["open_pnl_sda"] = total_pnl
    result["open_unrealized_pnl_sda"] = total_pnl
    result["wallet_sync_at"] = synced_at
    _save(PORTFOLIO_FILE, result)
    print(f"Wallet portfolio sync: {len(new_current)} open positions, cost={total_cost:.2f} SDA, P/L={total_pnl:+.2f} SDA")
    return True


if __name__ == "__main__":
    sync()
