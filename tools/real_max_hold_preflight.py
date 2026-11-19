"""Read-only preflight: python -m tools.real_max_hold_preflight from the state checkout."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import real_market_price as market_prices
import real_swing_sell_executor as executor
import real_trader as trader
import real_trading_config as cfg


def reference_quote(token, decimals):
    owner = trader.wallet_address()
    amount = 10**18
    fee, output, _, _ = trader.quote_best_live_fee(
        cfg.WSDA_ADDRESS, token, token, owner, amount, value_wei=amount,
    )
    return {"price_sda": 10**decimals / output, "timestamp": datetime.now(UTC).isoformat(),
            "fee_tier": fee, "source": "PinetSwap 1 SDA BUY-side reference quote"}


def build_rows(state, snapshot, metadata):
    def evaluate(item):
        token, position = item
        meta = metadata.get(token) or {}
        symbol = meta.get("symbol") or position.get("symbol") or token
        market = market_prices.resolve_market_price(
            token, snapshot, supabase_lookup=market_prices.newest_supabase_price,
            reference_quote_lookup=lambda address: reference_quote(address, int(meta["decimals"])),
        ) or {}
        result, _ = executor.max_hold_preview(
            token, symbol, position, state, market.get("price_sda"), market.get("timestamp"), snapshot,
            decimals=meta.get("decimals"), diagnostic_quote=True,
        )
        return {**result, "price_source": market.get("source"),
                "price_timestamp": market.get("timestamp"), "trend_snapshot_at": snapshot.get("updated_at")}

    positions = [(token, position) for token, position in state.get("positions", {}).items()
                 if isinstance(position, dict)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        return list(pool.map(evaluate, positions))


def main():
    if cfg.REAL_TRADING_ENABLED and not cfg.REAL_TRADING_DRY_RUN:
        raise SystemExit("Preflight requires REAL_TRADING_ENABLED=false or REAL_TRADING_DRY_RUN=true")
    rows = build_rows(trader.load_state(), market_prices.load_market_snapshot(),
                      market_prices.load_token_metadata())
    for row in rows:
        print(json.dumps(row, ensure_ascii=True, sort_keys=True))


if __name__ == "__main__":
    main()