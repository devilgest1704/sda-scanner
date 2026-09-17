"""Runtime guard for the Telegram dashboard.

Normalizes malformed external data and V26 persistent state before rendering.
The V26 scanner keeps scan-to-scan history in v26_pump_state.json; an old or
corrupted history entry can be a list and cause the exact `'list' object has
no attribute 'get'` exception inside V26 itself, which market-data guards
cannot catch until too late.
"""


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _normalize_period_map(value):
    if isinstance(value, dict):
        return value
    if not isinstance(value, list):
        return {}
    periods = {}
    for item in value:
        if not isinstance(item, dict):
            continue
        period = item.get("period") or item.get("window") or item.get("timeframe") or item.get("interval")
        if period:
            periods[str(period)] = item
    return periods


def _normalize_analysis(analysis):
    if not isinstance(analysis, dict):
        return {}
    out = dict(analysis)
    out["flow"] = _normalize_period_map(out.get("flow"))
    out["momentum"] = _normalize_period_map(out.get("momentum"))
    return out


def _normalize_token(token):
    if not isinstance(token, dict):
        return {}
    out = dict(token)
    if "analysis" in out:
        out["analysis"] = _normalize_analysis(out.get("analysis"))
    else:
        out = _normalize_analysis(out)
    return out


def _normalize_market(md):
    if not isinstance(md, dict):
        return {"tokens": {}}
    out = dict(md)
    tokens = out.get("tokens")
    if not isinstance(tokens, dict):
        out["tokens"] = {}
        return out
    out["tokens"] = {address: _normalize_token(token) for address, token in tokens.items()}
    return out


def _normalize_loaded(path, value):
    if path in ("market_data.json", "market_analysis.json"):
        return _normalize_market(value)
    if path in ("whale_data.json", "token_metadata.json"):
        return _mapping(value)
    return value


def _sanitize_v26_state(state):
    """Repair V26 state shapes before v26._score/_cooldown can call .get()."""
    if not isinstance(state, dict):
        return {}
    out = dict(state)
    history = out.get("history")
    if not isinstance(history, dict):
        history = {}
    clean_history = {}
    for key, entry in history.items():
        if not isinstance(entry, dict):
            # A malformed list/string entry has no usable scan history. Drop it
            # rather than letting V26 call entry.get(...) and crash.
            continue
        clean = dict(entry)
        if not isinstance(clean.get("last"), dict):
            clean["last"] = {}
        clean_history[str(key).lower()] = clean
    out["history"] = clean_history
    cooldowns = out.get("cooldowns")
    out["cooldowns"] = cooldowns if isinstance(cooldowns, dict) else {}
    return out


def patch_dashboard(dashboard):
    if getattr(dashboard, "_sda_runtime_guard_patched", False):
        return dashboard

    original_load = dashboard.load

    def guarded_load(path, default):
        value = original_load(path, default)
        return _normalize_loaded(path, value)

    dashboard.load = guarded_load

    # The dashboard renderer calls V26.decision(), which has its own persistent
    # state file. Guard that state too; otherwise a malformed history entry can
    # still raise before the dashboard renderer gets a chance to handle it.
    try:
        import v26_pump_hunter as v26
        original_v26_load = v26._load
        if not getattr(v26, "_sda_state_guard_patched", False):
            def guarded_v26_load():
                return _sanitize_v26_state(original_v26_load())
            v26._load = guarded_v26_load
            v26._sda_state_guard_patched = True
    except Exception:
        pass

    original = dashboard.top_buy

    def guarded_top_buy(md, ws, meta, rows=None, snapshot=None):
        safe_md = _normalize_market(md)
        safe_ws = _mapping(ws)
        safe_meta = _mapping(meta)
        try:
            return original(safe_md, safe_ws, safe_meta, rows, snapshot)
        except (AttributeError, TypeError):
            try:
                return original(_normalize_market(safe_md), _mapping(safe_ws), _mapping(safe_meta), rows, snapshot)
            except Exception as retry_exc:
                return (
                    "🔥 TOP BUY CANDIDATES • V26 PUMP-HUNTER\n\n"
                    "⚠️ Market data temporarily malformed; dashboard kept alive.\n"
                    f"Renderer: {type(retry_exc).__name__}: {retry_exc}"
                )

    dashboard.top_buy = guarded_top_buy
    dashboard._sda_runtime_guard_patched = True
    return dashboard
