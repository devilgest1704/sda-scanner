import real_bot_menu


def test_worker_status_is_worker_derived():
    from datetime import datetime, timezone
    status = {"enabled": True, "dry_run": False,
              "updated_at": datetime.now(timezone.utc).isoformat()}
    assert real_bot_menu._status(status) == "🟢 LIVE"


def test_dashboard_position_line_is_compact_and_hides_diagnostics():
    line = real_bot_menu._position_lines("AAA", {
        "market_value_sda": 18.45, "market_pnl_sda": -1.55,
    })
    assert line == ["🔴 AAA 18.45 SDA -1.55"]
