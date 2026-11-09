#!/usr/bin/env python3
"""Read-only Sidra Blockscout history collector and conservative REAL accounting.

No wallet key, signing, transaction submission, allowance changes, or trading calls.
All explorer pages and per-transaction evidence are retained verbatim.
"""
import json, os, sys, time, hashlib
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import requests

BASE = os.getenv("SIDRA_EXPLORER_API", "https://ledger.sidrachain.com/api/v2").rstrip("/")
WALLET = os.getenv("REAL_ACCOUNTING_WALLET", "0x9e3643f2ac15c91ee83aa2814015f15b18d573f2").lower()
WSDA = os.getenv("SIDRA_WSDA_ADDRESS", "0xE4095a910209D7BE03B55D02F40d4554B1666182").lower()
CHAIN_ID = 97453
OUT = Path("accounting_evidence")
OUT.mkdir(exist_ok=True)
S = requests.Session()
S.headers.update({"User-Agent":"sda-real-accounting-readonly/1.0","Accept":"application/json"})
REQUESTS = 0

def get(path, params=None):
    global REQUESTS
    url = BASE + path
    for attempt in range(5):
        try:
            r = S.get(url, params=params, timeout=(15, 90))
            REQUESTS += 1
            if r.status_code == 429:
                time.sleep(2 ** attempt); continue
            r.raise_for_status()
            return r.json()
        except Exception:
            if attempt == 4: raise
            time.sleep(2 ** attempt)

def paged(path, params=None):
    params = dict(params or {})
    seen, rows, pages = set(), [], 0
    while True:
        data = get(path, params)
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise RuntimeError(f"Unexpected paginated response for {path}: {str(data)[:500]}")
        pages += 1
        batch = data["items"]
        rows.extend(batch)
        nxt = data.get("next_page_params")
        if not nxt: break
        key = json.dumps(nxt, sort_keys=True)
        if key in seen: raise RuntimeError(f"Pagination cursor loop at {path}")
        seen.add(key)
        params = dict(nxt)
        if pages > 100000: raise RuntimeError(f"Pagination safety cap exceeded at {path}")
    return rows, pages

