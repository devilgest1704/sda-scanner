import json, requests

META_FILE = "token_metadata.json"
MARKET_FILE = "market_data.json"
BASE = "https://ledger.sidrachain.com/api/v2"
DEX_ICON_HOSTS = (
    "https://normal-sidra-dx.vercel.app/tokens/{symbol}.png",
    "https://web3.sidradex.pw/tokens/{symbol}.png",
)


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def valid_image_url(url):
    try:
        r = requests.get(
            url,
            timeout=10,
            headers={"User-Agent": "sda-scanner/1.0"},
            stream=True,
        )
        ctype = str(r.headers.get("content-type") or "").lower()
        ok = r.status_code == 200 and ctype.startswith("image/")
        r.close()
        return ok
    except Exception:
        return False


def discover_icon(address, item, data=None):
    data = data if isinstance(data, dict) else {}
    for key in ("icon_url", "logo_url", "icon", "image_url", "logo"):
        value = data.get(key)
        if isinstance(value, str) and value.strip() and valid_image_url(value.strip()):
            return value.strip(), "blockscout"

    symbol = str(item.get("symbol") or data.get("symbol") or "").strip()
    if not symbol:
        return None, None

    for template in DEX_ICON_HOSTS:
        url = template.format(symbol=symbol.upper())
        if valid_image_url(url):
            return url, "sidradex"

    return None, None


def main():
    meta = load(META_FILE, {})
    market = load(MARKET_FILE, {})
    addresses = set(str(x).lower() for x in (market.get("tokens", {}) or {}).keys())
    addresses.update(str(x).lower() for x in meta.keys())
    changed = 0
    discovered = 0

    for address in sorted(addresses):
        item = meta.get(address)
        if not isinstance(item, dict):
            continue

        data = None
        try:
            r = requests.get(f"{BASE}/tokens/{address}", timeout=15)
            if r.status_code == 200:
                candidate = r.json()
                if isinstance(candidate, dict):
                    data = candidate
                    for key in ("name", "symbol", "decimals"):
                        if candidate.get(key) is not None and not item.get(key):
                            item[key] = candidate.get(key)
        except Exception as e:
            print("Icon metadata lookup failed", address, e)

        icon_url, source = discover_icon(address, item, data)
        if icon_url:
            if item.get("icon_url") != icon_url or item.get("icon_source") != source:
                item["icon_url"] = icon_url
                item["icon_source"] = source
                changed += 1
            discovered += 1

    with open(META_FILE, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)

    print(f"Token icons available: {discovered}; metadata changed: {changed}")


if __name__ == "__main__":
    main()
