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

def _read_only_buy_market_quote(token,decimals,wallet):
    from real_market_price import read_only_pinet_swap_buy_reference_quote
    return read_only_pinet_swap_buy_reference_quote(token,decimals,wallet)


def _read_only_buy_price(token,decimals,wallet):
    return _read_only_buy_market_quote(token,decimals,wallet)["price_sda"]

def _price_holding(holding,wallet,analysis=None):
    """Apply the shared fresh market mark; executable SELL quotes are never used."""
    address=str(holding.get("address") or "").lower()
    symbol=str(holding.get("symbol") or address[:10])
    amount=Decimal(str(holding.get("amount") or 0))
    if address==_WSDA:
        holding.update({"price_sda":Decimal(1),"market_value_sda":amount,
            "price_timestamp":datetime.now(timezone.utc).isoformat(),"price_age_seconds":0,
            "price_stale":False,"price_source":"WSDA 1:1","market_error":None})
        return holding
    from real_market_price import resolve_market_price
    def reference_quote(token):
        quote=_read_only_buy_market_quote(token,int(holding["decimals"]),wallet)
        return {"price_sda":str(quote["price_sda"]),"timestamp":datetime.now(timezone.utc).isoformat(),
            "source":"PinetSwap 1 SDA BUY-side reference quote",
            "quote_amount_sda":1,"fee_tier":quote["fee_tier"]}
    market=resolve_market_price(address,analysis,now=datetime.now(timezone.utc),
        supabase_lookup=rb._newest_supabase_price,reference_quote_lookup=reference_quote)
    if market is not None:
        price=Decimal(str(market["price_sda"]))
        holding.update({"price_sda":price,"market_value_sda":amount*price,
            "price_timestamp":market["timestamp"],"price_age_seconds":market["age_seconds"],
            "price_stale":False,"price_source":market["source"],
            "price_fee_tier":market.get("fee_tier"),"market_error":None})
        return holding
    holding["market_value_sda"]=None
    holding["price_sda"]=None
    holding["price_stale"]=True
    holding["market_error"]="no fresh market trade or live PinetSwap BUY reference quote"
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
        with ThreadPoolExecutor(max_workers=min(4,len(priced))) as pool:
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

def _tracked_wallet_snapshot(accounting, dashboard=None):
    """Value current bot contracts from the same market source used by SELL V2."""
    expected = {
        str(item.get("token") or "").lower()
        for item in (accounting.get("open_positions") or [])
        if isinstance(item, dict) and item.get("token")
    }
    runtime_positions = {}
    analysis = {}
    if dashboard is not None:
        try:
            runtime_state = dashboard.load("real_trade_state.json", {})
            runtime_positions = runtime_state.get("positions", {}) if isinstance(runtime_state, dict) else {}
        except Exception:
            runtime_positions = {}
        try:
            analysis = rb._remote_json(dashboard, "market_analysis.json")
        except Exception:
            try:
                analysis = dashboard.load("market_analysis.json", {})
            except Exception:
                analysis = {}
    if isinstance(runtime_positions, dict):
        for token, position in runtime_positions.items():
            if not isinstance(position, dict):
                continue
            quantity = position.get("remaining_quantity")
            if quantity is None:
                quantity = position.get("token_amount_raw")
            try:
                has_quantity = Decimal(str(quantity)) > 0
            except Exception:
                has_quantity = False
            if has_quantity:
                expected.add(str(position.get("token") or token).lower())
    try:
        snapshot = _ORIGINAL_FETCH_WALLET_SNAPSHOT(include_prices=False)
    except Exception as exc:
        return {"native_sda": None, "holdings": [], "token_balances_loaded": False,
                "tracked_expected_count": len(expected), "tracked_missing_contracts": sorted(expected),
                "market_complete": False, "errors": [f"{type(exc).__name__}: {str(exc)[:160]}"]}
    if not isinstance(snapshot, dict):
        snapshot = {}
    all_holdings = snapshot.get("holdings")
    if not isinstance(all_holdings, list):
        all_holdings = []
    wsda_present = any(isinstance(item, dict)
                       and str(item.get("address") or "").lower() == _WSDA
                       for item in all_holdings)
    tracked_addresses = set(expected)
    if wsda_present:
        tracked_addresses.add(_WSDA)
    tracked = [item for item in all_holdings if isinstance(item, dict)
               and str(item.get("address") or "").lower() in tracked_addresses]
    wallet = str(snapshot.get("wallet_address") or rb._wallet_address()).lower()
    if tracked:
        with ThreadPoolExecutor(max_workers=min(4, len(tracked))) as pool:
            tracked = list(pool.map(lambda item: _price_holding(item, wallet, analysis), tracked))
    present = {str(item.get("address") or "").lower() for item in tracked}
    missing = sorted(expected - present)
    valued = sum(item.get("market_value_sda") is not None for item in tracked)
    snapshot["holdings"] = tracked
    snapshot["tracked_expected_count"] = len(tracked_addresses)
    snapshot["tracked_missing_contracts"] = missing
    snapshot["market_valued_count"] = valued
    snapshot["market_complete"] = valued == len(tracked_addresses) and not missing
    snapshot["token_balances_loaded"] = bool(snapshot.get("token_balances_loaded"))
    snapshot["market_price_method"] = "shared scanner/Supabase fresh trade; fallback PinetSwap 1 SDA BUY reference eth_call"
    return snapshot



