"""Canonical paper runner.

V26-PUMP-HUNTER is the only active paper decision/exit policy. The runner is
still named V19 for workflow compatibility. No real-wallet order is executed.
"""
import engine
import main as scanner_main
import v26_pump_hunter

# V26 owns paper BUY, position creation and exits.
v26_pump_hunter.patch(scanner_main, engine)

engine.TELEGRAM_TOKEN = ""
engine.BUY_THRESHOLD = v26_pump_hunter.ENTRY_SCORE
engine.SL_PCT = v26_pump_hunter.SL_PCT
engine.MAX_OPEN_POSITIONS = v26_pump_hunter.MAX_OPEN
engine.MAX_NEW_BUYS_PER_RUN = v26_pump_hunter.MAX_BUYS_PER_RUN
scanner_main.engine.score = engine.score
scanner_main.engine.create = engine.create
scanner_main.engine._auto_exit = engine._auto_exit

if __name__ == "__main__":
    engine.main()