def dump(name, obj):
    path = OUT / name
    path.write_text(json.dumps(obj, indent=2, sort_keys=True, default=str)+"\n", encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()

def addr(x):
    if isinstance(x, dict): return str(x.get("hash") or "").lower()
    return str(x or "").lower()

def main():
    info = get(f"/addresses/{WALLET}")
    txs, tx_pages = paged(f"/addresses/{WALLET}/transactions")
    transfers, transfer_pages = paged(f"/addresses/{WALLET}/token-transfers")
    internal, internal_pages = paged(f"/addresses/{WALLET}/internal-transactions")
    tokens = get(f"/addresses/{WALLET}/tokens", {"type":"ERC-20"})
    # Capture receipts/logs for every wallet transaction, including failed txs.
    hashes = sorted({str(x.get("hash") or "").lower() for x in txs if x.get("hash")})
    details, detail_errors = {}, {}
    for i, h in enumerate(hashes, 1):
        try:
            details[h] = get(f"/transactions/{h}")
        except Exception as e:
            detail_errors[h] = f"{type(e).__name__}: {e}"
        if i % 25 == 0: print(f"Fetched transaction detail {i}/{len(hashes)}", flush=True)
    # Transaction endpoint usually embeds receipt/logs; explicitly retrieve logs
    # and tx-specific token/internal transfers where explorer exposes them.
    logs, tx_token_transfers, tx_internal = {}, {}, {}
    for i, h in enumerate(hashes, 1):
        try: logs[h] = get(f"/transactions/{h}/logs")
        except Exception as e: logs[h] = {"fetch_error":str(e)}
        try: tx_token_transfers[h] = get(f"/transactions/{h}/token-transfers")
        except Exception as e: tx_token_transfers[h] = {"fetch_error":str(e)}
        try: tx_internal[h] = get(f"/transactions/{h}/internal-transactions")
        except Exception as e: tx_internal[h] = {"fetch_error":str(e)}
    evidence = {
      "meta":{"chain_id":CHAIN_ID,"wallet":WALLET,"wsda":WSDA,"explorer_api":BASE,
       "retrieved_at":datetime.now(timezone.utc).isoformat(),"request_count":REQUESTS,
       "pagination":{"transactions_pages":tx_pages,"token_transfer_pages":transfer_pages,
        "internal_transaction_pages":internal_pages},
       "counts":{"transactions":len(txs),"token_transfers":len(transfers),
        "internal_transactions":len(internal),"transaction_details":len(details),
        "transaction_detail_errors":len(detail_errors)}},
      "address":info,"transactions":txs,"token_transfers":transfers,
      "internal_transactions":internal,"token_holdings":tokens,
      "transaction_details":details,"transaction_logs":logs,
      "transaction_token_transfers":tx_token_transfers,
      "transaction_internal_transactions":tx_internal,"detail_errors":detail_errors
    }
    raw_hash = dump("blockchain_raw.json", evidence)
    # Conservative tx-level classification based on wallet deltas and method.
    by_hash={}
    for tr in transfers:
        h=str(tr.get("transaction_hash") or "").lower()
        if h: by_hash.setdefault(h,[]).append(tr)
    classifications=[]
    for tx in txs:
        h=str(tx.get("hash") or "").lower()
        d=details.get(h,{})
        status=str(d.get("status") or tx.get("status") or d.get("result") or "").lower()
        success=not any(k in status for k in ("fail","error","revert")) and status not in ("0","false")
        method=str((d.get("decoded_input") or {}).get("method_call") or d.get("method") or tx.get("method") or "").lower()
        wallet_token_deltas=[]
        for tr in by_hash.get(h,[]):
            token=tr.get("token") or {}
            dec=int(token.get("decimals") or 0)
            raw=tr.get("total") or tr.get("amount") or "0"
            try: qty=Decimal(str(raw))/(Decimal(10)**dec)
            except Exception: qty=None
            wallet_token_deltas.append({"contract":addr(token),"symbol":token.get("symbol"),
              "decimals":dec,"from":addr(tr.get("from")),"to":addr(tr.get("to")),
              "quantity":str(qty) if qty is not None else None,
              "direction":"IN" if addr(tr.get("to"))==WALLET else "OUT" if addr(tr.get("from"))==WALLET else "OTHER"})
        if not success: kind="FAILED TRANSACTION"
        elif "approve" in method: kind="APPROVAL"
        elif "deposit" in method and any(x["contract"]==WSDA and x["direction"]=="IN" for x in wallet_token_deltas): kind="WRAP SDA -> WSDA"
        elif "withdraw" in method and any(x["contract"]==WSDA and x["direction"]=="OUT" for x in wallet_token_deltas): kind="UNWRAP WSDA -> SDA"
        else: kind="OTHER / NEEDS SWAP RECONCILIATION"
        classifications.append({"hash":h,"timestamp":tx.get("timestamp") or d.get("timestamp"),
          "block":tx.get("block") or d.get("block"),"status":status,"method":method,
          "classification":kind,"native_value":tx.get("value") or d.get("value"),
          "fee":tx.get("fee") or d.get("fee"),"gas_used":tx.get("gas_used") or d.get("gas_used"),
          "gas_price":tx.get("gas_price") or d.get("gas_price"),
          "token_deltas":wallet_token_deltas,"explorer_detail":d})
    report={"schema_version":1,"wallet":WALLET,"chain_id":CHAIN_ID,
      "source":"Sidra Blockscout REST API v2","raw_evidence_file":"accounting_evidence/blockchain_raw.json",
      "raw_evidence_sha256":raw_hash,"retrieval":evidence["meta"],
      "classification":"CONSERVATIVE — swap direction/cost basis not finalized until every receipt and native/internal SDA flow is reconciled",
      "transactions":classifications,
      "validation":{"all_pages_retrieved":True,"detail_fetch_errors":detail_errors,
       "current_balances_available":bool(tokens),"market_value_sda":None,
       "realized_pnl_sda":None,"open_pnl_sda":None,"total_pnl_sda":None,
       "reconciliation_status":"INCOMPLETE","blockers":[
        "Review decoded receipts/logs and internal SDA/WSDA flows to distinguish swaps from transfers",
        "Obtain authoritative SDA market marks for every current token holding",
        "Reconcile complete cash flows and FIFO lots before publishing PnL"]}}
    dump("real_accounting_report.json",report)
    print(json.dumps({"wallet":WALLET,"chain_id":CHAIN_ID,"explorer_api":BASE,
      "pages":evidence["meta"]["pagination"],"counts":evidence["meta"]["counts"],
      "raw_evidence_sha256":raw_hash,"current_holdings_count":len(tokens.get("items",[]) if isinstance(tokens,dict) else tokens or []),
      "detail_errors":len(detail_errors),"report":"accounting_evidence/real_accounting_report.json",
      "status":"INCOMPLETE — no PnL publication permitted"},indent=2))

if __name__=="__main__":
    main()
