from types import SimpleNamespace
from decimal import Decimal

import pytest

import real_bot_menu as rb
import real_bot_views as views


POSITION_ROWS = [
    ("XEN", 18.85563804280111, -1.225334957200023622),
    ("DBN", 20.358743854622848, 0.293084854621928774),
    ("DBI", 19.806287205950127, -0.256858294050757037),
    ("IDM", 19.07769086437782, -0.988455135623106044),
    ("PCH", 20.419310183332747, 0.419144790831159232),
    ("FREEt", 20.026326148366508, 0.026160769614920364),
    ("SGWA", 20.453709912231375, 0.453380354731375),
    ("VLCP", 17.215845328477638, -2.784667449172362),
    ("BZST", 19.603389179236153, -0.463184820764779036),
]
SELL_ROWS = [
    ("SDS", -1.389311804706278277),
    ("GLNs", -0.207068953724308303),
    ("DMCS", -0.418518220341676338),
    ("HLMR", -0.160258308835173758),
    ("FBAY", 0.074889385407531477),
    ("HLMR", 4.190738484673996154),
    ("RIDEX", -1.295448529017973710),
    ("FBAY", -0.240310312406703862),
    ("JSFT", 18.018624866103863595),
    ("JSFT", 4.699070555885755263),
    ("RIDEX", -0.369607395991292282),
    ("JSFT", 0.099437438156996876),
    ("HAQ", 9.890209305792412365),
    ("GLNs", -0.331355454339035573),
]


def _accounting_state():
    positions = []
    for i, (symbol, value, pnl) in enumerate(POSITION_ROWS):
        cost = value - pnl
        positions.append({
            "token": "0x" + f"{i+1:040x}",
            "symbol": symbol,
            "quantity": "100",
            "value_sda": str(value),
            "cost_basis_sda": str(cost),
            "open_pnl_sda": str(pnl),
            "pnl_pct": str(pnl / cost * 100),
            "buy_tx": f"buy-{symbol}",
            "at": "2026-11-08T10:00:00Z",
        })
    buys = [{
        "tx": f"buy-{i}",
        "at": f"2026-11-{(i % 9)+1:02d}T10:00:00Z",
        "symbol": ["XEN", "DBN", "DBI", "IDM", "PCH", "FREEt", "SGWA", "VLCP", "BZST"][i % 9],
        "contract": "0x" + f"{i+1:040x}",
        "quantity": "100",
        "invested_sda": "20" if i else "36.5906",
    } for i in range(21)]
    sells = [{
        "tx": f"sell-{i}",
        "at": f"2026-11-{(i % 9)+1:02d}T12:00:00Z",
        "symbol": symbol,
        "net_pnl_sda": str(pnl),
        "proceeds_sda": str(20 + pnl),
        "cost_basis_sda": "20",
        "quantity": "100",
    } for i, (symbol, pnl) in enumerate(SELL_ROWS)]
    return {
        "schema_version": 1,
        "bot_reconciliation_status": "COMPLETE",
        "bot_unresolved_transactions": [],
        "wallet_value_sda": "350.9958932906058869876318570",
        "wallet_value_complete": False,
        "native_balance_sda": "120.499217823303884285",
        "wallet_valuation": {"valued_assets_count": 31, "total_assets_count": 32},
        "realized_pnl_sda": "32.56109105665811362536319366",
        "open_pnl_sda": "-4.526729887011644369",
        "total_pnl_sda": "28.03436116964646925636319366",
        "open_positions": positions,
        "buy_transactions": buys,
        "sell_transactions": sells,
        "statistics": {
            "open_positions_count": 9,
            "confirmed_buys_count": 21,
            "matched_sells_count": 14,
            "wins": 6,
            "losses": 8,
            "win_rate_pct": "42.857142857142854",
            "average_trade_sda": "2.3257922183327224",
            "average_win_sda": "6.1621616726700926",
            "average_loss_sda": "-0.5514848724203053",
            "best_trade": {"symbol": "JSFT", "net_pnl_sda": "18.018624866103863595"},
            "worst_trade": {"symbol": "SDS", "net_pnl_sda": "-1.389311804706278277"},
        },
        "excluded_external_activity": {
            "unmatched_sells": [{"token": "PINE", "sell_tx": "0xexternal"}],
            "unknown_basis_assets": [{"symbol": "PINE"}, {"symbol": "JOINIT"}],
        },
    }


@pytest.fixture
def built_data(monkeypatch):
    state = _accounting_state()
    dashboard = SimpleNamespace(load=lambda path, default: state if path == "real_accounting_state.json" else default)
    monkeypatch.setattr(rb, "_load_worker_status", lambda _d: {"enabled": True, "dry_run": False})
    monkeypatch.setattr(rb, "_status", lambda _w: "🟢 LIVE")
    return views._build_dashboard_data(dashboard)


