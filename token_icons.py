import json, requests

META_FILE = "token_metadata.json"
MARKET_FILE = "market_data.json"
BASE = "https://ledger.sidrachain.com/api/v2"

# The old SidraDEX /tokens/*.png paths return 404 from GitHub Actions.
# Prefer live project-owned avatars/favicons instead of dead CDN paths.
STATIC_ICONS = {
    "AIR": {"url": "https://unavatar.io/twitter/GLNsGlobal", "source": "GLNs Global official X avatar"},
    "GLNS": {"url": "https://unavatar.io/twitter/GLNsGlobal", "source": "GLNs Global official X avatar"},
    "REGS": {"url": "https://unavatar.io/twitter/GLNsGlobal", "source": "GLNs Global official X / GLNs Global branding"},
    "FBAY": {"url": "https://falconbayglobal.com/favicon.ico", "source": "FalconBay official website favicon"},
    "FREET": {"url": "https://freelancium.pro/favicon.ico", "source": "Freelancium official website favicon"},
    "GACP": {"url": "https://gagrichain.com/favicon.ico", "source": "GLNAgriChain official website favicon"},
}


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def valid_image_url(url):
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "sda-scanner/1.0"}, stream=True, allow_redirects=True)
        ctype = str(r.headers.get("content-type") or "").lower()
        ok = r.status_code == 200 and ctype.startswith("image/")
        r.close()
        return ok
    except Exception:
        return False


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
        symbol = str(item.get("symbol") or "").strip().upper()
        static = STATIC_ICONS.get(symbol)
        if static and valid_image_url(static["url"]):
            item["icon_url"] = static["url"]
            item["icon_source"] = static["source"]
            item.pop("icon_crop", None)
            discovered += 1
            changed += 1
            continue

        existing = item.get("icon_url")
        if isinstance(existing, str) and existing.strip() and valid_image_url(existing.strip()):
            discovered += 1
            continue

        try:
            r = requests.get(f"{BASE}/tokens/{address}", timeout=12)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict):
                    for key in ("name", "symbol", "decimals"):
                        if data.get(key) is not None and not item.get(key):
                            item[key] = data.get(key)
                    for key in ("icon_url", "logo_url", "icon", "image_url", "logo"):
                        value = data.get(key)
                        if isinstance(value, str) and value.strip() and valid_image_url(value.strip()):
                            item["icon_url"] = value.strip()
                            item["icon_source"] = "blockscout"
                            discovered += 1
                            changed += 1
                            break
        except Exception as e:
            print("Icon metadata lookup failed", address, e)

    with open(META_FILE, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    print(f"Token icons available: {discovered}; metadata changed: {changed}")


if __name__ == "__main__":
    main()
