"""Read-only REAL Trading Telegram views and accounting.
This module only reads wallet balances, confirmed receipts and read-only
PinetSwap eth_call quotes. It never prepares, signs or broadcasts a transaction.
"""
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
import real_bot_menu as rb
import real_trading_config as cfg

_CACHE={"at":0.0,"data":None}
_CACHE_TTL=60.0
_ORIGINAL_FETCH_WALLET_SNAPSHOT=rb._fetch_wallet_snapshot
_WSDA=str(cfg.WSDA_ADDRESS).lower()

def clear_cache():
    _CACHE.update({"at":0.0,"data":None})

def _log(message):
    print(f"[REAL_TELEGRAM] {message}")

def _symbol(token,fallback=None):
    try:
        return rb._token_label(token,{"symbol":fallback or ""}).split(" — ",1)[0]
    except Exception:
        return fallback or str(token)[:10]

def _read_only_buy_price(token,decimals,wallet):
    import real_trader
    amount_in=10**18
    _fee,amount_out_raw,_calldata,_deadline=real_trader.quote_best_live_fee(
        cfg.WSDA_ADDRESS,token,token,wallet,amount_in,value_wei=amount_in)
    amount_out=Decimal(int(amount_out_raw))/(Decimal(10)**int(decimals))
    if amount_out<=0:
        raise ValueError("router returned zero token output")
    return Decimal(1)/amount_out

def _price_holding(holding,wallet):
    address=str(holding.get("address") or "").lower()
    symbol=str(holding.get("symbol") or address[:10])
    amount=Decimal(str(holding.get("amount") or 0))
    if address==_WSDA:
        holding.update({"price_sda":Decimal(1),"market_value_sda":amount,
            "price_timestamp":datetime.now(timezone.utc).isoformat(),"price_age_seconds":0,
            "price_stale":False,"price_source":"WSDA 1:1","market_error":None})
        return holding
    fallback={}
    try:
        fallback=rb._newest_supabase_price(address) or {}
    except Exception as fallback_exc:
        _log(f"Supabase price lookup failed symbol={symbol} reason={type(fallback_exc).__name__}: {fallback_exc}")
    fresh_price=rb._finite_number(fallback.get("price_sda"))
    stale=bool(fallback.get("stale"))
    # Prefer a recent observed swap price over a 1 SDA router quote: the latter
    # includes trade-size price impact and is not a spot mark for the whole balance.
    if fresh_price is not None and fresh_price>0 and not stale:
        holding.update({"price_sda":Decimal(str(fresh_price)),
            "market_value_sda":amount*Decimal(str(fresh_price)),
            "price_timestamp":fallback.get("timestamp"),"price_age_seconds":fallback.get("age_seconds"),
            "price_stale":False,"price_source":"Supabase fresh observed swap price","market_error":None})
        return holding
    try:
        price=_read_only_buy_price(address,holding.get("decimals",18),wallet)
        holding.update({"price_sda":price,"market_value_sda":amount*price,
            "price_timestamp":datetime.now(timezone.utc).isoformat(),"price_age_seconds":0,
            "price_stale":False,"price_source":"PinetSwap read-only BUY quote (1 SDA; price impact included)",
            "market_error":None})
        return holding
    except Exception as exc:
        holding["market_value_sda"]=None
        reason=fallback.get("error") or ("stale Supabase price" if stale else "no valid Supabase price row")
        holding["market_error"]=f"{type(exc).__name__}: {str(exc)[:180]}; Supabase: {reason}"
        holding["price_source"]=None
        _log(f"valuation unavailable symbol={symbol} address={address} reason={holding['market_error']}")
        return holding

