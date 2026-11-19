"""Read-only MAX_HOLD_STAGNANT_PROFIT preflight for every persisted REAL position.

This script performs only public/read-only market calls (eth_call/eth_estimateGas).
It never saves state, creates SELL intents, signs, or broadcasts transactions.
"""
from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import real_market_price as market_prices
import real_stagnant_profit as stagnant
import real_swing_sell_executor as sell
import real_trader as trader
import real_trading_config as cfg


def _load(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}


def _reference_quote(token, metadata, wallet):
    item = metadata.get(str(token).lower())
    if not isinstance(item, dict) or item.get("decimals") is None:
        return None
    decimals = int(item["decimals"])
    amount_in = 10 ** 18
    fee, amount_out, _calldata, _deadline = trader.quote_best_live_fee(
        cfg.WSDA_ADDRESS, token, token, wallet, amount_in, value_wei=amount_in
    )
    if int(amount_out) <= 0:
        return None
    return {
        "price_sda": (10 ** decimals) / int(amount_out),
        "timestamp": datetime.now(UTC).isoformat(),
        "source": "PinetSwap read-only 1 SDA BUY reference",
        "quote_amount_sda": 1,
        "quote_side": "BUY_REFERENCE",
        "fee_tier": int(fee),
    }


def main():
    now = datetime.now(UTC)
    state = _load(cfg.STATE_FILE)
    analysis = _load("market_analysis.json")
    metadata = _load("token_metadata.json")
    positions = state.get("positions") or {}
    wallet = trader.wallet_address()
    print("READ-ONLY MAX_HOLD_STAGNANT_PROFIT PREFLIGHT")
    print("positions=", len(positions), "wallet=", wallet, "as_of=", now.isoformat())
    print("TOKEN | SOURCE | AGE_H | GROSS_PNL_% | NET_EXIT_SDA | IMPROVEMENT_PP | 1H | 4H | MACD | FLOW_1H/4H | COVERAGE_% | DECISION | BLOCKER")
    rows = []
    for token, position in positions.items():
        if not isinstance(position, dict):
            continue
        symbol = str(metadata.get(str(token).lower(), {}).get("symbol") or position.get("symbol") or token[:10])
        source = "MANUAL" if (str(position.get("source") or "").upper() == "MANUAL" or position.get("manual_imported")) else "AUTO"
        position = dict(position)
        position.setdefault("sellManagementEnabled", True)
        manual_position = (str(position.get("source") or "").upper() == "MANUAL"
                           or bool(position.get("manual_imported")))
        if manual_position and not position.get("buy_tx") and (
                not position.get("position_id") or position.get("position_id") == token):
            opened_identity = position.get("opened_at") or position.get("at") or position.get("buy_timestamp")
            if opened_identity:
                position.setdefault("history_position_id", f"MANUAL:{opened_identity}")
        try:
            market = market_prices.resolve_market_price(
                token, analysis, now=now,
                supabase_lookup=market_prices.newest_supabase_price,
                reference_quote_lookup=lambda t: _reference_quote(t, metadata, wallet),
            )
            if market is None:
                raise RuntimeError("STALE_PRICE")
            # A live BUY-reference quote may be stamped a few seconds after
            # resolver startup; evaluate against a clock captured after acquisition.
            evaluation_now = datetime.now(UTC)
            decimals = int(metadata[str(token).lower()]["decimals"])
            quantity_raw = int(position.get("remaining_quantity", position.get("token_amount_raw", 0)) or 0)
            quantity = Decimal(quantity_raw) / Decimal(10 ** decimals)
            basis = Decimal(str(position.get("remaining_cost_basis",
                                             position.get("amount_sda", position.get("cost_sda", 0))) or 0))
            if quantity <= 0 or basis <= 0:
                raise RuntimeError("INVALID_POSITION_BASIS")
            price = Decimal(str(market["price_sda"]))
            entry = basis / quantity
            pnl_pct = float((price / entry - Decimal(1)) * 100)
            gross_pnl_sda = price * quantity - basis
            market_context = dict((analysis.get("tokens") or {}).get(str(token).lower()) or {})
            market_context["_snapshot_updated_at"] = analysis.get("updated_at")
            trend = stagnant.assess_trend(market_context, now=evaluation_now)
            identity = str(position.get("history_position_id") or position.get("position_id")
                           or position.get("buy_tx") or token)
            hist_key = f"{str(token).lower()}:{identity}"
            history_record = (state.get("real_stagnation_history") or {}).get(hist_key) or {}
            history = history_record.get("observations") or []
            first = stagnant.evaluate(
                position, now=evaluation_now, price=price, price_timestamp=market.get("timestamp"),
                history=history, trend=trend, current_pnl_pct=pnl_pct,
                net_exit_pnl_sda=None,
                pending_sell=bool((state.get("pending_sells") or {}).get(token)),
            )
            quote = None
            net_exit = None
            quote_blocker = None
            result = first
            opened = stagnant._time(position.get("opened_at") or position.get("at") or position.get("buy_timestamp"))
            age_hours = (evaluation_now - opened).total_seconds() / 3600 if opened else None
            # Preflight quote is read-only and is also collected for positive aged
            # positions blocked by stale trend/history, so the report can show NET EXIT.
            if pnl_pct > 0 and age_hours is not None and age_hours >= 72:
                try:
                    quote = sell._max_hold_executable_quote(token, position, quantity_raw, owner=wallet)
                    net_exit = Decimal(quote["expected_net_pnl_sda"])
                    result = stagnant.evaluate(
                        position, now=evaluation_now, price=price, price_timestamp=market.get("timestamp"),
                        history=history, trend=trend, current_pnl_pct=pnl_pct,
                        net_exit_pnl_sda=net_exit,
                        pending_sell=bool((state.get("pending_sells") or {}).get(token)),
                    )
                except (requests.RequestException, RuntimeError, ValueError, TypeError, OSError, ArithmeticError) as exc:
                    quote_blocker = f"{type(exc).__name__}:{str(exc)[:140]}"
                    if first.get("eligible_without_net"):
                        result = {**first, "blocker": "EXECUTABLE_QUOTE_UNAVAILABLE"}
            row = {
                "token": symbol, "address": token, "source": source,
                "age_hours": round(age_hours, 2) if age_hours is not None else None,
                "current_pnl_pct": round(pnl_pct, 3), "gross_pnl_sda": str(gross_pnl_sda),
                "net_exit_pnl_sda": str(net_exit) if net_exit is not None else None,
                "improvement_pp": result.get("improvement_pp"),
                "momentum_1h": trend.get("momentum_1h"), "momentum_4h": trend.get("momentum_4h"),
                "macd": trend.get("macd_state"),
                "technical_trend_1h": trend.get("technical_trend_1h"),
                "technical_trend_4h": trend.get("technical_trend_4h"),
                "flow_1h": trend.get("flow_1h"),
                "flow_4h": trend.get("flow_4h"),
                "history_coverage_pct": result.get("history_coverage_pct"),
                "decision": result.get("decision", "HOLD"),
                "blocker": result.get("blocker"), "quote_blocker": quote_blocker,
            }
        except (requests.RequestException, RuntimeError, ValueError, TypeError, KeyError, OSError, ArithmeticError) as exc:
            row = {
                "token": symbol, "address": token, "source": source,
                "age_hours": None, "current_pnl_pct": None, "gross_pnl_sda": None,
                "net_exit_pnl_sda": None, "improvement_pp": None,
                "momentum_1h": None, "momentum_4h": None, "macd": None,
                "technical_trend_1h": None, "technical_trend_4h": None,
                "flow_1h": None, "flow_4h": None, "history_coverage_pct": 0,
                "decision": "HOLD", "blocker": str(exc)[:160], "quote_blocker": None,
            }
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False, sort_keys=True))
    print("SPECIAL POSITIONS")
    for wanted in ("FREEt", "SGWA"):
        found = [row for row in rows if row["token"].lower() == wanted.lower()]
        print(wanted, json.dumps(found[0] if found else {"decision": "NOT_OPEN"}, ensure_ascii=False))
    print("READ-ONLY GUARANTEE: state writes=0, SELL intents created=0, signed transactions=0, blockchain transactions=0")


if __name__ == "__main__":
    main()
