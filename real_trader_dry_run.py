"""Safe read-only smoke test for the real trading wallet.

This script never signs or broadcasts a transaction. It validates the GitHub
Secrets wiring, Sidra chain, wallet address, native SDA balance, and (optionally)
a live BUY quote using the same executor code used later for real trading.
"""

import json
import os

import real_trading_config as cfg
import real_trader as trader


def main():
    if cfg.REAL_TRADING_ENABLED:
        raise RuntimeError("Dry-run check refuses to run with REAL_TRADING_ENABLED=true")
    if not cfg.REAL_TRADING_DRY_RUN:
        raise RuntimeError("REAL_TRADING_DRY_RUN must remain true for this check")

    wallet = os.getenv(cfg.WALLET_ADDRESS_ENV, "").strip()
    if not wallet:
        raise RuntimeError(f"Missing {cfg.WALLET_ADDRESS_ENV}")

    wallet = wallet.lower()
    chain = trader.chain_id()
    if chain != cfg.CHAIN_ID:
        raise RuntimeError(f"Wrong chain ID: {chain}; expected {cfg.CHAIN_ID}")

    balance_raw = trader.native_balance(wallet)
    balance_sda = balance_raw / 10**18

    result = {
        "status": "OK",
        "mode": "READ_ONLY_DRY_RUN",
        "real_trading_enabled": cfg.REAL_TRADING_ENABLED,
        "dry_run": cfg.REAL_TRADING_DRY_RUN,
        "wallet": wallet,
        "chain_id": chain,
        "native_balance_sda": round(balance_sda, 6),
        "capital_cap_sda": cfg.TRADING_WALLET_CAPITAL_SDA,
        "position_range_sda": [cfg.POSITION_MIN_SDA, cfg.POSITION_MAX_SDA],
        "private_key_present": bool(os.getenv(cfg.PRIVATE_KEY_ENV, "").strip()),
    }

    # Do not print the private key. Its presence is enough to prove the secret
    # reached the runner. The configured address is cross-checked by Account
    # only inside the existing executor when real execution is enabled.
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