def _fetch_wallet_snapshot():
    snapshot=_ORIGINAL_FETCH_WALLET_SNAPSHOT(include_prices=False)
    holdings=snapshot.get("holdings") if isinstance(snapshot,dict) else None
    if not isinstance(holdings,list):
        return snapshot
    wallet=str(snapshot.get("wallet_address") or rb._wallet_address()).lower()
    priced=[h for h in holdings if isinstance(h,dict)]
    if priced:
        with ThreadPoolExecutor(max_workers=min(16,len(priced))) as pool:
            updated=list(pool.map(lambda h:_price_holding(h,wallet),priced))
        snapshot["holdings"]=updated
    snapshot["market_valued_count"]=sum(h.get("market_value_sda") is not None for h in priced)
    snapshot["market_complete"]=snapshot["market_valued_count"]==len(priced)
    snapshot["market_total_count"]=len(priced)
    return snapshot

def _verify_trades(state,wallet):
    rows=[x for x in (state.get("trades") or []) if isinstance(x,dict)
          and str(x.get("action") or "").upper() in {"BUY","SELL"}]
    if not rows:
        return []
    def verify(trade):
        try:
            result=rb._verify_trade(trade,wallet)
        except Exception as exc:
            result={"verified":False,"status":"UNVERIFIED","reason":f"{type(exc).__name__}: {str(exc)[:120]}"}
        action=str(trade.get("action") or "").upper()
        record={"trade":trade,**result,"action":action}
        if action=="SELL" and result.get("verified"):
            cost=rb._finite_number(trade.get("cost_sda",trade.get("amount_sda")))
            proceeds=rb._finite_number(result.get("proceeds_sda"))
            record["cost_sda"]=cost
            record["realized_pnl_sda"]=proceeds-cost if proceeds is not None and cost is not None else None
        return record
    with ThreadPoolExecutor(max_workers=min(12,len(rows))) as pool:
        return list(pool.map(verify,rows))