def _build_dashboard_data(dashboard):
    # Accounting is blockchain/FIFO-derived; wallet valuation is refreshed
    # live and excludes unverified external receipts, airdrops and unmatched sells.
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
    accounting_positions={str(p.get("token") or "").lower():p for p in (accounting.get("open_positions") or []) if isinstance(p,dict) and p.get("token")}
    try:
        runtime_state=dashboard.load("real_trade_state.json",{})
    except Exception:
        runtime_state={}
    runtime_positions=runtime_state.get("positions",{}) if isinstance(runtime_state,dict) else {}
    try:
        metadata=dashboard.load("token_metadata.json",{})
    except Exception:
        metadata={}
    holdings_by_address={}
    snapshot=_tracked_wallet_snapshot(accounting, dashboard)
    holdings=snapshot.get("holdings") if isinstance(snapshot.get("holdings"),list) else []
    for item in holdings:
        if isinstance(item,dict):
            holdings_by_address[str(item.get("address") or "").lower()]=item
    position_source=[]
    if isinstance(runtime_positions,dict):
        for token,position in runtime_positions.items():
            if isinstance(position,dict):
                try:
                    raw=position.get("remaining_quantity")
                    if raw is None:
                        raw=position.get("token_amount_raw")
                    raw_quantity=Decimal(str(raw or 0))
                except Exception:
                    raw_quantity=Decimal(0)
                if raw_quantity>0:
                    position_source.append((str(position.get("token") or token).lower(),position,True))
    if not position_source:
        position_source=[(str(p.get("token") or "").lower(),p,False) for p in (accounting.get("open_positions") or []) if isinstance(p,dict) and p.get("token")]
    positions=[]
    for token,position,is_runtime in position_source:
        if not token: continue
        meta=metadata.get(token,{}) if isinstance(metadata,dict) else {}
        meta=meta if isinstance(meta,dict) else {}
        symbol=str(meta.get("symbol") or position.get("symbol") or "UNKNOWN")
        holding=holdings_by_address.get(token)
        decimals=meta.get("decimals",holding.get("decimals") if holding else None)
        try:
            decimals=int(decimals)
            raw=position.get("remaining_quantity")
            if raw is None:
                raw=position.get("token_amount_raw")
            raw_quantity=Decimal(str(raw))
            quantity=raw_quantity/(Decimal(10)**decimals)
        except Exception:
            quantity=Decimal(str(position.get("quantity") or position.get("amount") or 0))
        accounting_position=accounting_positions.get(token,{})
        cost_raw=(accounting_position.get("cost_basis_sda") if accounting_position else None)
        if cost_raw is None:
            cost_raw=position.get("remaining_cost_basis",position.get("amount_sda",position.get("cost_sda")))
        cost=rb._finite_number(cost_raw)
        price=rb._finite_number(holding.get("price_sda")) if holding else None
        value=(float(quantity)*price if price is not None and quantity>0 else
               rb._finite_number(holding.get("market_value_sda")) if holding and not is_runtime else None)
        pnl=value-cost if value is not None and cost is not None else None
        pnl_pct=pnl/cost*100 if pnl is not None and cost and cost>0 else None
        positions.append({"token":token,"symbol":symbol,"amount":str(quantity),
            "value_sda":value,"cost_sda":cost,"pnl_sda":pnl,"pnl_pct":pnl_pct,
            "price_sda":price,"price_timestamp":holding.get("price_timestamp") if holding else None,
            "price_age_seconds":holding.get("price_age_seconds") if holding else None,
            "price_source":holding.get("price_source") if holding else None,
            "valued":value is not None,"balance_matches":True,"buy_verified":True})
    market_count=sum(item.get("market_value_sda") is not None for item in holdings)
    token_total=sum((float(item["market_value_sda"]) for item in holdings
                     if item.get("market_value_sda") is not None),0.0)
    native=rb._finite_number(snapshot.get("native_sda"))
    portfolio=native+token_total if native is not None else None
    expected_count=int(snapshot.get("tracked_expected_count") or 0)
    portfolio_total_count=1+expected_count
    portfolio_valued_count=(1 if native is not None else 0)+market_count
    portfolio_complete=(native is not None and bool(snapshot.get("token_balances_loaded"))
                        and bool(snapshot.get("market_complete"))
                        and not snapshot.get("tracked_missing_contracts"))
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
    buy_values=[x["invested_sda"] for x in buy_records]
    open_values=[x["value_sda"] for x in positions if x["value_sda"] is not None]
    open_costs=[x["cost_sda"] for x in positions if x["cost_sda"] is not None]
    live_open_pnl=sum(x["pnl_sda"] for x in positions if x["pnl_sda"] is not None) if positions and all(x["pnl_sda"] is not None for x in positions) else None
    live_open_value=sum(open_values) if open_values else 0.0
    live_open_cost=sum(open_costs) if open_costs else 0.0
    live_open_complete=complete and len(positions)>0 and all(x["value_sda"] is not None and x["pnl_sda"] is not None for x in positions)
    best=stats.get("best_trade") if isinstance(stats.get("best_trade"),dict) else {}
    worst=stats.get("worst_trade") if isinstance(stats.get("worst_trade"),dict) else {}
    return {"source":"blockchain_fifo","load_error":None,"worker":worker,
        "worker_status":rb._status(worker),"state":accounting,"snapshot":snapshot,
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
        "open_value":live_open_value,
        "open_cost":live_open_cost,
        "open_pnl":live_open_pnl,
        "open_complete":live_open_complete,
        "realized_pnl":rb._finite_number(accounting.get("realized_pnl_sda")),
        "realized_partial":not complete,
        "total_pnl":(rb._finite_number(accounting.get("realized_pnl_sda"))+live_open_pnl) if live_open_pnl is not None and rb._finite_number(accounting.get("realized_pnl_sda")) is not None else None,
        "total_partial":not (complete and live_open_complete),"portfolio_value":portfolio,
        "portfolio_complete":portfolio_complete,
        "portfolio_valued_count":portfolio_valued_count,
        "portfolio_total_count":portfolio_total_count,
        "portfolio_token_value":token_total,
        "portfolio_missing_contracts":snapshot.get("tracked_missing_contracts") or [],
        "native_sda":native,"holdings":holdings,
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
        [{"text":"🔴 SELL","callback_data":"REAL_BOT_SELL"},{"text":"🎯 SELL V2","callback_data":"REAL_BOT_SELL_V2"}],
        [{"text":"📈 Statistics","callback_data":"REAL_BOT_STATS"},{"text":"🔄 Refresh","callback_data":"REAL_BOT_REFRESH"}],
        [{"text":"⬅️ Back","callback_data":"MAIN"}],
    ]}

