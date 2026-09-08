# SDA Scanner V16 — hardened canonical entry point
# This module centralizes portfolio valuation/accounting without changing engine's scanner logic.
import json
import math
import os
import engine
from engine import *

STATE_FILE = "decision_state_v15.json"
engine.BUY_THRESHOLD = 75
engine.MIN_TRADES_1H = 3
engine.MAX_NEW_BUYS_PER_RUN = 1


def _n(v, default=0.0):
    try:
        x = default if v is None else float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def _decimals(token, meta):
    m = meta.get(str(token).lower(), {}) if isinstance(meta, dict) else {}
    try:
        d = int(m.get("decimals"))
        return d if 0 <= d <= 36 else None
    except Exception:
        return None


def _normalize_trade_amount(tr, meta):
    raw = _n(tr.get("amount"))
    if raw <= 0:
        return 0.0
    vol = _n(tr.get("cost_sda") if str(tr.get("side")).upper() == "BUY" else tr.get("proceeds_sda"))
    px = _n(tr.get("price_sda"))
    expected = vol / px if vol > 0 and px > 0 else 0.0
    if expected > 0 and raw <= expected * 1000:
        return raw
    decimals = _decimals(tr.get("token"), meta)
    if decimals is None:
        return raw if expected <= 0 else 0.0
    return raw / (10 ** decimals)


def _valuation_status(h):
    if not isinstance(h, dict): return "UNKNOWN"
    px, value, amount = _n(h.get("price_sda")), h.get("value_sda"), _n(h.get("amount"))
    if not (px > 0 and amount > 0 and value is not None): return "UNKNOWN"
    value = _n(value, -1)
    if value < 0: return "INVALID"
    symbol = str(h.get("symbol") or "").upper()
    if "USD" in symbol and not (0.01 <= px <= 10.0): return "INVALID"
    expected = amount * px
    ratio = value / expected if expected > 0 else 0
    return "VALID" if math.isfinite(ratio) and 0.80 <= ratio <= 1.20 else "INVALID"


def _safe_wallet(md, meta):
    w = engine.wallet_snapshot(md, meta)
    if not isinstance(w, dict): return {}
    for h in w.get("holdings", []) or []:
        st = _valuation_status(h)
        h["valuation_status"] = st
        if st != "VALID":
            h["price_sda"] = 0.0
            h["value_sda"] = None
    known = sum(_n(h.get("value_sda")) for h in w.get("holdings", []) if h.get("value_sda") is not None)
    w["total_token_value_sda"] = known
    if w.get("native_sda") is not None:
        w["total_value_sda"] = _n(w.get("native_sda")) + known
    return w


def _rebuild_fifo(p, meta):
    source=[]
    for src in p.get("trades", []) or []:
        if isinstance(src, dict):
            tr=dict(src); tr["amount"]=_normalize_trade_amount(tr, meta)
            if tr["amount"] > 0: source.append(tr)
    lots={}; realized=0.0; matched=excluded=0; rebuilt=[]
    for tr in sorted(source, key=lambda x:(x.get("block_number",x.get("block",0)),str(x.get("timestamp")),str(x.get("tx")))):
        token=str(tr.get("token","")).lower(); side=str(tr.get("side","")).upper()
        if not token: continue
        if side=="BUY":
            lots.setdefault(token,[]).append({"amount":tr["amount"],"cost_sda":max(0,_n(tr.get("cost_sda")))})
            rebuilt.append(tr); continue
        if side!="SELL": continue
        qty=tr["amount"]; removed=0.0; q=lots.setdefault(token,[])
        while qty>1e-12 and q:
            lot=q[0]; take=min(qty,lot["amount"]); unit=lot["cost_sda"]/lot["amount"] if lot["amount"] else 0
            removed += take*unit; lot["amount"]-=take; lot["cost_sda"]=max(0,lot["cost_sda"]-take*unit); qty-=take
            if lot["amount"]<=1e-12:q.pop(0)
        matched_amt=tr["amount"]-qty; proceeds=max(0,_n(tr.get("proceeds_sda")))
        matched_proceeds=proceeds*(matched_amt/tr["amount"]) if tr["amount"] else 0
        tr.update(cost_basis_sda=removed,matched_amount=matched_amt,unmatched_amount=max(0,qty),matched_proceeds_sda=matched_proceeds)
        if matched_amt>=tr["amount"]*0.99: realized += matched_proceeds-removed; matched+=1
        else: excluded+=1
        rebuilt.append(tr)
    current={}
    for token,q in lots.items():
        amount=sum(x["amount"] for x in q); cost=sum(x["cost_sda"] for x in q)
        if amount<=1e-12: continue
        buys=[x for x in source if str(x.get("token","")).lower()==token and str(x.get("side")).upper()=="BUY"]
        current[token]={"amount":amount,"cost_sda":cost,"avg_cost_sda":cost/amount,"lots":q,"symbol":buys[-1].get("symbol",token[:10]+"...") if buys else token[:10]+"..."}
    p["trades"]=rebuilt[-500:]; p["current"]=current; p["realized_pnl_sda"]=realized; p["matched_sell_count"]=matched; p["excluded_unmatched_sell_count"]=excluded
    return p


