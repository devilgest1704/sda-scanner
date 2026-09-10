from buy_threshold_config import apply as _apply_buy_threshold
import engine_legacy as _legacy

# Re-export the canonical engine API while enforcing the hard BUY threshold.
globals().update({k: v for k, v in _legacy.__dict__.items() if not k.startswith("__")})
_apply_buy_threshold(_legacy)

if __name__ == "__main__":
    _legacy.main()
