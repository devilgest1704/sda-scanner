"""Fail-closed health for the last completed scanner cycle."""
import json
from datetime import datetime, timezone
from pathlib import Path

MAX_CHECKPOINT_AGE_SECONDS = 1200
TIMESTAMP_FIELD = "last_successful_scan_at"


def checkpoint_health(data, now):
    """Return HEALTHY, STALE or UNKNOWN; only a proven fresh scan is healthy."""
    if not isinstance(data, dict):
        return "UNKNOWN"
    try:
        value = data[TIMESTAMP_FIELD]
        if not isinstance(value, str) or not value.strip():
            return "UNKNOWN"
        stamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            return "UNKNOWN"
        age = (now - stamp.astimezone(timezone.utc)).total_seconds()
        if age < 0:
            return "UNKNOWN"
        return "STALE" if age > MAX_CHECKPOINT_AGE_SECONDS else "HEALTHY"
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return "UNKNOWN"


def stale(data, now):
    return checkpoint_health(data, now) != "HEALTHY"


def main():
    try:
        data = json.loads(Path("real_worker_status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        data = None
    print(checkpoint_health(data, datetime.now(timezone.utc)))


if __name__ == "__main__":
    main()
