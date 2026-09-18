"""Canonical SDA scanner entrypoint: V28 adaptive paper Pump Hunter.

The legacy stack remains available inside main_core for compatibility.
Paper Trading decisions are replaced by V28. Real wallet remains read-only.
"""
import main_core as _core

for _name,_value in _core.__dict__.items():
    if not _name.startswith("__"):
        globals()[_name]=_value

import v26_pump_hunter as _v28
_v28.patch(_core,_core._paper)

MAX_WIN_BUY_THRESHOLD=_v28.ENTRY_SCORE
MAX_WIN_MAX_OPEN_POSITIONS=_v28.MAX_OPEN
MAX_WIN_MAX_NEW_BUYS_PER_RUN=_v28.MAX_BUYS_PER_RUN
MAX_WIN_SL_COOLDOWN_SCANS=0
PUMP_HUNTER_VERSION="V28-ADAPTIVE-PUMP-HUNTER"

_paper.BUY_THRESHOLD=_v28.ENTRY_SCORE
_paper.MAX_OPEN_POSITIONS=_v28.MAX_OPEN
_paper.MAX_NEW_BUYS_PER_RUN=_v28.MAX_BUYS_PER_RUN
_paper.SL_PCT=_v28.SL_PCT
engine=_paper

if __name__=="__main__":
    engine.main()