def test_main_menu_uses_bot_pnl_and_wallet_total_separately(built_data):
    text = views._main_text(built_data)
    assert "🤖 REAL TRADING 🟢" in text
    assert "Portfolio ≥351.00 SDA (31/32)" in text
    assert "Bot PnL +28.03 SDA" in text
    assert "Open PnL -4.53 SDA" in text
    assert "Realized +32.56 SDA" in text
    assert "Positions 9" in text
    assert "(partial)" not in text


def test_portfolio_contains_only_nine_blockchain_fifo_positions(built_data):
    text = views._portfolio_text(built_data)
    for symbol, _value, _pnl in POSITION_ROWS:
        assert symbol in text
    assert "HAQ" not in text and "PINE" not in text and "JOINIT" not in text
    assert len(built_data["positions"]) == 9
    assert "XEN 18.86 SDA -1.23" in text


def test_buy_history_uses_only_confirmed_bot_buys(built_data):
    text = views._buy_text(built_data)
    assert "Confirmed bot BUY transactions: 21" in text
    assert "Total invested: 436.59 SDA" in text
    assert "PINE" not in text


def test_sell_history_contains_only_fifo_matched_bot_sells(built_data):
    text = views._sell_text(built_data)
    assert "Confirmed matched bot SELL transactions: 14" in text
    assert "PINE" not in text
    assert "JSFT" in text and "HAQ" in text


def test_statistics_reconcile_exactly_with_main_portfolio_and_history(built_data):
    assert built_data["buy_count"] == 21
    assert built_data["sell_count"] == 14
    assert built_data["closed_count"] == 14
    assert built_data["wins"] == 6 and built_data["losses"] == 8
    assert built_data["win_rate"] == pytest.approx(42.857142857)
    assert built_data["realized_pnl"] == pytest.approx(sum(x[1] for x in SELL_ROWS))
    assert built_data["open_pnl"] == pytest.approx(sum(x[2] for x in POSITION_ROWS))
    assert built_data["total_pnl"] == pytest.approx(built_data["realized_pnl"] + built_data["open_pnl"])
    assert built_data["best_trade_label"] == "JSFT"
    assert built_data["worst_trade_label"] == "SDS"
    text = views._stats_text(built_data)
    for expected in [
        "Open positions 9", "Confirmed bot BUY 21", "Confirmed matched bot SELL 14",
        "Wins 6", "Losses 8", "Win rate 42.9%", "Average trade +2.33 SDA",
        "Best JSFT +18.02 SDA", "Worst SDS -1.39 SDA",
        "Realized +32.56 SDA", "Open -4.53 SDA", "Total +28.03 SDA",
    ]:
        assert expected in text


def test_external_pine_and_unpriced_assets_do_not_make_bot_pnl_partial(built_data):
    assert built_data["realized_partial"] is False
    assert built_data["total_partial"] is False
    assert built_data["open_complete"] is True
    assert built_data["portfolio_complete"] is False
    assert "(partial)" not in views._main_text(built_data)
    assert "Wallet valuation partial: 31/32 assets" in views._stats_text(built_data)


def test_unreconciled_bot_transaction_keeps_partial_and_reports_exact_hash(monkeypatch):
    state = _accounting_state()
    state["bot_reconciliation_status"] = "INCOMPLETE"
    state["bot_unresolved_transactions"] = ["0xbot-unmatched-sell"]
    dashboard = SimpleNamespace(load=lambda _path, _default: state)
    monkeypatch.setattr(rb, "_load_worker_status", lambda _d: None)
    monkeypatch.setattr(rb, "_status", lambda _w: "⚪ UNKNOWN")
    data = views._build_dashboard_data(dashboard)
    assert "(partial)" in views._main_text(data)
    assert "0xbot-unmatched-sell" in views._stats_text(data)


def test_all_views_share_the_same_cached_accounting_snapshot(monkeypatch, built_data):
    views.clear_cache()
    calls = []
    monkeypatch.setattr(views, "_build_dashboard_data", lambda _d: calls.append("load") or built_data)
    dashboard = SimpleNamespace()
    views.render_view(dashboard, "main")
    views.render_view(dashboard, "portfolio", cache_only=True)
    views.render_view(dashboard, "buy", cache_only=True)
    views.render_view(dashboard, "sell", cache_only=True)
    views.render_view(dashboard, "stats", cache_only=True)
    assert calls == ["load"]
    views.clear_cache()
