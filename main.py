"""Canonical paper trading entrypoint: V30 clean pump hunter."""
import main_core as _core
for _name,_value in _core.__dict__.items():
    if not _name.startswith("__"):
        globals()[_name]=_value
import v30_pump_hunter as _v30
_v30.patch(_core,_core._paper)
MAX_WIN_BUY_THRESHOLD=_v30.ENTRY_SCORE
MAX_WIN_MAX_OPEN_POSITIONS=_v30.MAX_OPEN
MAX_WIN_MAX_NEW_BUYS_PER_RUN=_v30.MAX_BUYS_PER_RUN
MAX_WIN_SL_COOLDOWN_SCANS=0
PUMP_HUNTER_VERSION=_v30.VERSION
_paper.BUY_THRESHOLD=_v30.ENTRY_SCORE
_paper.MAX_OPEN_POSITIONS=_v30.MAX_OPEN
_paper.MAX_NEW_BUYS_PER_RUN=_v30.MAX_BUYS_PER_RUN
_paper.SL_PCT=_v30.SL_PCT
engine=_paper

def _run_with_paper_telemetry():
    """Generate diagnostics from the SAME runtime ledger the paper engine uses."""
    try:
        return engine.main()
    finally:
        # REAL market snapshot uses the same V30 scoring math with REAL-owned history.
        try:
            _v30.write_real_market_snapshot("market_data.json")
        except Exception as exc:
            print("::error::REAL market snapshot failed closed:", repr(exc))
        # PAPER telemetry runs in parallel and cannot authorize or block REAL BUY.
        try:
            _v30.write_scan_funnel("market_analysis.json")
        except Exception as exc:
            print("::warning::V30 candidate telemetry failed:", repr(exc))
        try:
            import os
            os.environ.setdefault("REAL_BUY_PIPELINE_ENABLED", "true")
            from real_buy_pipeline import run as run_real_buy_pipeline
            print("REAL BUY PIPELINE:", run_real_buy_pipeline())
        except Exception as exc:
            print("::error::REAL BUY pipeline failed closed:", repr(exc))
        # Bridge remains responsible for REAL wallet reconciliation and SELL only.
        try:
            from real_paper_bridge import reconcile
            print("REAL wallet/SELL reconciliation:", reconcile())
        except Exception as exc:
            print("::error::REAL reconciliation failed:", repr(exc))
        # Report generation must never mutate or replace the trading ledger.
        try:
            from pathlib import Path
            import json
            from datetime import datetime, timezone
            from tools.paper_telemetry_report import report
            ledger = Path(engine.POSITIONS_FILE)
            result = report(ledger)
            result["generated_at"] = datetime.now(timezone.utc).isoformat()
            result["source"] = str(ledger.resolve())
            output = Path("v30_paper_telemetry_report.json")
            temp = output.with_suffix(".json.tmp")
            temp.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
            temp.replace(output)
            print("V30 PAPER TELEMETRY:", result["status"],
                  "closed:", result.get("closed_count", 0),
                  "annotated:", result.get("annotated_closed_count", 0),
                  "open:", result.get("open_count", 0))
            if result["status"] != "ok":
                print("::warning::V30 paper telemetry ledger unavailable or empty")
        except Exception as exc:
            print("::warning::V30 paper telemetry report failed:", repr(exc))

if __name__=="__main__":
    _run_with_paper_telemetry()
