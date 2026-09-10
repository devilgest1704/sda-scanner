"""Stable V19 paper-trading entry point.

Keeps the scanner source untouched at runtime. BUY logic remains in main.py.
Only paper exits are extended with the V19 emergency protection.
"""
import engine
import main as scanner_main

engine.TELEGRAM_TOKEN = ""
engine.SL_PCT = 0.05


def auto_exit_v19(p, tokens, ws):
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

        old = state.get(address, {}) if isinstance(state.get(address), dict) else {}
        negative_count = int(engine.num(old.get("neg")))
        weakening_count = int(engine.num(old.get("weak")))

        # Hard safety brake: do not wait for confirmation when a position is
        # deeply underwater and the market signal is clearly negative.
        emergency = roi <= -15.0 and score < 35 and momentum_1h < 0 and flow_1h < 0

        # Normal V19 AUTO EXIT: still requires three consecutive confirmations.
        negative = score < 30 and momentum_1h < -1.0 and flow_1h < 0 and roi < -3.0
        weakening = roi > 0 and score < 40 and (momentum_1h < 0 or flow_1h < 0)

        negative_count = min(5, negative_count + 1) if negative else 0
        weakening_count = min(5, weakening_count + 1) if weakening else 0
        state[address] = {"neg": negative_count, "weak": weakening_count}

        if emergency:
            result = engine.close(p, address, current, "EMERGENCY SELL")
            if result:
                events.append(
                    f"🚨 EMERGENCY SELL {result['label']} | "
                    f"Profit: {result['closed_profit_sda']:+.2f} SDA | "
                    f"Score: {score:.0f}/100 | ROI: {roi:+.2f}% | Mode: PAPER AUTO"
                )
        elif weakening_count >= 2 and not position.get("tp1_hit"):
            result = engine.close(p, address, current, "AUTO PARTIAL SELL", 0.5)
            if result:
                events.append(
                    f"🟠 AUTO PARTIAL SELL {result['label']} | "
                    f"Price: {engine.price(current)} SDA | "
                    f"Profit: {result['closed_profit_sda']:+.2f} SDA | "
                    f"Score: {score:.0f}/100"
                )
        elif negative_count >= 3:
            result = engine.close(p, address, current, "AUTO SELL / EXIT")
            if result:
                events.append(
                    f"🔴 AUTO SELL / EXIT {result['label']} | "
                    f"Price: {engine.price(current)} SDA | "
                    f"Profit: {result['closed_profit_sda']:+.2f} SDA | "
                    f"Score: {score:.0f}/100"
                )

    engine._save_auto(state)
    return events


engine._auto_exit = auto_exit_v19
scanner_main.engine._auto_exit = auto_exit_v19
scanner_main.engine.SL_PCT = 0.05

if __name__ == "__main__":
    engine.main()
