#!/usr/bin/env python3
"""Decide whether an overdue DAILY V31 PAPER research run needs recovery.

GitHub cron is best-effort. The continuously-running scanner checks this
read-only policy after a checkpoint, and only dispatches a separate workflow.
No scanner/position/Champion state is changed here.
"""
from __future__ import annotations
import json
import sys
from datetime import datetime, timedelta, timezone

ACTIVE = frozenset({"in_progress", "queued", "pending", "waiting", "requested"})
EVENTS = frozenset({"schedule", "workflow_dispatch", "push"})
SCHEDULE_HOUR_UTC = 3
SCHEDULE_MINUTE_UTC = 17
GRACE_MINUTES = 45
RETRY_AFTER_MINUTES = 60


def parsed_time(value):
    if not isinstance(value, str):
        return None
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return d.astimezone(timezone.utc) if d.tzinfo else None
    except (ValueError, OverflowError):
        return None


def recovery_needed(now, runs):
    """True only if today's intended run has no successful/active replacement.

    Accept a successful manual/push run after the UTC target as valid research.
    Avoid duplicate dispatches while queued or for one hour after any failed
    attempt. Ignore PR runs and future/invalid timestamps. Fail closed if
    workflow-run data is absent or malformed.
    """
    if now.tzinfo is None or not isinstance(runs, list) or not runs:
        return False
    now = now.astimezone(timezone.utc)
    target = now.replace(hour=SCHEDULE_HOUR_UTC, minute=SCHEDULE_MINUTE_UTC,
                         second=0, microsecond=0)
    if now < target + timedelta(minutes=GRACE_MINUTES):
        return False
    recent = []
    for run in runs:
        if not isinstance(run, dict) or run.get("event") not in EVENTS:
            continue
        created = parsed_time(run.get("createdAt"))
        if created is None or not (target <= created <= now + timedelta(seconds=120)):
            continue
        recent.append((created, run))
    if not recent:
        return True
    if any(run.get("status") in ACTIVE or run.get("conclusion") == "success"
           for _, run in recent):
        return False
    latest = max(t for t, _ in recent)
    return now - latest >= timedelta(minutes=RETRY_AFTER_MINUTES)


def main():
    try:
        runs = json.load(sys.stdin)
    except (ValueError, OSError):
        return 2
    if not isinstance(runs, list):
        return 2
    if recovery_needed(datetime.now(timezone.utc), runs):
        print("V31 daily research missed: recovery dispatch is due.")
        return 0
    print("V31 daily research is recent, pending, or inside schedule grace.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
