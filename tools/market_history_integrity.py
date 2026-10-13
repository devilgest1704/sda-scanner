"""Deterministic transaction-history integrity for the SDA scanner.

Only an observed token transaction hash identifies a replay. Timestamp, price,
and volume alone do NOT identify a trade: genuinely distinct swaps may share
these values. Caller chooses the per-token scope.
"""


def transaction_hash(row):
    """Normalize the existing per-token transaction identity, if supplied."""
    if not isinstance(row, dict):
        return None
    value = row.get("hash")
    if not isinstance(value, str):
        return None
    return value.strip().lower() or None


def compact_history(history, limit=5000):
    """Sort, remove observed hash replays and bound per-token history.

    Earlier chronological records win for a repeated hash. Unidentified
    transactions are preserved rather than inventing identities. Return a
    hash index for O(1) duplicate rejection during the current fetch.
    """
    if not isinstance(history, list) or limit < 1:
        raise ValueError("Invalid transaction history or retention limit")
    if any(not isinstance(row, dict) for row in history):
        raise ValueError("Invalid transaction history row")
    ordered = sorted(history, key=lambda row: str(row.get("timestamp") or ""))
    unique = []
    seen = set()
    removed = 0
    for row in ordered:
        identity = transaction_hash(row)
        if identity is not None:
            if identity in seen:
                removed += 1
                continue
            seen.add(identity)
        unique.append(row)
    retained = unique[-limit:]
    # Only retained hashes can block replay on a subsequent scanner cycle.
    # A previously discarded (older-than-retention) event may be loaded, but
    # the chronological truncation after scanning will discard it again.
    return retained, {transaction_hash(x) for x in retained
                      if transaction_hash(x) is not None}, removed


def append_transaction(history, transaction, known_hashes):
    """Append unseen events using the FULL retained history, not its last 200.

    Missing hash is intentionally not deduplicated, as two distinct trades
    can share timestamp/price/volume.
    """
    if not isinstance(history, list) or not isinstance(transaction, dict):
        raise ValueError("Invalid transaction append")
    identity = transaction_hash(transaction)
    if identity is not None and identity in known_hashes:
        return False
    history.append(transaction)
    if identity is not None:
        known_hashes.add(identity)
    return True
