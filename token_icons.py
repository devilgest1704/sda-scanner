import json, os, requests

META_FILE = "token_metadata.json"
MARKET_FILE = "market_data.json"
BASE = "https://ledger.sidrachain.com/api/v2"


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f: return json.load(f)
    except Exception: return default


def main():
    meta = load(META_FILE, {})
    market = load(MARKET_FILE, {})
    addresses = set(str(x).lower() for x in (market.get("tokens", {}) or {}).keys())
    addresses.update(str(x).lower() for x in meta.keys())
    changed = 0
    for address in sorted(addresses):
        item = meta.get(address)
        if not isinstance(item, dict):
            continue
        if item.get("icon_url"):
            continue
        try:
            r = requests.get(f"{BASE}/tokens/{address}", timeout=15)
            if r.status_code != 200:
                continue
            data = r.json()
            if not isinstance(data, dict):
                continue
            icon = data.get("icon_url") or data.get("logo_url") or data.get("icon")
            if isinstance(icon, str) and icon.strip():
                item["icon_url"] = icon.strip()
                item["icon_source"] = "blockscout"
                changed += 1
        except Exception as e:
            print("Icon lookup failed", address, e)
    with open(META_FILE, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    print(f"Token icons discovered: {changed}")


if __name__ == "__main__":
    main()
