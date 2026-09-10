import os

# Real trading is deliberately OFF until the trading wallet and dry-run checks
# have been verified. Never commit a private key or seed phrase.
REAL_TRADING_ENABLED = os.getenv("REAL_TRADING_ENABLED", "false").strip().lower() == "true"
REAL_TRADING_DRY_RUN = os.getenv("REAL_TRADING_DRY_RUN", "true").strip().lower() == "true"

RPC_URL = os.getenv("SIDRA_RPC_URL", "https://node.sidrachain.com")
CHAIN_ID = int(os.getenv("SIDRA_CHAIN_ID", "97453"))
EXPLORER_URL = os.getenv("SIDRA_EXPLORER_URL", "https://ledger.sidrachain.com")

ROUTER_ADDRESS = os.getenv("SIDRA_ROUTER_ADDRESS", "0x35cAC72Db00e8dAC0e4f7F8A0F53D339E0cC23fb")
POOL_ADDRESS = os.getenv("SIDRA_POOL_ADDRESS", "0xCB94460F967f49E3a955278f252Ca9D6056ecE75")
WSDA_ADDRESS = os.getenv("SIDRA_WSDA_ADDRESS", "0xE4095a910209D7BE03B55D02F40d4554B1666182")

# Capital/risk limits requested for the real bot.
TRADING_WALLET_CAPITAL_SDA = float(os.getenv("REAL_TRADING_CAPITAL_SDA", "200"))
POSITION_MIN_SDA = float(os.getenv("REAL_POSITION_MIN_SDA", "10"))
POSITION_MAX_SDA = float(os.getenv("REAL_POSITION_MAX_SDA", "20"))
POSITION_DEFAULT_SDA = float(os.getenv("REAL_POSITION_DEFAULT_SDA", "15"))
MAX_OPEN_POSITIONS = int(os.getenv("REAL_MAX_OPEN_POSITIONS", "5"))
MAX_DAILY_LOSS_SDA = float(os.getenv("REAL_MAX_DAILY_LOSS_SDA", "30"))
MAX_SLIPPAGE_PCT = float(os.getenv("REAL_MAX_SLIPPAGE_PCT", "1.0"))
DEADLINE_SECONDS = int(os.getenv("REAL_DEADLINE_SECONDS", "120"))
GAS_LIMIT = int(os.getenv("REAL_GAS_LIMIT", "350000"))

PRIVATE_KEY_ENV = "REAL_TRADING_PRIVATE_KEY"
WALLET_ADDRESS_ENV = "REAL_TRADING_WALLET_ADDRESS"
STATE_FILE = "real_trade_state.json"

BUY_SELECTOR = "0x414bf389"  # exactInputSingle((address,address,uint24,address,uint256,uint256,uint256,uint160))
APPROVE_SELECTOR = "0x095ea7b3"
BALANCE_OF_SELECTOR = "0x70a08231"
ALLOWANCE_SELECTOR = "0xdd62ed3e"
UNWRAP_SELECTOR = "0x2e1a7d4d"  # WSDA withdraw(uint256)

def position_sda(value=None):
    value = POSITION_DEFAULT_SDA if value is None else float(value)
    return max(POSITION_MIN_SDA, min(POSITION_MAX_SDA, value))
