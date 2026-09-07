import json
from pathlib import Path
p=json.loads(Path('positions.json').read_text(encoding='utf-8'))
t=p.get('closed_trades',[])
if not t:
    print('No closed paper trades yet.'); raise SystemExit
profits=[float(x.get('closed_profit_sda',0)) for x in t]; wins=[x for x in profits if x>0]; losses=[x for x in profits if x<=0]
print('SDA PAPER TRADING RESULTS')
print('--------------------------')
print('Closed trades:',len(t)); print('Winners:',len(wins)); print('Losers:',len(losses)); print(f'Win rate: {len(wins)/len(t)*100:.1f}%'); print(f'Total P/L: {sum(profits):+.2f} SDA')
print('Profit factor:', '∞' if not losses else f'{sum(wins)/abs(sum(losses)):.2f}')
