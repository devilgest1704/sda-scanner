"""Safely initialize the independent REAL BUY signal ledger without resetting it."""
from __future__ import annotations

from pathlib import Path


def ensure_state_file(path: str | Path = "real_buy_signal_state.json") -> str:
    """Initialize only a missing/zero-byte placeholder; preserve or reject all other state."""
    target = Path(path)
    from tools.real_buy_pipeline import initial_state, load_state, save_state

    if not target.exists() or target.stat().st_size == 0:
        save_state(initial_state(), target)
        return "INITIALIZED"

    _state, error = load_state(target)
    if error:
        raise RuntimeError(f"REAL_BUY_SIGNAL_STATE_INVALID:{error}")
    return "VALID"


def main() -> None:
    print("REAL BUY signal state:", ensure_state_file())


if __name__ == "__main__":
    main()
