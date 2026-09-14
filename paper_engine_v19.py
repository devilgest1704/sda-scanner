"""Stable V19/V21/V22 paper-trading entry point.

BUY and exit decisions are canonicalized through main.py and strategy_v21.
V22 is pump-oriented: normal pullbacks are tolerated; confirmed breakdowns
are exited, while profitable momentum/flow is allowed to run.
"""
import engine
import main as scanner_main
import v25_runtime_tuning_patch
from strategy_v21 import patch_engine, technical_sell_confirmed, evaluate_exit

# The scanner workflow executes this module directly. Apply the V25 runtime
# gate here as well as in the dashboard API so paper-hunter BUY decisions are
# identical in both execution paths.
v25_runtime_tuning_patch.patch()

engine.TELEGRAM_TOKEN = ""
engine.SL_PCT = 0.05
engine.BUY_THRESHOLD = float(getattr(scanner_main, "MAX_WIN_BUY_THRESHOLD", 78.0))
patch_engine(engine)
scanner_main.engine.score = engine.score


def auto_exit_v21(p, tokens, ws):
    state = engine._load_auto(); events=[]
    for address in list(p.get("positions",{})):
        td=tokens.get(address,{}) or {}; analysis=td.get("analysis",td) if isinstance(td,dict) else {}; current=engine.num(analysis.get("price_in_sda")); position=p["positions"].get(address)
        if not position or current<=0: continue
        signal=engine.score(address,analysis,ws); score=engine.num(signal.get("confidence")); momentum_1h=engine.num(signal.get("m1h")); flow_1h=engine.num(signal.get("net_1h")); entry=engine.num(position.get("entry_price")); roi=(current-entry)/entry*100 if entry else 0.0; technical_bearish=technical_sell_confirmed(analysis); exit_signal=evaluate_exit(roi,score,momentum_1h,flow_1h,technical_bearish)
        old=state.get(address,{}) if isinstance(state.get(address),dict) else {}; negative_count=int(engine.num(old.get("neg"))); weakening_count=int(engine.num(old.get("weak"))); negative_count=min(5,negative_count+1) if exit_signal["negative"] else 0; weakening_count=min(5,weakening_count+1) if exit_signal["weakening"] else 0; state[address]={"neg":negative_count,"weak":weakening_count}
        if exit_signal["emergency"]:
            result=engine.close(p,address,current,"EMERGENCY SELL")
            if result: events.append(f"🚨 EMERGENCY SELL {result['label']} | Profit: {result['closed_profit_sda']:+.2f} SDA | Score: {score:.0f}/100 | ROI: {roi:+.2f}% | Mode: PAPER AUTO")
        elif weakening_count>=2 and not position.get("tp1_hit") and not exit_signal.get("pump_hold"):
            result=engine.close(p,address,current,"AUTO PARTIAL SELL",0.5)
            if result: events.append(f"🟠 AUTO PARTIAL SELL {result['label']} | Price: {engine.price(current)} SDA | Profit: {result['closed_profit_sda']:+.2f} SDA | Score: {score:.0f}/100")
        elif negative_count>=3 and not exit_signal.get("pump_hold"):
            result=engine.close(p,address,current,"AUTO SELL / EXIT")
            if result: events.append(f"🔴 AUTO SELL / EXIT {result['label']} | Price: {engine.price(current)} SDA | Profit: {result['closed_profit_sda']:+.2f} SDA | Score: {score:.0f}/100 | Mode: PAPER AUTO")
    engine._save_auto(state); return events

engine._auto_exit=auto_exit_v21
scanner_main.engine._auto_exit=auto_exit_v21
scanner_main.engine.SL_PCT=0.05
scanner_main.engine.BUY_THRESHOLD=engine.BUY_THRESHOLD

if __name__=="__main__": engine.main()
