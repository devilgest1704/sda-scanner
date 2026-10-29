"""Reproducible quote-cohort search and observed PAPER entry attribution.

No model here can promote a strategy or reconstruct executable fills. The
search selects on TRAIN only; validation/test remain unopened if none passes.
"""
import argparse
import hashlib
import itertools
import json
from collections import defaultdict
from datetime import timedelta
from pathlib import Path
from tools.missed_pump_learning import normalize, summary


def cohort(rows):
    pnl = [float(r['closed_profit_sda']) for r in rows]
    return {'closed':len(rows), 'net_sda':round(sum(pnl),6),
            'wins':sum(x>0 for x in pnl)}


def attribution(ledger):
    groups = defaultdict(list)
    for row in sorted(ledger['closed_trades'],key=lambda r:r.get('opened_at','')):
        m=row.get('entry_metrics') or {}
        if m.get('paper_buy_mode')!='temporal_recovery':
            continue
        t=m.get('paper_temporal_recovery') or {}
        lane=t.get('lane','temporal_recovery') if t.get('ready') else 'unconfirmed_fallback'
        groups[lane].append(row)
    result={}
    for lane,rows in groups.items():
        cut=int(len(rows)*.7)
        holdout=rows[cut:]
        boundary=holdout[0]['opened_at'] if holdout else ''
        # Do not train on a position whose final outcome was still unknown
        # at the first later entry. This is descriptive, not portfolio replay.
        train=[r for r in rows[:cut] if r.get('closed_at','')<boundary]
        result[lane]={'overall':cohort(rows),'earlier':cohort(train),'later':cohort(holdout)}
    return result


def research(observations):
    # normalize() normally studies rejected entries. Here both admissions and
    # rejections are included, with all ORIGINAL frozen features/prices intact.
    records=[{**r,'eligible':False,'gates':r.get('gates') or ['observed_entry']}
             for r in observations]
    rows=normalize(records)
    a,b=int(len(rows)*.6),int(len(rows)*.8)
    train,valid,test=rows[:a],rows[a:b],rows[b:]
    if not valid or not test:
        return {'status':'INSUFFICIENT_DATA','can_promote':False}
    train=[r for r in train if r['at']<valid[0]['at']-timedelta(minutes=75)]
    valid=[r for r in valid if r['at']<test[0]['at']-timedelta(minutes=75)]
    ranges=(('m15','max',[.5,1,2,3,4]),('m15','min',[.5,1,2]),
            ('m1h','max',[2,4,6,8]),('m4h','max',[0,4,8,12,20]),
            ('m4h','min',[0,2]),('score','min',[50,60,70,80]),
            ('quality','min',[45,55,65]),('buy_ratio','min',[1.5,2,3]),
            ('trades_1h','min',[12,20,30]),('flow_15m','min',[20,60,150]),
            ('volume_1h','min',[500,1000]))
    alternatives=[(k,op,v) for k,op,vs in ranges for v in vs]
    rules=[(r,) for r in alternatives]+[(x,y) for x,y in itertools.combinations(alternatives,2)
                                             if x[0]!=y[0]]

    def pick(data,rule):
        selected=[]
        for r in data:
            f=r['features']
            if not (f['trades_1h']>=6 and f['volume_1h']>=250 and f['flow_1h']>0
                    and f['buy_ratio']>=1.1 and 0<=f['m1h']<=12 and 0<=f['m15']<=8):
                continue
            if all(f[k]>=v if op=='min' else f[k]<=v for k,op,v in rule):
                selected.append(r)
        return selected

    proposals=[]
    for rule in rules:
        stats=summary(pick(train,rule))
        if (stats['samples']>=20 and stats['unique_tokens']>=6
                and stats['net_sda_model']>0 and stats['profit_factor_model']>=1.15):
            objective=(stats['mean_sda_model']-.02*stats['max_drawdown_sda_model']
                       -.1*(len(rule)-1))
            proposals.append((objective,rule,stats))
    result={'method':'frozen_features_60m_observed_quotes_not_fills',
            'source_observations':len(observations),'valid_decorrelated_rows':len(rows),
            'train_rows':len(train),'validation_rows':len(valid),'test_rows':len(test),
            'purge_minutes':75,'tested_rules':len(rules),'qualifying_train_rules':len(proposals),
            'can_promote':False,'selected':None}
    if not proposals:
        return {**result,'status':'NO_PROFITABLE_TRAIN_RULE','holdout_opened':False}
    proposals.sort(reverse=True)
    _,rule,stats=proposals[0]
    return {**result,'status':'RESEARCH_ONLY_SINGLE_TRAIN_SELECTION','holdout_opened':True,
            'selected':{'rules':rule,'train':stats,'validation':summary(pick(valid,rule)),
                        'test':summary(pick(test,rule))}}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    paths=['positions.json','strategy_lab/candidate_outcomes.json']
    inputs=[Path(p).read_bytes() for p in paths]
    ledger,archive=[json.loads(c) for c in inputs]
    result={'paper_only':True,'can_promote':False,'source_at':archive.get('updated_at'),
            'input_sha256':{p:hashlib.sha256(c).hexdigest() for p,c in zip(paths,inputs)},
            'entry_attribution':attribution(ledger),
            'search':research(archive['observations']),
            'limitation':'Executed-trade cohorts are selected history, not counterfactual portfolio P/L; quote outcomes are not fills.'}
    output=Path(args.output);output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    main()
