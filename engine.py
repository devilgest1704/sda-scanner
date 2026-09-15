from buy_threshold_config import apply as _apply_buy_threshold
import engine_legacy as _legacy
import paper_stats_patch as _paper_stats_patch

# Re-export the canonical engine API while enforcing the hard BUY threshold.
globals().update({k: v for k, v in _legacy.__dict__.items() if not k.startswith("__")})
_apply_buy_threshold(_legacy)
_paper_stats_patch.patch(_legacy)
BUY_THRESHOLD = _legacy.BUY_THRESHOLD

if __name__ == "__main__":
    _legacy.main()
