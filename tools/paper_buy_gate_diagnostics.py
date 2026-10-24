"""Read-only PAPER BUY blocker report from persisted scanner state.

Run: python tools/paper_buy_gate_diagnostics.py
No state writes, trading actions, or strategy changes.
"""
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "v30_pump_state.json"
SCORE = 72
QUALITY = 65


def analyze(state):
    history = state.get("history", {})
    rows = list(history.values())
    stages = Counter(str(x.get("phase", "UNKNOWN")) for x in rows)
    gates = {
        "quote_age_le_180": lambda x, c: isinstance(x.get("data_age_sec"), (int, float)) and 0 <= x["data_age_sec"] <= 180,
        "trades_ge_12": lambda x, c: c.get("trades", 0) >= 12,
        "volume_ge_500": lambda x, c: c.get("vol", 0) >= 500,
        "flow_ge_200": lambda x, c: c.get("flow", 0) >= 200,
        "flow15_ge_60": lambda x, c: c.get("flow15", 0) >= 60,
        "buy_ratio_ge_1_3": lambda x, c: c.get("buy_ratio", 0) >= 1.3,
        "m15_1_5_to_8": lambda x, c: 1.5 <= c.get("m15", 0) <= 8,
        "m1_0_to_8": lambda x, c: 0 < c.get("m1", 0) <= 8,
        "score_ge_72": lambda x, c: x.get("score", 0) >= SCORE,
        "quality_ge_65": lambda x, c: x.get("quality", 0) >= QUALITY,
        "phase_ignition_confirmation": lambda x, c: x.get("phase") in ("IGNITION", "CONFIRMATION"),
        "impulse_ge_6": lambda x, c: x.get("impulse", 0) >= 6,
    }
    counts = {name: sum(bool(fn(x, x.get("last") or {})) for x in rows) for name, fn in gates.items()}
    impulse_gates = {
        "previous_sample": lambda x,c: bool(x.get("prev")),
        "trades_ge_5": lambda x,c: c.get("trades",0) >= 5,
        "volume_ge_100": lambda x,c: c.get("vol",0) >= 100,
        "positive_flow": lambda x,c: c.get("flow",0) > 0,
        "positive_m1": lambda x,c: c.get("m1",0) > 0,
        "positive_m15": lambda x,c: c.get("m15",0) > 0,
    }
    raw_positive = [x for x in rows if x.get("raw_impulse",0) > 0]
    impulse_pass = {name:sum(bool(fn(x,x.get("last") or {})) for x in raw_positive)
                    for name,fn in impulse_gates.items()}
    raw_all = sum(all(fn(x,x.get("last") or {}) for fn in impulse_gates.values())
                  for x in raw_positive)
    all_visible = sum(all(fn(x, x.get("last") or {}) for fn in gates.values()) for x in rows)
    return {
        "state_updated_at": state.get("updated_at"),
        "tokens": len(rows),
        "phase_counts": dict(stages),
        "gate_pass_counts": counts,
        "all_observable_gates_pass": all_visible,
        "raw_impulse_positive": len(raw_positive),
        "raw_impulse_gate_pass_counts": impulse_pass,
        "raw_impulse_pass_all_gates": raw_all,
        "note": "Snapshot only. Does not evaluate fresh delta, liquidity_ok, price_flow_ok, temporal recovery, MACD, cooldown or loss circuit.",
    }


if __name__ == "__main__":
    print(json.dumps(analyze(json.loads(STATE.read_text(encoding="utf-8"))),
                     indent=2, ensure_ascii=False))
