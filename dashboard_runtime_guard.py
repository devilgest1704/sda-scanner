"""Runtime guard for the Telegram dashboard.

The market API occasionally returns a nested market field as a list instead of
its normal mapping shape. V26 scoring expects mappings and the exception used
to blank the whole dashboard. Normalize only the presentation snapshot and
retry the existing V26 renderer; no trading data is written or changed.
"""


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _normalize_analysis(analysis):
    if not isinstance(analysis, dict):
        return analysis
    out = dict(analysis)
    for field in ("flow", "momentum"):
        value = out.get(field)
        if isinstance(value, dict):
            continue
        # Some API payloads can contain a list of period records. Convert
        # records that explicitly identify their period into the expected map.
        if isinstance(value, list):
            periods = {}
            for item in value:
                if not isinstance(item, dict):
                    continue
                period = item.get("period") or item.get("window") or item.get("timeframe") or item.get("interval")
                if period:
                    periods[str(period)] = item
            out[field] = periods
        else:
            out[field] = {}
    return out


def _normalize_market(md):
    if not isinstance(md, dict):
        return {"tokens": {}}
    out = dict(md)
    tokens = out.get("tokens")
    if isinstance(tokens, dict):
        normalized = {}
        for address, token in tokens.items():
            if isinstance(token, dict):
                td = dict(token)
                if isinstance(td.get("analysis"), dict):
                    td["analysis"] = _normalize_analysis(td["analysis"])
                else:
                    td = _normalize_analysis(td)
                normalized[address] = td
        out["tokens"] = normalized
    else:
        out["tokens"] = {}
    return out


def patch_dashboard(dashboard):
    if getattr(dashboard, "_sda_runtime_guard_patched", False):
        return dashboard
    original = dashboard.top_buy

    def guarded_top_buy(md, ws, meta, rows=None, snapshot=None):
        try:
            return original(md, ws, meta, rows, snapshot)
        except (AttributeError, TypeError) as exc:
            safe_md = _normalize_market(md)
            try:
                return original(safe_md, _mapping(ws), _mapping(meta), rows, snapshot)
            except Exception as retry_exc:
                return (
                    "🔥 TOP BUY CANDIDATES • V26 PUMP-HUNTER\n\n"
                    "⚠️ Market data temporarily malformed; dashboard kept alive.\n"
                    f"Renderer: {type(retry_exc).__name__}: {retry_exc}"
                )

    dashboard.top_buy = guarded_top_buy
    dashboard._sda_runtime_guard_patched = True
    return dashboard
