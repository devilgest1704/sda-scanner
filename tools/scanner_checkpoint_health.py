"""Fail-closed health for the last completed scanner cycle and worker heartbeat."""
import json
from datetime import datetime, timezone
from pathlib import Path

MAX_CHECKPOINT_AGE_SECONDS = 1200
MAX_HEARTBEAT_AGE_SECONDS = 900
TIMESTAMP_FIELD = "last_successful_scan_at"
HEARTBEAT_FIELD = "lastWorkerHeartbeat"


def _timestamp(value):
    if not isinstance(value, str) or not value.strip():
        return None
    stamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        return None
    return stamp.astimezone(timezone.utc)


def checkpoint_health(data, now):
    """Return HEALTHY, IN_PROGRESS, STALE or UNKNOWN.

    A fresh heartbeat with no recent completed-scan marker means the worker is
    alive and may still be starting/completing a cycle. It must not be killed
    solely because the previous scan marker is old. A stale heartbeat is proof
    that the worker stopped making progress.
    """
    if not isinstance(data, dict):
        return "UNKNOWN"
    try:
        heartbeat = _timestamp(data.get(HEARTBEAT_FIELD))
        if heartbeat is None:
            return "UNKNOWN"
        heartbeat_age = (now - heartbeat).total_seconds()
        if heartbeat_age < 0:
            return "UNKNOWN"
        if heartbeat_age > MAX_HEARTBEAT_AGE_SECONDS:
            return "STALE"

        scan_value = data.get(TIMESTAMP_FIELD)
        if scan_value in (None, ""):
            return "IN_PROGRESS"
        scan = _timestamp(scan_value)
        if scan is None:
            return "UNKNOWN"
        scan_age = (now - scan).total_seconds()
        if scan_age < 0:
            return "UNKNOWN"
        if scan_age > MAX_CHECKPOINT_AGE_SECONDS:
            return "IN_PROGRESS"
        return "HEALTHY"
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return "UNKNOWN"


def stale(data, now):
    return checkpoint_health(data, now) in {"STALE", "UNKNOWN"}


def main():
    try:
        data = json.loads(Path("real_worker_status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        data = None
    print(checkpoint_health(data, datetime.now(timezone.utc)))


if __name__ == "__main__":
    main()
