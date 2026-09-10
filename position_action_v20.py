"""Shared V20 position-action policy for Telegram display.

The paper engine remains authoritative for actual exits. This module mirrors the
same V19 emergency guard in the dashboard so POSITION ACTION cannot hide it.
"""


def patch_dashboard(dashboard):
    original_recommendations = dashboard._position_recommendations
    original_main_dashboard = dashboard.main_dashboard

    def position_recommendations_v20(md, ws, meta, portfolio):
        rows = original_recommendations(md, ws, meta, portfolio)
        tokens = md.get("tokens", {}) if isinstance(md, dict) else {}
        current = portfolio.get("current", {}) if isinstance(portfolio, dict) else {}

        for row in rows:
            token = row.get("token")
            pf = current.get(token, {}) if isinstance(current, dict) else {}
            pnl_raw = pf.get("unrealized_pnl_pct")
            if pf.get("cost_sda") is None or pnl_raw is None:
                continue

            analysis = dashboard._analysis(tokens.get(token, {}) or {})
            try:
                s = dashboard.engine.score(token, analysis, ws) if analysis else {
                    "confidence": 0, "m1h": 0, "net_1h": 0
                }
            except Exception:
                s = {"confidence": 0, "m1h": 0, "net_1h": 0}

            score = dashboard.engine.num(s.get("confidence"))
            m1h = dashboard.engine.num(s.get("m1h"))
            flow = dashboard.engine.num(s.get("net_1h"))
            pnl = dashboard.engine.num(pnl_raw)

            # Must exactly mirror paper_engine_v19.py.
            if pnl <= -15.0 and score < 35 and m1h < 0 and flow < 0:
                row["action"] = "EMERGENCY SELL"
                row["reason"] = "V20 emergency guard: ROI ≤ -15%, score <35, negative momentum + flow"

        order = {
            "EMERGENCY SELL": 0,
            "SELL / EXIT": 1,
            "PARTIAL SELL": 2,
            "HOLD / TRAIL": 3,
            "HOLD / WATCH": 4,
            "HOLD / NO COST BASIS": 5,
        }
        return sorted(rows, key=lambda x: (order.get(x.get("action"), 9), -dashboard.engine.num(x.get("score"))))

    def main_dashboard_v20(*args, **kwargs):
        text = original_main_dashboard(*args, **kwargs)
        lines = []
        for line in text.splitlines():
            if line.startswith("🟡 ") and ": EMERGENCY SELL" in line:
                line = "🚨 " + line[2:]
            lines.append(line)
        return "\n".join(lines)

    dashboard._position_recommendations = position_recommendations_v20
    dashboard.main_dashboard = main_dashboard_v20
    return dashboard
