from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace
import pytest
import real_bot_menu as rb
import real_bot_views as views
import real_trading_config as cfg

XEN="0x"+"1"*40
HAQ="0x"+"2"*40
RIDEX="0x"+"3"*40
BAD="0x"+"4"*40
PCH="0x"+"5"*40
JOINIT="0x"+"6"*40
WALLET=rb.BOT_WALLET_ADDRESS.lower()

def _trade(action,symbol,token,tx,at,amount=20):
    return {"action":action,"symbol":symbol,"token":token,"tx":tx,"at":at,"amount_sda":amount}

def _fixture():
    state={"positions":{
        XEN:{"symbol":"XEN","token":XEN,"amount_sda":20,"buy_tx":"buy-xen","token_amount_raw":100*10**18},
        HAQ:{"symbol":"HAQ","token":HAQ,"amount_sda":20,"buy_tx":"buy-haq","token_amount_raw":200*10**18}},
        "trades":[
        _trade("BUY","XEN",XEN,"buy-xen","2026-11-01T10:00:00+00:00"),
        _trade("BUY","HAQ",HAQ,"buy-haq","2026-11-02T10:00:00+00:00"),
        _trade("BUY","RIDEX",RIDEX,"buy-ridex","2026-11-03T10:00:00+00:00"),
        _trade("BUY","BAD",BAD,"buy-bad","2026-11-04T10:00:00+00:00"),
        _trade("SELL","XEN",XEN,"sell-xen","2026-11-05T10:00:00+00:00"),
        _trade("SELL","RIDEX",RIDEX,"sell-ridex","2026-11-06T10:00:00+00:00"),
        _trade("SELL","PCH",PCH,"sell-pch","2026-11-07T10:00:00+00:00"),
        {"action":"SELL","symbol":"MANUAL","token":BAD,"source":"manual_user_confirmed","amount_sda":20,"at":"2026-11-08T10:00:00+00:00"}]}
    holdings=[
        {"address":XEN,"symbol":"XEN","amount":Decimal("100"),"amount_raw":100*10**18,"decimals":18,"market_value_sda":Decimal("22"),"market_error":None},
        {"address":HAQ,"symbol":"HAQ","amount":Decimal("200"),"amount_raw":200*10**18,"decimals":18,"market_value_sda":Decimal("18"),"market_error":None},
        {"address":cfg.WSDA_ADDRESS.lower(),"symbol":"WSDA","amount":Decimal("1"),"amount_raw":10**18,"decimals":18,"market_value_sda":Decimal("1"),"market_error":None},
        {"address":JOINIT,"symbol":"JOINIT","amount":Decimal("1"),"amount_raw":10**18,"decimals":18,"market_value_sda":None,"market_error":"RPC ERROR execution reverted; raw technical detail"}]
    snapshot={"wallet_address":WALLET,"native_sda":10.0,"holdings":holdings,
        "token_balances_loaded":True,"market_complete":False,"market_valued_count":3,
        "market_total_count":4,"errors":["raw RPC dictionary must never render"]}
    return state,snapshot

@pytest.fixture
def built_data(monkeypatch):
    state,snapshot=_fixture()
    monkeypatch.setattr(rb,"_load_real_state",lambda _d:(state,"authoritative",None))
    monkeypatch.setattr(rb,"_load_worker_status",lambda _d:{"enabled":True,"dry_run":False})
    monkeypatch.setattr(rb,"_status",lambda _w:"🟢 LIVE")
    monkeypatch.setattr(views,"_fetch_wallet_snapshot",lambda:deepcopy(snapshot))
    monkeypatch.setattr(views,"_symbol",lambda _token,fallback=None:fallback or "TOKEN")
    def verify(trade,_wallet):
        tx=trade.get("tx")
        if tx=="buy-xen":return {"verified":True,"status":"VERIFIED","token_amount":Decimal("100"),"sda_transfer":Decimal("20")}
        if tx=="buy-haq":return {"verified":True,"status":"VERIFIED","token_amount":Decimal("200"),"sda_transfer":Decimal("20")}
        if tx=="buy-ridex":return {"verified":True,"status":"VERIFIED","token_amount":Decimal("50"),"sda_transfer":Decimal("20")}
        if tx=="sell-xen":return {"verified":True,"status":"VERIFIED","proceeds_sda":25.0}
        if tx=="sell-ridex":return {"verified":True,"status":"VERIFIED","proceeds_sda":19.0}
        if tx=="sell-pch":return {"verified":True,"status":"VERIFIED","proceeds_sda":22.0}
        return {"verified":False,"status":"UNVERIFIED MANUAL","reason":"missing transaction hash"}
    monkeypatch.setattr(rb,"_verify_trade",verify)
    return views._build_dashboard_data(SimpleNamespace(REMOTE_BASE="https://example.invalid/"))

