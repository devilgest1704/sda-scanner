#!/usr/bin/env python3
"""Telegram PNG heartbeat for forward paper strategy learning."""
import json
import os
from pathlib import Path
from urllib import request

REPORT = Path("strategy_lab/autonomous_agent_report.json")
PNG = Path("strategy_lab/autonomous_agent_status.png")


def plot(report, destination=PNG):
    from PIL import Image, ImageDraw, ImageFont
    destination.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGB", (1100, 720), "#101827")
    d = ImageDraw.Draw(im)
    try:
        regular = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        heavy = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        font = lambda n, bold=False: ImageFont.truetype(heavy if bold else regular, n)
    except OSError:
        font = lambda n, bold=False: ImageFont.load_default()
    d.text((45, 27), "SDA AUTONOMOUS PAPER AGENT", fill="#F5F7FB", font=font(35, True))
    status = report.get("status", "UNKNOWN")
    d.text((48, 91), f"STATUS: {status}", fill="#F3BD72" if status.startswith("WAIT") or status.startswith("COLLECT") else "#77D6AE", font=font(23, True))
    d.text((48, 135), f"Generation: {report.get('champion',{}).get('generation',0)}   Mode: PAPER ONLY", fill="#B1C5DE", font=font(18))
    d.text((48, 177), "FORWARD SHADOW - realized virtual trades after modeled fees/slippage", fill="#B1C5DE", font=font(17))
    rows = report.get("strategies", {})
    baseline = [(name, ((value.get("all") or {}).get("net_sda") or 0),
                 ((value.get("all") or {}).get("closed") or 0))
                for name,value in rows.items()]
    max_abs = max([abs(x[1]) for x in baseline] + [10])
    mid = 580
    scale = 400 / max_abs
    d.line((mid, 230, mid, 594), fill="#7C91A9", width=2)
    for i,(name,pnl,count) in enumerate(baseline[:8]):
        y = 232 + i*46
        d.text((50, y), f"{name}  ({count} exits)", fill="#D5DFE9", font=font(16))
        x2 = mid + pnl*scale
        color = "#64CF98" if pnl > 0 else "#EF7878"
        if pnl:
            d.rectangle((min(mid,x2), y+4, max(mid,x2), y+24), fill=color)
        d.text((1030, y), f"{pnl:+.1f} SDA", fill=color, font=font(16, True), anchor="ra")
    d.text((48, 613), "CHAMPION CONFIG CHANGES ONLY AFTER TRAIN + HOLDOUT + RISK GATES", fill="#B1C5DE", font=font(17))
    reason = str(report.get("reason") or "")
    d.text((48, 651), reason[:96], fill="#A5B3C7", font=font(16))
    d.text((48, 690), "NOT FINANCIAL PROFIT. Forward quotes are approximations, not executable fills.", fill="#A5B3C7", font=font(14))
    im.save(destination, "PNG")
    return destination


def telegram_send(report, png):
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TG_BOT_TOKEN") or os.environ.get("TELEGRAM_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID") or os.environ.get("TG_CHAT_ID") or os.environ.get("CHAT_ID")
    if not token or not chat:
        print("Forward agent Telegram skipped: no token/chat configured")
        return False
    import uuid
    boundary = "----ForwardPaper" + uuid.uuid4().hex
    def field(key, val, filename=None, content_type=None):
        head = f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"'
        if filename: head += f'; filename="{filename}"'
        head += "\r\n"
        if content_type: head += f"Content-Type: {content_type}\r\n"
        return (head+"\r\n").encode() + val + b"\r\n"
    caption = (
        "SDA AUTONOMOUS PAPER AGENT\n"
        f"Status: {report.get('status')}\n"
        f"Champion generation: {report.get('champion',{}).get('generation',0)}\n"
        f"Promotion: {report.get('promotion_applied',False)} / rollback: {report.get('rollback_applied',False)}\n"
        "Virtual forward P/L includes modeled friction. Not real executed profit."
    )
    data = (field("chat_id",str(chat).encode()) +
            field("caption",caption.encode()) +
            field("photo",Path(png).read_bytes(),"forward_agent.png","image/png") +
            f"--{boundary}--\r\n".encode())
    req = request.Request("https://api.telegram.org/bot"+token+"/sendPhoto",
                          data=data, headers={"Content-Type":"multipart/form-data; boundary="+boundary}, method="POST")
    with request.urlopen(req,timeout=30) as response:
        accepted = json.load(response)
    if not accepted.get("ok"):
        raise RuntimeError("Telegram refused forward agent PNG")
    print("FORWARD AGENT TELEGRAM: accepted")
    return True


def main():
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    path = plot(report)
    telegram_send(report,path)


if __name__ == "__main__":
    main()
