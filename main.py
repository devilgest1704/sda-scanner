"""Canonical SDA scanner entrypoint: V26 self-learning Pump Hunter.

The legacy/MAX-WIN stack remains available inside main_core for compatibility,
but Paper Trading decisions are replaced by V26. Real wallet remains read-only.
"""
import main_core as _core

for _name, _value in _core.__dict__.items():
    if not _name.startswith("__"):
        globals()[_name] = _value

import v26_pump_hunter as _v26
_v26.patch(_core, _core._paper)

# Public compatibility constants used by dashboard/workflows.
MAX_WIN_BUY_THRESHOLD = _v26.ENTRY_SCORE
MAX_WIN_MAX_OPEN_POSITIONS = _v26.MAX_OPEN
MAX_WIN_MAX_NEW_BUYS_PER_RUN = _v26.MAX_BUYS_PER_RUN
MAX_WIN_SL_COOLDOWN_SCANS = 0
PUMP_HUNTER_VERSION = "V26-PUMP-HUNTER"

# Keep both public and legacy paper-engine references on the same V26 policy.
_paper.BUY_THRESHOLD = _v26.ENTRY_SCORE
_paper.MAX_OPEN_POSITIONS = _v26.MAX_OPEN
_paper.MAX_NEW_BUYS_PER_RUN = _v26.MAX_BUYS_PER_RUN
_paper.SL_PCT = _v26.SL_PCT
engine = _paper

if __name__ == "__main__":
    engine.main()
