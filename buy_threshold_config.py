BUY_THRESHOLD = 65


def apply(engine):
    """Apply the canonical hard BUY threshold used by scanner and dashboards."""
    engine.BUY_THRESHOLD = BUY_THRESHOLD
    return engine
