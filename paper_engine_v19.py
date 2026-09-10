"""Stable V19/V21 paper-trading entry point.

Keeps scanner source untouched at runtime. BUY logic remains in main.py, with a
V21 technical-confirmation overlay applied to the shared engine score. Paper
exits use the same centralized V21 policy as Telegram POSITION ACTION.
"""
import engine
import main as scanner_main
from strategy_v21 import patch_engine, technical_sell_confirmed, evaluate_exit

engine.TELEGRAM_TOKEN = ""
engine.SL_PCT = 0.05
engine.BUY_THRESHOLD = 65
patch_engine(engine)
scanner_main.engine.score = engine.score


def auto_exit_v21(p, tokens, ws):
    state = engine._load_auto()
    events = []
    for address in list(p.get("positions", {})):
        td = tokens.get(address, {}) or {}
        analysis = td.get("analysis", td) if isinstance(td, dict) else {}
        current = engine.num(analysis.get("price_in_sda"))
        position = p["positions"].get(address)
        if not position or current <= 0:
            continue

        signal = engine.score(address, analysis, ws)
        score = engine.num(signal.get("confidence"))
        momentum_1h = engine.num(signal.get("m1h"))
        flow_1h = engine.num(signal.get("net_1h"))
        entry = engine.num(position.get("entry_price"))
        roi = (current - entry) / entry * 100 if entry else 0.0
        technical_bearish = technical_sell_confirmed(analysis)
        exit_signal = evaluate_exit(roi, score, momentum_1h, flow_1h, technical_bearish)

        old = state.get(address, {}) if isinstance(state.get(address), dict) else {}
        negative_count = int(engine.num(old.get("neg")))
        weakening_count = int(engine.num(old.get("weak")))
        negative_count = min(5, negative_count + 1) if exit_signal["negative"] else 0
        weakening_count = min(5, weakening_count + 1) if exit_signal["weakening"] else 0
        state[address] = {"neg": negative_count, "weak": weakening_count}

        if exit_signal["emergency"]:
            result = engine.close(p, address, current, "EMERGENCY SELL")
            if result:
                events.append(f"🚨 EMERGENCY SELL {result['label']} | Profit: {result['closed_profit_sda']:+.2f} SDA | Score: {score:.0f}/100 | ROI: {roi:+.2f}% | Mode: PAPER AUTO")
        elif weakening_count >= 2 and not position.get("tp1_hit"):
            result = engine.close(p, address, current, "AUTO PARTIAL SELL", 0.5)
            if result:
                events.append(f"🟠 AUTO PARTIAL SELL {result['label']} | Price: {engine.price(current)} SDA | Profit: {result['closed_profit_sda']:+.2f} SDA | Score: {score:.0f}/100")
        elif negative_count >= 3:
            result = engine.close(p, address, current, "AUTO SELL / EXIT")
            if result:
                events.append(f"🔴 AUTO SELL / EXIT {result['label']} | Price: {engine.price(current)} SDA | Profit: {result['closed_profit_sda']:+.2f} SDA | Score: {score:.0f}/100")

    engine._save_auto(state)
    return events


engine._auto_exit = auto_exit_v21
scanner_main.engine._auto_exit = auto_exit_v21
scanner_main.engine.SL_PCT = 0.05
scanner_main.engine.BUY_THRESHOLD = 65

if __name__ == "__main__":
    engine.main()
