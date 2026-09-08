import json
from datetime import datetime, timezone
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

EXPLORER_API = "https://ledger.sidrachain.com/api/v2"
RPC = "https://node.sidrachain.com"
POOL = "0xCB94460F967f49E3a955278f252Ca9D6056ecE75"
WSDA = "0xE4095a910209D7BE03B55D02F40d4554B1666182"
ROUTER = "0x35cAC72Db00e8dAC0e4f7F8A0F53D339E0cC23fb"
SWAP_SELECTOR = "0x8ab5246f"
BUY_SELECTOR = "0x414bf389"
SELL_SELECTOR = "0xc04b8d59"
TRADE_SIZE_SDA = 50.0
MAX_TXS = 50
ROUTER_MAX_TXS = 30
SAMPLE_COUNT = 8
ROUTER_SAMPLE_COUNT = 5
TIMEOUT = 6
RPC_TIMEOUT = 8
DISCOVERY_FILE = "sidra_swap_discovery.json"
LIQUIDITY_FILE = "liquidity_data.json"

session = requests.Session()
session.headers.update({"User-Agent": "sda-scanner/24.0"})


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
    """Normalize Blockscout address fields, which may be strings or nested dicts."""
    if isinstance(x, str):
        return x.lower()
    if isinstance(x, dict):
        for key in ("hash", "address", "value"):
            v = x.get(key)
            if isinstance(v, str) and v:
                return v.lower()
            if isinstance(v, dict):
                nested = addr(v)
                if nested:
                    return nested
    return ""


def selector(raw):
    if not isinstance(raw, str) or not raw.startswith("0x") or len(raw) < 10:
        return ""
    return raw[:10].lower()


def decode_call(raw):
    raw = raw or ""
    body = raw[10:]
    words = []
    for i in range(0, len(body), 64):
        w = body[i:i + 64]
        if len(w) == 64:
            words.append("0x" + w)
    token = ""
    if words:
        token = "0x" + words[0][-40:]
    return {
        "selector": selector(raw),
        "words": words,
        "token": token,
        "arg1": int(words[1], 16) if len(words) > 1 else None,
        "arg2": int(words[2], 16) if len(words) > 2 else None,
    }


def transfer_value(t):
    for key in ("total", "value", "amount"):
        v = t.get(key)
        if isinstance(v, dict):
            v = v.get("value") or v.get("raw")
        if v is not None:
            try:
                return Decimal(str(v))
            except Exception:
                pass
    return Decimal(0)


def normalize_transfer(t):
    token = t.get("token") or {}
    raw = t.get("total") or {}
    if isinstance(raw, dict):
        raw_value = raw.get("value") or raw.get("raw") or "0"
        formatted = raw.get("formatted")
    else:
        raw_value = raw
        formatted = None
    try:
        raw_int = int(raw_value)
    except Exception:
        raw_int = 0
    if formatted is None:
        decimals = token.get("decimals")
        try:
            formatted = float(Decimal(raw_int) / (Decimal(10) ** int(decimals))) if decimals is not None else float(raw_int)
        except Exception:
            formatted = float(raw_int)
    return {
        "token_address": addr(token.get("address") or t.get("token_address")),
        "symbol": token.get("symbol") or "",
        "decimals": token.get("decimals"),
        "raw_value": raw_int,
        "value": formatted,
        "from": addr(t.get("from")),
        "to": addr(t.get("to")),
        "log_index": t.get("log_index"),
        "method": t.get("method"),
    }


def list_pool_txs():
    data = get_json(
        f"{EXPLORER_API}/addresses/{POOL}/transactions",
        {"filter": "to", "items_count": MAX_TXS},
    )
    return data.get("items", []) if isinstance(data, dict) else []


def list_router_txs():
    data = get_json(
        f"{EXPLORER_API}/addresses/{ROUTER}/transactions",
        {"filter": "to", "items_count": ROUTER_MAX_TXS},
    )
    return data.get("items", []) if isinstance(data, dict) else []


def transaction_detail(tx_hash):
    try:
        data = get_json(f"{EXPLORER_API}/transactions/{tx_hash}")
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def tx_hash_of(tx):
    h = tx.get("hash") or tx.get("transaction_hash") or tx.get("tx_hash")
    return h if isinstance(h, str) else addr(h)


