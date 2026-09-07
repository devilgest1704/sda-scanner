import json
from datetime import datetime, timezone
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

EXPLORER_API = "https://ledger.sidrachain.com/api/v2"
RPC = "https://node.sidrachain.com"
POOL = "0xCB94460F967f49E3a955278f252Ca9D6056ecE75"
WSDA = "0xE4095a910209D7BE03B55D02F40d4554B1666182"
SWAP_SELECTOR = "0x8ab5246f"
TRADE_SIZE_SDA = 50.0
MAX_TXS = 50
SAMPLE_COUNT = 10
TIMEOUT = 8
RPC_TIMEOUT = 12
DISCOVERY_FILE = "sidra_swap_discovery.json"
LIQUIDITY_FILE = "liquidity_data.json"

session = requests.Session()
session.headers.update({"User-Agent": "sda-scanner/18.0"})

def now():
    return datetime.now(timezone.utc).isoformat()

def get_json(url, params=None):
    r = session.get(url, params=params or {}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def addr(x):
    if isinstance(x, dict):
        return x.get("hash") or x.get("address_hash") or ""
    return str(x or "")

def selector(raw):
    return raw[:10].lower() if isinstance(raw, str) and raw.startswith("0x") and len(raw) >= 10 else ""

def decode_call(raw):
    if selector(raw) != SWAP_SELECTOR:
        return None
    body = raw[10:]
    if len(body) < 192:
        return None
    return {
        "token": "0x" + body[24:64],
        "arg1": int(body[64:128], 16),
        "arg2": int(body[128:192], 16),
    }

def transfer_value(t):
    total = t.get("total")
    if isinstance(total, dict):
        raw = total.get("value")
        decimals = total.get("decimals")
    else:
        raw = t.get("value") or t.get("amount") or t.get("raw_value")
        decimals = t.get("decimals")
    try:
        raw_int = int(raw) if raw is not None else None
    except Exception:
        raw_int = None
    try:
        dec = int(decimals) if decimals is not None else None
    except Exception:
        dec = None
    formatted = None
    if raw_int is not None and dec is not None:
        formatted = float(Decimal(raw_int) / (Decimal(10) ** dec))
    return raw_int, dec, formatted

def normalize_transfer(t):
    token = t.get("token") or {}
    raw, dec, formatted = transfer_value(t)
    return {
        "token": (token.get("address_hash") or "").lower(),
        "symbol": token.get("symbol"),
        "decimals": dec,
        "raw_value": raw,
        "value": formatted,
        "from": addr(t.get("from")).lower(),
        "to": addr(t.get("to")).lower(),
        "log_index": t.get("log_index"),
        "method": t.get("method"),
    }

def list_pool_txs():
    data = get_json(
        f"{EXPLORER_API}/addresses/{POOL}/transactions",
        {"filter": "to", "items_count": MAX_TXS},
    )
    return data.get("items", []) if isinstance(data, dict) else []

def transfers_for_tx(tx_hash):
    data = get_json(f"{EXPLORER_API}/transactions/{tx_hash}/token-transfers")
    items = data.get("items", []) if isinstance(data, dict) else []
    return [normalize_transfer(x) for x in items]

def pad_word(value):
    return f"{int(value):064x}"

def build_observed_calldata(token, fee_tier, arg2):
    return SWAP_SELECTOR + pad_word(int(token, 16)) + pad_word(fee_tier) + pad_word(arg2)

def rpc_json(method, params, timeout=RPC_TIMEOUT):
    r = requests.post(
        RPC,
        json={"jsonrpc": "2.0", "id": 17, "method": method, "params": params},
        timeout=timeout,
        headers={"Content-Type": "application/json"},
    )
    r.raise_for_status()
    return r.json()

def eth_call(data, to_address=POOL, from_address=None, value_wei=0, block_tag="latest"):
    payload = {
        "jsonrpc": "2.0",
        "id": 17,
        "method": "eth_call",
        "params": [{
            "to": to_address,
            "from": from_address or "0x0000000000000000000000000000000000000001",
            "data": data,
            "value": hex(int(value_wei)),
        }, block_tag if isinstance(block_tag, str) else hex(int(block_tag))],
    }
    try:
        out = rpc_json("eth_call", payload["params"], timeout=RPC_TIMEOUT)
        if "error" in out:
            return {"ok": False, "result": None, "error": out["error"]}
        return {"ok": True, "result": out.get("result"), "error": None}
    except Exception as e:
        return {"ok": False, "result": None, "error": str(e)}

def eth_get_balance(address, block_tag="latest"):
    try:
        out = rpc_json("eth_getBalance", [
            address, block_tag if isinstance(block_tag, str) else hex(int(block_tag))
        ])
        if "error" in out:
            return None, out["error"]
        return int(out.get("result", "0x0"), 16), None
    except Exception as e:
        return None, str(e)

def decode_uint256_result(result):
    if not isinstance(result, str) or not result.startswith("0x"):
        return None
    raw = result[2:]
    if len(raw) < 64:
        return None
    try:
        return int(raw[-64:], 16)
    except Exception:
        return None

def analyze(tx):
    raw = tx.get("raw_input") or ""
    if selector(raw) != SWAP_SELECTOR:
        return None
    call = decode_call(raw)
    h = tx.get("hash") or tx.get("transaction_hash")
    transfers = transfers_for_tx(h)
    target = call["token"].lower()
    wsda = WSDA.lower()
    pool = POOL.lower()

    buyers = [
        t["from"] for t in transfers
        if t["token"] == wsda and t["to"] == pool and t["value"] is not None
    ]
    buyer = buyers[0] if buyers else ""

    sda_in = sum(
        t["value"] for t in transfers
        if t["token"] == wsda and t["from"] == buyer and t["to"] == pool
        and t["value"] is not None
    )
    token_out_transfers = [
        t for t in transfers
        if t["token"] == target and t["from"] == pool and t["to"] == buyer
        and t["value"] is not None
    ]
    token_out = sum(t["value"] for t in token_out_transfers)
    token_out_raw = sum(
        int(t["raw_value"]) for t in token_out_transfers
        if t.get("raw_value") is not None
    )
    token_decimals = token_out_transfers[0].get("decimals") if token_out_transfers else None

    return {
        "tx": h,
        "block": tx.get("block_number"),
        "status": tx.get("status"),
        "method": tx.get("method"),
        "selector": selector(raw),
        "raw_input": raw,
        "token": call["token"],
        "arg1": call["arg1"],
        "arg2": call["arg2"],
        "buyer": buyer,
        "sda_in": sda_in,
        "token_out": token_out,
        "token_out_raw": token_out_raw,
        "token_decimals": token_decimals,
        "arg2_matches_raw_output": token_out_raw > 0 and call["arg2"] == token_out_raw,
        "arg2_difference_raw": call["arg2"] - token_out_raw if token_out_raw > 0 else None,
        "tokens_per_sda": token_out / sda_in if sda_in > 0 else None,
        "arg2_per_sda": call["arg2"] / sda_in if sda_in > 0 else None,
        "transfers": transfers,
    }

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a9df523b3ef"

def keccak_topic(signature):
    try:
        from Crypto.Hash import keccak
        k = keccak.new(digest_bits=256)
        k.update(signature.encode())
        return "0x" + k.hexdigest()
    except Exception:
        return None

APPROVAL_TOPIC = keccak_topic("Approval(address,address,uint256)")

def topic_name(topic):
    t = (topic or "").lower()
    if t == TRANSFER_TOPIC:
        return "Transfer"
    if APPROVAL_TOPIC and t == APPROVAL_TOPIC.lower():
        return "Approval"
    return "UNKNOWN"

def topic_address(topic):
    if isinstance(topic, str) and len(topic) >= 40:
        return "0x" + topic[-40:]
    return ""

def decode_word(data):
    if not isinstance(data, str) or not data.startswith("0x"):
        return None
    try:
        return int(data, 16)
    except Exception:
        return None

def decode_receipt_logs(receipt):
    out = []
    for i, log in enumerate((receipt or {}).get("logs") or []):
        topics = log.get("topics") or []
        data = log.get("data") or "0x"
        t0 = topics[0].lower() if topics else None
        item = {
            "index": i,
            "address": (log.get("address") or "").lower(),
            "topic0": t0,
            "event": topic_name(t0),
            "topics": topics,
            "data": data,
        }
        if t0 == TRANSFER_TOPIC and len(topics) >= 3:
            item["from"] = topic_address(topics[1]).lower()
            item["to"] = topic_address(topics[2]).lower()
            item["amount_raw"] = decode_word(data)
        elif APPROVAL_TOPIC and t0 == APPROVAL_TOPIC.lower() and len(topics) >= 3:
            item["owner"] = topic_address(topics[1]).lower()
            item["spender"] = topic_address(topics[2]).lower()
            item["amount_raw"] = decode_word(data)
        else:
            h = data[2:] if data.startswith("0x") else data
            item["data_words"] = [
                "0x" + h[i:i+64] for i in range(0, len(h), 64)
                if len(h[i:i+64]) == 64
            ]
        out.append(item)
    return out

def get_internal_txs(tx_hash):
    try:
        data = get_json(f"{EXPLORER_API}/transactions/{tx_hash}/internal-transactions")
        return data.get("items", []) if isinstance(data, dict) else []
    except Exception:
        return []

def rpc_trace(tx_hash):
    attempts = []
    for method, params in [
        ("trace_transaction", [tx_hash]),
        ("debug_traceTransaction", [tx_hash, {"tracer": "callTracer"}]),
        ("trace_replayTransaction", [tx_hash, ["trace"]]),
    ]:
        try:
            out = rpc_json(method, params, timeout=20)
            attempts.append({"method": method, "response": out})
            if out.get("result") is not None:
                return {"method": method, "result": out["result"], "attempts": attempts}
        except Exception as e:
            attempts.append({"method": method, "exception": str(e)})
    return {"method": None, "result": None, "attempts": attempts}

def flatten_trace_calls(node, out=None, depth=0):
    if out is None:
        out = []
    if not isinstance(node, dict):
        return out
    typ = node.get("type")
    if typ in ("CALL", "DELEGATECALL", "STATICCALL", "CALLCODE", "CREATE", "CREATE2"):
        inp = node.get("input") or node.get("data") or ""
        out.append({
            "depth": depth,
            "type": typ,
            "from": (node.get("from") or "").lower(),
            "to": (node.get("to") or "").lower(),
            "value": node.get("value"),
            "input": inp,
            "selector": selector(inp),
            "output": node.get("output"),
            "error": node.get("error"),
        })
    for child in node.get("calls") or []:
        flatten_trace_calls(child, out, depth + 1)
    return out

def trace_calls(trace):
    result = trace.get("result") if trace else None
    if isinstance(result, list):
        return [
            {
                "depth": 0,
                "type": x.get("type"),
                "from": (x.get("action", {}).get("from") or "").lower(),
                "to": (x.get("action", {}).get("to") or "").lower(),
                "value": x.get("action", {}).get("value"),
                "input": x.get("action", {}).get("input") or "",
                "selector": selector(x.get("action", {}).get("input") or ""),
                "output": x.get("result"),
                "error": x.get("error"),
            }
            for x in result if isinstance(x, dict)
        ]
    return flatten_trace_calls(result)

def extract_push4(bytecode):
    if not isinstance(bytecode, str) or not bytecode.startswith("0x"):
        return []
    raw = bytecode[2:]
    out, i = [], 0
    while i + 2 <= len(raw):
        op = int(raw[i:i+2], 16)
        i += 2
        if op == 0x63 and i + 8 <= len(raw):
            s = "0x" + raw[i:i+8].lower()
            if s not in out:
                out.append(s)
            i += 8
        elif 0x60 <= op <= 0x7f:
            i += 2 * (op - 0x5f)
    return out


# Known selectors used by common V3-style pools. We probe them with eth_call only;
# unsupported selectors simply revert and are recorded as such.
KNOWN_READ_SELECTORS = {
    "0x0dfe1681": "token0()",
    "0xd21220a7": "token1()",
    "0xddca3f43": "fee()",
    "0x3850c7bd": "slot0()",
    "0x1a686502": "liquidity()",
    "0x0902f1ac": "getReserves()",
    "0x18160ddd": "totalSupply()",
    "0x70a08231": "balanceOf(address)",
}

# Correct canonical ERC-20 Transfer topic.
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a9df523b3ef"


def probe_selector(sel, name):
    r = eth_call(sel, to_address=POOL, from_address="0x0000000000000000000000000000000000000001")
    return {
        "selector": sel,
        "name": name,
        "ok": bool(r.get("ok")),
        "result": r.get("result"),
        "error": r.get("error"),
    }


def decode_static_words(result):
    if not isinstance(result, str) or not result.startswith("0x"):
        return []
    raw = result[2:]
    if len(raw) % 64:
        return []
    return ["0x" + raw[i:i+64] for i in range(0, len(raw), 64)]


def signed_int256_word(word):
    x = int(word, 16)
    return x - (1 << 256) if x >= (1 << 255) else x


def decode_swap_event_log(log):
    topics = log.get("topics") or []
    data = log.get("data") or ""
    if not topics or topics[0].lower() != "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67":
        return None
    words = decode_static_words(data)
    if len(words) != 5:
        return None
    return {
        "sender": topic_address(topics[1]) if len(topics) > 1 else "",
        "recipient": topic_address(topics[2]) if len(topics) > 2 else "",
        "amount0": signed_int256_word(words[0]),
        "amount1": signed_int256_word(words[1]),
        "sqrt_price_x96": int(words[2], 16),
        "liquidity": int(words[3], 16),
        "tick": signed_int256_word(words[4]),
    }


def fetch_receipt(tx_hash):
    try:
        r = rpc_json("eth_getTransactionReceipt", [tx_hash])
        return r.get("result") if isinstance(r, dict) else None
    except Exception:
        return None


def event_analysis(tx_hash):
    receipt = fetch_receipt(tx_hash)
    if not receipt:
        return None
    swaps = []
    for log in receipt.get("logs") or []:
        item = decode_swap_event_log(log)
        if item:
            item["address"] = (log.get("address") or "").lower()
            swaps.append(item)
    return {
        "tx": tx_hash,
        "block": int(receipt.get("blockNumber", "0x0"), 16) if receipt.get("blockNumber") else None,
        "status": receipt.get("status"),
        "log_count": len(receipt.get("logs") or []),
        "swap_events": swaps,
    }

def main():
    print("💧 LIQUIDITY V18 — RECEIPT EVENT / ABI FORENSICS")
    print(f"Pool: {POOL}")
    print(f"Target trade: {TRADE_SIZE_SDA:.0f} SDA")
    print("Safety: READ-ONLY / no broadcast")

    try:
        txs = list_pool_txs()
    except Exception as e:
        print("Pool transaction discovery error:", e)
        txs = []

    swap_txs = [t for t in txs if selector(t.get("raw_input") or "") == SWAP_SELECTOR]

    counts = {}
    for t in txs:
        s = selector(t.get("raw_input") or "")
        if s:
            counts[s] = counts.get(s, 0) + 1

    print(f"Pool calls discovered: {len(txs)}")
    print("Selector counts:")
    for s, n in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {s}: {n}")
    print(f"Real {SWAP_SELECTOR} transactions: {len(swap_txs)}")
    print(f"Analyzing {min(SAMPLE_COUNT, len(swap_txs))} samples concurrently")

    samples = []
    with ThreadPoolExecutor(max_workers=min(5, max(1, len(swap_txs[:SAMPLE_COUNT])))) as ex:
        futures = [ex.submit(analyze, t) for t in swap_txs[:SAMPLE_COUNT]]
        for f in as_completed(futures):
            try:
                a = f.result()
                if a:
                    samples.append(a)
            except Exception as e:
                print("Analysis error:", e)

    usable = []
    for a in sorted(samples, key=lambda x: x.get("tx", "")):
        print()
        print("TX " + str(a["tx"]))
        print("  token=" + a["token"])
        print("  arg1=" + str(a["arg1"]))
        print("  arg2=" + str(a["arg2"]))
        print("  buyer=" + a["buyer"])
        print("  WSDA user→pool=" + str(a["sda_in"]))
        print("  token pool→user=" + str(a["token_out"]))
        print("  token raw output=" + str(a["token_out_raw"]))
        print("  token decimals=" + str(a["token_decimals"]))
        print("  arg2 == raw token output: " + str(a["arg2_matches_raw_output"]))
        print("  arg2 - raw output=" + str(a["arg2_difference_raw"]))
        print("  tokens/SDA=" + str(a["tokens_per_sda"]))

        for t in a["transfers"]:
            if t["value"] is not None:
                print(
                    f"    {t['symbol'] or t['token']} {t['value']} "
                    f"{t['from'][:10]}→{t['to'][:10]}"
                )

        if a["sda_in"] > 0 and a["token_out"] > 0:
            usable.append(a)

    forensic = None
    if usable:
        # Prefer the newest successful BUY sample.
        forensic_sample = max(usable, key=lambda x: int(x.get("block") or 0))
        h = forensic_sample["tx"]

        print()
        print("🔎 FORENSICS TX " + h)

        tx, txerr = None, None
        rec, recerr = None, None
        try:
            txj = rpc_json("eth_getTransactionByHash", [h])
            tx, txerr = txj.get("result"), txj.get("error")
        except Exception as e:
            txerr = str(e)

        try:
            recj = rpc_json("eth_getTransactionReceipt", [h])
            rec, recerr = recj.get("result"), recj.get("error")
        except Exception as e:
            recerr = str(e)

        print("  RPC from=" + str((tx or {}).get("from")))
        print("  RPC to=" + str((tx or {}).get("to")))
        print("  RPC value=" + str((tx or {}).get("value")))
        print("  RPC block=" + str((tx or {}).get("blockNumber")))
        print("  receipt status=" + str((rec or {}).get("status")))
        print("  receipt gasUsed=" + str((rec or {}).get("gasUsed")))

        logs = decode_receipt_logs(rec)
        print("  receipt logs=" + str(len(logs)))
        print("  decoded receipt logs=" + str(sum(1 for x in logs if x["event"] != "UNKNOWN")))

        for x in logs:
            print(
                "    LOG " + str(x["index"]) +
                " address=" + str(x["address"]) +
                " event=" + str(x["event"]) +
                " topic0=" + str(x["topic0"])
            )
            if x["event"] == "Transfer":
                print(
                    "      Transfer " + x["from"] + " -> " + x["to"] +
                    " amount_raw=" + str(x["amount_raw"])
                )
            elif x["event"] == "Approval":
                print(
                    "      Approval " + x["owner"] + " -> " + x["spender"] +
                    " amount_raw=" + str(x["amount_raw"])
                )
            else:
                print("      topics=" + str(x["topics"]))
                print("      data=" + str(x["data"]))
                if x.get("data_words"):
                    print("      data_words=" + str(x["data_words"]))

        print()
        print("  unique topic0 across sampled BUY receipts:")
        topic_counts = {}
        for a in usable[:SAMPLE_COUNT]:
            try:
                rj = rpc_json("eth_getTransactionReceipt", [a["tx"]])
                rr = rj.get("result") or {}
                for lg in rr.get("logs") or []:
                    ts = lg.get("topics") or []
                    if ts:
                        t0 = ts[0].lower()
                        topic_counts[t0] = topic_counts.get(t0, 0) + 1
            except Exception as e:
                print("    receipt error:", e)

        for t0, n in sorted(topic_counts.items(), key=lambda x: -x[1]):
            print("    " + str(n) + "x " + t0 + "  " + topic_name(t0))

        internal = get_internal_txs(h)
        print()
        print("  explorer internal transactions=" + str(len(internal)))
        for item in internal[:20]:
            print(
                "    " + str(addr(item.get("from"))) + " -> " +
                str(addr(item.get("to"))) +
                " type=" + str(item.get("type")) +
                " method=" + str(item.get("method")) +
                " value=" + str(item.get("value"))
            )

        trace = rpc_trace(h)
        calls = trace_calls(trace)
        print("  trace method=" + str(trace.get("method")))
        print("  trace calls=" + str(len(calls)))
        for c in calls[:30]:
            print(
                "    " * min(c.get("depth", 0) + 1, 6) +
                str(c.get("type")) + " " + str(c.get("from")) +
                " -> " + str(c.get("to")) +
                " value=" + str(c.get("value")) +
                " selector=" + str(c.get("selector")) +
                " error=" + str(c.get("error"))
            )

        block = (tx or {}).get("blockNumber") or "latest"
        code, codeerr = rpc_get_code(POOL, block) if "rpc_get_code" in globals() else (None, None)
        if code is None:
            try:
                cj = rpc_json("eth_getCode", [POOL, block])
                code, codeerr = cj.get("result"), cj.get("error")
            except Exception as e:
                code, codeerr = None, str(e)

        selectors = extract_push4(code or "0x")
        print("  pool code bytes=" + str(max(0, (len(code or "") - 2) // 2)))
        print("  pool PUSH4 selectors=" + str(len(selectors)))
        print("    " + ", ".join(selectors))
        print("  pool contains 0x8ab5246f: " + str(SWAP_SELECTOR in selectors))

        forensic = {
            "tx": h,
            "transaction": tx,
            "receipt": rec,
            "decoded_logs": logs,
            "trace": trace,
            "trace_calls": calls,
            "internal_transactions": internal,
            "pool_push4": selectors,
        }

    discovery = load_json(DISCOVERY_FILE, {})
    discovery["updated_at"] = now()
    discovery["version"] = 18
    discovery["pool"] = POOL
    discovery["swap_selector"] = SWAP_SELECTOR
    discovery["samples"] = usable
    discovery["forensics"] = forensic
    discovery["arg1_observed"] = sorted(set(a["arg1"] for a in usable))
    discovery["arg2_exact_raw_output_matches"] = sum(
        1 for a in usable if a["arg2_matches_raw_output"]
    )

    # V18: probe common read-only state selectors on the pool. No state-changing
    # transaction is ever sent. This is deliberately separate from the historical
    # forensic evidence so a guessed ABI can never be treated as verified.
    print()
    print("🔬 V18 READ-ONLY POOL STATE PROBES")
    probes = []
    for sel, name in KNOWN_READ_SELECTORS.items():
        p = probe_selector(sel, name)
        probes.append(p)
        if p["ok"]:
            words = decode_static_words(p["result"])
            print(f"  {sel} {name}: OK words={len(words)} result={p['result']}")
        else:
            print(f"  {sel} {name}: REVERT/UNSUPPORTED")

    event_evidence = []
    for item in usable[:SAMPLE_COUNT]:
        ev = event_analysis(item["tx"])
        if ev:
            event_evidence.append(ev)
            for sw in ev["swap_events"]:
                print()
                print(f"  Swap event {item['tx']}")
                print(f"    amount0={sw['amount0']}")
                print(f"    amount1={sw['amount1']}")
                print(f"    sqrtPriceX96={sw['sqrt_price_x96']}")
                print(f"    liquidity={sw['liquidity']}")
                print(f"    tick={sw['tick']}")

    discovery["v18_read_only_probes"] = probes
    discovery["v18_swap_events"] = event_evidence
    save_json(DISCOVERY_FILE, discovery)
    save_json(DISCOVERY_FILE, discovery)

    liquidity = load_json(LIQUIDITY_FILE, {})
    liquidity.update({
        "updated_at": now(),
        "version": 18,
        "pool": POOL,
        "trade_size_sda": TRADE_SIZE_SDA,
        "status": "UNKNOWN",
        "reason": "Quote execution semantics not yet proven; V18 read-only ABI/state discovery completed; executable quote still requires semantic validation.",
        "observed_buy_samples": len(usable),
        "arg1_values": sorted(set(a["arg1"] for a in usable)),
        "arg2_exact_raw_output_matches": sum(
            1 for a in usable if a["arg2_matches_raw_output"]
        ),
        "v18_quote_status": "UNVERIFIED",
        "v18_note": "Historical Swap events are decoded read-only. Pool state selectors are probed but no guessed ABI result is promoted to a quote until validated against historical amount0/amount1 semantics.",
    })
    save_json(LIQUIDITY_FILE, liquidity)

    print()
    print("Saved " + DISCOVERY_FILE)
    print("Saved " + LIQUIDITY_FILE)
    print("Overall liquidity status: UNKNOWN")
    print("Next: use discovered read-only selectors/state and V3-style Swap event data to validate a live quote without broadcasting.")

if __name__ == "__main__":
    main()