def portfolio_history_v16(wallet, md, meta, ld, previous=None, wallet_obj=None):
    p=engine.portfolio_history(wallet,md,meta,ld,previous,wallet_obj)
    if not isinstance(p,dict): return p
    p=_rebuild_fifo(p,meta); w=wallet_obj if isinstance(wallet_obj,dict) else _safe_wallet(md,meta)
    holdings={str(h.get("address","")).lower():h for h in w.get("holdings",[]) if h.get("address")}
    for token,cur in p.get("current",{}).items():
        h=holdings.get(token)
        if not h: continue
        cur["amount"]=_n(h.get("amount"),cur.get("amount",0)); cur["symbol"]=h.get("symbol") or cur.get("symbol")
        if _valuation_status(h)=="VALID":
            cur["price_sda"]=_n(h.get("price_sda")); cur["value_sda"]=_n(h.get("value_sda")); cur["unrealized_pnl_sda"]=cur["value_sda"]-_n(cur.get("cost_sda")); cur["unrealized_pnl_pct"]=cur["unrealized_pnl_sda"]/_n(cur.get("cost_sda"))*100 if _n(cur.get("cost_sda")) else None
        else:
            cur["price_sda"]=0.0; cur["value_sda"]=None; cur["unrealized_pnl_sda"]=None; cur["unrealized_pnl_pct"]=None
            cur["cost_basis_status"]="PRICE_UNKNOWN"
    known=[x for x in p.get("current",{}).values() if x.get("unrealized_pnl_sda") is not None]
    p["open_cost_sda"]=sum(_n(x.get("cost_sda")) for x in known); p["open_value_sda"]=sum(_n(x.get("value_sda")) for x in known); p["open_unrealized_pnl_sda"]=sum(_n(x.get("unrealized_pnl_sda")) for x in known); p["known_open_positions"]=len(known); p["known_total_pnl_sda"]=_n(p.get("realized_pnl_sda"))+p["open_unrealized_pnl_sda"]
    return p


def portfolio_recommendations_v16(portfolio,md,ws,meta,ld):
    # Preserve V15 recommendation thresholds/state logic while using the V16 valuation-safe portfolio.
    out=[]; cur=portfolio.get("current",{}) if isinstance(portfolio,dict) else {}; tokens=md.get("tokens",{}) if isinstance(md,dict) else {}
    try:
        with open(STATE_FILE,encoding="utf-8") as f: state=json.load(f)
        if not isinstance(state,dict): state={}
    except Exception: state={}
    nxt={}
    for token,pf in cur.items():
        an=(tokens.get(token,{}) or {}).get("analysis",tokens.get(token,{}) or {}); s=engine.score(token,an,ws) if isinstance(an,dict) and an else {"confidence":0,"m1h":0,"net_1h":0}
        c,m1,flow=_n(s.get("confidence")),_n(s.get("m1h")),_n(s.get("net_1h")); pnl_raw=pf.get("unrealized_pnl_pct"); pnl=_n(pnl_raw)
        old=state.get(token,{}) if isinstance(state.get(token),dict) else {}; neg=int(_n(old.get("negative_count"))); weak=int(_n(old.get("profit_weakening_count")))
        neg=min(5,neg+1) if (c<35 and m1<0 and flow<0) or (pnl<=-15 and m1<0 and flow<0) else 0; weak=min(5,weak+1) if pnl>=8 and (m1<0 or flow<0) else 0; nxt[token]={"negative_count":neg,"profit_weakening_count":weak}
        if pf.get("cost_sda") is None or pnl_raw is None: action="HOLD / NO COST BASIS"; reason="cost basis or valid valuation unavailable"
        elif weak>=2: action="PARTIAL SELL"; reason="profit + weakening confirmed twice"
        elif neg>=3: action="SELL / EXIT"; reason="trend + negative SDA flow confirmed 3 times"
        elif c>=70 and m1>0 and flow>0: action="HOLD / TRAIL"; reason="positive trend and SDA flow"
        else: action="HOLD / WATCH"; reason="no confirmed exit condition"
        out.append({"token":token,"symbol":pf.get("symbol") or engine.lbl(token,meta),"action":action,"reason":reason,"pnl_pct":pnl_raw,"pnl_sda":pf.get("unrealized_pnl_sda"),"score":c,"m1h":m1,"flow_1h":flow})
    tmp=STATE_FILE+".tmp"
    try:
        with open(tmp,"w",encoding="utf-8") as f: json.dump(nxt,f,indent=2,ensure_ascii=False)
        os.replace(tmp,STATE_FILE)
    except Exception: pass
    order={"SELL / EXIT":0,"PARTIAL SELL":1,"HOLD / TRAIL":2,"HOLD / WATCH":3,"HOLD / NO COST BASIS":4}
    return sorted(out,key=lambda x:(order.get(x["action"],9),-_n(x["score"])))


