#!/usr/bin/env python3
"""Read-only integrity check for SDA SHADOW reports. No trading or rule changes."""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "strategy_lab/forward_shadow_state.json"
REPORT = ROOT / "strategy_lab/autonomous_agent_report.json"

def parse_time(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))

def main():
    state = json.loads(STATE.read_text(encoding="utf-8"))
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    errors = []
    warnings = []
    state_time = parse_time(state.get("updated_at"))
    report_time = parse_time(report.get("updated_at"))
    if not state_time or not report_time:
        errors.append("Missing state/report updated_at")
    elif state_time > report_time:
        warnings.append(f"Report is older than SHADOW state by {(state_time-report_time).total_seconds()/60:.1f} minutes")
    for name, strategy in state.get("strategies", {}).items():
        if name not in report.get("strategies", {}):
            continue
        valid = [t for t in strategy.get("trades", []) if t.get("valid_for_learning") is True]
        expected_count = len(valid)
        expected_net = sum(float(t.get("pnl_sda", 0)) for t in valid)
        actual = report["strategies"][name].get("all", {})
        reported_count = actual.get("closed")
        reported_net = actual.get("net_sda")
        if reported_count != expected_count or reported_net is None or abs(float(reported_net)-expected_net) > 0.02:
            message = (f"{name}: state={expected_count} valid / {expected_net:.4f} SDA; "
                       f"report={reported_count} valid / {reported_net} SDA")
            if state_time and report_time and state_time > report_time:
                warnings.append("Stale snapshot: " + message)
            else:
                errors.append("Inconsistent snapshot: " + message)
    parity = report.get("runtime_parity", {})
    if not parity.get("verified", False):
        warnings.append("SHADOW/PAPER execution parity NOT verified; no strategy promotion justified")
    print(json.dumps({"status": "FAIL" if errors else "WARN" if warnings else "PASS",
                      "state_updated_at": state.get("updated_at"),
                      "report_updated_at": report.get("updated_at"),
                      "errors": errors, "warnings": warnings},
                     indent=2, ensure_ascii=False))
    return 1 if errors else 0

if __name__ == "__main__":
    sys.exit(main())