def raw_input_of(obj):
    for key in ("raw_input", "input", "calldata"):
        v = obj.get(key) if isinstance(obj, dict) else None
        if isinstance(v, str):
            return v
        if isinstance(v, dict):
            for sub in ("value", "data", "raw_input", "input"):
                sv = v.get(sub)
                if isinstance(sv, str):
                    return sv
    return ""


def numeric_wei(x):
    if isinstance(x, dict):
        for key in ("value", "raw", "amount"):
            if key in x:
                return numeric_wei(x[key])
        return 0
    if x is None:
        return 0
    if isinstance(x, int):
        return x
    if isinstance(x, str):
        try:
            return int(x, 0) if x.startswith("0x") else int(x)
        except Exception:
            return 0
    return 0


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
        json={"jsonrpc": "2.0", "id": 21, "method": method, "params": params},
        timeout=timeout,
        headers={"Content-Type": "application/json"},
    )
    r.raise_for_status()
    return r.json()


def eth_call(data, to_address=POOL, from_address=None, value_wei=0, block_tag="latest"):
    params = [{
        "to": to_address,
        "from": from_address or "0x0000000000000000000000000000000000000001",
        "data": data,
        "value": hex(int(value_wei)),
    }, block_tag if isinstance(block_tag, str) else hex(int(block_tag))]
    try:
        out = rpc_json("eth_call", params, timeout=RPC_TIMEOUT)
        if "error" in out:
            return {"ok": False, "result": None, "error": out["error"]}
        return {"ok": True, "result": out.get("result"), "error": None}
    except Exception as e:
        return {"ok": False, "result": None, "error": str(e)}


def eth_get_balance(address, block_tag="latest"):
    try:
        out = rpc_json("eth_getBalance", [address, block_tag if isinstance(block_tag, str) else hex(int(block_tag))])
        if "error" in out:
            return None, out["error"]
        return int(out.get("result", "0x0"), 16), None
    except Exception as e:
        return None, str(e)


def eth_get_code(address, block_tag="latest"):
    try:
        out = rpc_json("eth_getCode", [address, block_tag if isinstance(block_tag, str) else hex(int(block_tag))])
        if "error" in out:
            return None, out["error"]
        return out.get("result", "0x"), None
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
    raw = raw_input_of(tx)
    if selector(raw) != SWAP_SELECTOR:
        return None
    call = decode_call(raw)
    h = tx.get("hash") or tx.get("transaction_hash")
    transfers = transfers_for_tx(h)
    target = addr(call["token"])
    wsda = WSDA.lower()
    pool = POOL.lower()
    buyers = [t["from"] for t in transfers if t["token_address"] == wsda and t["to"] == pool and t["value"] > 0]
    buyer = buyers[0] if buyers else ""
    token_out = next((t for t in transfers if t["token_address"] == target and t["from"] == pool), None)
    wsda_in = next((t for t in transfers if t["token_address"] == wsda and t["to"] == pool), None)
    return {
        "tx": h,
        "token": target,
        "arg1": call["arg1"],
        "arg2": call["arg2"],
        "buyer": buyer,
        "sda_in": float(wsda_in["value"]) if wsda_in else 0.0,
        "token_out": float(token_out["value"]) if token_out else 0.0,
        "token_raw_output": token_out["raw_value"] if token_out else None,
        "token_decimals": token_out["decimals"] if token_out else None,
        "arg2_matches_raw_output": bool(token_out and call["arg2"] == token_out["raw_value"]),
        "transfers": transfers,
    }


def pad_hex_bytes(data):
    return data[2:] if isinstance(data, str) and data.startswith("0x") else data


def decode_words(raw):
    body = pad_hex_bytes(raw or "")
    words = []
    for i in range(0, len(body), 64):
        w = body[i:i + 64]
        if len(w) == 64:
            words.append("0x" + w)
    return words


def word_as_uint(word):
    try:
        return int(word, 16)
    except Exception:
        return None


def word_as_address(word):
    if not isinstance(word, str) or len(word) < 42:
        return ""
    return "0x" + word[-40:].lower()


