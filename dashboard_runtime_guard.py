"""Runtime guard for the Telegram dashboard.

The market API occasionally returns nested fields as lists instead of their
normal mapping shape. Normalize the complete presentation snapshot before V26
renders it so a malformed API field can never blank the whole dashboard.
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


def patch_dashboard(dashboard):
    if getattr(dashboard, "_sda_runtime_guard_patched", False):
        return dashboard
    original = dashboard.top_buy

    def guarded_top_buy(md, ws, meta, rows=None, snapshot=None):
        # Normalize before the first render, not only after an exception.
        # This is important because malformed lists can be reached through
        # more than one nested field before the old retry handler gets a chance.
        safe_md = _normalize_market(md)
        safe_ws = _mapping(ws)
        safe_meta = _mapping(meta)
        try:
            return original(safe_md, safe_ws, safe_meta, rows, snapshot)
        except (AttributeError, TypeError):
            # Keep a second defensive retry with freshly normalized objects.
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
