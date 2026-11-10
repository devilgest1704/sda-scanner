"""Persistent PAPER research dataset: one atomic JSON file per PAPER position.

Written by the PAPER worker only (never by the optimizer). Each file holds the
entry record, every fresh price observation (deduplicated by quote time and
price, never back-filled) and every simulated exit event. Timestamps are UTC.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

DATASET_DIR = Path(os.environ.get("SDA_PAPER_DATASET_DIR") or "strategy_lab/paper_paths")
SCHEMA = 1
MAX_OBSERVATIONS = 20000


def _now():
    return datetime.now(timezone.utc).isoformat()


def position_id(address, opened_at):
    raw = f"{str(address).lower()}|{opened_at}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _path(pid, root=None):
    return Path(root or DATASET_DIR) / f"{pid}.json"


def load_record(pid, root=None):
    try:
        value = json.loads(_path(pid, root).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _save(record, root=None):
    path = _path(record["position_id"], root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _num(value):
    try:
        result = float(value)
        return result if result == result else None
    except (TypeError, ValueError):
        return None


def _entry(pos, fee_rate, config_hash, strategy_version):
    entry_price = _num(pos.get("entry_price"))
    investment = _num(pos.get("investment_sda"))
    metrics = pos.get("entry_metrics") if isinstance(pos.get("entry_metrics"), dict) else {}
    temporal = pos.get("entry_temporal_recovery") if isinstance(pos.get("entry_temporal_recovery"), dict) else {}
    return {
        "token": pos.get("label"), "contract": str(pos.get("address") or "").lower(),
        "strategy": pos.get("paper_strategy"), "strategy_version": strategy_version,
        "config_hash": config_hash, "sell_policy": pos.get("paper_exit_profile"),
        "buy_lane": temporal.get("lane") or pos.get("entry_phase"),
        "buy_reason": temporal.get("reason") or metrics.get("paper_buy_block_reason"),
        "opened_at": pos.get("opened_at"), "decision_price": _num(pos.get("reference_price")),
        "fill_price": entry_price, "investment_sda": investment,
        "quantity": investment / entry_price if entry_price and investment else None,
        "cost_basis_sda": investment * (1 + fee_rate) if investment else None,
        "fee_rate": fee_rate,
        "metrics": {k: _num(metrics.get(k)) for k in ("pump_score", "pump_quality", "pump_change", "net_1h",
                                                       "flow_15m", "volume_1h", "trades_1h", "buy_ratio",
                                                       "m1h", "m15", "m4h", "data_age_sec")},
    }


def ensure_record(pos, fee_rate, config_hash=None, strategy_version=None, root=None):
    pid = position_id(pos.get("address"), pos.get("opened_at"))
    record = load_record(pid, root)
    if record is None:
        try:
            opened = datetime.fromisoformat(str(pos.get("opened_at")).replace("Z", "+00:00"))
            late = (datetime.now(timezone.utc) - opened).total_seconds() > 900
        except (TypeError, ValueError):
            late = True
        record = {"schema": SCHEMA, "position_id": pid, "paper_only": True, "source": "paper_worker",
                  "created_at": _now(), "entry": _entry(pos, fee_rate, config_hash, strategy_version),
                  "observations": [], "exits": [], "status": "OPEN", "data_quality": {
                      # Positions first seen long after entry have no complete path.
                      "complete_from_entry": not late, "rejected_out_of_order": 0}}
        _save(record, root)
    return record


def record_observation(pos, obs, fee_rate, config_hash=None, strategy_version=None, root=None):
    """Append one fresh observation; returns False for duplicates or out-of-order quotes."""
    record = ensure_record(pos, fee_rate, config_hash, strategy_version, root)
    observations = record.setdefault("observations", [])
    key = (obs.get("quote_at"), obs.get("price"))
    if observations:
        last = observations[-1]
        if (last.get("quote_at"), last.get("price")) == key:
            return False
        if str(obs.get("at") or "") < str(last.get("at") or ""):
            record["data_quality"]["rejected_out_of_order"] = record["data_quality"].get("rejected_out_of_order", 0) + 1
            _save(record, root)
            return False
    if len(observations) >= MAX_OBSERVATIONS:
        record["data_quality"]["complete_from_entry"] = False
        return False
    observations.append(obs)
    _save(record, root)
    return True


def record_exit(pos, closed_row, fee_rate, final, root=None):
    record = ensure_record(pos, fee_rate, root=root)
    pnl = _num(closed_row.get("closed_profit_sda"))
    # engine.py folds earlier partial profit into the final row; keep each exit's own PnL.
    if pnl is not None and _num(closed_row.get("realized_from_partial_exits_sda")):
        pnl -= _num(closed_row.get("realized_from_partial_exits_sda"))
    record.setdefault("exits", []).append({
        "at": closed_row.get("closed_at"), "reason": closed_row.get("close_reason"),
        "price": _num(closed_row.get("close_price")), "fraction_of_original": _num(closed_row.get("closed_fraction")),
        "proceeds_sda": _num(closed_row.get("closed_value_sda")), "pnl_sda": pnl,
        "final": bool(final)})
    if final:
        record["status"] = "CLOSED"
        record["closed_at"] = closed_row.get("closed_at")
    _save(record, root)


def load_all(root=None):
    folder = Path(root or DATASET_DIR)
    records = []
    if not folder.exists():
        return records
    for path in sorted(folder.glob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(value, dict) and value.get("position_id"):
            records.append(value)
    return records
