"""Canonical paper trading entrypoint: V30 clean pump hunter."""
import main_core as _core
for _name,_value in _core.__dict__.items():
    if not _name.startswith("__"):
        globals()[_name]=_value
import v30_pump_hunter as _v30
_v30.patch(_core,_core._paper)
MAX_WIN_BUY_THRESHOLD=_v30.ENTRY_SCORE
MAX_WIN_MAX_OPEN_POSITIONS=_v30.MAX_OPEN
MAX_WIN_MAX_NEW_BUYS_PER_RUN=_v30.MAX_BUYS_PER_RUN
MAX_WIN_SL_COOLDOWN_SCANS=0
PUMP_HUNTER_VERSION=_v30.VERSION
_paper.BUY_THRESHOLD=_v30.ENTRY_SCORE
_paper.MAX_OPEN_POSITIONS=_v30.MAX_OPEN
_paper.MAX_NEW_BUYS_PER_RUN=_v30.MAX_BUYS_PER_RUN
_paper.SL_PCT=_v30.SL_PCT
engine=_paper
if __name__=="__main__":
    engine.main()
