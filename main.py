from buy_threshold_config import apply as _apply_buy_threshold
import main_legacy as _legacy

# Re-export the canonical legacy entry point API while enforcing the hard BUY threshold.
_apply_buy_threshold(_legacy.engine)
globals().update({k: v for k, v in _legacy.__dict__.items() if not k.startswith("__")})
_apply_buy_threshold(engine)

if __name__ == "__main__":
    engine.main()