def _submenu_keyboard(view):
    rows=[]
    if view=="portfolio":
        rows.append([{"text":"🔄 Refresh","callback_data":"REAL_BOT_PORTFOLIO_REFRESH"}])
    elif view=="sell_v2":
        rows.append([{"text":"🔄 Refresh","callback_data":"REAL_BOT_SELL_V2_REFRESH"}])
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
        f"📈 Bot PnL {total}" if data.get("total_pnl") is not None else "📈 Bot PnL N/A",
        f"📊 Open PnL {open_pnl}",
        f"💵 Realized {realized}" if data.get("realized_pnl") is not None else "💵 Realized N/A",
        f"📦 Positions {len(data.get('positions') or [])}"])

def _portfolio_text(data):
    lines=["📊 PORTFOLIO"]
    total=data.get("portfolio_value")
    if total is None: lines.append("💰 Tracked wallet total unavailable")
    else: lines.append(f"💰 Tracked wallet total {total:.4f} SDA" + ("" if data.get("portfolio_complete") else " (partial)"))
    native=data.get("native_sda")
    lines.append(f"💵 Native SDA {native:.4f} SDA" if native is not None else "💵 Native SDA unavailable")
    lines += ["","Verified open BOT positions"]
    for p in data.get("positions") or []:
        value,pnl=p.get("value_sda"),p.get("pnl_sda")
        if value is None:lines.append(f"⚪ {p['symbol']} unavailable");continue
        if pnl is None:lines.append(f"⚪ {p['symbol']} {value:.2f} SDA PnL unavailable");continue
        icon="🟢" if pnl>=0 else "🔴"
        pct=f" ({p['pnl_pct']:+.1f}%)" if p.get("pnl_pct") is not None else ""
        price=f" @ {p['price_sda']:.6g}" if p.get("price_sda") is not None else ""
        lines.append(f"{icon} {p['symbol']} {value:.2f} SDA {pnl:+.2f}{pct}{price}")
    if not data.get("positions"):lines.append("No open REAL positions")
    if not data.get("open_complete"):
        valued=sum(p.get("value_sda") is not None for p in data.get("positions") or [])
        lines.append(f"⚠ Partial PnL: {valued}/{len(data.get('positions') or [])} positions valued")
    return "\n".join(lines)

