"""Read-only checkpoint freshness for the single-writer watchdog."""
import json
from datetime import datetime, timezone
from pathlib import Path


MAX_CHECKPOINT_AGE_SECONDS = 1200


def checkpoint_health(data, now):
    """Return HEALTHY, STALE, or UNKNOWN; missing evidence is never healthy."""
    if not isinstance(data, dict):
        return "UNKNOWN"
    try:
        value = data["updated_at"]
        if not isinstance(value, str) or not value.strip():
            return "UNKNOWN"
        stamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        # A naive timestamp has no reliable timezone and must not be trusted.
        if stamp.tzinfo is None:
            return "UNKNOWN"
        age = (now - stamp.astimezone(timezone.utc)).total_seconds()
        # Future timestamps indicate clock/data corruption, not a fresh checkpoint.
        if age < 0:
            return "UNKNOWN"
        return "STALE" if age > MAX_CHECKPOINT_AGE_SECONDS else "HEALTHY"
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return "UNKNOWN"


def stale(data, now):
    """Backward-compatible fail-safe predicate: only HEALTHY is non-stale."""
    return checkpoint_health(data, now) != "HEALTHY"


def main():
    try:
        data = json.loads(Path("market_analysis.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        data = None
    print(checkpoint_health(data, datetime.now(timezone.utc)))


if __name__ == "__main__":
    main()
