"""V31 experimental profit gate. Never enable live without out-of-sample validation."""
from dataclasses import dataclass
from typing import Optional

@dataclass(frozen=True)
class ProfitGateConfig:
    min_trades: int = 8
    min_volume_sda: float = 500.0
    min_flow_sda: float = 100.0
    min_buy_ratio: float = 1.25
    min_m15_pct: float = 1.5
    max_m15_pct: float = 12.0
    min_m1h_pct: float = 0.0
    min_quality: float = 65.0
    max_data_age_sec: float = 300.0

def evaluate(snapshot: dict, config: Optional[ProfitGateConfig] = None) -> dict:
    """Fail closed on missing/invalid market fields; returns reasons for audit."""
    c = config or ProfitGateConfig()
    thresholds = (
        ("trades", c.min_trades, "min"),
        ("vol", c.min_volume_sda, "min"),
        ("flow", c.min_flow_sda, "min"),
        ("buy_ratio", c.min_buy_ratio, "min"),
        ("m15", c.min_m15_pct, "min"),
        ("m15", c.max_m15_pct, "max"),
        ("m1", c.min_m1h_pct, "min"),
        ("quality", c.min_quality, "min"),
        ("data_age_sec", c.max_data_age_sec, "max"),
    )
    reasons = []
    for key, limit, direction in thresholds:
        try:
            value = float(snapshot[key])
            if not __import__("math").isfinite(value):
                raise ValueError("nonfinite")
        except (KeyError, TypeError, ValueError):
            reasons.append(f"{key}: missing/invalid")
            continue
        if direction == "min" and value < limit:
            reasons.append(f"{key}: {value:g} < {limit:g}")
        if direction == "max" and value > limit:
            reasons.append(f"{key}: {value:g} > {limit:g}")
    return {"eligible": not reasons, "reasons": reasons, "mode": "SHADOW_ONLY"}