def _build_dashboard_data(dashboard):
    """All REAL Telegram screens consume one blockchain-derived accounting state."""
    try:
        accounting=dashboard.load("real_accounting_state.json",{})
    except Exception as exc:
        accounting={}
        load_error=f"{type(exc).__name__}: {str(exc)[:180]}"
    else:
        load_error=None
    if not isinstance(accounting,dict) or accounting.get("schema_version")!=1:
        return {"source":"unavailable","load_error":load_error or "real_accounting_state.json missing or unsupported",
            "worker":None,"worker_status":"⚪ UNKNOWN","state":{},"snapshot":{},
            "positions":[],"buy_records":[],"sell_records":[],"buy_count":0,"sell_count":0,
            "unverified_buy_count":0,"unverified_sell_count":0,"closed_count":0,"wins":0,"losses":0,
            "win_rate":None,"average_win":None,"average_loss":None,"average_trade":None,
            "best_trade":None,"worst_trade":None,"best_trade_label":None,"worst_trade_label":None,
            "total_invested":None,"open_value":None,"open_cost":None,"open_pnl":None,
            "open_complete":False,"realized_pnl":None,"realized_partial":True,"total_pnl":None,
            "total_partial":True,"portfolio_value":None,"portfolio_complete":False,
            "portfolio_valued_count":0,"portfolio_total_count":0,"native_sda":None,"holdings":[],
            "bot_reconciliation_status":"INCOMPLETE","bot_unresolved_transactions":[]}
    try:
        worker=rb._load_worker_status(dashboard)
    except Exception:
        worker=None
    stats=accounting.get("statistics") if isinstance(accounting.get("statistics"),dict) else {}
    complete=str(accounting.get("bot_reconciliation_status") or "INCOMPLETE").upper()=="COMPLETE"
    positions=[]
    for p in accounting.get("open_positions") or []:
        if not isinstance(p,dict): continue
        positions.append({"token":p.get("token"),"symbol":p.get("symbol") or "UNKNOWN",
            "amount":p.get("quantity"),"value_sda":rb._finite_number(p.get("value_sda")),
            "cost_sda":rb._finite_number(p.get("cost_basis_sda")),
            "pnl_sda":rb._finite_number(p.get("open_pnl_sda")),
            "pnl_pct":rb._finite_number(p.get("pnl_pct")),"valued":p.get("value_sda") is not None,
            "balance_matches":True,"buy_verified":True})
    buy_records=[{"symbol":x.get("symbol") or "UNKNOWN","at":x.get("at"),
        "invested_sda":rb._finite_number(x.get("invested_sda")),"tx":x.get("tx")}
        for x in (accounting.get("buy_transactions") or []) if isinstance(x,dict)]
    buy_records.sort(key=lambda x:str(x.get("at") or ""),reverse=True)
    sell_records=[{"symbol":x.get("symbol") or "UNKNOWN","at":x.get("at"),
        "proceeds_sda":rb._finite_number(x.get("proceeds_sda")),
        "cost_sda":rb._finite_number(x.get("cost_basis_sda")),
        "pnl_sda":rb._finite_number(x.get("net_pnl_sda")),"tx":x.get("tx"),"verified":True}
        for x in (accounting.get("sell_transactions") or []) if isinstance(x,dict)]
    unresolved=accounting.get("bot_unresolved_transactions") or []
    wallet_meta=accounting.get("wallet_valuation") if isinstance(accounting.get("wallet_valuation"),dict) else {}
    buy_values=[x["invested_sda"] for x in buy_records]
    open_values=[x["value_sda"] for x in positions if x["value_sda"] is not None]
    open_costs=[x["cost_sda"] for x in positions if x["cost_sda"] is not None]
    best=stats.get("best_trade") if isinstance(stats.get("best_trade"),dict) else {}
    worst=stats.get("worst_trade") if isinstance(stats.get("worst_trade"),dict) else {}
    return {"source":"blockchain_fifo","load_error":None,"worker":worker,
        "worker_status":rb._status(worker),"state":accounting,"snapshot":{},
        "positions":positions,"buy_records":buy_records,"sell_records":sell_records,
        "buy_count":int(stats.get("confirmed_buys_count") or len(buy_records)),
        "sell_count":int(stats.get("matched_sells_count") or len(sell_records)),
        "unverified_buy_count":0,"unverified_sell_count":len(unresolved),
        "closed_count":int(stats.get("matched_sells_count") or len(sell_records)),
        "wins":int(stats.get("wins") or 0),"losses":int(stats.get("losses") or 0),
        "win_rate":rb._finite_number(stats.get("win_rate_pct")),
        "average_win":rb._finite_number(stats.get("average_win_sda")),
        "average_loss":rb._finite_number(stats.get("average_loss_sda")),
        "average_trade":rb._finite_number(stats.get("average_trade_sda")),
        "best_trade":rb._finite_number(best.get("net_pnl_sda")),
        "worst_trade":rb._finite_number(worst.get("net_pnl_sda")),
        "best_trade_label":best.get("symbol"),"worst_trade_label":worst.get("symbol"),
        "total_invested":sum(buy_values) if buy_values and all(x is not None for x in buy_values) else None,
        "open_value":sum(open_values) if open_values else 0.0,
        "open_cost":sum(open_costs) if open_costs else 0.0,
        "open_pnl":rb._finite_number(accounting.get("open_pnl_sda")),
        "open_complete":complete and all(p["value_sda"] is not None and p["pnl_sda"] is not None for p in positions),
        "realized_pnl":rb._finite_number(accounting.get("realized_pnl_sda")),
        "realized_partial":not complete,
        "total_pnl":rb._finite_number(accounting.get("total_pnl_sda")),
        "total_partial":not complete,"portfolio_value":rb._finite_number(accounting.get("wallet_value_sda")),
        "portfolio_complete":bool(accounting.get("wallet_value_complete")),
        "portfolio_valued_count":int(wallet_meta.get("valued_assets_count") or 0),
        "portfolio_total_count":int(wallet_meta.get("total_assets_count") or 0),
        "native_sda":rb._finite_number(accounting.get("native_balance_sda")),"holdings":[],
        "bot_reconciliation_status":accounting.get("bot_reconciliation_status"),
        "bot_unresolved_transactions":unresolved}

def _get_data(dashboard,refresh=False,cache_only=False):
    now=time.monotonic()
    if not refresh and _CACHE.get("data") is not None:
        if cache_only or now-float(_CACHE.get("at") or 0)<=_CACHE_TTL:
            return _CACHE["data"]
    if cache_only:
        return _CACHE.get("data")
    data=_build_dashboard_data(dashboard)
    _CACHE.update({"at":time.monotonic(),"data":data})
    return data

