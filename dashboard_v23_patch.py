"""Render V23 diagnostics on top of the canonical MAX-WIN dashboard.

V23 is diagnostic/shadow only. This module never creates a second BUY policy;
it uses the same main.paper_decision() path and only replaces the stale
legacy predictor line with V23 diagnostics.
"""
import re


def _safe_num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def patch_dashboard(dashboard):
    if getattr(dashboard, "_sda_v23_dashboard_patched", False):
        return dashboard

    original_debug = getattr(dashboard, "market_debug_report_canonical", None)
    if not original_debug:
        return dashboard

    def _v23_for_label(label):
        try:
            import main as scanner
            md = dashboard.load("market_data.json", {"tokens": {}})
            ws = dashboard.load("whale_data.json", {})
            meta = dashboard.load("token_metadata.json", {})
            target = str(label or "").split("/", 1)[0].strip().upper()
            tokens = md.get("tokens", {}) if isinstance(md, dict) else {}
            for address, td in tokens.items():
                if not isinstance(td, dict):
                    continue
                analysis = dashboard._analysis(td)
                if not analysis:
                    continue
                raw = td.get("symbol") or analysis.get("symbol")
                try:
                    sym = str(scanner.lbl(address, meta) if hasattr(scanner, "lbl") else raw).split("/", 1)[0].strip().upper()
                except Exception:
                    sym = str(raw or "").split("/", 1)[0].strip().upper()
                if sym != target:
                    continue
                decision = scanner.paper_decision(address, analysis, ws)
                v = decision.get("v23") or (decision.get("data") or {}).get("v23_prediction") or {}
                return v if isinstance(v, dict) else {}
        except Exception:
            return {}
        return {}

    def _v23_line(v):
        if not v:
            return "   V23 PUMP: N/A"
        mode = "READY" if v.get("ready") else "WARMING"
        return (
            f"   V23 PUMP: {mode} | P(+5) {_safe_num(v.get('p5')):.0%}"
            f" | P(+10) {_safe_num(v.get('p10')):.0%}"
            f" | P(+20) {_safe_num(v.get('p20')):.0%}"
            f" | P(+30) {_safe_num(v.get('p30')):.0%}"
            f" | mean {_safe_num(v.get('mean_roi')):+.1f}%"
            f" | pump {_safe_num(v.get('pump_score')):.0f}/100"
            f" | samples {int(_safe_num(v.get('samples')))}"
        )

    def market_debug_report_canonical(snapshot=None):
        text = original_debug(snapshot)
        lines = text.splitlines()
        out = []
        current_label = None
        for line in lines:
            m = re.match(r"^\d+\.\s+([^•]+?)\s+•", line)
            if m:
                current_label = m.group(1).strip()
                out.append(line)
                continue
            if current_label and line.strip().startswith("PREDICTOR:"):
                v = _v23_for_label(current_label)
                out.append(_v23_line(v))
                current_label = None
                continue
            out.append(line)
        return "\n".join(out)

    dashboard.market_debug_report_canonical = market_debug_report_canonical
    dashboard.market_debug_report = market_debug_report_canonical
    dashboard._sda_v23_dashboard_patched = True
    return dashboard
