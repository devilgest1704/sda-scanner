"""Legacy V23/V24/V25 dashboard compatibility shim.

V26 is now the only dashboard presentation/decision path. The old shadow
renderer must not mutate top_buy, main_dashboard, Position Action, or DEBUG.
The module remains importable because the hourly workflow still imports it.
"""


def patch_dashboard(dashboard):
    return dashboard