def _main_keyboard():
    return {"inline_keyboard":[
        [{"text":"📊 Portfolio","callback_data":"REAL_BOT_PORTFOLIO"},{"text":"🟢 BUY","callback_data":"REAL_BOT_BUY"}],
        [{"text":"🔴 SELL","callback_data":"REAL_BOT_SELL"},{"text":"📈 Statistics","callback_data":"REAL_BOT_STATS"}],
        [{"text":"🔄 Refresh","callback_data":"REAL_BOT_REFRESH"}],
        [{"text":"⬅️ Back","callback_data":"MAIN"}],
    ]}

def _submenu_keyboard(view):
    rows=[]
    if view=="portfolio":
        rows.append([{"text":"🔄 Refresh","callback_data":"REAL_BOT_PORTFOLIO_REFRESH"}])
    rows.append([{"text":"⬅️ REAL Menu","callback_data":"REAL_BOT_BACK"}])
    return {"inline_keyboard":rows}

def _fmt(value,signed=False):
    if value is None:return "N/A"
    return f"{float(value):+.2f}" if signed else f"{float(value):.2f}"

def _main_text(data):
    status=data.get("worker_status") or "⚪ UNKNOWN"
    status_icon="🟢" if "LIVE" in status else ("🟡" if "DRY RUN" in status else "🔴")
    if data.get("portfolio_value") is None: portfolio="unavailable"
    elif data.get("portfolio_complete"): portfolio=f"{data['portfolio_value']:.2f} SDA"
    else: portfolio=f"≥{data['portfolio_value']:.2f} SDA ({data['portfolio_valued_count']}/{data['portfolio_total_count']})"
    total=f"{_fmt(data.get('total_pnl'),signed=True)} SDA"
    if data.get("total_pnl") is not None and data.get("total_partial"):total+=" (partial)"
    realized=f"{_fmt(data.get('realized_pnl'),signed=True)} SDA"
    if data.get("realized_partial") and data.get("realized_pnl") is not None:realized+=" (partial)"
    open_pnl=f"{_fmt(data.get('open_pnl'),signed=True)} SDA"
    if data.get("open_pnl") is not None and not data.get("open_complete"):open_pnl+=" (partial)"
    return "\n".join([f"🤖 REAL TRADING {status_icon}","",f"💰 Portfolio {portfolio}",
        f"📈 Total PnL {total}" if data.get("total_pnl") is not None else "📈 Total PnL N/A",
        f"📊 Open PnL {open_pnl}",
        f"💵 Realized {realized}" if data.get("realized_pnl") is not None else "💵 Realized N/A",
        f"📦 Positions {len(data.get('positions') or [])}"])

def _portfolio_text(data):
    lines=["📊 PORTFOLIO"]
    for p in data.get("positions") or []:
        value,pnl=p.get("value_sda"),p.get("pnl_sda")
        if value is None:lines.append(f"⚪ {p['symbol']} unavailable");continue
        if pnl is None:lines.append(f"⚪ {p['symbol']} {value:.2f} SDA PnL unavailable");continue
        icon="🟢" if pnl>=0 else "🔴"
        pct=f" ({p['pnl_pct']:+.1f}%)" if p.get("pnl_pct") is not None else ""
        lines.append(f"{icon} {p['symbol']} {value:.2f} SDA {pnl:+.2f}{pct}")
    if not data.get("positions"):lines.append("No open REAL positions")
    if not data.get("open_complete"):
        valued=sum(p.get("value_sda") is not None for p in data.get("positions") or [])
        lines.append(f"⚠ Partial PnL: {valued}/{len(data.get('positions') or [])} positions valued")
    return "\n".join(lines)

def _date_short(value):
    stamp=rb._parse_utc(value)
    return stamp.strftime("%d.%m") if stamp else ""