def test_real_main_menu_is_short_and_has_six_actions(monkeypatch,built_data):
    monkeypatch.setattr(views,"_get_data",lambda *_a,**_k:built_data)
    text,keyboard=views.render_view(None,"main")
    assert "🤖 REAL TRADING" in text
    assert "Portfolio 50.00 SDA" in text
    assert "Total PnL +6.00 SDA (partial)" in text
    assert "Positions 2" in text
    assert "XEN" not in text and "HAQ" not in text and "RIDEX" not in text
    callbacks=[b["callback_data"] for row in keyboard["inline_keyboard"] for b in row]
    assert callbacks==["REAL_BOT_PORTFOLIO","REAL_BOT_BUY","REAL_BOT_SELL","REAL_BOT_STATS","REAL_BOT_REFRESH","MAIN"]

def test_portfolio_shows_only_current_open_real_positions_one_line_each(monkeypatch,built_data):
    monkeypatch.setattr(views,"_get_data",lambda *_a,**_k:built_data)
    text,keyboard=views.render_view(None,"portfolio")
    assert "📊 PORTFOLIO" in text
    assert "XEN 22.00 SDA +2.00 (+10.0%)" in text
    assert "HAQ 18.00 SDA -2.00 (-10.0%)" in text
    assert "RIDEX" not in text and "PCH" not in text and "JOINIT" not in text
    assert "0x" not in text and "RPC" not in text and "execution reverted" not in text
    assert keyboard["inline_keyboard"][0][0]["callback_data"]=="REAL_BOT_PORTFOLIO_REFRESH"
    assert keyboard["inline_keyboard"][-1][0]["callback_data"]=="REAL_BOT_BACK"

def test_only_confirmed_real_buys_count_and_ridex_remains_in_history(built_data):
    assert built_data["buy_count"]==3 and built_data["total_invested"]==60.0
    text=views._buy_text(built_data)
    assert "BUY transactions: 3" in text and "Total invested: 60.00 SDA" in text
    assert "RIDEX" in text and "BAD" not in text

def test_only_confirmed_sells_count_and_failed_sell_is_not_a_loss(built_data):
    assert built_data["sell_count"]==3 and built_data["closed_count"]==3
    assert built_data["wins"]==2 and built_data["losses"]==1
    text=views._sell_text(built_data)
    assert "SELL transactions: 3" in text and "Wins: 2  Losses: 1" in text
    assert "MANUAL" not in text and "unverified SELL excluded" in text

def test_win_rate_average_best_worst_and_realized_pnl(built_data):
    assert built_data["win_rate"]==pytest.approx(66.6666667)
    assert built_data["average_win"]==pytest.approx(3.5)
    assert built_data["average_loss"]==pytest.approx(-1.0)
    assert built_data["best_trade"]==pytest.approx(5.0)
    assert built_data["worst_trade"]==pytest.approx(-1.0)
    assert built_data["realized_pnl"]==pytest.approx(6.0)
    assert built_data["realized_partial"] is True

def test_unrealized_and_total_pnl_are_consistent_and_partial(built_data):
    assert built_data["open_value"]==pytest.approx(40.0)
    assert built_data["open_cost"]==pytest.approx(40.0)
    assert built_data["open_pnl"]==pytest.approx(0.0)
    assert built_data["total_pnl"]==pytest.approx(6.0) and built_data["total_partial"] is True
    stats=views._stats_text(built_data)
    assert "Open +0.00 SDA" in stats
    assert "Realized +6.00 SDA (partial)" in stats
    assert "Total +6.00 SDA (partial)" in stats

def test_statistics_contains_all_required_metrics(built_data):
    text=views._stats_text(built_data)
    for expected in ["BUY 3","SELL 3","Open 2","Closed 3","Wins 2","Losses 1","Win rate 66.7%",
        "Total invested 60.00 SDA","Current open value 40.00 SDA","Avg win +3.50 SDA",
        "Avg loss -1.00 SDA","Best +5.00 SDA","Worst -1.00 SDA"]:
        assert expected in text

def test_wallet_airdrops_and_non_position_tokens_are_excluded_from_real_portfolio(built_data):
    assert len(built_data["positions"])==2
    # Native 10 + open REAL positions XEN 22 and HAQ 18.
    # WSDA and JOINIT are wallet residuals, not active REAL positions.
    assert built_data["portfolio_value"]==pytest.approx(50.0)
    assert built_data["portfolio_complete"] is True
    assert built_data["portfolio_valued_count"]==2 and built_data["portfolio_total_count"]==2
    assert "Portfolio 50.00 SDA" in views._main_text(built_data)
    assert "≥" not in views._main_text(built_data)

def test_raw_technical_errors_never_appear_in_normal_views(built_data):
    for text in (views._main_text(built_data),views._portfolio_text(built_data),views._buy_text(built_data),views._sell_text(built_data),views._stats_text(built_data)):
        assert "RPC ERROR" not in text and "execution reverted" not in text
        assert "raw RPC dictionary" not in text and "0x" not in text

