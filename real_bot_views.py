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
    with ThreadPoolExecutor(max_workers=2) as pool:
        state_future=pool.submit(rb._load_real_state,dashboard)
        worker_future=pool.submit(rb._load_worker_status,dashboard)
        try:
            state,source,load_error=state_future.result(timeout=7.0)
        except Exception as exc:
            state,source,load_error={},"unavailable",f"{type(exc).__name__}: {exc}"
        try:
            worker=worker_future.result(timeout=5.0)
        except Exception:
            worker=None
    state=state if isinstance(state,dict) else {}
    positions=state.get("positions") if isinstance(state.get("positions"),dict) else {}
    wallet=str(rb._wallet_address()).lower()
    if source!="unavailable":
        with ThreadPoolExecutor(max_workers=2) as pool:
            snapshot_future=pool.submit(_fetch_wallet_snapshot)
            verified_future=pool.submit(_verify_trades,state,wallet)
            try:
                snapshot=snapshot_future.result(timeout=45.0)
            except Exception as exc:
                _log(f"wallet snapshot failed: {type(exc).__name__}: {exc}")
                snapshot={"wallet_address":wallet,"native_sda":None,"holdings":[],
                    "token_balances_loaded":False,"market_complete":False,
                    "market_valued_count":0,"market_total_count":0,"errors":[]}
            try:
                verified=verified_future.result(timeout=45.0)
            except Exception as exc:
                _log(f"REAL receipt verification failed: {type(exc).__name__}: {exc}")
                verified=[]
    else:
        snapshot={"wallet_address":wallet,"native_sda":None,"holdings":[],
            "token_balances_loaded":False,"market_complete":False,
            "market_valued_count":0,"market_total_count":0,"errors":[]}
        verified=[]
    wallet=str(snapshot.get("wallet_address") or wallet).lower()
    verified_buys=[r for r in verified if r["action"]=="BUY" and r.get("verified")]
    verified_sells=[r for r in verified if r["action"]=="SELL" and r.get("verified")]
    unverified_buys=[r for r in verified if r["action"]=="BUY" and not r.get("verified")]
    unverified_sells=[r for r in verified if r["action"]=="SELL" and not r.get("verified")]
    verified_buy_by_tx={str(r["trade"].get("tx") or r["trade"].get("tx_hash") or "").lower():r
                        for r in verified_buys if r.get("token_amount") is not None}
    holdings={str(h.get("address") or "").lower():h for h in snapshot.get("holdings",[]) if isinstance(h,dict)}
    open_positions=[]
    for token,position in positions.items():
        if not isinstance(position,dict):
            continue
        address=str(token).lower()
        holding=holdings.get(address)
        if not holding or Decimal(str(holding.get("amount") or 0))<=0:
            continue
        label=_symbol(address,position.get("symbol"))
        buy_hash=str(position.get("buy_tx") or "").lower()
        buy_record=verified_buy_by_tx.get(buy_hash)
        buy_verified=buy_record is not None
        actual_amount=Decimal(str(holding.get("amount") or 0))
        verified_buy_amount=Decimal(str(buy_record.get("token_amount"))) if buy_record is not None else Decimal(0)
        balance_matches=(buy_verified and verified_buy_amount>0 and
            abs(actual_amount-verified_buy_amount)<=max(Decimal("1e-18"),verified_buy_amount*Decimal("1e-5")))
        cost=rb._finite_number(position.get("amount_sda",position.get("cost_sda")))
        value=rb._finite_number(holding.get("market_value_sda"))
        complete_basis=bool(balance_matches and buy_verified and cost is not None and cost>0)
        pnl=value-cost if value is not None and complete_basis else None
        pct=pnl/cost*100 if pnl is not None and cost else None
        open_positions.append({"token":address,"symbol":label,"amount":holding.get("amount"),
            "value_sda":value,"cost_sda":cost if complete_basis else None,"pnl_sda":pnl,
            "pnl_pct":pct,"valued":value is not None,"balance_matches":balance_matches,
            "buy_verified":buy_verified})
    open_value_rows=[p["value_sda"] for p in open_positions if p["value_sda"] is not None]
    open_cost_rows=[p["cost_sda"] for p in open_positions if p["cost_sda"] is not None]
    open_pnl_rows=[p["pnl_sda"] for p in open_positions if p["pnl_sda"] is not None]
    open_complete=(len(open_positions)>0 and len(open_value_rows)==len(open_positions)
        and len(open_cost_rows)==len(open_positions) and len(open_pnl_rows)==len(open_positions))
    open_value=sum(open_value_rows) if open_value_rows else None
    open_cost=sum(open_cost_rows) if open_cost_rows else None
    open_pnl=sum(open_pnl_rows) if open_pnl_rows else None
    sell_records=[]
    for record in verified_sells:
        trade=record["trade"]
        sell_records.append({"symbol":_symbol(str(trade.get("token") or "").lower(),trade.get("symbol")),
            "at":trade.get("at") or trade.get("timestamp"),"proceeds_sda":record.get("proceeds_sda"),
            "cost_sda":record.get("cost_sda"),"pnl_sda":record.get("realized_pnl_sda"),"verified":True})
    buy_records=[]
    for record in verified_buys:
        trade=record["trade"]
        buy_records.append({"symbol":_symbol(str(trade.get("token") or "").lower(),trade.get("symbol")),
            "at":trade.get("at") or trade.get("timestamp"),
            "invested_sda":rb._finite_number(record.get("sda_transfer")),"verified":True})
    def sort_time(row):
        return rb._parse_utc(row.get("at")) or datetime.min.replace(tzinfo=timezone.utc)
    buy_records.sort(key=sort_time,reverse=True)
    sell_records.sort(key=sort_time,reverse=True)
    pnl_records=[r for r in sell_records if r["pnl_sda"] is not None]
    pnl_values=[r["pnl_sda"] for r in pnl_records]
    wins=sum(value>0 for value in pnl_values)
    losses=sum(value<0 for value in pnl_values)
    closed_count=len(verified_sells)
    win_rate=wins/(wins+losses)*100 if wins+losses else None
    average_win=sum(value for value in pnl_values if value>0)/wins if wins else None
    average_loss=sum(value for value in pnl_values if value<0)/losses if losses else None
    realized=sum(pnl_values) if pnl_values else (None if verified_sells else 0.0)
    invested_values=[r["invested_sda"] for r in buy_records]
    total_invested=sum(invested_values) if invested_values and all(v is not None for v in invested_values) else None
    realized_partial=bool(unverified_sells or unverified_buys or len(pnl_records)!=len(verified_sells))
    total_pnl=open_pnl+realized if open_pnl is not None and realized is not None else None
    total_partial=(total_pnl is not None and (realized_partial or not open_complete or source!="authoritative"))
    # The REAL Telegram portfolio mirrors the bot's active REAL positions,
    # not every ERC-20 dust/airdrop balance returned by the explorer. Wallet
    # tokens outside the active position ledger (including spam) are excluded.
    native=rb._finite_number(snapshot.get("native_sda"))
    portfolio_position_values=[p["value_sda"] for p in open_positions if p.get("value_sda") is not None]
    portfolio_valued_count=len(portfolio_position_values)
    portfolio_total_count=len(open_positions)
    known_open_value=sum(portfolio_position_values)
    portfolio_value=(native+known_open_value) if native is not None else None
    portfolio_complete=(native is not None and snapshot.get("token_balances_loaded")
        and portfolio_valued_count==portfolio_total_count)
    return {"source":source,"load_error":load_error,"worker":worker,"worker_status":rb._status(worker),
        "state":state,"snapshot":snapshot,"positions":open_positions,"buy_records":buy_records,
        "sell_records":sell_records,"buy_count":len(verified_buys),"sell_count":len(verified_sells),
        "unverified_buy_count":len(unverified_buys),"unverified_sell_count":len(unverified_sells),
        "closed_count":closed_count,"wins":wins,"losses":losses,"win_rate":win_rate,
        "average_win":average_win,"average_loss":average_loss,"best_trade":max(pnl_values) if pnl_values else None,
        "worst_trade":min(pnl_values) if pnl_values else None,"total_invested":total_invested,
        "open_value":open_value,"open_cost":open_cost,"open_pnl":open_pnl,"open_complete":open_complete,
        "realized_pnl":realized,"realized_partial":realized_partial,"total_pnl":total_pnl,
        "total_partial":total_partial,"portfolio_value":portfolio_value,"portfolio_complete":portfolio_complete,
        "portfolio_valued_count":portfolio_valued_count,
        "portfolio_total_count":portfolio_total_count,
        "native_sda":native,"holdings":snapshot.get("holdings",[])}

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
