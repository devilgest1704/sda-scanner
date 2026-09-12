BUY_THRESHOLD = 60

# Canonical Paper Trading capacity.
# Real trading has its own independent limits in real_trading_config.py.
PAPER_MAX_OPEN_POSITIONS = 15
PAPER_MAX_NEW_BUYS_PER_RUN = 3


def apply(engine):
    """Apply canonical BUY threshold and Paper Trading limits."""
    engine.BUY_THRESHOLD = BUY_THRESHOLD

    # engine_legacy.py is the active autonomous Paper engine. Keep its
    # capacity synchronized here so the wrapper/legacy modules cannot silently
    # fall back to the old 5-position / 1-buy limits.
    try:
        if getattr(engine, "__name__", "") == "engine_legacy":
            engine.MAX_OPEN_POSITIONS = PAPER_MAX_OPEN_POSITIONS
            engine.MAX_NEW_BUYS_PER_RUN = PAPER_MAX_NEW_BUYS_PER_RUN
    except Exception:
        pass
    return engine


# main_core imports this module before importing the Paper engine. Importing
# the Paper engine here guarantees the canonical limits are applied at startup.
try:
    import engine_legacy as _paper_engine
    _paper_engine.BUY_THRESHOLD = BUY_THRESHOLD
    _paper_engine.MAX_OPEN_POSITIONS = PAPER_MAX_OPEN_POSITIONS
    _paper_engine.MAX_NEW_BUYS_PER_RUN = PAPER_MAX_NEW_BUYS_PER_RUN
except Exception:
    pass
