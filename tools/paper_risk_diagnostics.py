#!/usr/bin/env python3
"""Read-only PAPER diagnostics. Never places orders or edits strategy settings."""
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def load(path):
    with (ROOT / path).open(encoding="utf-8") as f:
        return json.load(f)

def age_minutes(ts):
    if not ts:
        return None
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return round((datetime.now(timezone.utc) - dt).total_seconds() / 60, 1)

def main():
    stats = load("paper_stats.json")
    diag = load("v30_no_buy_diagnostic.json")
    lessons = load("strategy_lab/trade_lessons.json")
    shadow_report = load("strategy_lab/autonomous_agent_report.json")
    optimizer = load("v30_ai_optimizer.json")
    market = diag.get("market", {})
    loss = diag.get("paper_loss_circuit", {})
    reasons = lessons.get("by_exit_reason", [])
    top_losses = sorted(reasons, key=lambda r: r.get("net_pnl_sda", 0))[:12]
    result = {
        "mode": "READ_ONLY",
        "paper": {
            "closed": stats.get("closed_trades"),
            "net_sda": stats.get("realized_pnl_sda"),
            "open_positions": stats.get("open_positions"),
            "win_rate_pct": stats.get("win_rate_pct"),
        },
        "market": {
            "age_minutes": age_minutes(market.get("market_at")),
            "tokens": market.get("tokens"),
            "quotes_last_5m": market.get("quotes_last_5m"),
            "quotes_last_15m": market.get("quotes_last_15m"),
            "fresh_with_8_trades_1h": market.get("fresh_with_8_trades_1h"),
        },
        "loss_circuit": {
            "blocked": loss.get("blocked"),
            "status": loss.get("status"),
            "window_pnl_sda": loss.get("window_pnl_sda"),
        },
        "entry_funnel": {
            "champion_ready": diag.get("funnel", {}).get("champion_ready"),
            "risk_only_rejected_rows": diag.get("funnel", {}).get("risk_only_rejected_rows"),
        },
        "lessons": {
            "as_of": lessons.get("generated_at"),
            "closed": lessons.get("overall", {}).get("closed"),
            "net_sda": lessons.get("overall", {}).get("net_pnl_sda"),
            "top_negative_exit_reasons": [
                {"reason": r.get("reason"), "closed": r.get("closed"),
                 "net_sda": r.get("net_pnl_sda")} for r in top_losses
            ],
        },
        "research": {
            "runtime_parity_verified": shadow_report.get("runtime_parity", {}).get("verified", False),
            "shadow_strategies": {
                name: {
                    "closed": metrics.get("all", {}).get("closed"),
                    "net_sda": metrics.get("all", {}).get("net_sda"),
                    "max_drawdown_sda": metrics.get("all", {}).get("max_drawdown_sda"),
                    "profit_factor": metrics.get("all", {}).get("profit_factor"),
                }
                for name, metrics in shadow_report.get("strategies", {}).items()
            },
            "optimizer_candidates_tested": optimizer.get("candidates_tested"),
            "challenger_test_net_sda": optimizer.get("challenger", {}).get("test", {}).get("pnl_sda"),
            "challenger_recommend_promotion": optimizer.get("challenger", {}).get("recommend_promotion"),
            "promotion_applied": optimizer.get("promotion_applied"),
        },
        "warnings": [],
    }
    if result["market"]["age_minutes"] is None or result["market"]["age_minutes"] > 15:
        result["warnings"].append("MARKET_DATA_STALE_OVER_15_MIN")
    if market.get("tokens") and market.get("quotes_last_5m", 0) / market["tokens"] < 0.10:
        result["warnings"].append("QUOTE_COVERAGE_BELOW_10_PERCENT")
    if lessons.get("overall", {}).get("closed") != stats.get("closed_trades"):
        result["warnings"].append("TRADE_LESSONS_NOT_CURRENT")
    if not result["research"]["runtime_parity_verified"]:
        result["warnings"].append("SHADOW_PAPER_PARITY_UNVERIFIED")
    if result["research"]["promotion_applied"] and not result["research"]["runtime_parity_verified"]:
        result["warnings"].append("PROMOTION_WITHOUT_VERIFIED_PARITY")
    if loss.get("blocked"):
        result["warnings"].append("PAPER_LOSS_CIRCUIT_ACTIVE")
    print(json.dumps(result, indent=2, ensure_ascii=False))

if __name__ == "__main__":
    main()
