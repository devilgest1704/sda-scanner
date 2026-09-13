"""Dashboard adapter for V23 diagnostics.

It never computes a second BUY decision.  It only renders the V23 values
returned by main.paper_decision, so Top Buy Candidates, Position Action and
Market Debug continue to share one canonical decision path.
"""
import re


def patch_dashboard(dashboard):
    if getattr(dashboard, "_sda_v23_dashboard_patched", False):
        return dashboard
    dashboard._sda_v23_dashboard_patched = True
    original_debug = getattr(dashboard, "market_debug_report_canonical", None)
    original_real = getattr(dashboard, "real_trading_report_canonical", None)

    def _v23_line(paper):
        v = paper.get("v23") or paper.get("data", {}).get("v23_prediction") or {}
        if not isinstance(v, dict):
            return ""
        mode = "READY" if v.get("ready") else "WARMING"
        return (
            f"   V23 PUMP: {mode} | P(+10) {float(v.get('p10', 0)):.0%}"
            f" | P(+20) {float(v.get('p20', 0)):.0%}"
            f" | P(+30) {float(v.get('p30', 0)):.0%}"
            f" | mean {float(v.get('mean_roi', 0)):+.1f}%"
            f" | pump score {float(v.get('pump_score', 0)):.0f}/100"
            f" | clean samples {int(v.get('samples', 0) or 0)}"
        )

    if original_debug:
        def market_debug_report_canonical(snapshot=None):
            text = original_debug(snapshot)
            rows = (snapshot or {}).get("rows", []) if isinstance(snapshot, dict) else []
            if not rows:
                return text
            out = []
            for line in text.splitlines():
                out.append(line)
                m = re.match(r"^(\d+)\. .*$", line)
                if m:
                    try:
                        idx = int(m.group(1)) - 1
                        if 0 <= idx < len(rows):
                            extra = _v23_line(rows[idx].get("paper") or {})
                            if extra:
                                out.append(extra)
                    except Exception:
                        pass
            return "\n".join(out)
        dashboard.market_debug_report_canonical = market_debug_report_canonical

    if original_real:
        def real_trading_report_canonical(*args, **kwargs):
            return original_real(*args, **kwargs)
        dashboard.real_trading_report_canonical = real_trading_report_canonical
    return dashboard
