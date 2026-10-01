#!/usr/bin/env python3
"""Read-only V30 state audit. No trading, network access, or state mutation."""
import argparse
import json
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc) if dt.tzinfo else None
    except ValueError:
        return None

def audit(path, stale_minutes=15):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    history = data.get("history", {})
    if not isinstance(history, dict):
        raise ValueError("history must be an object")
    now = datetime.now(timezone.utc)
    issues = []
    phases = Counter()
    fresh = zero_volume = severe_negative_accel = 0
    sample_statuses = Counter()
    for token, row in history.items():
        if not isinstance(row, dict):
            issues.append({"token": token, "issue": "invalid_row"})
            continue
        phase = str(row.get("phase", "UNKNOWN"))
        phases[phase] += 1
        is_fresh = row.get("fresh") is True
        fresh += is_fresh
        sample_statuses[str(row.get("sample_status", "UNKNOWN"))] += 1
        last = row.get("last", {})
        if not isinstance(last, dict):
            issues.append({"token": token, "issue": "invalid_last"})
            continue
        volume = last.get("vol")
        accel = last.get("accel")
        if isinstance(volume, (int, float)) and math.isfinite(volume):
            zero_volume += volume == 0
            if volume > 0 and isinstance(accel, (int, float)) and math.isfinite(accel) and accel <= -90:
                severe_negative_accel += 1
        else:
            issues.append({"token": token, "issue": "missing_or_invalid_volume"})
        updated = timestamp(row.get("updated_at"))
        if updated is None:
            issues.append({"token": token, "issue": "missing_or_invalid_timestamp"})
        elif (now - updated).total_seconds() > stale_minutes * 60 and is_fresh:
            issues.append({"token": token, "issue": "fresh_flag_with_old_timestamp"})
        if phase in {"IGNITION", "CONFIRMATION", "BREAKOUT", "BUY"} and not is_fresh:
            issues.append({"token": token, "issue": "entry_like_phase_not_fresh"})
        if phase in {"IGNITION", "CONFIRMATION", "BREAKOUT", "BUY"} and volume == 0:
            issues.append({"token": token, "issue": "entry_like_phase_zero_volume"})
    return {
        "state_updated_at": data.get("updated_at"),
        "token_count": len(history),
        "phases": dict(phases),
        "fresh_count": fresh,
        "sample_statuses": dict(sample_statuses),
        "zero_last_volume_count": zero_volume,
        "acceleration_le_minus90_with_volume": severe_negative_accel,
        "issues": issues,
        "note": "Diagnostic flags are not proof of trading bugs. No order logic is changed.",
    }

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("state_file", nargs="?", default="v30_pump_state.json")
    parser.add_argument("--stale-minutes", type=int, default=15)
    args = parser.parse_args()
    if args.stale_minutes <= 0:
        parser.error("--stale-minutes must be positive")
    print(json.dumps(audit(args.state_file, args.stale_minutes), indent=2, ensure_ascii=False))

if __name__ == "__main__":
    main()
