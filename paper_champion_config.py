"""PAPER-only champion configuration (BUY filters) with promotion history and rollback.

The live PAPER decision reads this file. REAL trading never reads it; REAL BUYs
only follow PAPER positions that PAPER has already opened.
"""
from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

CONFIG_FILE = Path("paper_champion_config.json")
BASELINE_VERSION = "PAPER-CHAMPION-1"
SELL_POLICY = "swing_v2"
# Stricter-only BUY filters; an empty dict leaves the PAPER BUY decision unchanged.
FILTER_KEYS = {
    "min_score": ("pump_score", "min"),
    "min_quality": ("pump_quality", "min"),
    "min_impulse": ("pump_change", "min"),
    "min_flow_1h": ("net_1h", "min"),
    "min_flow_15m": ("flow_15m", "min"),
    "min_volume_1h": ("volume_1h", "min"),
    "min_trades_1h": ("trades_1h", "min"),
    "min_buy_ratio": ("buy_ratio", "min"),
    "max_m1h": ("m1h", "max"),
    "min_m15": ("m15", "min"),
}

_CACHE = {"mtime": None, "path": None, "config": None}


def _now():
    return datetime.now(timezone.utc).isoformat()


def baseline():
    return {"schema": 1, "paper_only": True, "real_trading": False,
            "version": BASELINE_VERSION, "sell_policy": SELL_POLICY,
            "buy_filters": {}, "source": "baseline_current_paper",
            "updated_at": None, "promotion": None, "previous": None, "history": []}


def config_hash(buy_filters, sell_policy=SELL_POLICY):
    raw = json.dumps({"buy_filters": buy_filters or {}, "sell_policy": sell_policy}, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def load(path=None):
    path = Path(path or CONFIG_FILE)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return baseline()
    if _CACHE["path"] == str(path) and _CACHE["mtime"] == mtime and _CACHE["config"] is not None:
        return deepcopy(_CACHE["config"])
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # A damaged file must never loosen or crash PAPER BUY: fall back to the baseline.
        return baseline()
    if not isinstance(value, dict) or not isinstance(value.get("buy_filters", {}), dict):
        return baseline()
    merged = {**baseline(), **value}
    merged["buy_filters"] = {k: v for k, v in (value.get("buy_filters") or {}).items() if k in FILTER_KEYS}
    _CACHE.update({"path": str(path), "mtime": mtime, "config": deepcopy(merged)})
    return merged


def save(config, path=None):
    path = Path(path or CONFIG_FILE)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    _CACHE.update({"path": None, "mtime": None, "config": None})


def buy_veto(metrics, config=None):
    """Return failed champion filters for PAPER BUY metrics (empty list = no veto)."""
    filters = (config or load()).get("buy_filters") or {}
    failures = []
    for key, threshold in filters.items():
        field, kind = FILTER_KEYS.get(key, (None, None))
        if field is None:
            continue
        try:
            value = float(metrics.get(field))
            limit = float(threshold)
        except (TypeError, ValueError):
            failures.append(f"{key}: missing {field}")
            continue
        if (kind == "min" and value < limit) or (kind == "max" and value > limit):
            failures.append(f"{key} {value:.2f} vs {limit:.2f}")
    return failures


def promote(challenger, metrics, reason, path=None):
    """PAPER-only promotion; previous champion is preserved for rollback."""
    current = load(path)
    new = baseline()
    new.update({
        "version": challenger["version"], "buy_filters": dict(challenger.get("buy_filters") or {}),
        "source": "PAPER_OPTIMIZER_PROMOTION", "updated_at": _now(),
        "promotion": {"at": _now(), "challenger_id": challenger.get("id"), "reason": reason,
                      "metrics": metrics, "config_hash": config_hash(challenger.get("buy_filters"))},
        "previous": {k: current.get(k) for k in ("version", "buy_filters", "sell_policy", "source", "promotion")},
        "history": (current.get("history") or [])[-20:] + [{"event": "PROMOTION", "at": _now(),
                                                             "from": current.get("version"),
                                                             "to": challenger["version"]}],
    })
    save(new, path)
    return new


def rollback(reason, path=None):
    """Restore the previous PAPER champion; returns None when there is nothing to restore."""
    current = load(path)
    previous = current.get("previous")
    if not isinstance(previous, dict):
        return None
    restored = baseline()
    restored.update({
        "version": previous.get("version") or BASELINE_VERSION,
        "buy_filters": dict(previous.get("buy_filters") or {}),
        "source": "PAPER_OPTIMIZER_ROLLBACK", "updated_at": _now(),
        "promotion": previous.get("promotion"), "previous": None,
        "rolled_back": {"at": _now(), "from": current.get("version"), "reason": reason},
        "history": (current.get("history") or [])[-20:] + [{"event": "ROLLBACK", "at": _now(),
                                                             "from": current.get("version"),
                                                             "to": previous.get("version"), "reason": reason}],
    })
    save(restored, path)
    return restored
