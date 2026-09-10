"""Shared V20/V21 position-action policy for Telegram display.

The paper engine remains authoritative for actual exits. This module mirrors the
same V21 score, technical confirmations, emergency guard, and exit counters so
POSITION ACTION and paper trading show the same decision state.
"""
from strategy_v21 import patch_engine, technical_sell_confirmed


def patch_dashboard(dashboard):
    original_main_dashboard = dashboard.main_dashboard

    # Use the exact same V21 score overlay as paper_engine_v19.py.
    # Guard against double-patching if the dashboard module is reloaded.
    if not getattr(dashboard.engine, "_sda_v21_patched", False):
        patch_engine(dashboard.engine)
        dashboard.engine._sda_v21_patched = True

    def position_recommendations_v21(md, ws, meta, portfolio):
        tokens = md.get("tokens", {}) if isinstance(md, dict) else {}
        current = portfolio.get("current", {}) if isinstance(portfolio, dict) else {}
        auto_state = dashboard.load("paper_auto_state.json", {})
        rows = []

        for token, pf in current.items():
            if not isinstance(pf, dict):
                pf = {}
            pnl_raw = pf.get("unrealized_pnl_pct")
            cost = pf.get("cost_sda")
            if cost is None or pnl_raw is None:
                action = "HOLD / NO COST BASIS"
                reason = "no cost basis"
                score = 0
                m1h = 0
                flow1 = 0
            else:
                analysis = dashboard._analysis(tokens.get(token, {}) or {})
                try:
                    signal = dashboard.engine.score(token, analysis, ws) if analysis else {
                        "confidence": 0, "m1h": 0, "net_1h": 0
                    }
                except Exception:
                    signal = {"confidence": 0, "m1h": 0, "net_1h": 0}

                score = dashboard.engine.num(signal.get("confidence"))
                m1h = dashboard.engine.num(signal.get("m1h"))
                flow1 = dashboard.engine.num(signal.get("net_1h"))
                pnl = dashboard.engine.num(pnl_raw)
                bearish = technical_sell_confirmed(analysis) if analysis else False

                old = auto_state.get(token, {}) if isinstance(auto_state, dict) else {}
                neg = int(dashboard.engine.num(old.get("neg")))
                weak = int(dashboard.engine.num(old.get("weak")))
                tp1_hit = bool(pf.get("tp1_hit"))

                # Exact V21 paper-engine conditions.
                emergency = (
                    pnl <= -15.0
                    and score < 35
                    and m1h < 0
                    and flow1 < 0
                )
                negative = (
                    score < 30
                    and m1h < -1.0
                    and flow1 < 0
                    and pnl < -3.0
                    and bearish
                )
                weakening = (
                    pnl > 0
                    and score < 40
                    and (m1h < 0 or flow1 < 0)
                    and bearish
                )

                if emergency:
                    action = "EMERGENCY SELL"
                    reason = "V21 emergency: ROI ≤ -15%, score <35, negative momentum + flow"
                elif weak >= 2 and not tp1_hit:
                    action = "PARTIAL SELL"
                    reason = "V21 weakening confirmed 2 times"
                elif neg >= 3:
                    action = "SELL / EXIT"
                    reason = "V21 negative exit confirmed 3 times"
                elif weakening and not tp1_hit:
                    action = "HOLD / WATCH"
                    reason = f"weakening signal {weak}/2 confirmations"
                elif negative:
                    action = "HOLD / WATCH"
                    reason = f"negative exit signal {neg}/3 confirmations"
                elif score >= 70 and m1h > 0 and flow1 > 0:
                    action = "HOLD / TRAIL"
                    reason = "positive trend and SDA flow"
                else:
                    action = "HOLD / WATCH"
                    reason = "no confirmed exit condition"

            rows.append({
                "token": token,
                "symbol": pf.get("symbol") or dashboard.engine.lbl(token, meta),
                "action": action,
                "reason": reason,
                "pnl_pct": pnl_raw,
                "pnl_sda": pf.get("unrealized_pnl_sda"),
                "score": score,
                "m1h": m1h,
                "flow_1h": flow1,
            })

        order = {
            "EMERGENCY SELL": 0,
            "SELL / EXIT": 1,
            "PARTIAL SELL": 2,
            "HOLD / TRAIL": 3,
            "HOLD / WATCH": 4,
            "HOLD / NO COST BASIS": 5,
        }
        return sorted(
            rows,
            key=lambda x: (order.get(x.get("action"), 9), -dashboard.engine.num(x.get("score")))
        )

    def main_dashboard_v21(*args, **kwargs):
        text = original_main_dashboard(*args, **kwargs)
        lines = []
        for line in text.splitlines():
            if line.startswith("🟡 ") and ": EMERGENCY SELL" in line:
                line = "🚨 " + line[2:]
            lines.append(line)
        return "\n".join(lines)

    dashboard._position_recommendations = position_recommendations_v21
    dashboard.main_dashboard = main_dashboard_v21
    return dashboard
