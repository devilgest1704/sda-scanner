import os, requests
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# PinetSwap direct wallet trade history
# Uses the exact query observed in PinetSwap:
# BUY  => tx_type=buy  AND to_address=wallet
# SELL => tx_type=sell AND from_address=wallet
# The API returns `amount` in raw token units, plus price/volume/block/time.
# ---------------------------------------------------------------------------
PINET_TX_URL = "https://uhrsigapvhlpudafxqfg.supabase.co/rest/v1/token_transactions"
PINET_HEADERS = {
    "apikey": "sb_publishable_fL6m94CTRdZESg1licW9Qw_BuLIkm1Z",
    "accept-profile": "public",
}
PINET_WALLET_TRADE_LIMIT = int(os.environ.get("PINET_WALLET_TRADE_LIMIT", "5000"))

def _pinet_wallet_trades(wallet, max_rows=PINET_WALLET_TRADE_LIMIT):
    wallet = str(wallet).lower()
    rows=[]
    offset=0
    page=1000
    while len(rows) < max_rows:
        params = {
            "select": "tx_hash,token_address,amount,volume_in_sda,price_in_sda,block_number,tx_timestamp,tx_type",
            "or": f"(and(tx_type.eq.buy,to_address.eq.{wallet}),and(tx_type.eq.sell,from_address.eq.{wallet}))",
            "order": "block_number.asc",
            "offset": str(offset),
            "limit": str(min(page, max_rows-len(rows))),
        }
        r=requests.get(PINET_TX_URL, params=params, headers=PINET_HEADERS, timeout=30)
        r.raise_for_status()
        batch=r.json()
        if not isinstance(batch,list) or not batch:
            break
        rows.extend(batch)
        if len(batch) < page:
            break
        offset += len(batch)
    # deduplicate by tx hash while preserving chronological order
    seen=set(); out=[]
    for x in rows:
        if not isinstance(x,dict): continue
        h=str(x.get("tx_hash") or "")
        if not h or h in seen: continue
        seen.add(h)
        out.append(x)
    return out

def _pinet_decimals_for(address):
    """Best-effort decimals lookup from existing token metadata."""
    try:
        md = load_json_file("token_metadata.json", {})
        x = md.get(str(address).lower(), {}) if isinstance(md,dict) else {}
        for k in ("decimals","token_decimals","decimal"):
            if isinstance(x,dict) and x.get(k) is not None:
                return int(x[k])
    except Exception:
        pass
    return 18

def _pinet_amount(row):
    raw = row.get("amount")
    try:
        rawf=float(raw)
    except Exception:
        return 0.0
    dec=_pinet_decimals_for(row.get("token_address"))
    return rawf/(10**dec)

def _pinet_trade_objects(wallet):
    raw=_pinet_wallet_trades(wallet)
    trades=[]
    for x in raw:
        side=str(x.get("tx_type") or "").lower()
        if side not in ("buy","sell"): continue
        token=str(x.get("token_address") or "").lower()
        if not token: continue
        amount=_pinet_amount(x)
        volume=float(x.get("volume_in_sda") or 0)
        price=float(x.get("price_in_sda") or 0)
        if amount <= 0 and price > 0 and volume > 0:
            amount=volume/price
        if amount <= 0: continue
        trades.append({
            "tx_hash": str(x.get("tx_hash") or ""),
            "token_address": token,
            "side": side,
            "amount": amount,
            "volume_sda": volume,
            "price_sda": price,
            "block_number": int(x.get("block_number") or 0),
            "timestamp": x.get("tx_timestamp"),
            "source": "pinet-supabase-wallet-trades-v1",
        })
    return trades


def build_direct_pinet_portfolio(wallet, current_positions):
    """
    Reconstructs cost basis from PinetSwap's own wallet-filtered trade history.
    Returns portfolio rows + detected trade counts + realized P/L.
    """
    trades=_pinet_trade_objects(wallet)
    lots={}
    realized=0.0
    detected_buy=detected_sell=0
    rows_by_token={}
    for t in trades:
        a=t["token_address"]; side=t["side"]; qty=t["amount"]; px=t["price_sda"]
        if side=="buy":
            detected_buy += 1
            lots.setdefault(a, []).append([qty, qty*px, t["timestamp"]])
        else:
            detected_sell += 1
            remaining=qty
            proceeds=qty*px
            while remaining > 1e-12 and lots.get(a):
                q,c,opened=lots[a][0]
                take=min(q,remaining)
                unit_cost=c/q if q else 0.0
                realized += take*px - take*unit_cost
                q-=take; c-=take*unit_cost; remaining-=take
                if q <= 1e-12: lots[a].pop(0)
                else: lots[a][0]=[q,c,opened]
            # If there is no matching lot, don't invent a loss/cost basis.
    # Build current position rows from known wallet snapshot.
    result=[]
    by_addr={str(x.get("token_address") or x.get("address") or "").lower():x for x in (current_positions or []) if isinstance(x,dict)}
    for a,p in by_addr.items():
        qty=float(p.get("amount") or p.get("balance") or 0)
        now=float(p.get("value_sda") or p.get("value") or 0)
        symbol=p.get("symbol") or ""
        lot_qty=sum(z[0] for z in lots.get(a,[]))
        cost=sum(z[1] for z in lots.get(a,[]))
        # Prefer lots as reconstructed balance; compare with chain position.
        if lot_qty > 1e-10:
            avg=cost/lot_qty if lot_qty else 0
            upl=now - (qty*avg)
            result.append({
                "symbol":symbol,"token_address":a,"amount":qty,
                "cost":qty*avg,"now":now,"pnl":upl,
                "avg_cost":avg,
                "age": next((z[2] for z in lots[a] if z[2]), None),
                "cost_basis_known": True,
                "reconstructed_qty":lot_qty,
                "balance_delta":qty-lot_qty,
            })
        else:
            result.append({
                "symbol":symbol,"token_address":a,"amount":qty,
                "cost":None,"now":now,"pnl":None,
                "avg_cost":0.0,"age":None,"cost_basis_known":False,
                "reconstructed_qty":0.0,"balance_delta":qty,
            })
    return result, trades, detected_buy, detected_sell, realized
