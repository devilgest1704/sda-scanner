"""Canonical paper runner.

V26-PUMP-HUNTER is the only active paper decision/exit policy. The runner is
still named V19 for workflow compatibility. No real-wallet order is executed.
"""
import engine
import main as scanner_main
import v30_pump_hunter

# V30 owns paper BUY, position creation and exits. The filename remains
# paper_engine_v19.py for workflow compatibility, but V30 is the only active
# paper policy. Real trading remains disabled.
v30_pump_hunter.patch(scanner_main, engine)

engine.TELEGRAM_TOKEN = ""
engine.BUY_THRESHOLD = v30_pump_hunter.ENTRY_SCORE
engine.SL_PCT = v30_pump_hunter.SL_PCT
engine.MAX_OPEN_POSITIONS = v30_pump_hunter.MAX_OPEN
engine.MAX_NEW_BUYS_PER_RUN = v30_pump_hunter.MAX_BUYS_PER_RUN
scanner_main.engine.score = engine.score
scanner_main.engine.create = engine.create
scanner_main.engine._auto_exit = engine._auto_exit

if __name__ == "__main__":
    engine.main()
