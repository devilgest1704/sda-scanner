"""Read-only checkpoint freshness for the single-writer watchdog."""
import json
from datetime import datetime, timezone
from pathlib import Path

def stale(data, now):
    try:
        value = data["updated_at"]
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            return False
        age = (now-stamp).total_seconds()
        return age > 1200
    except (KeyError, TypeError, ValueError, AttributeError):
        return False  # Unknown evidence must not cause a restart loop.

if __name__ == "__main__":
    try:
        data = json.loads(Path("market_analysis.json").read_text())
    except (OSError, ValueError):
        data = {}
    print("true" if stale(data, datetime.now(timezone.utc)) else "false")