def extract_push4(bytecode):
    if not isinstance(bytecode, str) or not bytecode.startswith("0x"):
        return []
    raw = bytecode[2:]
    out, i = [], 0
    while i + 2 <= len(raw):
        op = int(raw[i:i + 2], 16)
        i += 2
        if op == 0x63 and i + 8 <= len(raw):
            s = "0x" + raw[i:i + 8].lower()
            if s not in out:
                out.append(s)
            i += 8
        elif 0x60 <= op <= 0x7f:
            i += 2 * (op - 0x5f)
    return out


def block_number_of(obj):
    if not isinstance(obj, dict):
        return 0
    for key in ("block", "block_number", "blockNumber"):
        v = obj.get(key)
        if isinstance(v, dict):
            for sub in ("height", "number", "value"):
                v2 = v.get(sub)
                if isinstance(v2, (int, str)):
                    v = v2
                    break
        try:
            if isinstance(v, str):
                return int(v, 0) if v.startswith("0x") else int(v)
            if isinstance(v, int):
                return v
        except Exception:
            pass
    return 0

def replace_buy_words(raw_input, recipient, amount_in_wei, amount_out_min=0, deadline=None):
    words = decode_words(raw_input[10:] if isinstance(raw_input, str) and raw_input.startswith("0x") else raw_input)
    if len(words) < 8:
        return raw_input
    vals = [int(w, 16) for w in words]
    vals[3] = int(recipient, 16)
    vals[4] = int(deadline if deadline is not None else vals[4])
    vals[5] = int(amount_in_wei)
    vals[6] = int(amount_out_min)
    return BUY_SELECTOR + "".join(pad_word(v) for v in vals)

def token_output_from_transfers(transfers, recipient, token_address):
    recipient = addr(recipient)
    token_address = addr(token_address)
    for t in transfers:
        if t.get("token_address") == token_address and t.get("to") == recipient and t.get("raw_value", 0) > 0:
            return t.get("raw_value")
    return None

def find_funded_address(addresses, minimum_wei):
    unique = []
    seen = set()
    for a in addresses:
        a = addr(a)
        if a and a not in seen:
            seen.add(a); unique.append(a)
    def check(a):
        bal, err = eth_get_balance(a, "latest")
        return a, bal or 0, err
    with ThreadPoolExecutor(max_workers=10) as ex:
        results = list(ex.map(check, unique[:30]))
    results.sort(key=lambda x: x[1], reverse=True)
    for a, bal, err in results:
        if bal >= minimum_wei:
            return a, bal
    return "", 0

def fetch_router_forensics():
    txs = list_router_txs()
    methods = {}
    interesting = []

    def one(tx):
        h = tx_hash_of(tx)
        if not h:
            return None
        # Prefer fields already present in the list response; fetch detail only
        # when calldata/value/block are missing. This avoids dozens of slow calls.
        raw = raw_input_of(tx)
        detail = tx if raw else transaction_detail(h)
        if not detail:
            return None
        raw = raw_input_of(detail)
        sel = selector(raw)
        value_wei = numeric_wei(detail.get("value"))
        block = block_number_of(detail)
        item = {
            "tx": h,
            "method": sel,
            "from": addr(detail.get("from")),
            "to": addr(detail.get("to")),
            "block": block,
            "value_wei": value_wei,
            "value_sda": float(Decimal(value_wei) / Decimal(10 ** 18)),
            "raw_input": raw,
            "words": decode_words(raw[10:]) if raw else [],
        }
        if sel in {BUY_SELECTOR, SELL_SELECTOR}:
            # Token transfers are only needed for the small interesting set.
            item["transfers"] = transfers_for_tx(h)
        else:
            item["transfers"] = []
        return item

    with ThreadPoolExecutor(max_workers=12) as ex:
        futures = [ex.submit(one, tx) for tx in txs]
        for fut in as_completed(futures):
            try:
                item = fut.result()
                if not item:
                    continue
                sel = item["method"]
                methods[sel] = methods.get(sel, 0) + 1
                if sel in {BUY_SELECTOR, SELL_SELECTOR}:
                    words = item["words"]
                    item["word_uints"] = [word_as_uint(w) for w in words]
                    item["word_addresses"] = [word_as_address(w) for w in words]
                    interesting.append(item)
            except Exception:
                pass
    return txs, methods, interesting