def _buy_text(data):
    lines=["🟢 BUY HISTORY"]
    for item in (data.get("buy_records") or [])[:18]:
        date=_date_short(item.get("at"));prefix=f"{date} " if date else ""
        lines.append(f"{prefix}{item['symbol']} {_fmt(item.get('invested_sda'))} SDA")
    lines += ["",f"BUY transactions: {data.get('buy_count',0)}",
        f"Total invested: {_fmt(data.get('total_invested'))} SDA"]
    if data.get("unverified_buy_count"):lines.append(f"⚠ {data['unverified_buy_count']} unverified BUY excluded")
    return "\n".join(lines)

def _sell_text(data):
    lines=["🔴 SELL HISTORY"]
    for item in (data.get("sell_records") or [])[:18]:
        date=_date_short(item.get("at"));prefix=f"{date} " if date else ""
        pnl=item.get("pnl_sda")
        icon="✅" if pnl is not None and pnl>0 else ("❌" if pnl is not None and pnl<0 else "⚪")
        lines.append(f"{icon} {prefix}{item['symbol']} {_fmt(pnl,signed=True)} SDA")
    lines += ["",f"SELL transactions: {data.get('sell_count',0)}",
        f"Wins: {data.get('wins',0)}  Losses: {data.get('losses',0)}",
        f"Realized PnL: {_fmt(data.get('realized_pnl'),signed=True)} SDA"]
    if data.get("unverified_sell_count"):lines.append(f"⚠ {data['unverified_sell_count']} unverified SELL excluded")
    return "\n".join(lines)

def _stats_text(data):
    open_value=f"{_fmt(data.get('open_value'))} SDA"
    if data.get("open_value") is not None and not data.get("open_complete"):open_value+=" (partial)"
    open_pnl=f"{_fmt(data.get('open_pnl'),signed=True)} SDA"
    if data.get("open_pnl") is not None and not data.get("open_complete"):open_pnl+=" (partial)"
    lines=["📈 REAL STATISTICS","","Trades",f"BUY {data.get('buy_count',0)}",
        f"SELL {data.get('sell_count',0)}",f"Open {len(data.get('positions') or [])}",
        f"Closed {data.get('closed_count',0)}","","Results",f"Wins {data.get('wins',0)}",
        f"Losses {data.get('losses',0)}",
        f"Win rate {data['win_rate']:.1f}%" if data.get("win_rate") is not None else "Win rate N/A",
        "","PnL",f"Total invested {_fmt(data.get('total_invested'))} SDA",
        f"Current open value {open_value}",f"Open {open_pnl}",
        f"Realized {_fmt(data.get('realized_pnl'),signed=True)} SDA"+(" (partial)" if data.get("realized_partial") else ""),
        f"Total {_fmt(data.get('total_pnl'),signed=True)} SDA"+(" (partial)" if data.get("total_partial") else ""),
        "","Closed trades",f"Avg win {_fmt(data.get('average_win'),signed=True)} SDA",
        f"Avg loss {_fmt(data.get('average_loss'),signed=True)} SDA",
        f"Best {_fmt(data.get('best_trade'),signed=True)} SDA",
        f"Worst {_fmt(data.get('worst_trade'),signed=True)} SDA"]
    if data.get("unverified_sell_count"):lines += ["",f"⚠ {data['unverified_sell_count']} unverified SELL excluded; PnL is partial"]
    if not data.get("portfolio_complete"):lines += ["",f"⚠ Wallet valuation partial: {data.get('portfolio_valued_count',0)}/{data.get('portfolio_total_count',0)} assets"]
    return "\n".join(lines)

def render_view(dashboard,view="main",refresh=False,cache_only=False):
    data=_get_data(dashboard,refresh=refresh,cache_only=cache_only)
    if data is None:return "REAL Trading data is not loaded yet. Open REAL Menu or tap Refresh.",_main_keyboard()
    renderer={"main":_main_text,"portfolio":_portfolio_text,"buy":_buy_text,
        "sell":_sell_text,"stats":_stats_text}.get(view,_main_text)
    keyboard=_main_keyboard() if view=="main" else _submenu_keyboard(view)
    return renderer(data),keyboard
