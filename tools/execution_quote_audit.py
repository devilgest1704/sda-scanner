"""Bounded read-only full-model-position sell probes; no wallet or signing APIs.

A successful eth_call is a block-pinned third-party-holder simulation, never
an executable fill or a guarantee of future liquidation. Missing allowance or
historical route evidence is UNKNOWN, not proof of zero liquidity.
"""
import json
import re
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import requests

RPC_URL = "https://node.sidrachain.com"
ROUTER = "0x35cac72db00e8dac0e4f7f8a0f53d339e0cc23fb"
WSDA = "0xe4095a910209d7be03b55d02f40d4554b1666182"
REPORT = Path("strategy_lab/execution_quote_audit.json")
ALLOWED_RPC = frozenset(("eth_blockNumber", "eth_getBlockByNumber", "eth_call"))
MAX_SECONDS = 18
SESSION = requests.Session()


def timestamp(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


def address(value):
    return isinstance(value, str) and re.fullmatch(r"0x[0-9a-fA-F]{40}", value) is not None


def uint(value):
    if isinstance(value, bool):
        raise ValueError("Invalid integer")
    result = int(value)
    if not 0 <= result < 2**256:
        raise ValueError("Invalid uint256")
    return result


def word(value):
    return f"{uint(value):064x}"


def rpc(method, params):
    if method not in ALLOWED_RPC:
        raise ValueError("RPC method forbidden in read-only audit")
    response = SESSION.post(RPC_URL, json={"jsonrpc": "2.0", "id": 1,
                            "method": method, "params": params}, timeout=(2, 3))
    response.raise_for_status()
    data = response.json()
    if data.get("error") or "result" not in data:
        raise ValueError("RPC unavailable or simulation reverted")
    return data["result"]


def quote_provenance(liquidity):
    quotes = liquidity.get("quotes_50_sda") or {}
    counts = {}
    for token, row in quotes.items():
        reasons = []
        if row.get("source") == "fallback_fee":
            reasons.append("unbound_fallback_template")
        if str(row.get("token_out") or "").lower() != token.lower():
            reasons.append("missing_or_mismatched_token_binding")
        if not row.get("block_number") or not timestamp(row.get("quoted_at")):
            reasons.append("no_per_quote_block_and_time")
        for reason in reasons:
            counts[reason] = counts.get(reason, 0)+1
    return {"quotes_checked": len(quotes), "issues": counts,
            "sellability_verified": False,
            "note": "BUY amountOut alone never validates SELL liquidity"}


def buy_template(discovery, token):
    # Decode exact bytes; decimal JSON arrays may have lost uint256 precision.
    for item in discovery.get("v25_router_transactions") or []:
        raw = item.get("raw_input") or ""
        if not re.fullmatch(r"0x414bf389[0-9a-fA-F]{512}", raw):
            continue
        words = [int(raw[i:i+64], 16) for i in range(10, len(raw), 64)]
        if words[0] != int(WSDA, 16) or words[1] != int(token, 16):
            continue
        owner = item.get("from")
        if address(owner) and 0 < words[2] < 2**24:
            return {"owner": owner.lower(), "fee": words[2], "base_tx": item.get("tx")}
    return None


def sell_calldata(token, fee, owner, amount, deadline):
    if not address(token) or not address(owner):
        raise ValueError("Invalid route address")
    return "0x414bf389"+"".join(word(x) for x in (
        int(token, 16), int(WSDA, 16), fee, int(owner, 16), deadline, amount, 0, 0))


def audit(state, liquidity, discovery, market, now=None, call=rpc):
    now = now or datetime.now(timezone.utc)
    result = {"updated_at": now.isoformat(), "source_at": state.get("updated_at"),
              "mode": "READ_ONLY_SIMULATION_NOT_FILLS", "can_promote": False,
              "provenance": quote_provenance(liquidity), "positions": []}
    source = timestamp(state.get("updated_at"))
    if source is None or not 0 <= (now-source).total_seconds() <= 300:
        return {**result, "status": "STALE_SHADOW_SOURCE"}
    targets = {}
    for name in ("research_temporal_recovery_tp4", "research_temporal_recovery"):
        for token, pos in state.get("strategies", {}).get(name, {}).get("positions", {}).items():
            targets.setdefault(token, (name, pos))
    if not targets:
        return {**result, "status": "NO_OPEN_TEMPORAL_POSITIONS"}
    started = time.monotonic()

    def read(method, params):
        if time.monotonic()-started > MAX_SECONDS:
            raise TimeoutError("Read-only audit budget exhausted")
        return call(method, params)

    try:
        block = read("eth_blockNumber", [])
        chain = read("eth_getBlockByNumber", [block, False])
        block_at = datetime.fromtimestamp(int(chain["timestamp"], 16), timezone.utc)
        if not 0 <= (now-block_at).total_seconds() <= 300:
            raise ValueError("Stale/future chain block")
        result["block_number"] = int(block, 16)
    except Exception as exc:
        return {**result, "status": "RPC_UNAVAILABLE", "error": type(exc).__name__}
    for token, (name, pos) in list(targets.items())[:2]:
        row = {"token": token, "strategy": name, "opened_at": pos.get("opened_at"),
               "status": "UNVERIFIED", "quantity_basis": "50_SDA_model_after_entry_friction",
               "executable_fill": False}
        result["positions"].append(row)
        try:
            if not address(token):
                raise ValueError("Invalid token")
            route = buy_template(discovery, token)
            if route is None:
                row["status"] = "NO_TOKEN_BOUND_ROUTE_TEMPLATE"
                continue
            row["route_source_tx"] = route["base_tx"]
            owner, fee = route["owner"], route["fee"]

            def token_read(data):
                return int(read("eth_call", [{"to": token, "data": data}, block]), 16)

            decimals = token_read("0x313ce567")
            if not 0 <= decimals <= 36:
                raise ValueError("Unknown decimals")
            entry = Decimal(str(pos["entry_price_sda"]))
            if not entry.is_finite() or entry <= 0:
                raise ValueError("Invalid model entry")
            tokens = Decimal(50)/(entry*Decimal("1.011"))
            amount = uint(int(tokens*Decimal(10)**decimals))
            if amount == 0:
                raise ValueError("Zero token amount")
            row["token_amount_raw"] = str(amount)
            balance = token_read("0x70a08231"+word(int(owner, 16)))
            allowance = token_read("0xdd62ed3e"+word(int(owner, 16))+word(int(ROUTER, 16)))
            if balance < amount or allowance < amount:
                row["status"] = "HOLDER_BALANCE_OR_ALLOWANCE_UNAVAILABLE"
                continue
            data = sell_calldata(token, fee, owner, amount, int(now.timestamp())+300)
            raw = read("eth_call", [{"from": owner, "to": ROUTER, "data": data,
                                      "value": "0x0"}, block])
            if not re.fullmatch(r"0x[0-9a-fA-F]{64}", raw) or int(raw, 16) <= 0:
                raise ValueError("Unverified return shape")
            output = Decimal(int(raw, 16))/Decimal(10)**18
            row.update(status="SIMULATED_SELL_NOT_FILL", amount_out_sda=float(output),
                       output_asset="WSDA", sda_equivalence="1:1 wrapped SDA model, no unwrap simulated",
                       fee=fee, block_number=int(block, 16))
            info = (market.get("tokens") or {}).get(token, {}).get("analysis", {})
            observed_at = timestamp(info.get("last_transaction"))
            price = Decimal(str(info.get("price_in_sda") or 0))
            if observed_at and 0 <= (now-observed_at).total_seconds() <= 300 and price > 0:
                row["haircut_vs_last_swap_pct"] = float((1-output/(tokens*price))*100)
        except Exception as exc:
            row.update(status="SIMULATION_UNAVAILABLE", error=type(exc).__name__)
    result["targets_deferred"] = max(0, len(targets)-2)
    return {**result, "status": "AUDITED_NOT_EXECUTION_PROOF"}


def load(path):
    return json.loads(Path(path).read_text())


def main():
    result = audit(load("strategy_lab/forward_shadow_state.json"), load("liquidity_data.json"),
                   load("sidra_swap_discovery.json"), load("market_analysis.json"))
    REPORT.parent.mkdir(exist_ok=True)
    temp = REPORT.with_suffix(".tmp")
    temp.write_text(json.dumps(result, indent=2)+"\n")
    temp.replace(REPORT)
    print("EXECUTION QUOTE AUDIT:", result["status"], "positions:", len(result["positions"]))


if __name__ == "__main__":
    main()
