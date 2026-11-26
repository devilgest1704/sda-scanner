from datetime import datetime, timedelta, timezone

import real_bot_menu as menu


def _worker(scan_stamp):
    now = datetime.now(timezone.utc)
    return {
        "enabled": True,
        "dry_run": False,
        "lastWorkerHeartbeat": now.isoformat(),
        "last_successful_scan_at": scan_stamp,
        "syncStatus": "FRESH",
        "worker_health": "HEALTHY",
    }


def test_telegram_reports_live_only_with_fresh_completed_scan():
    now = datetime.now(timezone.utc)
    assert menu._status(_worker(now.isoformat())) == "🟢 LIVE"


def test_telegram_does_not_report_live_when_scan_checkpoint_is_stale():
    stale = (datetime.now(timezone.utc) - timedelta(seconds=menu.REAL_SCAN_FRESHNESS_SECONDS + 1)).isoformat()
    result = menu._status(_worker(stale))
    assert result.startswith("⚪ STATUS UNKNOWN")
    assert "stale" in result


def test_telegram_fails_closed_when_scan_checkpoint_is_missing_or_future():
    assert menu._status(_worker(None)).startswith("⚪ STATUS UNKNOWN")
    future = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
    assert menu._status(_worker(future)).startswith("⚪ STATUS UNKNOWN")
