import json
import os
import time
from pathlib import Path

import requests

MARKET_DATA_FILE = "market_data.json"
METADATA_FILE = "token_metadata.json"

BLOCKSCOUT_BASE = "https://ledger.sidrachain.com/api/v2"
RPC_URL = "https://node.sidrachain.com"

TIMEOUT = 20
SLEEP_BETWEEN_REQUESTS = 0.15

# ERC-20 selectors
NAME_SELECTOR = "0x06fdde03"
SYMBOL_SELECTOR = "0x95d89b41"
DECIMALS_SELECTOR = "0x313ce567"


def load_json(path, default):
    if not Path(path).exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            value = json.load(f)
        return value
    except Exception:
        return default


def save_json(path, value):
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(value, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def normalize_address(address):
    return str(address or "").lower()


def clean_text(value):
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def decode_abi_string(result):
    if not result or not isinstance(result, str):
        return None

    raw = result[2:] if result.startswith("0x") else result
    if len(raw) < 128:
        return None

    try:
        # Standard dynamic ABI string:
        # offset (32 bytes), length (32 bytes), UTF-8 bytes.
        offset = int(raw[:64], 16) * 2
        if offset + 64 > len(raw):
            return None

        length = int(raw[offset:offset + 64], 16)
        start = offset + 64
        end = start + length * 2
        if end > len(raw):
            return None

        return bytes.fromhex(raw[start:end]).decode("utf-8", errors="replace").strip("\x00 ")
    except Exception:
        return None


def decode_abi_uint(result):
    if not result or not isinstance(result, str):
        return None
    try:
        raw = result[2:] if result.startswith("0x") else result
        return int(raw[-64:], 16)
    except Exception:
        return None


def blockscout_token(address):
    url = f"{BLOCKSCOUT_BASE}/tokens/{address}"
    response = requests.get(url, timeout=TIMEOUT)

    if response.status_code != 200:
        return None

    data = response.json()
    if not isinstance(data, dict):
        return None

    name = clean_text(data.get("name"))
    symbol = clean_text(data.get("symbol"))
    decimals = data.get("decimals")

    if name or symbol:
        return {
            "name": name,
            "symbol": symbol,
            "decimals": decimals,
            "source": "blockscout",
            "address": address,
        }

    return None


def blockscout_address(address):
    url = f"{BLOCKSCOUT_BASE}/addresses/{address}"
    response = requests.get(url, timeout=TIMEOUT)

    if response.status_code != 200:
        return None

    data = response.json()
    if not isinstance(data, dict):
        return None

    token = data.get("token")
    if not isinstance(token, dict):
        return None

    name = clean_text(token.get("name"))
    symbol = clean_text(token.get("symbol"))
    decimals = token.get("decimals")

    if name or symbol:
        return {
            "name": name,
            "symbol": symbol,
            "decimals": decimals,
            "source": "blockscout-address",
            "address": address,
        }

    return None


def rpc_call(address, data):
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [{"to": address, "data": data}, "latest"],
        "id": 1,
    }

    response = requests.post(RPC_URL, json=payload, timeout=TIMEOUT)
    if response.status_code != 200:
        return None

    body = response.json()
    if body.get("error"):
        return None

    return body.get("result")


def rpc_token(address):
    name = decode_abi_string(rpc_call(address, NAME_SELECTOR))
    symbol = decode_abi_string(rpc_call(address, SYMBOL_SELECTOR))
    decimals = decode_abi_uint(rpc_call(address, DECIMALS_SELECTOR))

    if not name and not symbol:
        return None

    return {
        "name": name,
        "symbol": symbol,
        "decimals": decimals,
        "source": "sidra-rpc",
        "address": address,
    }


def resolve(address):
    # Keep the address exactly as supplied for output, while using lowercase
    # keys so checksum/case differences do not create duplicate entries.
    address = str(address)

    for resolver in (blockscout_token, blockscout_address, rpc_token):
        try:
            result = resolver(address)
            if result:
                return result
        except Exception as exc:
            print(f"Metadata lookup failed for {address}: {exc}")

    return {
        "name": None,
        "symbol": None,
        "decimals": None,
        "source": "unresolved",
        "address": address,
    }


def main():
    market_data = load_json(MARKET_DATA_FILE, {})
    tokens = market_data.get("tokens", {}) if isinstance(market_data, dict) else {}

    existing = load_json(METADATA_FILE, {})
    if not isinstance(existing, dict):
        existing = {}

    addresses = sorted(tokens.keys())
    print(f"Token metadata: {len(addresses)} token addresses found")

    for address in addresses:
        key = normalize_address(address)
        current = existing.get(key)

        # Retry unresolved entries on every run. Resolved entries are cached.
        if isinstance(current, dict) and current.get("symbol"):
            continue

        print(f"Resolving {address} ...")
        existing[key] = resolve(address)
        time.sleep(SLEEP_BETWEEN_REQUESTS)

    save_json(METADATA_FILE, existing)

    resolved = sum(
        1 for item in existing.values()
        if isinstance(item, dict) and item.get("symbol")
    )
    print(f"Token metadata resolved: {resolved}/{len(addresses)}")


if __name__ == "__main__":
    main()