# Known selectors used by common V3-style pools. Read-only only.
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

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
SWAP_EVENT_TOPIC = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
Q96 = 1 << 96


def probe_selector(sel, name, target=POOL):
    r = eth_call(sel, to_address=target, from_address="0x0000000000000000000000000000000000000001")
    return {"selector": sel, "name": name, "target": target, "ok": bool(r.get("ok")), "result": r.get("result"), "error": r.get("error")}


def decode_static_words(result):
    return decode_words(result)


def signed_int256_word(word):
    x = int(word, 16)
    return x - (1 << 256) if x >= (1 << 255) else x


def fetch_receipt(tx_hash):
    try:
        r = rpc_json("eth_getTransactionReceipt", [tx_hash])
        return r.get("result") if isinstance(r, dict) else None
    except Exception:
        return None


def decode_swap_event_log(log):
    topics = log.get("topics") or []
    if not topics or topics[0].lower() != SWAP_EVENT_TOPIC:
        return None
    words = decode_static_words(log.get("data") or "")
    if len(words) != 5:
        return None
    return {
        "sender": word_as_address(topics[1]) if len(topics) > 1 else "",
        "recipient": word_as_address(topics[2]) if len(topics) > 2 else "",
        "amount0": signed_int256_word(words[0]),
        "amount1": signed_int256_word(words[1]),
        "sqrt_price_x96": int(words[2], 16),
        "liquidity": int(words[3], 16),
        "tick": signed_int256_word(words[4]),
    }


def event_analysis(tx_hash):
    receipt = fetch_receipt(tx_hash)
    if not receipt:
        return None
    swaps = []
    for log in receipt.get("logs") or []:
        item = decode_swap_event_log(log)
        if item:
            item["address"] = addr(log.get("address"))
            swaps.append(item)
    return {
        "tx": tx_hash,
        "block": int(receipt.get("blockNumber", "0x0"), 16) if receipt.get("blockNumber") else None,
        "status": receipt.get("status"),
        "log_count": len(receipt.get("logs") or []),
        "swap_events": swaps,
    }


def quote_zero_for_one(amount0_in_raw, sqrt_price_x96, liquidity):
    if amount0_in_raw <= 0 or sqrt_price_x96 <= 0 or liquidity <= 0:
        return None
    sp = Decimal(sqrt_price_x96)
    L = Decimal(liquidity)
    dx = Decimal(amount0_in_raw)
    new_sp = (L * sp * Q96) / (L * Q96 + dx * sp)
    dy = L * (sp - new_sp) / Q96
    return max(0, int(dy))


def quote_one_for_zero(amount1_in_raw, sqrt_price_x96, liquidity):
    if amount1_in_raw <= 0 or sqrt_price_x96 <= 0 or liquidity <= 0:
        return None
    sp = Decimal(sqrt_price_x96)
    L = Decimal(liquidity)
    dy = Decimal(amount1_in_raw)
    new_sp = sp + dy * Q96 / L
    dx = L * (new_sp - sp) * Q96 / (sp * new_sp)
    return max(0, int(dx))


def validate_historical_math(item, swap):
    a0, a1 = swap["amount0"], swap["amount1"]
    sp_post = Decimal(swap["sqrt_price_x96"])
    L = Decimal(swap["liquidity"])
    if L <= 0 or sp_post <= 0:
        return None
    try:
        if a0 < 0 and a1 > 0:
            dx = Decimal(-a0)
            inv_pre = Decimal(1) / sp_post - dx / (L * Q96)
            if inv_pre <= 0:
                return None
            sp_pre = Decimal(1) / inv_pre
            predicted = L * (sp_pre - sp_post) / Q96
            actual = Decimal(a1)
            direction = "token0_in_token1_out"
        elif a1 < 0 and a0 > 0:
            dy = Decimal(-a1)
            sp_pre = sp_post - dy * Q96 / L
            if sp_pre <= 0:
                return None
            predicted = L * (sp_post - sp_pre) * Q96 / (sp_pre * sp_post)
            actual = Decimal(a0)
            direction = "token1_in_token0_out"
        else:
            return None
        err = (predicted - actual) / actual if actual else Decimal(0)
        return {
            "direction": direction,
            "pre_sqrt_price_x96": int(sp_pre),
            "predicted_other_amount_raw": int(predicted),
            "actual_other_amount_raw": int(actual),
            "relative_error": float(err),
            "relative_error_pct": float(err * 100),
        }
    except Exception:
        return None