def _date_short(value):
    stamp=rb._parse_utc(value)
    return stamp.strftime("%d.%m") if stamp else ""

def _buy_text(data):
    lines=["🟢 CONFIRMED BOT BUY HISTORY"]
    for item in (data.get("buy_records") or []):
        date=_date_short(item.get("at"));prefix=f"{date} " if date else ""
        lines.append(f"{prefix}{item['symbol']} {_fmt(item.get('invested_sda'))} SDA")
    lines += ["",f"Confirmed bot BUY transactions: {data.get('buy_count',0)}",
        f"Total invested: {_fmt(data.get('total_invested'))} SDA"]
    if data.get("unverified_buy_count"):lines.append(f"⚠ {data['unverified_buy_count']} unverified BUY excluded")
    return "\n".join(lines)

def _sell_text(data):
    lines=["🔴 MATCHED BOT SELL HISTORY"]
    for item in (data.get("sell_records") or []):
        date=_date_short(item.get("at"));prefix=f"{date} " if date else ""
        pnl=item.get("pnl_sda")
        icon="✅" if pnl is not None and pnl>0 else ("❌" if pnl is not None and pnl<0 else "⚪")
        lines.append(f"{icon} {prefix}{item['symbol']} {_fmt(pnl,signed=True)} SDA")
    lines += ["",f"Confirmed matched bot SELL transactions: {data.get('sell_count',0)}",
        f"Wins: {data.get('wins',0)}  Losses: {data.get('losses',0)}",
        f"Realized PnL: {_fmt(data.get('realized_pnl'),signed=True)} SDA"]
    if data.get("unverified_sell_count"):lines.append(f"⚠ {data['unverified_sell_count']} unverified SELL excluded")
    return "\n".join(lines)

def _stats_text(data):
    open_value=f"{_fmt(data.get('open_value'))} SDA"
    if data.get("open_value") is not None and not data.get("open_complete"):open_value+=" (partial)"
    open_pnl=f"{_fmt(data.get('open_pnl'),signed=True)} SDA"
    if data.get("open_pnl") is not None and not data.get("open_complete"):open_pnl+=" (partial)"
    lines=["📈 REAL STATISTICS","","Trades",
        f"Open positions {len(data.get('positions') or [])}",
        f"Confirmed bot BUY {data.get('buy_count',0)}",
        f"Confirmed matched bot SELL {data.get('sell_count',0)}",
        f"Closed trades {data.get('closed_count',0)}","","Results",f"Wins {data.get('wins',0)}",
        f"Losses {data.get('losses',0)}",
        f"Win rate {data['win_rate']:.1f}%" if data.get("win_rate") is not None else "Win rate N/A",
        "","PnL",f"Total invested {_fmt(data.get('total_invested'))} SDA",
        f"Current open value {open_value}",f"Open {open_pnl}",
        f"Realized {_fmt(data.get('realized_pnl'),signed=True)} SDA"+(" (partial)" if data.get("realized_partial") else ""),
        f"Total {_fmt(data.get('total_pnl'),signed=True)} SDA"+(" (partial)" if data.get("total_partial") else ""),
        "","Closed trades",f"Average trade {_fmt(data.get('average_trade'),signed=True)} SDA",
        f"Avg win {_fmt(data.get('average_win'),signed=True)} SDA",
        f"Avg loss {_fmt(data.get('average_loss'),signed=True)} SDA",
        f"Best {data.get('best_trade_label') or 'trade'} {_fmt(data.get('best_trade'),signed=True)} SDA",
        f"Worst {data.get('worst_trade_label') or 'trade'} {_fmt(data.get('worst_trade'),signed=True)} SDA"]
    if data.get("bot_unresolved_transactions"):
        lines += ["","⚠ Unreconciled BOT transactions",*data["bot_unresolved_transactions"]]
    if not data.get("portfolio_complete"):lines += ["",f"⚠ Tracked wallet valuation partial: {data.get('portfolio_valued_count',0)}/{data.get('portfolio_total_count',0)} assets"]
    return "\n".join(lines)

