from copy import deepcopy
import inspect
import json
import os

from buy_threshold_config import apply as _apply_buy_threshold
import main_legacy as _legacy
import engine_legacy as _paper

# Re-export the canonical legacy entry point API while enforcing the hard BUY threshold.
_apply_buy_threshold(_legacy.engine)
globals().update({k: v for k, v in _legacy.__dict__.items() if not k.startswith("__")})
_apply_buy_threshold(engine)

# ---------------------------------------------------------------------------
# PAPER BUY QUALITY GUARD
# ---------------------------------------------------------------------------
# Keep BUY_THRESHOLD=65. We do not make the score threshold harder; instead we
# reject weak market structure around a 65+ score and prevent immediate
# re-entry into a token that has just stopped out.
PAPER_BUY_GUARD_FILE = "paper_buy_guard_state.json"
PAPER_SL_COOLDOWN_SCANS = 4
PAPER_MIN_1H_MOMENTUM = 0.0
PAPER_MIN_1H_FLOW = 0.0
PAPER_MIN_15M_MOMENTUM = -0.75

_paper_original_score = _paper.score
_paper_original_main = _paper.main
_paper_guard_state = {}
_paper_guard_prepared = False
_paper_buy_scan = False


def _paper_guard_load():
    try:
        with open(PAPER_BUY_GUARD_FILE, encoding="utf-8") as f:
            x = json.load(f)
        return x if isinstance(x, dict) else {}
    except Exception:
        return {}


def _paper_guard_save(x):
    tmp = PAPER_BUY_GUARD_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(x, f, indent=2, ensure_ascii=False)
        os.replace(tmp, PAPER_BUY_GUARD_FILE)
    except Exception:
        pass


def _paper_guard_prepare():
    global _paper_guard_state, _paper_guard_prepared
    if _paper_guard_prepared:
        return
    state = _paper_guard_load()
    positions = _paper_guard_load_positions()
    closed = positions.get("closed_trades", []) if isinstance(positions, dict) else []

    # A new SL starts a four-scan cooldown. We detect it from the persistent
    # closed-trade ledger, so a restart cannot accidentally forget the cooldown.
    latest_sl = {}
    for tr in closed:
        if not isinstance(tr, dict):
            continue
        reason = str(tr.get("close_reason") or "").upper()
        if "SL" not in reason:
            continue
        address = str(tr.get("address") or "").lower()
        if not address:
            continue
        stamp = str(tr.get("closed_at") or "")
        if stamp > str(latest_sl.get(address) or ""):
            latest_sl[address] = stamp

    cooldowns = state.get("cooldowns", {}) if isinstance(state.get("cooldowns"), dict) else {}
    last_sl = state.get("last_sl", {}) if isinstance(state.get("last_sl"), dict) else {}
    for address, stamp in latest_sl.items():
        if stamp and stamp != str(last_sl.get(address) or ""):
            cooldowns[address] = PAPER_SL_COOLDOWN_SCANS
            last_sl[address] = stamp

    # One scanner run consumes one cooldown scan. Newly-created cooldowns are
    # deliberately not decremented until the next run.
    previous = state.get("prepared_at")
    if previous:
        for address in list(cooldowns):
            try:
                cooldowns[address] = max(0, int(cooldowns[address]) - 1)
            except Exception:
                cooldowns[address] = 0
    _paper_guard_state = {"cooldowns": cooldowns, "last_sl": last_sl}
    _paper_guard_save({**_paper_guard_state, "prepared_at": _paper.now()})
    _paper_guard_prepared = True


def _paper_guard_finish():
    global _paper_guard_prepared
    if _paper_guard_prepared:
        _paper_guard_save({**_paper_guard_state, "prepared_at": _paper.now()})
    _paper_guard_prepared = False


def _paper_guard_load_positions():
    try:
        with open(_paper.POSITIONS_FILE, encoding="utf-8") as f:
            x = json.load(f)
        return x if isinstance(x, dict) else {}
    except Exception:
        return {}


def _paper_score_guarded(address, analysis, whale_state):
    s = _paper_original_score(address, analysis, whale_state)

    # Exit logic must continue to see the real score. The guard applies only
    # to score() calls made by the paper engine's candidate BUY loop.
    stack = inspect.stack()
    in_auto_exit = any(frame.function == "_auto_exit" for frame in stack)
    in_paper_main = any(frame.function == "main" and frame.frame.f_globals.get("__name__") == _paper.__name__ for frame in stack)
    if not in_paper_main or in_auto_exit:
        return s

    address = str(address).lower()
    cooldown = int((_paper_guard_state.get("cooldowns") or {}).get(address, 0) or 0)
    m1 = float(s.get("m1h") or 0)
    m15 = float(s.get("m15") or 0)
    flow1 = float(s.get("net_1h") or 0)

    reasons = []
    if cooldown > 0:
        reasons.append(f"SL cooldown {cooldown}")
    if m1 <= PAPER_MIN_1H_MOMENTUM:
        reasons.append("1h momentum <= 0")
    if flow1 <= PAPER_MIN_1H_FLOW:
        reasons.append("1h SDA flow <= 0")
    if m15 < PAPER_MIN_15M_MOMENTUM:
        reasons.append("15m momentum too weak")

    if reasons:
        s = dict(s)
        s["paper_buy_blocked"] = True
        s["paper_buy_block_reason"] = "; ".join(reasons)
        # Keep the original score visible in diagnostics, but make the actual
        # candidate fail the existing hard BUY_THRESHOLD=65 gate.
        s["paper_raw_confidence"] = s.get("confidence")
        s["confidence"] = min(float(s.get("confidence") or 0), float(_paper.BUY_THRESHOLD) - 1.0)
    else:
        s = dict(s)
        s["paper_buy_blocked"] = False
        s["paper_buy_block_reason"] = ""
        s["paper_raw_confidence"] = s.get("confidence")
    return s


# The paper engine remains the source of truth for execution/exit mechanics.
# We only wrap its score function during its BUY-candidate pass.
_paper.score = _paper_score_guarded


def _paper_main_guarded():
    _paper_guard_prepare()
    try:
        return _paper_original_main()
    finally:
        _paper_guard_finish()


_paper.main = _paper_main_guarded
engine.main = _paper_main_guarded

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
