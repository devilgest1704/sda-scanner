import io
import json
import os
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps

PORTFOLIO_FILE = "portfolio_data.json"
META_FILE = "token_metadata.json"
OUT_FILE = "portfolio_card.png"
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
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


def font(size, bold=False):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    for p in candidates:
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def load_icon_url(url):
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        r = requests.get(url.strip(), timeout=12, headers={"User-Agent": "sda-scanner/1.0"})
        r.raise_for_status()
        ctype = str(r.headers.get("content-type") or "").lower()
        if not ctype.startswith("image/"):
            return None
        im = Image.open(io.BytesIO(r.content)).convert("RGBA")
        return ImageOps.fit(im, (78, 78), method=Image.Resampling.LANCZOS)
    except Exception as exc:
        print("Icon download failed:", url, exc)
        return None


def icon(symbol, url=None):
    ico = load_icon_url(url)
    if ico:
        return ico

    symbol = str(symbol or "?").strip()
    candidates = []
    for template in DEX_ICON_HOSTS:
        candidates.append(template.format(symbol=symbol.upper()))
    for candidate in candidates:
        ico = load_icon_url(candidate)
        if ico:
            return ico
    return None


def fmt_amount(x):
    try:
        x = float(x)
    except Exception:
        x = 0.0
    if abs(x) >= 1000000:
        return f"{x:,.0f}"
    if abs(x) >= 1000:
        return f"{x:,.2f}"
    if abs(x) >= 1:
        return f"{x:,.4f}".rstrip("0").rstrip(".")
    return f"{x:,.8f}".rstrip("0").rstrip(".")


def fallback_badge(d, symbol, x, y):
    # Never show a confusing '?' placeholder. Use the token symbol as a clear fallback.
    d.ellipse((x, y, x + 78, y + 78), fill=(35, 45, 55), outline=(120, 135, 150), width=2)
    text = str(symbol or "?")[:5].upper()
    f = font(20 if len(text) <= 4 else 16, True)
    box = d.textbbox((0, 0), text, font=f)
    tw = box[2] - box[0]
    th = box[3] - box[1]
    d.text((x + (78 - tw) / 2, y + (78 - th) / 2 - 2), text, font=f, fill=(235, 240, 245))


def main():
    p = load(PORTFOLIO_FILE, {})
    meta = load(META_FILE, {})
    cur = p.get("current", {}) if isinstance(p, dict) else {}
    rows = []
    for token, pf in sorted(cur.items(), key=lambda x: (x[1].get("symbol") or x[0]).lower()):
        m = meta.get(str(token).lower(), {}) if isinstance(meta, dict) else {}
        symbol = pf.get("symbol") or m.get("symbol") or str(token)[:10] + "..."
        name = m.get("name") or symbol
        value = pf.get("value_sda")
        pnl = pf.get("unrealized_pnl_sda")
        pct = pf.get("unrealized_pnl_pct")
        rows.append((token, symbol, name, pf.get("amount"), value, pnl, pct, m.get("icon_url")))

    W = 1200
    header_h = 150
    row_h = 135
    footer_h = 180
    H = header_h + max(1, len(rows)) * row_h + footer_h
    im = Image.new("RGB", (W, H), (245, 247, 250))
    d = ImageDraw.Draw(im)
    title = font(42, True)
    sub = font(24)
    namef = font(29, True)
    small = font(22)
    pnlf = font(25, True)

    d.rectangle((0, 0, W, header_h), fill=(24, 31, 38))
    d.text((45, 28), "REAL PORTFOLIO", font=title, fill=(255, 255, 255))
    d.text((48, 92), "SDA accumulation • current wallet positions", font=sub, fill=(190, 205, 215))

    y = header_h
    for token, symbol, name, amount, value, pnl, pct, icon_url in rows:
        d.rectangle((25, y + 8, W - 25, y + row_h - 8), fill=(255, 255, 255), outline=(215, 220, 225), width=2)
        ico = icon(symbol, icon_url)
        if ico:
            im.paste(ico, (48, y + 28), ico)
        else:
            fallback_badge(d, symbol, 48, y + 28)

        d.text((150, y + 22), f"{symbol} — {name}", font=namef, fill=(25, 30, 35))
        d.text((150, y + 67), f"{fmt_amount(amount)} {symbol}", font=small, fill=(75, 82, 90))
        valtxt = f"{float(value):.2f} SDA" if value is not None else "UNKNOWN"
        d.text((540, y + 67), f"Value: {valtxt}", font=small, fill=(75, 82, 90))

        if pnl is None or pct is None:
            pnl_txt = "P/L UNKNOWN"
            fill = (115, 120, 125)
        else:
            pv = float(pnl)
            pp = float(pct)
            pnl_txt = f"P/L {pv:+.2f} SDA ({pp:+.2f}%)"
            fill = (25, 155, 75) if pv > 0 else ((205, 45, 45) if pv < 0 else (100, 105, 110))
        d.text((890, y + 50), pnl_txt, font=pnlf, fill=fill)
        y += row_h

    d.line((45, y + 10, W - 45, y + 10), fill=(130, 135, 140), width=2)
    op = p.get("open_unrealized_pnl_sda")
    rp = p.get("realized_pnl_sda")
    tp = p.get("known_total_pnl_sda")
    summary = [
        ("Current open P/L", op),
        ("Historical matched P/L", rp),
        ("Known total P/L", tp),
    ]
    sy = y + 35
    for label, val in summary:
        v = float(val or 0)
        fill = (25, 155, 75) if v > 0 else ((205, 45, 45) if v < 0 else (100, 105, 110))
        d.text((55, sy), label + ":", font=small, fill=(60, 65, 70))
        d.text((430, sy), f"{v:+.2f} SDA", font=pnlf, fill=fill)
        sy += 40
    d.text((760, y + 45), "READ-ONLY", font=small, fill=(80, 85, 90))
    d.text((760, y + 85), "Token logos: SidraDEX / Blockscout", font=small, fill=(80, 85, 90))

    im.save(OUT_FILE, quality=95)
    print(f"Created {OUT_FILE}: {W}x{H}")

    if TELEGRAM_TOKEN and CHAT_ID and Path(OUT_FILE).exists():
        try:
            with open(OUT_FILE, "rb") as f:
                r = requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
                    data={"chat_id": CHAT_ID, "caption": "🖼️ REAL PORTFOLIO — token icons + P/L"},
                    files={"photo": (OUT_FILE, f, "image/png")},
                    timeout=30,
                )
            print("Telegram portfolio card:", r.status_code)
        except Exception as exc:
            print("Telegram portfolio card error:", exc)


if __name__ == "__main__":
    main()