def _sell_v2_text(dashboard):
    """Read-only Swing SELL V2 preview; never calls the live executor."""
    import real_swing_sell_report as report
    def load(path):
        try:
            return rb._remote_json(dashboard, path)
        except Exception:
            try:
                return dashboard.load(path, {})
            except Exception:
                return {}
    state=load(cfg.STATE_FILE)
    if not isinstance(state,dict) or not isinstance(state.get("positions"),dict):
        return "🎯 SELL V2 • READ ONLY\\n\\n⚪ Data temporarily unavailable"
    analysis=load("market_analysis.json")
    metadata=load("token_metadata.json")
    wallet=rb._wallet_address().strip().lower()
    def reference_quote(token):
        item=metadata.get(str(token).lower(),{}) if isinstance(metadata,dict) else {}
        decimals=item.get("decimals") if isinstance(item,dict) else None
        if decimals is None:
            return None
        quote=_read_only_buy_market_quote(token,int(decimals),wallet)
        return {"price_sda":str(quote["price_sda"]),"timestamp":datetime.now(timezone.utc).isoformat(),
            "source":"PinetSwap 1 SDA BUY-side reference quote",
            "quote_amount_sda":1,"fee_tier":quote["fee_tier"]}
    rows=report.build_decision_rows(state, analysis, metadata,
        supabase_lookup=rb._newest_supabase_price,reference_quote_lookup=reference_quote)
    lines=["🎯 SELL V2 • READ ONLY",""]
    if not rows:
        lines.append("⚪ No open REAL positions")
        return "\n".join(lines)
    for row in rows:
        symbol=row["token"]
        entry="N/A" if row["entry_price"] is None else f'{row["entry_price"]:.6g}'
        current="N/A" if row["decision_price"] is None else f'{row["decision_price"]:.6g}'
        pnl="N/A" if row["pnl_pct"] is None else f'{row["pnl_pct"]:+.1f}%'
        icon={
            "HOLD":"🟡","PARTIAL_SELL_20":"🟢","TRAILING_HOLD":"🔵",
            "FULL_SELL_EMERGENCY":"🔴","FULL_SELL_TRAILING":"🔴",
            "FULL_SELL_TP":"🔴","PRICE_UNAVAILABLE":"⚪","SELL_QUARANTINED":"⚠️",
        }[row["action"]]
        value="N/A" if row.get("market_value_sda") is None else f'{row["market_value_sda"]:.2f} SDA'
        lines.append(f'{symbol}  Entry {entry} → Now {current}  Value {value}  {pnl}  {icon} {row["action"]}')
        if row.get("price_source"):
            age=int(row.get("price_age_seconds") or 0)
            lines.append(f'  Price {row["price_source"]} • age {age}s')
        lines.append(f'P:{ "Y" if row["profit_partial_done"] else "N"}  T:{ "Y" if row["trailing_armed"] else "N"}  State: {row["swing_state"]}')
        if row["trailing_armed"]:
            peak="N/A" if row["current_peak"] is None else f'{row["current_peak"]:.6g}'
            exit_price="N/A" if row["trailing_price"] is None else f'{row["trailing_price"]:.6g}'
            lines.append(f'  Peak {peak} | Exit {exit_price}')
    return "\n".join(lines)


def render_view(dashboard,view="main",refresh=False,cache_only=False):
    if view=="sell_v2":
        return _sell_v2_text(dashboard),_submenu_keyboard(view)
    data=_get_data(dashboard,refresh=refresh,cache_only=cache_only)
    if data is None:return "REAL Trading data is not loaded yet. Open REAL Menu or tap Refresh.",_main_keyboard()
    renderer={"main":_main_text,"portfolio":_portfolio_text,"buy":_buy_text,
        "sell":_sell_text,"stats":_stats_text}.get(view,_main_text)
    keyboard=_main_keyboard() if view=="main" else _submenu_keyboard(view)
    return renderer(data),keyboard