def test_read_only_buy_quote_values_token_without_sell_quote(monkeypatch):
    import real_trader
    monkeypatch.setattr(real_trader,"quote_best_live_fee",
        lambda token_in,token_out,_token,_wallet,amount_in,value_wei:(3000,20*10**18,None,None))
    holding={"address":"0x"+"7"*40,"symbol":"TEST","amount":Decimal("100"),"decimals":18,"market_value_sda":None,"price_stale":True,"price_sda":Decimal("0.5")}
    priced=views._price_holding(holding,WALLET)
    assert priced["market_value_sda"]==Decimal("5") and priced["price_sda"]==Decimal("0.05")
    assert priced["price_source"].startswith("PinetSwap read-only BUY")

def test_wsda_is_valued_one_to_one_without_router_call(monkeypatch):
    import real_trader
    monkeypatch.setattr(real_trader,"quote_best_live_fee",lambda *_a,**_k:(_ for _ in ()).throw(AssertionError("no router call")))
    holding={"address":cfg.WSDA_ADDRESS.lower(),"symbol":"WSDA","amount":Decimal("1.25"),"decimals":18}
    priced=views._price_holding(holding,WALLET)
    assert priced["price_sda"]==Decimal(1) and priced["market_value_sda"]==Decimal("1.25")

def test_stale_supabase_price_is_not_used_when_read_only_quote_fails(monkeypatch):
    monkeypatch.setattr(views,"_read_only_buy_price",lambda *_a:(_ for _ in ()).throw(RuntimeError("private RPC error")))
    monkeypatch.setattr(rb,"_newest_supabase_price",lambda _token:{"price_sda":0.5,"stale":True})
    holding={"address":"0x"+"8"*40,"symbol":"NOPOOL","amount":Decimal("10"),"decimals":18,"price_sda":Decimal("0.5"),"price_stale":True}
    priced=views._price_holding(holding,WALLET)
    assert priced["market_value_sda"] is None and priced["price_source"] is None

def test_confirmed_sell_without_cost_basis_counts_but_pnl_is_na(monkeypatch):
    state,snapshot=_fixture()
    state["trades"].append({"action":"SELL","symbol":"NO_BASIS","token":PCH,"tx":"sell-no-basis","at":"2026-11-08T12:00:00+00:00"})
    monkeypatch.setattr(rb,"_load_real_state",lambda _d:(state,"authoritative",None))
    monkeypatch.setattr(rb,"_load_worker_status",lambda _d:{"enabled":True,"dry_run":False})
    monkeypatch.setattr(rb,"_status",lambda _w:"🟢 LIVE")
    monkeypatch.setattr(views,"_fetch_wallet_snapshot",lambda:deepcopy(snapshot))
    monkeypatch.setattr(views,"_symbol",lambda _token,fallback=None:fallback or "TOKEN")
    def verify(trade,_wallet):
        tx=trade.get("tx")
        if tx=="sell-no-basis":return {"verified":True,"status":"VERIFIED","proceeds_sda":20.0}
        if tx=="buy-xen":return {"verified":True,"status":"VERIFIED","token_amount":Decimal("100"),"sda_transfer":Decimal("20")}
        if tx=="buy-haq":return {"verified":True,"status":"VERIFIED","token_amount":Decimal("200"),"sda_transfer":Decimal("20")}
        if tx=="buy-ridex":return {"verified":True,"status":"VERIFIED","token_amount":Decimal("50"),"sda_transfer":Decimal("20")}
        if tx=="sell-xen":return {"verified":True,"status":"VERIFIED","proceeds_sda":25.0}
        if tx=="sell-ridex":return {"verified":True,"status":"VERIFIED","proceeds_sda":19.0}
        if tx=="sell-pch":return {"verified":True,"status":"VERIFIED","proceeds_sda":22.0}
        return {"verified":False,"status":"UNVERIFIED"}
    monkeypatch.setattr(rb,"_verify_trade",verify)
    data=views._build_dashboard_data(SimpleNamespace())
    assert data["sell_count"]==4 and data["closed_count"]==4
    assert data["wins"]==2 and data["losses"]==1
    assert data["win_rate"]==pytest.approx(66.6666667)
    assert data["realized_pnl"]==pytest.approx(6.0) and data["realized_partial"] is True
    assert any(row["symbol"]=="NO_BASIS" and row["pnl_sda"] is None for row in data["sell_records"])

def test_submenu_and_back_reuse_cache_while_refresh_reloads(monkeypatch,built_data):
    views.clear_cache()
    calls=[]
    def build(_dashboard):
        calls.append("load")
        return built_data
    monkeypatch.setattr(views,"_build_dashboard_data",build)
    dashboard=SimpleNamespace()
    views.render_view(dashboard,"main")
    views.render_view(dashboard,"portfolio",cache_only=True)
    views.render_view(dashboard,"buy",cache_only=True)
    views.render_view(dashboard,"main",cache_only=True)
    assert calls==["load"]
    views.render_view(dashboard,"portfolio",refresh=True)
    assert calls==["load","load"]
    views.clear_cache()