def portfolio_message_v16(portfolio,recommendations):
    meta=engine.load(engine.META_FILE,{})
    cur=portfolio.get("current",{}) if isinstance(portfolio,dict) else {}; lines=["📊 REAL PORTFOLIO","","Current token positions — SDA accumulation","────────────────────────"]
    for token,pf in sorted(cur.items(),key=lambda x:(x[1].get("symbol") or x[0]).lower()):
        m=meta.get(token,{}) or {}; symbol=pf.get("symbol") or m.get("symbol") or token[:10]+"..."; name=m.get("name") or symbol; val=pf.get("value_sda")
        lines += [f"🔹 {symbol} — {name}",f"   {_fmt_amount(pf.get('amount'))} {symbol}  •  {(_n(val):.2f) if val is not None else 'UNKNOWN'} SDA",f"   {'⚪ P/L UNKNOWN' if pf.get('unrealized_pnl_sda') is None else f'P/L {_n(pf.get(\"unrealized_pnl_sda\")):+.2f} SDA ({_n(pf.get(\"unrealized_pnl_pct\")):+.2f}%)'}",""]
    op=portfolio.get("open_unrealized_pnl_sda"); rp=portfolio.get("realized_pnl_sda"); tp=portfolio.get("known_total_pnl_sda"); op_pct=op/_n(portfolio.get("open_cost_sda"))*100 if _n(portfolio.get("open_cost_sda")) else None
    lines += ["────────────────────────",f"{'⚪ Current open P/L UNKNOWN' if op is None else f'Current open P/L {_n(op):+.2f} SDA' + (f' ({op_pct:+.2f}%)' if op_pct is not None else '')}",f"Historical matched P/L {_n(rp):+.2f} SDA",f"Known total P/L {_n(tp):+.2f} SDA",f"Matched sells: {int(_n(portfolio.get('matched_sell_count')))}  •  Excluded unmatched: {int(_n(portfolio.get('excluded_unmatched_sell_count')))}","","🧭 POSITION ACTION"]
    for r in recommendations: lines.append(f"{'🔴' if r['action']=='SELL / EXIT' else '🟠' if r['action']=='PARTIAL SELL' else '🟢' if r['action']=='HOLD / TRAIL' else '🟡'} {r['symbol']}: {r['action']}  •  P/L {(_n(r.get('pnl_sda')) if r.get('pnl_sda') is not None else 'UNKNOWN')} SDA  •  score {int(_n(r['score']))}/100")
    lines += ["","ℹ️ Invalid/inconsistent valuations are UNKNOWN — never a fake loss.","🔒 Wallet is READ-ONLY."]
    return "\n".join(lines)


def wallet_message_v16(w):
    hs=w.get("holdings") or []; lines=["👛 REAL WALLET","",f"💰 SDA: {_n(w.get('native_sda')):.4f}",f"🪙 Token positions: {len(hs)}","────────────────────────"]; total=0
    for h in hs:
        st=_valuation_status(h); val=h.get("value_sda"); px=_n(h.get("price_sda")); symbol=h.get("symbol") or str(h.get("address",""))[:10]+"..."
        if st!="VALID": val=None; px=0
        if val is not None: total+=_n(val)
        lines += [f"🪙 {symbol} — {h.get('name') or symbol}",f"   {_fmt_amount(h.get('amount'))} {symbol}  •  {(_n(val):.2f) if val is not None else 'UNKNOWN'} SDA",f"   Price: {engine.price(px)+' SDA' if px>0 else 'UNKNOWN'}",""]
    lines += ["────────────────────────",f"📊 Known token value: {total:.2f} SDA"]
    if w.get("native_sda") is not None: lines.append(f"💼 TOTAL WALLET VALUE: {_n(w.get('native_sda'))+total:.2f} SDA")
    if w.get("holding_source"): lines.append(f"Source: {w['holding_source']}")
    if w.get("error"): lines += ["",f"⚠️ {str(w['error'])[:900]}"]
    return "\n".join(lines)

engine.portfolio_history=portfolio_history_v16
engine.portfolio_recommendations=portfolio_recommendations_v16
engine.portfolio_message=portfolio_message_v16
engine.wallet_message=wallet_message_v16
portfolio_history=portfolio_history_v16
portfolio_recommendations=portfolio_recommendations_v16
portfolio_message=portfolio_message_v16
wallet_message=wallet_message_v16

if __name__ == "__main__":
    engine.main()
