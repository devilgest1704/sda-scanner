BUY_THRESHOLD = 78

# Paper Trading — MAX WIN profile.
# Real trading has its own independent limits in real_trading_config.py.
PAPER_MAX_OPEN_POSITIONS = 6
PAPER_MAX_NEW_BUYS_PER_RUN = 1


def apply(engine):
    """Apply the stricter Paper Trading entry/capacity limits."""
    engine.BUY_THRESHOLD = BUY_THRESHOLD
    try:
        if getattr(engine, "__name__", "") == "engine_legacy":
            engine.MAX_OPEN_POSITIONS = PAPER_MAX_OPEN_POSITIONS
            engine.MAX_NEW_BUYS_PER_RUN = PAPER_MAX_NEW_BUYS_PER_RUN
    except Exception:
        pass
    return engine


try:
    import engine_legacy as _paper_engine
    _paper_engine.BUY_THRESHOLD = BUY_THRESHOLD
    _paper_engine.MAX_OPEN_POSITIONS = PAPER_MAX_OPEN_POSITIONS
    _paper_engine.MAX_NEW_BUYS_PER_RUN = PAPER_MAX_NEW_BUYS_PER_RUN
except Exception:
    pass
