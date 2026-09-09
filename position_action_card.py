import json
import os
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont

import engine

PORTFOLIO_FILE = "portfolio_data.json"
MARKET_FILE = "market_data.json"
WHALE_FILE = "whale_data.json"
STATE_FILE = "decision_state_v15.json"
OUT_FILE = "position_action_card.png"
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")


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


def action_style(action):
    if action == "SELL / EXIT":
        return "🔴", (205, 45, 45), (255, 242, 242)
    if action == "PARTIAL SELL":
        return "🟠", (220, 135, 25), (255, 248, 235)
    if action == "HOLD / TRAIL":
        return "🟢", (25, 155, 75), (240, 250, 244)
    return "🟡", (105, 115, 125), (248, 249, 250)


def strength(score):
    try:
        s = float(score)
    except Exception:
        s = 0
    if s < 20:
        return "VERY WEAK"
    if s < 40:
        return "WEAK"
    if s < 60:
        return "NEUTRAL"
    if s < 75:
        return "GOOD"
    return "STRONG"


def pnl_text(value):
    if value is None:
        return "UNKNOWN"
    try:
        return f"{float(value):+.2f} SDA"
    except Exception:
        return "UNKNOWN"


def build_recommendation(token, pf, analysis, ws, state):
    if not isinstance(analysis, dict) or not analysis:
        return "HOLD / WATCH", 0.0

    s = engine.score(token, analysis, ws)
    score = float(s.get("confidence", 0) or 0)
    m1 = float(s.get("m1h", 0) or 0)
    flow = float(s.get("net_1h", 0) or 0)
    pnl_raw = pf.get("unrealized_pnl_pct")
    pnl = float(pnl_raw) if pnl_raw is not None else 0.0

    old = state.get(token, {}) if isinstance(state.get(token), dict) else {}
    neg = int(float(old.get("negative_count", 0) or 0))
    weak = int(float(old.get("profit_weakening_count", 0) or 0))
    loss_weak = int(float(old.get("loss_weakening_count", 0) or 0))

    if pf.get("cost_sda") is None or pnl_raw is None:
        return "HOLD / NO COST BASIS", score
    if weak >= 2:
        return "PARTIAL SELL", score
    if neg >= 3:
        return "SELL / EXIT", score
    if loss_weak >= 3:
        return "SELL / EXIT", score
    if score >= 70 and m1 > 0 and flow > 0:
        return "HOLD / TRAIL", score
    return "HOLD / WATCH", score


def main():
    portfolio = load(PORTFOLIO_FILE, {})
    market = load(MARKET_FILE, {})
    whale = load(WHALE_FILE, {})
    state = load(STATE_FILE, {})
    current = portfolio.get("current", {}) if isinstance(portfolio, dict) else {}
    tokens = market.get("tokens", {}) if isinstance(market, dict) else {}

    rows = []
    for token, pf in current.items():
        if not isinstance(pf, dict):
            continue
        td = tokens.get(token, {}) if isinstance(tokens, dict) else {}
        analysis = td.get("analysis", td) if isinstance(td, dict) else {}
        action, score = build_recommendation(token, pf, analysis, whale, state)
        rows.append({
            "symbol": pf.get("symbol") or str(token)[:10] + "...",
            "action": action,
            "score": score,
            "pnl": pf.get("unrealized_pnl_sda"),
        })

    rows.sort(key=lambda r: ({"SELL / EXIT": 0, "PARTIAL SELL": 1, "HOLD / TRAIL": 2, "HOLD / WATCH": 3, "HOLD / NO COST BASIS": 4}.get(r["action"], 5), -float(r["score"] or 0)))

    W = 1400
    header_h = 145
    row_h = 82
    footer_h = 125
    H = header_h + max(1, len(rows)) * row_h + footer_h

    im = Image.new("RGB", (W, H), (245, 247, 250))
    d = ImageDraw.Draw(im)
    title = font(40, True)
    sub = font(22)
    rowf = font(23, True)
    small = font(20)

    d.rectangle((0, 0, W, header_h), fill=(24, 31, 38))
    d.text((45, 25), "POSITION ACTION", font=title, fill=(255, 255, 255))
    d.text((48, 88), "Autonomous V18 paper-trading decision monitor", font=sub, fill=(190, 205, 215))

    y = header_h
    for row in rows:
        marker, text_fill, bg = action_style(row["action"])
        d.rounded_rectangle((25, y + 7, W - 25, y + row_h - 7), radius=12, fill=bg, outline=(215, 220, 225), width=2)
        d.text((48, y + 25), marker, font=rowf, fill=text_fill)
        d.text((100, y + 24), row["symbol"], font=rowf, fill=(25, 30, 35))
        d.text((270, y + 24), row["action"], font=rowf, fill=text_fill)
        d.text((720, y + 24), f"P/L {pnl_text(row['pnl'])}", font=rowf, fill=text_fill)
        score = float(row["score"] or 0)
        d.text((1010, y + 24), f"score {score:.0f}/100", font=small, fill=(65, 70, 75))
        d.text((1170, y + 24), strength(score), font=small, fill=text_fill)
        y += row_h

    d.line((45, y + 8, W - 45, y + 8), fill=(130, 135, 140), width=2)
    op = portfolio.get("open_unrealized_pnl_sda")
    rp = portfolio.get("realized_pnl_sda")
    tp = portfolio.get("known_total_pnl_sda")
    d.text((55, y + 35), f"Open P/L: {pnl_text(op)}", font=small, fill=(60, 65, 70))
    d.text((430, y + 35), f"Matched P/L: {pnl_text(rp)}", font=small, fill=(60, 65, 70))
    d.text((790, y + 35), f"Known total: {pnl_text(tp)}", font=small, fill=(60, 65, 70))
    d.text((55, y + 75), "Score: 0–19 VERY WEAK • 20–39 WEAK • 40–59 NEUTRAL • 60–74 GOOD • 75–100 STRONG", font=small, fill=(90, 95, 100))

    im.save(OUT_FILE)
    print(f"Created {OUT_FILE}: {W}x{H}")

    if TELEGRAM_TOKEN and CHAT_ID:
        try:
            with open(OUT_FILE, "rb") as f:
                r = requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
                    data={"chat_id": CHAT_ID, "caption": "🧭 POSITION ACTION — V18"},
                    files={"photo": (OUT_FILE, f, "image/png")},
                    timeout=30,
                )
            print("Telegram position action card:", r.status_code)
        except Exception as exc:
            print("Telegram position action card error:", exc)


if __name__ == "__main__":
    main()
