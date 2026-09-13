"""Single canonical scoring/view policy for dashboard and Real Wallet."""
from copy import deepcopy
import re

def _action_icon(action):
    return {"EMERGENCY SELL":"🚨","SELL / EXIT":"🔴","PARTIAL SELL":"🟠","HOLD / TRAIL":"🟢","HOLD / PUMP":"🟢","HOLD / WATCH":"🟡","HOLD / NO COST BASIS":"🟡","HOLD / MARKET DATA N/A":"🟡"}.get(action,"🟡")

def patch_dashboard(dashboard):
    if getattr(dashboard,"_sda_dashboard_consistency_patched",False): return dashboard
    engine=dashboard.engine; original_merge_wallet=dashboard._merge_wallet_portfolio; original_main_dashboard=dashboard.main_dashboard
    def _wallet_synced_portfolio(wallet,portfolio):
        result=deepcopy(portfolio) if isinstance(portfolio,dict) else {}; current=result.get("current",{})
        if not isinstance(current,dict) or not isinstance(wallet,dict): return result
        syms={str(h.get("symbol") or "").strip().upper() for h in wallet.get("holdings",[]) or [] if isinstance(h,dict)}; addrs={str(h.get("address") or "").strip().lower() for h in wallet.get("holdings",[]) or [] if isinstance(h,dict)}
        result["current"]={k:v for k,v in current.items() if isinstance(v,dict) and (str(v.get("symbol") or "").strip().upper() in syms or str(v.get("address") or k).strip().lower() in addrs)}
        result["open_cost_sda"]=sum(engine.num(x.get("cost_sda")) for x in result["current"].values()); result["open_pnl_sda"]=sum(engine.num(x.get("unrealized_pnl_sda")) for x in result["current"].values()); result["open_unrealized_pnl_sda"]=result["open_pnl_sda"]; return result
    def _resolve_token_data(md,token,pf,meta):
        tokens=md.get("tokens",{}) if isinstance(md,dict) else {}; candidates=[token]
        if isinstance(pf,dict): candidates += [pf.get("address"),pf.get("token_address")]
        for c in candidates:
            if not c: continue
            k=str(c).strip()
            if k in tokens:return k,dashboard._analysis(tokens.get(k,{}) or {})
            if k.lower() in tokens:return k.lower(),dashboard._analysis(tokens.get(k.lower(),{}) or {})
        symbol=str((pf or {}).get("symbol") or token).strip().upper()
        for address,value in tokens.items():
            if not isinstance(value,dict):continue
            a=dashboard._analysis(value); label=engine.lbl(address,meta); raw=value.get("symbol") or a.get("symbol")
            if str(label).split("/",1)[0].strip().upper()==symbol or str(raw or "").strip().upper()==symbol:return str(address),a
        return "",{}
    def _paper_row(address,analysis,ws,meta,label=None):
        try:
            import main as scanner; d=scanner.paper_decision(address,analysis,ws)
        except Exception as exc:return {"score":None,"blocked":True,"reason":f"paper scorer error: {exc}","prediction":{},"prediction_blocked":True,"prediction_text":"","technical_bull":0,"technical_bear":0,"technical_evidence":[],"components":{},"raw_confidence":None,"near_threshold":False,"near_threshold_reason":"","score_band":"","data":{}}
        data=d.get("data") or {}; return {**d,"label":label or engine.lbl(address,meta),"m1h":engine.num(data.get("m1h")) if data.get("m1h") is not None else None,"m4h":engine.num(data.get("m4h")) if data.get("m4h") is not None else None,"m15":engine.num(data.get("m15")) if data.get("m15") is not None else None,"flow_1h":engine.num(data.get("net_1h")) if data.get("net_1h") is not None else None,"trades_1h":engine.num(data.get("trades_1h")) if data.get("trades_1h") is not None else None}
    def _canonical_recommendations(md,ws,meta,portfolio):
        wallet=dashboard.load("wallet_data.json",{}); synced=_wallet_synced_portfolio(wallet,portfolio); current=synced.get("current",{}) if isinstance(synced,dict) else {}; rows=[]
        for token,pf in current.items():
            if not isinstance(pf,dict):continue
            market_key,analysis=_resolve_token_data(md,token,pf,meta); d=_paper_row(market_key or token,analysis,ws,meta) if analysis else {"score":None,"blocked":True,"reason":"market data not resolved","data":{}}; data=d.get("data") or {}; score=engine.num(d.get("score")) if d.get("score") is not None else None; m1h,flow=d.get("m1h"),d.get("flow_1h"); pnl_raw=pf.get("unrealized_pnl_pct"); pnl=engine.num(pnl_raw) if pnl_raw is not None else None; pred=d.get("prediction") or {}; p5=engine.num(pred.get("p5")); p10=engine.num(pred.get("p10"))
            try:
                from strategy_v21 import technical_sell_confirmed,evaluate_exit
                bearish=technical_sell_confirmed(analysis) if analysis else False; ex=evaluate_exit(pnl or 0,score or 0,m1h or 0,flow or 0,bearish)
            except Exception: bearish,ex=False,{}
            neg=sum((m1h is not None and m1h<0,flow is not None and flow<0,bearish,score is not None and score<40)); strong=pnl is not None and pnl>=10 and m1h is not None and m1h>0 and flow is not None and flow>0 and not bearish; candidate=pnl is not None and pnl>0 and ((p10>=.40 and p5>=.62) or (score is not None and score>=78)) and m1h is not None and m1h>=0 and flow is not None and flow>=0 and not bearish
            if ex.get("emergency"):action,reason="EMERGENCY SELL","ROI ≤ -15% with weak score, momentum and SDA flow"
            elif pnl is not None and pnl<=-8 and neg>=3:action,reason="SELL / EXIT","loss > 8% with confirmed multi-factor weakness"
            elif pnl is not None and pnl<=-4 and neg>=3:action,reason="SELL / EXIT","loss > 4% with 3+ bearish signals"
            elif strong:action,reason="HOLD / PUMP","pump in progress: profit, momentum and SDA flow remain positive"
            elif candidate:action,reason="HOLD / TRAIL","profitable pump candidate; protect the move, do not exit prematurely"
            elif pnl is not None and pnl>10 and neg>=2:action,reason="PARTIAL SELL","large profit but confirmed weakening/reversal"
            elif pnl is not None and pnl>0 and neg>=2:action,reason="PARTIAL SELL","profit with confirmed weakening market evidence"
            elif pnl is not None and pnl<0 and neg>=2:action,reason="HOLD / WATCH","loss with mixed-to-bearish evidence; wait for confirmed breakdown"
            elif not analysis or score is None or m1h is None or flow is None:action,reason="HOLD / MARKET DATA N/A","market data not resolved for this wallet token"
            else:action,reason="HOLD / WATCH","no confirmed exit condition"
            rows.append({"token":token,"address":market_key,"symbol":pf.get("symbol") or engine.lbl(market_key or token,meta),"action":action,"reason":reason,"pnl_pct":pnl_raw,"pnl_sda":pf.get("unrealized_pnl_sda"),"score":score,"m1h":m1h,"flow_1h":flow,"paper_blocked":bool(d.get("blocked")),"paper_reason":d.get("reason") or "","paper_band":d.get("score_band") or "","p5":p5,"p10":p10,"pump_mode":bool(strong or candidate)})
        return sorted(rows,key=lambda x:(x.get("pnl_sda") is None,-engine.num(x.get("pnl_sda")),str(x.get("symbol") or "").upper()))
    def _canonical_buy_gate_rows(md,ws,meta,limit=5):
        rows=[]
        for address,td in (md.get("tokens",{}) or {}).items():
            a=dashboard._analysis(td)
            if not a or engine.num(a.get("price_in_sda"))<=0:continue
            f=a.get("flow",{}).get("1h",{}) or {}; trades=engine.num(f.get("buy_count"))+engine.num(f.get("sell_count")); vol=engine.num(f.get("total_volume"))
            if vol<250:continue
            d=_paper_row(address,a,ws,meta); score=engine.num(d.get("score")) if d.get("score") is not None else None; reasons=[]
            if score is None:reasons.append(d.get("reason") or "paper score unavailable")
            elif d.get("blocked"):reasons.append(d.get("reason") or "paper BUY veto")
            if trades<5:reasons.append(f"trades {trades:.0f}<5")
            if not reasons:reasons.append("ALL MAX-WIN BUY GATES PASS")
            rows.append({"address":address,"label":engine.lbl(address,meta),"analysis":a,"score_data":d.get("data") or {},"score":score or 0,"trades":trades,"volume_1h":vol,"m1h":d.get("m1h"),"m4h":d.get("m4h"),"m15":d.get("m15"),"net_1h":d.get("flow_1h"),"whale_net":engine.num((d.get("data") or {}).get("whale_net")),"reasons":reasons,"paper":d,"p5":engine.num((d.get("prediction") or {}).get("p5")),"p10":engine.num((d.get("prediction") or {}).get("p10"))})
        rows.sort(key=lambda x:(x["score"],x["volume_1h"],x["trades"]),reverse=True); return rows[:limit]
    def _sort_wallet_view(wallet,portfolio,md,ws,meta):
        text=original_merge_wallet(wallet,portfolio,md,ws,meta)
        if not text:return text
        lines=text.splitlines(); starts=[i for i,line in enumerate(lines) if line.startswith("🪙 ")]
        if len(starts)<2:return text
        prefix=lines[:starts[0]]; blocks=[]
        for n,start in enumerate(starts):
            end=starts[n+1] if n+1<len(starts) else len(lines); block=lines[start:end]; pnl=None
            for line in block:
                match=re.search(r"P/L\s+([+-]?\d+(?:[.,]\d+)?)\s+SDA",line)
                if match:
                    try:pnl=float(match.group(1).replace(",",""))
                    except ValueError:pnl=None
                    break
            blocks.append((pnl,block))
        blocks.sort(key=lambda item:(item[0] is None,-(item[0] or 0.0),item[1][0])); out=list(prefix)
        for _,block in blocks:out.extend(block)
        return "\n".join(out)
    def real_trading_report_canonical():
        md=dashboard.load("market_data.json",{"tokens":{}}); ws=dashboard.load("whale_data.json",{}); meta=dashboard.load("token_metadata.json",{}); wallet=dashboard.load("wallet_data.json",{}); portfolio=_wallet_synced_portfolio(wallet,dashboard.load("portfolio_data.json",{})); view=dashboard._merge_wallet_portfolio(wallet,portfolio,md,ws,meta); rows=_canonical_recommendations(md,ws,meta,portfolio); lines=[view,"","🧭 POSITION ACTION","────────────────────────"]
        if not rows:lines.append("⚪ No actionable real positions")
        else:
            for r in rows:
                pnl="UNKNOWN" if r.get("pnl_sda") is None else f"{engine.num(r.get('pnl_sda')):+.2f} SDA"; score="N/A" if r.get("score") is None else f"{engine.num(r.get('score')):.0f}/100"; lines += [f"{_action_icon(r.get('action'))} {r.get('symbol')}: {r.get('action')} • P/L {pnl} • score {score}",f"   {r.get('reason')}"]
        lines += ["","────────────────────────","👁 READ-ONLY • No real order is executed"]; return "\n".join(lines)
    def market_debug_report_canonical(snapshot=None):
        if snapshot is None:
            md=dashboard.load("market_data.json",{"tokens":{}}); ws=dashboard.load("whale_data.json",{}); meta=dashboard.load("token_metadata.json",{}); rows=_canonical_buy_gate_rows(md,ws,meta,5); snapshot={"md":md,"ws":ws,"meta":meta,"rows":rows,"id":dashboard._snapshot_id(md,ws,meta),"time":dashboard._snapshot_time(md,ws)}
        rows=snapshot["rows"]; md=snapshot["md"]; ws=snapshot["ws"]; tokens=md.get("tokens",{}) or {}; analyzed=sum(1 for td in tokens.values() if dashboard._analysis(td)); total_volume=sum(engine.num((dashboard._analysis(td).get("flow",{}).get("1h",{}) or {}).get("total_volume")) for td in tokens.values() if dashboard._analysis(td)); total_trades=sum(engine.num((dashboard._analysis(td).get("flow",{}).get("1h",{}) or {}).get("buy_count"))+engine.num((dashboard._analysis(td).get("flow",{}).get("1h",{}) or {}).get("sell_count")) for td in tokens.values() if dashboard._analysis(td)); ready=sum(1 for r in rows if r.get("reasons")==["ALL MAX-WIN BUY GATES PASS"])
        lines=["🐞 MARKET DEBUG • CANONICAL MAX-WIN PATH","",f"Snapshot: {snapshot['id']}",f"State time: {snapshot['time']}",f"Loaded tokens: {len(tokens)}",f"Analyzed tokens: {analyzed}",f"1H volume total: {total_volume:.0f} SDA",f"1H trades total: {total_trades:.0f}",f"Whale data entries: {len(ws) if isinstance(ws,dict) else 0}","","Active filter: 1H volume ≥ 250 SDA","BUY threshold: 78/100","BUY policy: score + prediction + technical + momentum + flow + activity","────────────────────────",f"🟢 MAX-WIN BUY READY in TOP {len(rows)}: {ready}","","🎯 TOP BUY CANDIDATES • CANONICAL","────────────────────────"]
        if not rows:lines.append("⚪ No active candidates")
        else:
            for i,r in enumerate(rows,1):
                p=r.get("paper",{}); pred=r.get("prediction") or {}; reasons=r.get("reasons") or []; status="🟢 BUY READY" if reasons==["ALL MAX-WIN BUY GATES PASS"] else "🔴 BLOCKED"; comp=p.get("components") or {}; lines += [f"{i}. {r['label']} • {status} • score {r['score']:.0f}/100",f"   V2: momentum {comp.get('momentum','N/A')} | flow {comp.get('flow','N/A')} | activity {comp.get('activity','N/A')} | prediction {comp.get('prediction','N/A')} | liquidity {comp.get('liquidity','N/A')}",f"   INPUT: M15/M1H/M4H {engine.num(r.get('m15')):+.1f}/{engine.num(r.get('m1h')):+.1f}/{engine.num(r.get('m4h')):+.1f}% | volume {r['volume_1h']:.0f} | trades {r['trades']:.0f}",f"   TECH: bull {p.get('technical_bull',0)} | bear {p.get('technical_bear',0)}",f"   PREDICTOR: {'READY' if pred.get('ready') else 'WARMING'} | P(+5) {engine.num(pred.get('p5')):.0%} | P(+10) {engine.num(pred.get('p10')):.0%} | mean {engine.num(pred.get('mean_roi')):+.1f}%",f"   {'; '.join(reasons)}"]
        return "\n".join(lines)
    def _main_dashboard_without_wallet(*args,**kwargs):
        text=original_main_dashboard(*args,**kwargs)
        if not isinstance(text,str) or "👛 REAL WALLET" not in text:return text
        lines=text.splitlines(); out=[]; skipping=False; wallet_summary=[]
        for line in lines:
            if line.startswith("👛 REAL WALLET"):skipping=True; continue
            if skipping and line.startswith("📊 Known token value:"):wallet_summary=["────────────────────────",line]; continue
            if skipping and line.startswith("💼 TOTAL WALLET VALUE:"):wallet_summary.append(line); continue
            if skipping and line.startswith("Source:"):wallet_summary.append(line); continue
            if skipping and line.startswith("💹 P/L SUMMARY"):wallet_summary.append(line); continue
            if skipping and (line.startswith("🟢 Current open P/L") or line.startswith("🔴 Current open P/L") or line.startswith("⚪ Current open P/L")):wallet_summary.append(line); continue
            if skipping and (line.startswith("🟢 Historical realized P/L") or line.startswith("🔴 Historical realized P/L") or line.startswith("⚪ Historical realized P/L")):wallet_summary.append(line); continue
            if skipping and (line.startswith("🟢 Total P/L") or line.startswith("🔴 Total P/L") or line.startswith("⚪ Total P/L")):wallet_summary.append(line); continue
            if skipping and line.startswith("🧭 POSITION ACTION"):
                skipping=False
                if wallet_summary:out.extend(["",*wallet_summary])
                out.append(line); continue
            if not skipping:out.append(line)
        return "\n".join(out)
    dashboard._position_recommendations=_canonical_recommendations; dashboard._merge_wallet_portfolio=_sort_wallet_view; dashboard.real_trading_report=real_trading_report_canonical; dashboard._buy_gate_rows=_canonical_buy_gate_rows; dashboard.market_debug_report=market_debug_report_canonical; dashboard.main_dashboard=_main_dashboard_without_wallet; dashboard._sda_action_icon=_action_icon; dashboard._sda_dashboard_consistency_patched=True; return dashboard
