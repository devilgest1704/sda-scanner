"""Single canonical scoring/view policy for dashboard and Real Wallet."""
from copy import deepcopy
import re


def _action_icon(action):
    return {
        "EMERGENCY SELL": "🚨", "SELL / EXIT": "🔴", "PARTIAL SELL": "🟠",
        "HOLD / TRAIL": "🟢", "HOLD / WATCH": "🟡", "HOLD / NO COST BASIS": "🟡",
        "HOLD / MARKET DATA N/A": "🟡",
    }.get(action, "🟡")


def patch_dashboard(dashboard):
    if getattr(dashboard, "_sda_dashboard_consistency_patched", False):
        return dashboard
    engine = dashboard.engine
    original_merge_wallet = dashboard._merge_wallet_portfolio
    original_main_dashboard = dashboard.main_dashboard

    def _wallet_synced_portfolio(wallet, portfolio):
        result = deepcopy(portfolio) if isinstance(portfolio, dict) else {}
        current = result.get("current", {})
        if not isinstance(current, dict) or not isinstance(wallet, dict): return result
        allowed_symbols = {str(h.get("symbol") or "").strip().upper() for h in wallet.get("holdings", []) or [] if isinstance(h, dict)}
        allowed_addresses = {str(h.get("address") or "").strip().lower() for h in wallet.get("holdings", []) or [] if isinstance(h, dict)}
        result["current"] = {key: value for key, value in current.items() if isinstance(value, dict) and (str(value.get("symbol") or "").strip().upper() in allowed_symbols or str(value.get("address") or key).strip().lower() in allowed_addresses)}
        result["open_cost_sda"] = sum(engine.num(x.get("cost_sda")) for x in result["current"].values())
        result["open_pnl_sda"] = sum(engine.num(x.get("unrealized_pnl_sda")) for x in result["current"].values())
        result["open_unrealized_pnl_sda"] = result["open_pnl_sda"]
        return result

    def _resolve_token_data(md, token, pf, meta):
        tokens = md.get("tokens", {}) if isinstance(md, dict) else {}
        candidates = [token]
        if isinstance(pf, dict): candidates += [pf.get("address"), pf.get("token_address")]
        for candidate in candidates:
            if not candidate: continue
            key = str(candidate).strip()
            if key in tokens: return key, dashboard._analysis(tokens.get(key, {}) or {})
            if key.lower() in tokens: return key.lower(), dashboard._analysis(tokens.get(key.lower(), {}) or {})
        symbol = str((pf or {}).get("symbol") or token).strip().upper()
        for address, value in tokens.items():
            if not isinstance(value, dict): continue
            analysis = dashboard._analysis(value); label = engine.lbl(address, meta); raw_symbol = value.get("symbol") or analysis.get("symbol")
            if str(label).split("/", 1)[0].strip().upper() == symbol or str(raw_symbol or "").strip().upper() == symbol: return str(address), analysis
        return "", {}

    def _paper_row(address, analysis, ws, meta, label=None):
        try:
            import main as scanner
            decision = scanner.paper_decision(address, analysis, ws)
        except Exception as exc:
            return {"score": None, "blocked": True, "reason": f"paper scorer error: {exc}", "prediction": {}, "prediction_blocked": True, "prediction_text": "", "technical_bull": 0, "technical_bear": 0, "technical_evidence": [], "components": {}, "raw_confidence": None, "near_threshold": False, "near_threshold_reason": "", "score_band": "", "data": {}}
        data = decision.get("data") or {}
        return {**decision, "label": label or engine.lbl(address, meta), "m1h": engine.num(data.get("m1h")) if data.get("m1h") is not None else None, "m4h": engine.num(data.get("m4h")) if data.get("m4h") is not None else None, "m15": engine.num(data.get("m15")) if data.get("m15") is not None else None, "flow_1h": engine.num(data.get("net_1h")) if data.get("net_1h") is not None else None, "trades_1h": engine.num(data.get("trades_1h")) if data.get("trades_1h") is not None else None}

    def _canonical_recommendations(md, ws, meta, portfolio):
        wallet = dashboard.load("wallet_data.json", {}); synced = _wallet_synced_portfolio(wallet, portfolio); current = synced.get("current", {}) if isinstance(synced, dict) else {}; rows = []
        for token, pf in current.items():
            if not isinstance(pf, dict): continue
            market_key, analysis = _resolve_token_data(md, token, pf, meta); decision = _paper_row(market_key or token, analysis, ws, meta) if analysis else {"score": None, "blocked": True, "reason": "market data not resolved", "data": {}}; data = decision.get("data") or {}; score = engine.num(decision.get("score")) if decision.get("score") is not None else None; m1h, flow1 = decision.get("m1h"), decision.get("flow_1h"); pnl_raw = pf.get("unrealized_pnl_pct"); pnl = engine.num(pnl_raw) if pnl_raw is not None else None; data_missing = not bool(analysis) or score is None or m1h is None or flow1 is None
            try:
                from strategy_v21 import technical_sell_confirmed, evaluate_exit
                bearish = technical_sell_confirmed(analysis) if analysis else False; exit_signal = evaluate_exit(pnl if pnl is not None else 0.0, score or 0.0, m1h or 0.0, flow1 or 0.0, bearish)
            except Exception: bearish, exit_signal = False, {}
            negative_evidence = sum((m1h is not None and m1h < 0, flow1 is not None and flow1 < 0, bearish, score is not None and score < 40)); positive_evidence = sum((m1h is not None and m1h > 0, flow1 is not None and flow1 > 0, score is not None and score >= 70, not bearish))
            if exit_signal.get("emergency"): action, reason = "EMERGENCY SELL", "ROI ≤ -15% with weak score, momentum and SDA flow"
            elif pnl is not None and pnl <= -8 and score is not None and score < 35 and m1h is not None and m1h < 0 and flow1 is not None and flow1 < 0: action, reason = "SELL / EXIT", "loss > 8% + weak score + negative momentum + SDA flow"
            elif pnl is not None and pnl <= -4 and negative_evidence >= 3: action, reason = "SELL / EXIT", "loss > 4% with 3+ bearish signals"
            elif pnl is not None and pnl > 5 and score is not None and score < 40 and (m1h is not None and m1h < 0 or flow1 is not None and flow1 < 0) and bearish: action, reason = "PARTIAL SELL", "profit > 5% but trend/flow is weakening"
            elif pnl is not None and pnl > 10 and score is not None and score < 45 and negative_evidence >= 2: action, reason = "PARTIAL SELL", "profit > 10% with weakening market evidence"
            elif pnl is not None and pnl > 0 and positive_evidence >= 3 and score is not None and score >= 55: action, reason = "HOLD / TRAIL", "position profitable with supportive trend/flow"
            elif pnl is not None and pnl < 0 and negative_evidence >= 2: action, reason = "HOLD / WATCH", "loss with mixed-to-bearish evidence; monitor next scan"
            elif data_missing: action, reason = "HOLD / MARKET DATA N/A", "market data not resolved for this wallet token"
            else: action, reason = "HOLD / WATCH", "no confirmed exit condition"
            rows.append({"token": token, "address": market_key, "symbol": pf.get("symbol") or engine.lbl(market_key or token, meta), "action": action, "reason": reason, "pnl_pct": pnl_raw, "pnl_sda": pf.get("unrealized_pnl_sda"), "score": score, "m1h": m1h, "flow_1h": flow1, "paper_blocked": bool(decision.get("blocked")), "paper_reason": decision.get("reason") or "", "paper_band": decision.get("score_band") or ""})
        return sorted(rows, key=lambda x: (x.get("pnl_sda") is None, -engine.num(x.get("pnl_sda")), str(x.get("symbol") or "").upper()))

    def _canonical_buy_gate_rows(md, ws, meta, limit=5):
        rows = []; tokens = md.get("tokens", {}) or {}; threshold = float(getattr(engine, "BUY_THRESHOLD", 60) or 60); min_trades = float(getattr(engine, "MIN_TRADES_1H", 3) or 3)
        for address, token_data in tokens.items():
            analysis = dashboard._analysis(token_data)
            if not analysis or engine.num(analysis.get("price_in_sda")) <= 0: continue
            flow = analysis.get("flow", {}).get("1h", {}) or {}; trades = engine.num(flow.get("buy_count")) + engine.num(flow.get("sell_count")); volume_1h = engine.num(flow.get("total_volume"))
            if volume_1h < 250: continue
            d = _paper_row(address, analysis, ws, meta); score = engine.num(d.get("score")) if d.get("score") is not None else None; reasons = []
            if score is None: reasons.append(d.get("reason") or "paper score unavailable")
            elif d.get("blocked"): reasons.append(d.get("reason") or "paper BUY veto")
            elif score < threshold: reasons.append(f"BUY score {score:.0f}<{threshold:.0f}")
            if trades < min_trades: reasons.append(f"trades {trades:.0f}<{min_trades:.0f}")
            if not reasons: reasons.append("ALL PAPER BUY GATES PASS")
            rows.append({"address": address, "label": engine.lbl(address, meta), "analysis": analysis, "score_data": d.get("data") or {}, "score": score or 0.0, "trades": trades, "volume_1h": volume_1h, "m1h": d.get("m1h"), "m4h": d.get("m4h"), "m15": d.get("m15"), "net_1h": d.get("flow_1h"), "whale_net": engine.num((d.get("data") or {}).get("whale_net")), "reasons": reasons, "paper": d})
        rows.sort(key=lambda x: (x["score"], x["volume_1h"], x["trades"]), reverse=True); return rows[:limit]

    def _sort_wallet_view(wallet, portfolio, md, ws, meta):
        text = original_merge_wallet(wallet, portfolio, md, ws, meta)
        if not text: return text
        lines = text.splitlines(); starts = [i for i, line in enumerate(lines) if line.startswith("🪙 ")]
        if len(starts) < 2: return text
        prefix = lines[:starts[0]]; blocks = []
        for n, start in enumerate(starts):
            end = starts[n + 1] if n + 1 < len(starts) else len(lines); block = lines[start:end]; pnl = None
            for line in block:
                match = re.search(r"P/L\s+([+-]?\d+(?:[.,]\d+)?)\s+SDA", line)
                if match:
                    try: pnl = float(match.group(1).replace(",", ""))
                    except ValueError: pnl = None
                    break
            blocks.append((pnl, block))
        blocks.sort(key=lambda item: (item[0] is None, -(item[0] or 0.0), item[1][0])); sorted_lines = list(prefix)
        for _, block in blocks: sorted_lines.extend(block)
        return "\n".join(sorted_lines)

    def real_trading_report_canonical():
        md = dashboard.load("market_data.json", {"tokens": {}}); ws = dashboard.load("whale_data.json", {}); meta = dashboard.load("token_metadata.json", {}); wallet = dashboard.load("wallet_data.json", {}); portfolio = _wallet_synced_portfolio(wallet, dashboard.load("portfolio_data.json", {})); wallet_view = dashboard._merge_wallet_portfolio(wallet, portfolio, md, ws, meta); rows = _canonical_recommendations(md, ws, meta, portfolio)
        lines = [wallet_view, "", "🧭 POSITION ACTION", "────────────────────────"]
        if not rows: lines.append("⚪ No actionable real positions")
        else:
            for row in rows:
                pnl = "UNKNOWN" if row.get("pnl_sda") is None else f"{engine.num(row.get('pnl_sda')):+.2f} SDA"; score = "N/A" if row.get("score") is None else f"{engine.num(row.get('score')):.0f}/100"; lines.append(f"{_action_icon(row.get('action'))} {row.get('symbol')}: {row.get('action')} • P/L {pnl} • score {score}"); lines.append(f"   {row.get('reason')}")
        lines += ["", "────────────────────────", "👁 READ-ONLY • No real order is executed"]; return "\n".join(lines)

    def market_debug_report_canonical(snapshot=None):
        if snapshot is None:
            md = dashboard.load("market_data.json", {"tokens": {}}); ws = dashboard.load("whale_data.json", {}); meta = dashboard.load("token_metadata.json", {}); rows = _canonical_buy_gate_rows(md, ws, meta, 5); snapshot = {"md": md, "ws": ws, "meta": meta, "rows": rows, "id": dashboard._snapshot_id(md, ws, meta), "time": dashboard._snapshot_time(md, ws)}
        rows = snapshot["rows"]; md = snapshot["md"]; ws = snapshot["ws"]; tokens = md.get("tokens", {}) or {}; active = analyzed = 0; total_volume = 0.0; total_trades = 0
        for td in tokens.values():
            an = dashboard._analysis(td)
            if an: analyzed += 1
            f = an.get("flow", {}).get("1h", {}) if isinstance(an, dict) else {}; vol = engine.num((f or {}).get("total_volume")); total_volume += vol; total_trades += engine.num((f or {}).get("buy_count")) + engine.num((f or {}).get("sell_count")); active += int(vol >= 250)
        threshold = float(getattr(engine, "BUY_THRESHOLD", 60) or 60); ready = sum(1 for r in rows if not r["reasons"] or r["reasons"] == ["ALL PAPER BUY GATES PASS"]); near = sum(1 for r in rows if r.get("paper", {}).get("near_threshold"))
        lines = ["🐞 MARKET DEBUG • CANONICAL PAPER PATH", "", f"Snapshot: {snapshot['id']}", f"State time: {snapshot['time']}", f"Loaded tokens: {len(tokens)}", f"Analyzed tokens: {analyzed}", f"Active tokens: {active}", f"1H volume total: {total_volume:.0f} SDA", f"1H trades total: {total_trades:.0f}", f"Whale data entries: {len(ws) if isinstance(ws, dict) else 0}", "", "Active filter: 1H volume ≥ 250 SDA", f"BUY threshold: {threshold:.0f}/100", "Trade count: informational gate", "────────────────────────", f"🟢 PAPER BUY READY in TOP {len(rows)}: {ready}", f"🟡 BORDERLINE/BRIDGE: {near}", "", "🎯 TOP PAPER BUY DIAGNOSTICS", "────────────────────────"]
        if not rows: lines.append("⚪ No active candidates"); return "\n".join(lines)
        for i, row in enumerate(rows, 1):
            p = row.get("paper", {}); comp = p.get("components") or {}; pred = p.get("prediction") or {}; status = "🟢 BUY READY" if not row["reasons"] or row["reasons"] == ["ALL PAPER BUY GATES PASS"] else "🔴 BLOCKED"; lines.append(f"{i}. {status} {row['label']} — {row['score']:.1f}/100"); lines.append(f"   V2: momentum {comp.get('momentum','N/A')} | flow {comp.get('flow','N/A')} | activity {comp.get('activity','N/A')} | prediction {comp.get('prediction','N/A')} | liquidity {comp.get('liquidity','N/A')}"); lines.append(f"   INPUT: 1H {row.get('m1h',0):+.2f}% | 4H {row.get('m4h',0):+.2f}% | 15M {row.get('m15',0):+.2f}% | volume {row['volume_1h']:.0f} | trades {row['trades']:.0f}"); lines.append(f"   TECH: bull {p.get('technical_bull',0)} | bear {p.get('technical_bear',0)} | adjustment {engine.num((p.get('data') or {}).get('v21_adjustment')):+.1f}"); lines.append(f"   PREDICTOR: {'READY' if pred.get('ready') else 'WARMING'} | P(+5) {engine.num(pred.get('p5')):.0%} | P(+10) {engine.num(pred.get('p10')):.0%} | mean {engine.num(pred.get('mean_roi')):+.1f}%" if pred else "   PREDICTOR: N/A")
            if p.get("reason"): lines.append(f"   BLOCK: {p.get('reason')}")
            elif row["reasons"] != ["ALL PAPER BUY GATES PASS"]: lines.append(f"   BLOCK: {'; '.join(row['reasons'])}")
            else: lines.append("   FINAL: PAPER BUY READY")
        return "\n".join(lines)

    def _main_dashboard_without_wallet(*args, **kwargs):
        """Main dashboard stays focused; full token list belongs in Real Wallet."""
        text = original_main_dashboard(*args, **kwargs)
        if not isinstance(text, str) or "👛 REAL WALLET" not in text: return text
        lines = text.splitlines(); out = []; skipping = False
        for line in lines:
            if line.startswith("👛 REAL WALLET"):
                skipping = True; continue
            if skipping and line.startswith("🧭 POSITION ACTION"):
                skipping = False; out.append(line); continue
            if not skipping: out.append(line)
        return "\n".join(out)

    dashboard._position_recommendations = _canonical_recommendations
    dashboard._merge_wallet_portfolio = _sort_wallet_view
    dashboard.real_trading_report = real_trading_report_canonical
    dashboard._buy_gate_rows = _canonical_buy_gate_rows
    dashboard.market_debug_report = market_debug_report_canonical
    dashboard.main_dashboard = _main_dashboard_without_wallet
    dashboard._sda_action_icon = _action_icon
    dashboard._sda_dashboard_consistency_patched = True
    return dashboard