def main():
    print("💧 LIQUIDITY V24 — FAST/CONCURRENT ROUTER FORENSICS + HISTORICAL BUY CALL VALIDATION")
    print(f"Pool: {POOL}")
    print(f"Router/helper: {ROUTER}")
    print(f"Target trade: {TRADE_SIZE_SDA:.0f} SDA")
    print("Safety: READ-ONLY / no broadcast")

    discovery = load_json(DISCOVERY_FILE, {})

    # --- Pool transaction discovery (carry forward V19/V20 evidence) ---
    try:
        pool_txs = list_pool_txs()
    except Exception as e:
        print(f"Pool discovery failed: {e}")
        pool_txs = []

    selector_counts = {}
    real = []
    for tx in pool_txs:
        raw = raw_input_of(tx)
        sel = selector(raw)
        selector_counts[sel] = selector_counts.get(sel, 0) + 1
        if sel == SWAP_SELECTOR:
            real.append(tx)

    print(f"Pool calls discovered: {len(pool_txs)}")
    print("Selector counts:")
    for sel, count in sorted(selector_counts.items(), key=lambda x: -x[1]):
        print(f"  {sel}: {count}")
    print(f"Real {SWAP_SELECTOR} transactions: {len(real)}")

    usable = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = [ex.submit(analyze, tx) for tx in real[:SAMPLE_COUNT]]
        for fut in as_completed(futures):
            try:
                x = fut.result()
                if x:
                    usable.append(x)
            except Exception as e:
                print(f"Sample error: {e}")
    usable.sort(key=lambda x: x["tx"])
    print(f"Analyzing {len(usable)} samples")
    for item in usable:
        print()
        print(f"TX {item['tx']}")
        print(f"  token={item['token']}")
        print(f"  arg1={item['arg1']}")
        print(f"  arg2={item['arg2']}")
        print(f"  buyer={item['buyer']}")
        print(f"  WSDA user→pool={item['sda_in']}")
        print(f"  token pool→user={item['token_out']}")
        print(f"  token raw output={item['token_raw_output']}")
        print(f"  token decimals={item['token_decimals']}")
        print(f"  arg2 == raw token output: {item['arg2_matches_raw_output']}")
        if item['sda_in']:
            print(f"  tokens/SDA={item['token_out'] / item['sda_in']}")
        for t in item["transfers"]:
            print(f"    {t['symbol'] or t['token_address']} {t['value']} {t['from'][:10]}→{t['to'][:10]}")

    discovery["v24_pool_samples"] = usable

    # --- Router bytecode / selector inventory ---
    print()
    print("🧩 V24 ROUTER / HELPER FORENSICS (CONCURRENT)")
    code, code_err = eth_get_code(ROUTER)
    if code:
        router_selectors = extract_push4(code)
        print(f"  router code bytes={max(0, (len(code) - 2) // 2)}")
        print(f"  router PUSH4 selectors={len(router_selectors)}")
        print("  " + ", ".join(router_selectors))
    else:
        router_selectors = []
        print(f"  router code unavailable: {code_err}")

    try:
        router_txs, router_methods, router_interesting = fetch_router_forensics()
    except Exception as e:
        router_txs, router_methods, router_interesting = [], {}, []
        print(f"  router transaction discovery failed: {e}")

    print(f"  router transactions discovered: {len(router_txs)}")
    print("  router method/selector counts:")
    for sel, count in sorted(router_methods.items(), key=lambda x: -x[1]):
        print(f"    {sel}: {count}")

    discovery["v24_router_selectors"] = router_selectors
    discovery["v24_router_method_counts"] = router_methods
    discovery["v24_router_transactions"] = router_interesting[:ROUTER_SAMPLE_COUNT]

    # --- Historical buy calls: decode calldata and simulate exact historical tx via eth_call ---
    buys = [x for x in router_interesting if x["method"] == BUY_SELECTOR]
    print()
    print(f"🔎 Historical {BUY_SELECTOR} BUY calls: {len(buys)}")
    historical_call_results = []
    for x in buys[:ROUTER_SAMPLE_COUNT]:
        print()
        print(f"BUY TX {x['tx']}")
        print(f"  block={x['block']} from={x['from']}")
        print(f"  native value={x['value_sda']} SDA")
        print(f"  calldata words={len(x['words'])}")
        for i, w in enumerate(x["words"]):
            print(f"    word[{i}] uint={x['word_uints'][i]} address={x['word_addresses'][i]}")

        # Re-run the exact historical call one block before execution. This is
        # an eth_call simulation only; it cannot broadcast or alter chain state.
        block_tag = max(0, x["block"] - 1)
        sim = eth_call(
            x["raw_input"],
            to_address=ROUTER,
            from_address=x["from"] or None,
            value_wei=x["value_wei"],
            block_tag=block_tag,
        )
        result_decoded = decode_static_words(sim.get("result")) if sim.get("ok") else []
        print(f"  historical eth_call: {'OK' if sim.get('ok') else 'REVERT'}")
        if sim.get("ok"):
            print(f"  return bytes={len(pad_hex_bytes(sim.get('result') or '')) // 2}")
            print(f"  return words={len(result_decoded)}")
            for i, w in enumerate(result_decoded[:8]):
                print(f"    return[{i}] uint={word_as_uint(w)}")
        else:
            print(f"  error={sim.get('error')}")

        token_outputs = [
            t for t in x["transfers"]
            if t["to"] == x["word_addresses"][3] and t["token_address"] != WSDA.lower() and t["raw_value"] > 0
        ]
        observed_output = token_outputs[0]["raw_value"] if token_outputs else None
        historical_call_results.append({
            "tx": x["tx"],
            "block": x["block"],
            "from": x["from"],
            "value_sda": x["value_sda"],
            "raw_input": x["raw_input"],
            "words": x["words"],
            "word_uints": x["word_uints"],
            "word_addresses": x["word_addresses"],
            "eth_call_ok": bool(sim.get("ok")),
            "eth_call_result": sim.get("result"),
            "eth_call_error": sim.get("error"),
            "eth_call_return_words": result_decoded,
            "observed_token_output_raw": observed_output,
        })

    discovery["v24_historical_buy_eth_calls"] = historical_call_results

    # --- 50 SDA probe with a funded simulation sender.
    # We preserve token/fee, but set recipient=simulation sender, amountIn=50 SDA,
    # amountOutMinimum=0 and a fresh deadline. This remains eth_call only.
    print()
    print("🧪 V24 50 SDA QUOTE PROBE — FUNDED SENDER + FRESH DEADLINE")
    funded, funded_bal = find_funded_address([x.get("from") for x in router_interesting], int(TRADE_SIZE_SDA * 10**18))
    print(f"  funded simulation sender={funded or 'NONE'}")
    if funded:
        print(f"  funded balance={float(Decimal(funded_bal)/Decimal(10**18)):.6f} SDA")
    quote_probe = None
    if buys and funded:
        base = buys[0]
        fresh_deadline = int(datetime.now(timezone.utc).timestamp()) + 600
        modified = replace_buy_words(
            base["raw_input"],
            funded,
            int(TRADE_SIZE_SDA * 10**18),
            0,
            fresh_deadline,
        )
        print(f"  base tx={base['tx']}")
        print(f"  token={base['word_addresses'][1]}")
        print(f"  fee/arg2={base['word_uints'][2]}")
        print(f"  recipient={funded}")
        print(f"  amountIn=50 SDA")
        print(f"  amountOutMinimum=0")
        print(f"  deadline={fresh_deadline}")
        sim50 = eth_call(
            modified,
            to_address=ROUTER,
            from_address=funded,
            value_wei=int(TRADE_SIZE_SDA * 10**18),
            block_tag="latest",
        )
        print(f"  50 SDA eth_call: {'OK' if sim50.get('ok') else 'REVERT'}")
        ret_words = decode_static_words(sim50.get("result")) if sim50.get("ok") else []
        if sim50.get("ok"):
            print(f"  return words={len(ret_words)}")
            for i, w in enumerate(ret_words[:8]):
                print(f"    return[{i}] uint={word_as_uint(w)}")
        else:
            print(f"  error={sim50.get('error')}")
        quote_probe = {
            "base_tx": base["tx"],
            "from": funded,
            "funded_balance_wei": funded_bal,
            "token": base["word_addresses"][1],
            "fee": base["word_uints"][2],
            "recipient": funded,
            "amount_in_sda": TRADE_SIZE_SDA,
            "amount_out_minimum": 0,
            "deadline": fresh_deadline,
            "raw_input": modified,
            "ok": bool(sim50.get("ok")),
            "result": sim50.get("result"),
            "error": sim50.get("error"),
            "return_words": ret_words,
        }
    elif not funded:
        print("  no router sender with >=50 SDA current balance; quote probe skipped")
    else:
        print("  no historical BUY available; quote probe skipped")
    discovery["v24_50_sda_quote_probe"] = quote_probe

    # --- Carry forward pool read-only probes ---
    print()
    print("🔬 V24 READ-ONLY POOL STATE PROBES")
    probes = []
    for sel, name in KNOWN_READ_SELECTORS.items():
        p = probe_selector(sel, name, POOL)
        probes.append(p)
        if p["ok"]:
            words = decode_static_words(p["result"])
            print(f"  {sel} {name}: OK words={len(words)} result={p['result']}")
        else:
            print(f"  {sel} {name}: REVERT/UNSUPPORTED")
    discovery["v24_pool_read_only_probes"] = probes

    # --- Historical V3 event evidence / math ---
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

    discovery["v24_swap_events"] = event_evidence

    print()
    print("🧮 V24 HISTORICAL V3 MATH VALIDATION")
    math_checks = []
    for ev_item in event_evidence:
        for sw in ev_item.get("swap_events", []):
            item = next((x for x in usable if x["tx"] == ev_item["tx"]), None)
            if not item:
                continue
            chk = validate_historical_math(item, sw)
            if chk:
                chk["tx"] = ev_item["tx"]
                chk["observed_sda_in"] = item.get("sda_in")
                chk["observed_token_out"] = item.get("token_out")
                math_checks.append(chk)
                print(f"  {ev_item['tx']}: {chk['direction']} error={chk['relative_error_pct']:.6f}%")
    discovery["v24_math_checks"] = math_checks
    discovery["v24_math_max_abs_error_pct"] = max((abs(x["relative_error_pct"]) for x in math_checks), default=None)

    save_json(DISCOVERY_FILE, discovery)

    liquidity = load_json(LIQUIDITY_FILE, {})
    liquidity.update({
        "updated_at": now(),
        "version": 23,
        "pool": POOL,
        "router": ROUTER,
        "trade_size_sda": TRADE_SIZE_SDA,
        "status": "UNKNOWN",
        "reason": "V24 reverse-engineers the live Sidra helper/router path using historical 0x414bf389 BUY calls and read-only eth_call simulations. No live liquidity or quote is promoted until return semantics are proven.",
        "observed_buy_samples": len(usable),
        "arg1_values": sorted(set(a["arg1"] for a in usable if a.get("arg1") is not None)),
        "arg2_exact_raw_output_matches": sum(1 for a in usable if a["arg2_matches_raw_output"]),
        "v24_router_buy_calls": len(buys),
        "v24_historical_eth_call_successes": sum(1 for x in historical_call_results if x["eth_call_ok"]),
        "v24_50_sda_probe_ok": bool(discovery.get("v24_50_sda_probe", {}).get("ok")),
        "v24_quote_status": "UNVERIFIED",
        "v24_note": "Historical router BUY calls are simulated with eth_call only. A successful return is not promoted as a quote until it is matched against observed token output and calldata semantics.",
    })
    save_json(LIQUIDITY_FILE, liquidity)

    print()
    print("Saved " + DISCOVERY_FILE)
    print("Saved " + LIQUIDITY_FILE)
    print("Overall liquidity status: UNKNOWN")
    print("Next: use the decoded 8-word BUY ABI and the funded 50 SDA eth_call probe to validate the exact quote return; no live transaction or quote promotion.")


if __name__ == "__main__":
    main()
