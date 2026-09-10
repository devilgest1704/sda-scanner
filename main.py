from copy import deepcopy

from buy_threshold_config import apply as _apply_buy_threshold
import main_legacy as _legacy

# Re-export the canonical legacy entry point API while enforcing the hard BUY threshold.
_apply_buy_threshold(_legacy.engine)
globals().update({k: v for k, v in _legacy.__dict__.items() if not k.startswith("__")})
_apply_buy_threshold(engine)

# Real-wallet statistics must keep the wallet-authoritative OPEN snapshot after
# rebuilding FIFO. FIFO is still used to recompute realized P/L and matched sells,
# but it must not resurrect positions that the current wallet snapshot says are gone.
_legacy_rebuild_fifo = _legacy._rebuild_fifo

def _rebuild_fifo(p, meta):
    wallet_sync = bool(isinstance(p, dict) and p.get("wallet_sync_at"))
    wallet_current = deepcopy(p.get("current", {})) if wallet_sync and isinstance(p.get("current"), dict) else None
    rebuilt = _legacy_rebuild_fifo(p, meta)
    if wallet_current is not None and isinstance(rebuilt, dict):
        rebuilt["current"] = wallet_current
        rebuilt["open_cost_sda"] = sum(
            float(x.get("cost_sda") or 0) for x in wallet_current.values() if isinstance(x, dict)
        )
        rebuilt["open_pnl_sda"] = sum(
            float(x.get("unrealized_pnl_sda") or 0) for x in wallet_current.values() if isinstance(x, dict)
        )
        rebuilt["open_unrealized_pnl_sda"] = rebuilt["open_pnl_sda"]
    return rebuilt

if __name__ == "__main__":
    engine.main()
