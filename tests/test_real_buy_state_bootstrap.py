from pathlib import Path

import pytest

from tools.real_buy_pipeline import VERSION, initial_state, load_state, save_state
from tools.real_buy_state_bootstrap import ensure_state_file


def test_bootstrap_initializes_only_zero_byte_placeholder(tmp_path):
    path = tmp_path / "real_buy_signal_state.json"
    path.write_text("", encoding="utf-8")

    assert ensure_state_file(path) == "INITIALIZED"

    state, error = load_state(path)
    assert error is None
    assert state["schema_version"] == VERSION
    assert state["market_data_status"] == "UNKNOWN"
    assert state["claims"] == {}
    assert state["consumed_signal_ids"] == []


def test_bootstrap_preserves_existing_signal_history_and_claims(tmp_path):
    path = tmp_path / "real_buy_signal_state.json"
    state = initial_state()
    state["consumed_signal_ids"] = ["signal-keep"]
    state["history"]["signal-keep"] = {"status": "CONSUMED"}
    save_state(state, path)
    before = path.read_text(encoding="utf-8")

    assert ensure_state_file(path) == "VALID"
    assert path.read_text(encoding="utf-8") == before


def test_bootstrap_fails_closed_on_nonempty_corrupt_state(tmp_path):
    path = tmp_path / "real_buy_signal_state.json"
    path.write_text("{broken", encoding="utf-8")

    with pytest.raises(RuntimeError, match="REAL_BUY_SIGNAL_STATE_INVALID"):
        ensure_state_file(path)

    assert path.read_text(encoding="utf-8") == "{broken"
