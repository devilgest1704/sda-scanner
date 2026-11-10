"""One shared, read-only market decision price resolver for REAL portfolio and Swing SELL V2.

Priority:
1. Fresh observed PinetSwap trade from the scanner snapshot.
2. Fresh observed PinetSwap trade from Supabase.
3. Fresh 1 SDA WSDA->token BUY-side reference quote (eth_call only).

The BUY-side reference is a small-notional market mark, not an executable SELL quote.
Executable SELL quotes remain transaction-specific and are deliberately excluded.
"""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

MAX_MARKET_PRICE_AGE_SECONDS = 300


def parse_timestamp(value):
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            return None
        return stamp.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def decimal_price(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and result > 0 else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def _validated(price, timestamp, source, now, max_age, **extra):
    value = decimal_price(price)
    stamp = parse_timestamp(timestamp)
    if value is None or stamp is None:
        return None
    age = (now.astimezone(timezone.utc) - stamp).total_seconds()
    if age < 0 or age > max_age:
        return None
    return {
        "price_sda": float(value),
        "timestamp": stamp.isoformat(),
        "age_seconds": age,
        "source": source,
        **extra,
    }


def scanner_market_price(token, analysis, now=None, max_age=MAX_MARKET_PRICE_AGE_SECONDS):
    now = now or datetime.now(timezone.utc)
    if not isinstance(analysis, dict):
        return None
    snapshot_value = analysis.get("updated_at")
    if snapshot_value:
        snapshot_stamp = parse_timestamp(snapshot_value)
        if snapshot_stamp is None:
            return None
        snapshot_age = (now.astimezone(timezone.utc) - snapshot_stamp).total_seconds()
        if snapshot_age < 0 or snapshot_age > max_age:
            return None
    tokens = analysis.get("tokens")
    item = tokens.get(str(token).lower()) if isinstance(tokens, dict) else None
    if not isinstance(item, dict):
        return None
    stamp = (item.get("last_transaction") or item.get("last_transaction_at")
             or item.get("price_timestamp"))
    return _validated(item.get("price_in_sda"), stamp, "scanner_market_analysis",
                      now, max_age)


def supabase_market_price(token, lookup, now=None, max_age=MAX_MARKET_PRICE_AGE_SECONDS):
    now = now or datetime.now(timezone.utc)
    if not callable(lookup):
        return None
    try:
        item = lookup(str(token).lower())
    except Exception:
        return None
    if not isinstance(item, dict):
        return None
    return _validated(item.get("price_sda"), item.get("timestamp"),
                      "supabase_transaction", now, max_age)


def resolve_market_price(token, analysis, now=None, supabase_lookup=None,
                         reference_quote_lookup=None,
                         max_age=MAX_MARKET_PRICE_AGE_SECONDS):
    """Resolve one shared market mark; never consumes an executable SELL quote."""
    now = now or datetime.now(timezone.utc)
    price = scanner_market_price(token, analysis, now, max_age)
    if price is not None:
        return price
    price = supabase_market_price(token, supabase_lookup, now, max_age)
    if price is not None:
        return price
    if callable(reference_quote_lookup):
        try:
            quote = reference_quote_lookup(str(token).lower())
        except Exception:
            quote = None
        if isinstance(quote, dict):
            # The read-only BUY-side reference quote is stamped at acquisition.
            # It must carry its own timestamp; we never refresh an old quote's age.
            quote_stamp = parse_timestamp(quote.get("timestamp"))
            validation_now = now
            # A live eth_call is timestamped after the resolver starts. Accept
            # only a tiny acquisition-time skew; larger future timestamps fail closed.
            if quote_stamp is not None:
                future_skew = (quote_stamp - now.astimezone(timezone.utc)).total_seconds()
                if 0 < future_skew <= 30:
                    validation_now = quote_stamp
            return _validated(
                quote.get("price_sda"), quote.get("timestamp"),
                quote.get("source") or "pinet_swap_buy_reference_1_sda",
                validation_now, max_age, quote_amount_sda=quote.get("quote_amount_sda", 1),
                quote_side="BUY_REFERENCE", fee_tier=quote.get("fee_tier"),
            )
    return None


def load_market_snapshot(path="market_analysis.json"):
    """Load the scanner snapshot without importing Telegram/menu code."""
    import json
    from pathlib import Path
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def load_token_metadata(path="token_metadata.json"):
    """Load scanner token metadata for the read-only 1 SDA reference mark."""
    import json
    from pathlib import Path
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def newest_supabase_price(token):
    """Return the newest observed Supabase trade price; freshness is validated by resolver."""
    import requests
    token = str(token).strip().lower()
    try:
        response = requests.get(
            "https://uhrsigapvhlpudafxqfg.supabase.co/rest/v1/token_transactions",
            params={"select": "token_address,price_in_sda,tx_timestamp,tx_type",
                    "token_address": f"eq.{token}", "order": "tx_timestamp.desc", "limit": "100"},
            headers={"apikey": "sb_publishable_fL6m94CTRdZESg1licW9Qw_BuLIkm1Z",
                     "accept-profile": "public"},
            timeout=(1.5, 3.0),
        )
        response.raise_for_status()
        rows = response.json()
    except Exception:
        return None
    if not isinstance(rows, list):
        return None
    now = datetime.now(timezone.utc)
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("token_address") or "").lower() != token:
            continue
        if str(row.get("tx_type") or "").lower() not in {"buy", "sell"}:
            continue
        stamp = parse_timestamp(row.get("tx_timestamp"))
        price = decimal_price(row.get("price_in_sda"))
        if stamp is None or price is None or stamp > now:
            continue
        return {"price_sda": float(price), "timestamp": stamp.isoformat(),
                "source": "Supabase token_transactions.price_in_sda"}
    return None
