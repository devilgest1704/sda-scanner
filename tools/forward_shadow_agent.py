#!/usr/bin/env python3
"""Forward-only virtual paper challengers; no orders and no historical hindsight.

Every strategy gets the same timestamped candidate snapshots and subsequent
per-token market quotes. We keep separate simulated positions, cost and exit
journals for each strategy. This is research evidence, NOT executable fills.
"""
from __future__ import annotations
import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path

from tools.missed_pump_learning import safe_missed_entry
from tools.v33_flow_continuation import observed_flow_signal
from tools.v33_cost_aware_breakout import (cost_aware_breakout_signal,
                                           SIDE_FRICTION as V33_SIDE_FRICTION,
                                           TARGET_GROSS as V33_TARGET,
                                           TRAIL_GROSS as V33_TRAIL,
                                           MAX_HOURS as V33_MAX_HOURS,
                                           modeled_net_sda as v33_net_sda)
from tools.v32_breakout import (
    research_signal_from_analysis, breakout_research_eligible,
    sparse_volume_research_eligible,
)
from tools.strategy_evolution_agent import STATE as EVOLUTION_STATE, load as load_evolution, experiment_policies

from tools.v32_trend_runner import (apply_observed_quote, close_record as runner_close_record,
                                   stale_writeoff as runner_stale_writeoff,
                                   AFTER_TAKE_NO_QUOTE_HOURS)

STATE = Path("strategy_lab/forward_shadow_state.json")
SIZE_SDA = 50.0
FEE_AND_SLIPPAGE = 0.011  # each side; conservative 2.2% round-trip
STOP_PCT = -0.025
TAKE_PCT = 0.08
TRAIL_PCT = 0.035
MAX_HOURS = 4
MAX_POSITIONS = 4
COOLDOWN_HOURS = 6
MAX_QUOTE_GAP_MIN = 30
MAX_HISTORY = 5000
# Only entry-gate parameters actually supported by V30 runtime are varied.
POLICIES = {
    "baseline": {"entry_score": 68, "quality": 52, "impulse": 8, "buy_ratio": 1.10, "trades": 5},
    "quality": {"entry_score": 72, "quality": 56, "impulse": 8, "buy_ratio": 1.20, "trades": 12},
    "momentum": {"entry_score": 76, "quality": 58, "impulse": 10, "buy_ratio": 1.25, "trades": 14},
    "selective": {"entry_score": 80, "quality": 62, "impulse": 12, "buy_ratio": 1.35, "trades": 18},
    "score": {"entry_score": 84, "quality": 52, "impulse": 8, "buy_ratio": 1.10, "trades": 12},
    "quality_plus": {"entry_score": 68, "quality": 62, "impulse": 8, "buy_ratio": 1.15, "trades": 12},
    # Observational only: WATCH research is NOT a deployable Champion lane.
    "research_watch": {"entry_score": 68, "quality": 55, "impulse": 8, "buy_ratio": 1.20, "trades": 8},
    "research_watch_strict": {"entry_score": 76, "quality": 65, "impulse": 12, "buy_ratio": 1.45, "trades": 12},
    # V32 MACD arms are research-only and must NEVER become a Champion.
    # Identical numerical entry gates to baseline; MACD is the sole addition.
    "research_macd_antipump": {"entry_score": 68, "quality": 52, "impulse": 8, "buy_ratio": 1.10, "trades": 5},
    "research_macd_confirmation": {"entry_score": 68, "quality": 52, "impulse": 8, "buy_ratio": 1.10, "trades": 5},
    # Independent early-trend research may evaluate WATCH as well as IGNITION.
    "research_breakout_volume_trend": {
        "entry_score": 55, "quality": 48, "impulse": 4,
        "buy_ratio": 1.15, "trades": 8,
    },
    # Tests a three-swap high-volume ignition without compromising the PAPER
    # risk circuit. This arm cannot be promoted to Champion.
    "research_sparse_volume_ignition": {
        "entry_score": 68, "quality": 58, "impulse": 6,
        "buy_ratio": 1.5, "trades": 6,
    },
    # Same entry gates as baseline: isolates the impact of runner EXITS.
    # Research-only; never allowed to change the V30 Champion.
    "research_trend_runner": {"entry_score": 68, "quality": 52, "impulse": 8,
                              "buy_ratio": 1.10, "trades": 5},
    # A DIFFERENT research hypothesis: re-evaluate a previously strong but
    # stale signal only after a genuinely newer fresh quote. Never reuse the
    # stale quote as a fill. Only independent forward Shadow, no promotion.
    "research_temporal_recovery": {"entry_score": 64, "quality": 60,
                                   "impulse": 4, "buy_ratio": 1.20, "trades": 12},
    # Research ONLY: confirmed 15m observed buyers with a 1h turnover floor,
    # before the 4h surge matures. This cannot replace the PAPER Champion.
    "research_v33_flow_continuation": {"entry_score": 70, "quality": 56,
                                      "impulse": 8, "buy_ratio": 1.20, "trades": 12},
    # Independent WATCH-capable breakout arm: conservative net-of-cost model.
    # This does NOT enter the PAPER Champion or auto-promotion candidate set.
    "research_v33_cost_aware_breakout": {"entry_score": 55, "quality": 48,
                                        "impulse": 4, "buy_ratio": 1.15, "trades": 8},
}
BREAKOUT_RESEARCH_MODE = "research_breakout_volume_trend"
SPARSE_VOLUME_RESEARCH_MODE = "research_sparse_volume_ignition"
BREAKOUT_EVIDENCE_MODES = frozenset((BREAKOUT_RESEARCH_MODE, SPARSE_VOLUME_RESEARCH_MODE))
TREND_RUNNER_RESEARCH_MODE = "research_trend_runner"
TEMPORAL_RESEARCH_MODE = "research_temporal_recovery"
FLOW_V33_RESEARCH_MODE = "research_v33_flow_continuation"
COST_BREAKOUT_V33_MODE = "research_v33_cost_aware_breakout"
MACD_RESEARCH_MODES = frozenset(("research_macd_antipump", "research_macd_confirmation"))
MACD_BAR_SECONDS = {"5m": 300, "15m": 900, "1h_completed": 3600}
MIN_MACD_CLOSED_BARS = 35
MIN_MACD_TRADED_BARS = 21


def when(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except (ValueError, OverflowError):
        return None


def finite(value):
    try:
        x = float(value)
        return x if math.isfinite(x) else None
    except (ValueError, TypeError, OverflowError):
        return None


def positive(value):
    n = finite(value)
    return n if n is not None and n > 0 else None


def default_state():
    return {
        "schema": 1, "paper_only": True, "method": "forward_observed_quote_shadow_simulation",
        "last_snapshot_at": None, "updated_at": None, "runs": 0,
        "strategies": {name: {"filters": filters.copy(), "positions": {},
                             "trades": [], "last_entry": {}}
                       for name, filters in POLICIES.items()},
    }


def load_state(path=STATE):
    if not path.exists():
        return default_state()
    # A malformed ledger is never silently reset. Preserve the file for audit.
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != 1 or data.get("paper_only") is not True:
        raise ValueError("Invalid forward shadow ledger; refusing to overwrite")
    strategies = data.get("strategies")
    if not isinstance(strategies, dict):
        raise ValueError("Missing forward shadow strategies")
    for name in POLICIES:
        if name not in strategies:
            strategies[name] = default_state()["strategies"][name]
        st = strategies[name]
        if not isinstance(st, dict) or not isinstance(st.get("positions"), dict) or not isinstance(st.get("trades"), list):
            raise ValueError("Invalid forward shadow position/trade state")
    return data


def quote_map(market, now):
    # Only a valid per-token transaction timestamp can advance a virtual price.
    raw = market.get("tokens", {}) if isinstance(market, dict) else {}
    result = {}
    if not isinstance(raw, dict):
        return result
    for addr, token in raw.items():
        if not isinstance(token, dict):
            continue
        info = token.get("analysis", token)
        if not isinstance(info, dict):
            continue
        stamp = when(info.get("last_transaction") or token.get("last_transaction"))
        price = positive(info.get("price_in_sda"))
        if stamp and price and stamp <= now:
            result[str(addr).lower()] = (stamp, price)
    return result



def observed_macd(analysis, ts, frame):
    """Accept only completed, sufficiently liquid historical candle features.

    The technical layer computes indicators from transactions no later than its
    market snapshot; this guard also disallows future and expired candles.
    Missing/bad data is NEVER read as zero or a bullish signal.
    """
    raw = (analysis.get("technical") or {}).get(frame) if isinstance(analysis, dict) else None
    if not isinstance(raw, dict) or raw.get("available") is not True:
        return None
    end = when(raw.get("last_completed_at"))
    age = finite(raw.get("quote_age_seconds"))
    n_bars = finite(raw.get("completed_bars"))
    traded = finite(raw.get("traded_bars"))
    if (end is None or end > ts or (ts-end).total_seconds() > 2*MACD_BAR_SECONDS[frame]
            or age is None or age < 0 or age > max(600, 2*MACD_BAR_SECONDS[frame])
            or n_bars is None or n_bars < MIN_MACD_CLOSED_BARS
            or traded is None):
        return None
    if frame == "1h_completed":
        # An hourly indicator must represent actual CLOCK hours, never
        # 35 scattered trade-hour samples compressed into a fake series.
        from tools.v32_hourly_macd import (
            MACD_METHOD, MIN_LOOKBACK_TRADE_HOURS,
            MIN_RECENT_TRADE_HOURS, MAX_LOOKBACK_GAP_HOURS,
            MAX_RECENT_GAP_HOURS,
        )
        lookback = finite(raw.get("lookback_traded_hours"))
        lookback_gap = finite(raw.get("max_lookback_gap_hours"))
        recent = finite(raw.get("recent_traded_hours"))
        recent_gap = finite(raw.get("max_recent_gap_hours"))
        if (raw.get("method") != MACD_METHOD
                or lookback is None or lookback < MIN_LOOKBACK_TRADE_HOURS
                or lookback_gap is None or lookback_gap > MAX_LOOKBACK_GAP_HOURS
                or recent is None or recent < MIN_RECENT_TRADE_HOURS
                or recent_gap is None or recent_gap > MAX_RECENT_GAP_HOURS):
            return None
    elif traded < MIN_MACD_TRADED_BARS:
        return None
    value = raw.get("macd")
    if not isinstance(value, dict):
        return None
    hist = finite(value.get("histogram_pct"))
    delta = finite(value.get("histogram_delta_pct"))
    if (hist is None or delta is None or
            not isinstance(value.get("histogram_rising"), bool) or
            not isinstance(value.get("histogram_fading_2"), bool)):
        return None
    return {"histogram_pct": hist, "histogram_delta_pct": delta,
            "rising": value["histogram_rising"],
            "fading_2": value["histogram_fading_2"],
            "last_completed_at": end.isoformat(),
            "traded_bars": int(traded)}


def macd_research_decision(row, name):
    if name not in MACD_RESEARCH_MODES:
        return True
    m = row.get("v32_macd")
    if not isinstance(m, dict):
        return False
    # Use validated 15m evidence when available; otherwise fall back to
    # completed 1h observed candles. 5m/15m are unavailable for nearly all
    # sparse SDA tokens, but missing data must never become fake momentum.
    slow = m.get("15m") or m.get("1h_completed")
    if not isinstance(slow, dict):
        return False  # Fail closed in Shadow research only.
    if name == "research_macd_antipump":
        return not ((slow["histogram_pct"] < 0 and slow["histogram_delta_pct"] < 0)
                    or (slow["histogram_pct"] > 0 and slow["fading_2"]))
    fast = m.get("5m")
    if isinstance(m.get("15m"), dict):
        return (isinstance(fast, dict) and
                slow["histogram_pct"] > 0 and slow["rising"] and
                fast["histogram_pct"] > 0 and fast["rising"])
    # Only the slower 1h chart is available. Treat this as a distinct,
    # forward-only exploratory hypothesis; no automatic promotion.
    return slow["histogram_pct"] > 0 and slow["rising"]



def evaluate_temporal_recovery(state, candidates, at_time):
    """Join only TWO historical, independently timestamped scanner signals.

    An old score/impulse cannot justify a PAPER or real BUY. Research is allowed
    only with an entirely NEW quote <=300s old, true current liquidity/activity,
    and a strong earlier signal blocked by age (300-900s).
    The historical signal influences Shadow hypothesis membership ONLY and is
    never treated as a current price, market fill or completed profit.
    """
    store=state.setdefault("timing_research",{})
    last=store.get("last_signals")
    if not isinstance(last,dict):
        last={}
    events=store.get("events")
    if not isinstance(events,list):
        events=[]
    n_seen=0
    n_recovered=0
    for candidate in candidates:
        candidate["research_temporal_recovery_ready"]=False
        address=str(candidate.get("address") or "").lower()
        quoted=when(candidate.get("quote_at"))
        decision=when(candidate.get("at"))
        current_age=finite(candidate.get("data_age_sec"))
        current_price=positive(candidate.get("price_sda"))
        if (not address or quoted is None or decision is None
                or quoted>at_time or not 0<=(at_time-decision).total_seconds()<=600
                or current_age is None or not 0<=current_age<=900
                or abs((decision-quoted).total_seconds()-current_age)>30
                or current_price is None):
            continue
        prior=last.get(address)
        prior_at=when(prior.get("decision_at")) if isinstance(prior,dict) else None
        prior_quote=when(prior.get("quote_at")) if isinstance(prior,dict) else None
        prior_age=finite(prior.get("data_age_sec")) if isinstance(prior,dict) else None
        prior_score=finite(prior.get("score")) if isinstance(prior,dict) else None
        prior_impulse=finite(prior.get("impulse")) if isinstance(prior,dict) else None
        present_score=finite(candidate.get("score"))
        present_impulse=finite(candidate.get("impulse"))
        present_quality=finite(candidate.get("quality"))
        present_m1h=finite(candidate.get("m1h"))
        present_m15=finite(candidate.get("m15"))
        present_volume=finite(candidate.get("volume_1h"))
        present_flow=finite(candidate.get("flow_1h"))
        present_trades=finite(candidate.get("trades_1h"))
        present_ratio=finite(candidate.get("buy_ratio"))
        current_lane=candidate.get("phase")
        if (prior_at is not None and prior_quote is not None and
                prior_age is not None and prior_score is not None and
                prior_impulse is not None and prior.get("phase") in
                ("IGNITION","CONFIRMATION") and
                prior_score>=68 and prior_impulse>=8 and 300<prior_age<=900
                and 0<(decision-prior_at).total_seconds()<=600
                and quoted>prior_quote and quoted<=decision
                and current_age<=300 and
                current_lane in ("IGNITION","CONFIRMATION")
                and all(v is not None for v in (
                    present_score,present_impulse,present_quality,present_m1h,
                    present_m15,present_volume,present_flow,present_trades,present_ratio))
                and present_score>=64 and present_quality>=60 and
                present_impulse>=4 and 0<=present_m1h<=12
                and present_m15>=(1.5 if current_lane=="CONFIRMATION" else .8)
                and present_volume>=500 and present_flow>=150 and
                present_trades>=12 and present_ratio>=1.2):
            n_recovered+=1
            candidate["research_temporal_recovery_ready"]=True
            events.append({
                "at":decision.isoformat(),"address":address,
                "symbol":str(candidate.get("symbol") or "")[:20],
                "previous_age_sec":round(prior_age,1),
                "current_age_sec":round(current_age,1),
                "previous_score":round(prior_score,1),
                "current_score":round(present_score,1),
                "previous_impulse":round(prior_impulse,1),
                "current_impulse":round(present_impulse,1),
                "quote_advanced":True,"research_only":True,
                "current_gates":list(candidate.get("gates") or []),
            })
        n_seen+=1
        last[address]={
            "decision_at":decision.isoformat(),
            "quote_at":quoted.isoformat(),
            "data_age_sec":current_age,
            "score":present_score,"impulse":present_impulse,
            "phase":current_lane,
        }
    last={address:row for address,row in last.items()
          if (stamp:=when(row.get("decision_at"))) is not None
          and 0<=(at_time-stamp).total_seconds()<=600}
    store["last_signals"]=dict(list(last.items())[-200:])
    store["events"]=events[-100:]
    store["total_recovered"]=int(store.get("total_recovered") or 0)+n_recovered
    store["last_at"]=at_time.isoformat()
    return {"compared_current_candidates":n_seen,
            "recovered_this_cycle":n_recovered,
            "recovered_total":store["total_recovered"],
            "recent_examples":events[-3:],
            "paper_only":True,"changes_champion":False,
            "entry_requires_current_fresh_quote":True}


def eligible(c, filters, research_watch=False, macd_mode=None):
    if not isinstance(c, dict):
        return False
    # The autonomous missed-pump learner discovers bounded early-entry
    # threshold experiments, using only features saved at decision time.
    # Research Shadow ONLY; no Champion entry/exit rule or loss circuit bypass.
    if isinstance(macd_mode,str) and macd_mode.startswith("research_miss_"):
        return safe_missed_entry(c, filters)
    if macd_mode == BREAKOUT_RESEARCH_MODE:
        return breakout_research_eligible(c, filters)
    if macd_mode == COST_BREAKOUT_V33_MODE:
        return isinstance(c.get("v33_cost_breakout"), dict) and c["v33_cost_breakout"].get("eligible") is True
    if macd_mode == SPARSE_VOLUME_RESEARCH_MODE:
        return sparse_volume_research_eligible(c, filters)
    if macd_mode == TEMPORAL_RESEARCH_MODE and c.get("research_temporal_recovery_ready") is not True:
        return False
    if macd_mode == FLOW_V33_RESEARCH_MODE and not (
            isinstance(c.get("v33_flow"), dict) and c["v33_flow"].get("eligible") is True):
        return False
    phase = c.get("phase")
    if research_watch:
        if phase != "WATCH":
            return False
    elif phase not in ("IGNITION", "CONFIRMATION"):
        return False
    age = finite(c.get("data_age_sec"))
    score = finite(c.get("score"))
    quality = finite(c.get("quality"))
    impulse = finite(c.get("snapshot_impulse") if research_watch else c.get("impulse"))
    trades = finite(c.get("trades_1h"))
    ratio = finite(c.get("buy_ratio"))
    volume = finite(c.get("volume_1h"))
    flow = finite(c.get("flow_1h"))
    m15 = finite(c.get("m15"))
    m1h = finite(c.get("m1h"))
    values = (age, score, quality, impulse, trades, ratio, volume, flow, m15, m1h)
    if any(v is None for v in values):
        return False
    if research_watch:
        # Collect error/missed-opportunity evidence with stricter data safety.
        # These WATCH experiments are never eligible for automatic promotion.
        if not (0 <= age <= 180 and 0 < m1h <= 8 and m15 >= 0.8
                and volume >= 600 and flow >= 250 and trades >= 8):
            return False
    else:
        if not (0 <= age <= 300 and 0 <= m1h <= 12):
            return False
        # Common hard V31 safety checks cannot be tuned away.
        if not (volume >= 500 and flow >= 150
                and m15 >= (1.5 if phase == "CONFIRMATION" else 0.8)
                and trades >= 12):
            return False
    # Optional, agent-authored hypotheses are bounded by the SAME immutable
    # V31 quote/activity gates above. They can make entry stricter, not loosen.
    if "m1h_max" in filters and m1h > filters["m1h_max"]:
        return False
    if "m15_max" in filters and m15 > filters["m15_max"]:
        return False
    if "flow_1h_min" in filters and flow < filters["flow_1h_min"]:
        return False
    return (score >= filters["entry_score"] and quality >= filters["quality"]
            and impulse >= filters["impulse"] and ratio >= filters["buy_ratio"]
            and trades >= filters["trades"]
            and macd_research_decision(c, macd_mode))

def exit_profit(entry, price):
    # Notional paper net result after modelled entry/exit friction, in SDA.
    return SIZE_SDA * ((price * (1 - FEE_AND_SLIPPAGE)) /
                       (entry * (1 + FEE_AND_SLIPPAGE)) - 1)


def step(data, market, funnel):
    if not isinstance(data, dict) or not isinstance(funnel, dict):
        raise ValueError("Invalid inputs")
    ts = when(funnel.get("at"))
    market_ts = when(market.get("updated_at")) if isinstance(market, dict) else None
    previous = when(data.get("last_snapshot_at"))
    if ts is None or market_ts is None or abs((ts - market_ts).total_seconds()) > 600:
        raise ValueError("Candidate and market snapshots are not time-aligned")
    if previous and ts <= previous:
        return {"status": "already_processed", "at": funnel.get("at"), "opened": 0, "closed": 0}
    quotes = quote_map(market, market_ts)
    # The agent may introduce NEW immutable research hypotheses, never V30
    # production/PAPER config. Retired arms still settle their open positions.
    evolution = load_evolution(EVOLUTION_STATE, {})
    dynamic_policies = experiment_policies(evolution)
    research_policies = {**POLICIES, **dynamic_policies}
    active_dynamic = {e["id"] for e in evolution.get("experiments",[])
                      if isinstance(e,dict) and e.get("status")=="FORWARD_TEST"
                      and e.get("id") in dynamic_policies}
    for name, filters in dynamic_policies.items():
        data["strategies"].setdefault(name, {"filters":filters.copy(),
            "positions":{}, "trades":[], "last_entry":{}})
    candidates = funnel.get("candidate_snapshots", [])
    if not isinstance(candidates, list):
        raise ValueError("Missing candidate snapshots")
    ranked = sorted((dict(x) for x in candidates if isinstance(x, dict)),
                    key=lambda x: finite(x.get("score")) or 0, reverse=True)
    # Research-only MACD values are joined to SAME-SCAN market data. Never
    # rewrite the underlying V30 funnel, paper decisions, or market prices.
    market_tokens = market.get("tokens", {})
    for candidate in ranked:
        address = str(candidate.get("address") or "").lower()
        token = market_tokens.get(address, {}) if isinstance(market_tokens, dict) else {}
        info = token.get("analysis", token) if isinstance(token, dict) else {}
        candidate["v32_macd"] = (
            {frame: observed_macd(info, ts, frame)
             for frame in MACD_BAR_SECONDS}
            if market_ts <= ts else None
        )
        candidate["v32_breakout"] = (
            research_signal_from_analysis(info, ts, market_ts)
            if market_ts <= ts else None
        )
        # Only completed, original swap buckets from this exact scan, and no
        # historical hindsight or additional Champion execution path.
        candidate["v33_flow"] = observed_flow_signal(candidate, info, ts, market_ts)
        candidate["v33_cost_breakout"] = cost_aware_breakout_signal(
            candidate, candidate.get("v32_breakout"), POLICIES[COST_BREAKOUT_V33_MODE])
    # Research-only signal timing joins prior and current frozen signals.
    temporal_diagnostic=evaluate_temporal_recovery(data,ranked,ts)
    # Diagnostics distinguish "no signal" from missing telemetry fields.
    activity_fields = ("trades_1h", "buy_ratio", "flow_1h", "volume_1h", "m15", "m1h")
    feature_complete = sum(all(finite(row.get(k)) is not None for k in activity_fields)
                           for row in ranked)
    entry_lanes = sum(row.get("phase") in ("IGNITION", "CONFIRMATION") for row in ranked)
    watch_lanes = sum(row.get("phase") == "WATCH" for row in ranked)
    # Explain why the learner has no trades rather than silently saying
    # "collecting". Counts are strictly observational and never override gates.
    phase_counts = {}
    for row in ranked:
        phase = str(row.get("phase") or "UNKNOWN")
        phase_counts[phase] = phase_counts.get(phase, 0) + 1
    policy_qualified = {}
    policy_quote_ready = {}
    breakout_records = [c for c in ranked if isinstance(c.get("v32_breakout"), dict)]
    breakout_rejections = {}
    for c in breakout_records:
        for reason in set(c["v32_breakout"].get("rejection_reasons") or []):
            breakout_rejections[str(reason)] = breakout_rejections.get(str(reason), 0) + 1
    def same_time_quote(c):
        addr = str(c.get("address") or "").lower()
        q = quotes.get(addr)
        price = positive(c.get("price_sda"))
        return (q is not None and price is not None and q[0] <= ts
                and 0 <= (ts - q[0]).total_seconds() <= 300
                and abs(q[1]/price - 1) <= 0.015)
    for name, filters in research_policies.items():
        matches = [c for c in ranked if eligible(
            c, filters, name.startswith("research_watch"), name)]
        policy_qualified[name] = len(matches)
        policy_quote_ready[name] = sum(same_time_quote(c) for c in matches)
    # Diagnostic only: explain *why* short-timeframe MACD is unavailable.
    # Never convert a missing indicator to a buy recommendation.
    macd_market_reasons = {}
    macd_candidate_reasons = {}
    for frame in MACD_BAR_SECONDS:
        all_reasons = {}
        selected_reasons = {}
        for token in market_tokens.values() if isinstance(market_tokens, dict) else []:
            info = token.get("analysis", token) if isinstance(token, dict) else {}
            t = info.get("technical", {}) if isinstance(info, dict) else {}
            raw = t.get(frame) if isinstance(t, dict) else None
            label = (str(raw.get("reason") or "unknown")
                     if isinstance(raw, dict) and raw.get("available") is not True
                     else ("calculated" if isinstance(raw, dict) else "missing_analysis"))
            all_reasons[label] = all_reasons.get(label, 0) + 1
        for c in ranked:
            addr = str(c.get("address") or "").lower()
            tok = market_tokens.get(addr, {}) if isinstance(market_tokens, dict) else {}
            info = tok.get("analysis", tok) if isinstance(tok, dict) else {}
            technical = info.get("technical", {}) if isinstance(info, dict) else {}
            raw = technical.get(frame) if isinstance(technical, dict) else None
            if isinstance(c.get("v32_macd"), dict) and c["v32_macd"].get(frame):
                label = "valid_at_candidate"
            elif isinstance(raw, dict) and raw.get("available") is True:
                label = "failed_same_scan_or_age_guard"
            elif isinstance(raw, dict):
                label = str(raw.get("reason") or "not_available")
            else:
                label = "missing_analysis"
            selected_reasons[label] = selected_reasons.get(label, 0) + 1
        macd_market_reasons[frame] = dict(sorted(all_reasons.items()))
        macd_candidate_reasons[frame] = dict(sorted(selected_reasons.items()))
    flow_records = [c for c in ranked if isinstance(c.get("v33_flow"), dict)]
    cost_records = [c for c in ranked if isinstance(c.get("v33_cost_breakout"), dict)]
    cost_rejections = {}
    for c in cost_records:
        for reason in set(c["v33_cost_breakout"].get("rejection_reasons") or []):
            cost_rejections[reason] = cost_rejections.get(reason, 0) + 1
    flow_rejections = {}
    for c in flow_records:
        for reason in set(c["v33_flow"].get("rejection_reasons") or []):
            flow_rejections[reason] = flow_rejections.get(reason, 0) + 1
    rejected_gates = {}
    for c in ranked:
        for gate in c.get("gates", []):
            name = str(gate)
            rejected_gates[name] = rejected_gates.get(name, 0) + 1
    diagnostic = {
        "at": ts.isoformat(), "market_tokens_with_quote": len(quotes),
        "sampled_candidates": len(ranked),
        "all_candidate_phases": funnel.get("all_candidate_phases", {}),
        "sampled_candidate_phases": phase_counts,
        "policy_qualified": policy_qualified,
        "policy_quote_ready": policy_quote_ready,
        "macd_research_coverage": {
            "5m": sum(isinstance(c.get("v32_macd"), dict)
                      and isinstance(c["v32_macd"].get("5m"), dict) for c in ranked),
            "15m": sum(isinstance(c.get("v32_macd"), dict)
                       and isinstance(c["v32_macd"].get("15m"), dict) for c in ranked),
            "1h_completed": sum(isinstance(c.get("v32_macd"), dict)
                       and isinstance(c["v32_macd"].get("1h_completed"), dict) for c in ranked),
            "market_tokens_analyzed": len(market_tokens)
                                      if isinstance(market_tokens, dict) else 0,
            "sampled_candidates": len(ranked),
            "market_unavailability_by_reason": macd_market_reasons,
            "candidate_unavailability_by_reason": macd_candidate_reasons,
            "research_only": True,
            "thresholds_modified": False,
        },
        "breakout_research": {
            "measured": len(breakout_records),
            "eligible_from_transactions": sum(
                c["v32_breakout"].get("eligible") is True for c in breakout_records),
            "eligible_after_shadow_gates": policy_qualified.get(BREAKOUT_RESEARCH_MODE, 0),
            "rejection_reasons": dict(sorted(
                breakout_rejections.items(), key=lambda kv: (-kv[1], kv[0]))[:8]),
            "qualified_samples": [
                {"address": c.get("address"),
                 "phase": c.get("phase"),
                 "breakout_pct": c["v32_breakout"].get("breakout_pct"),
                 "volume_multiple": c["v32_breakout"].get("volume_multiple"),
                 "reason": c["v32_breakout"].get("reason")}
                for c in breakout_records if c["v32_breakout"].get("eligible") is True
            ][:5],
        },
        "v33_cost_breakout_research": {
            "mode": "RESEARCH_ONLY", "paper_champion_unchanged": True,
            "measured": len(cost_records),
            "eligible_after_cost_gate": sum(c["v33_cost_breakout"].get("eligible") is True for c in cost_records),
            "eligible_after_shadow_gates": policy_qualified.get(COST_BREAKOUT_V33_MODE, 0),
            "fresh_quote_ready": policy_quote_ready.get(COST_BREAKOUT_V33_MODE, 0),
            "rejections": dict(sorted(cost_rejections.items(), key=lambda item: (-item[1], item[0]))[:12]),
            "modeled_friction_per_side_pct": V33_SIDE_FRICTION * 100,
            "execution_parity_verified": False,
        },
        "v33_flow_research": {
            "mode": "RESEARCH_ONLY", "paper_champion_unchanged": True,
            "measured": len(flow_records),
            "has_observed_market_data": sum(c["v33_flow"].get("available") is True
                                             for c in flow_records),
            "observed_pressure_ready": sum(c["v33_flow"].get("eligible") is True
                                           for c in flow_records),
            "eligible_after_common_shadow_gates": policy_qualified.get(FLOW_V33_RESEARCH_MODE, 0),
            "fresh_price_quote_ready": policy_quote_ready.get(FLOW_V33_RESEARCH_MODE, 0),
            "rejections": dict(sorted(flow_rejections.items(),
                                      key=lambda item: (-item[1], item[0]))[:12]),
            "pool_liquidity_verified": False,
            "execution_parity_verified": False,
        },
        "most_common_rejection_gates": dict(sorted(
            rejected_gates.items(), key=lambda kv: (-kv[1], kv[0]))[:8]),
        "temporal_recovery_research": temporal_diagnostic,
    }
    opened = closed = 0
    for name, filters in research_policies.items():
        strategy = data["strategies"][name]
        positions = strategy["positions"]
        trades = strategy["trades"]
        last_entry = strategy.setdefault("last_entry", {})
        # Process exits BEFORE new entries. A new entry never receives its own
        # opening quote as a future exit, and cannot use unseen future prices.
        for address, position in list(positions.items()):
            quote = quotes.get(address)
            last = when(position.get("last_quote_at"))
            start = when(position.get("opened_at"))
            if start is None or last is None:
                raise ValueError("Corrupt forward position time")
            if quote is None or quote[0] <= last or quote[0] <= start:
                # Never allow stale/unquoted token positions to permanently
                # occupy the research portfolio. This is NOT an observed fill.
                unquoted_limit = (AFTER_TAKE_NO_QUOTE_HOURS
                                  if name == TREND_RUNNER_RESEARCH_MODE else 8)
                if (ts - start).total_seconds() >= unquoted_limit*3600:
                    if name == TREND_RUNNER_RESEARCH_MODE:
                        # Only the remaining 50% can be conservatively written
                        # off after a documented partial take. No fake fill.
                        stale_result = runner_stale_writeoff(position, ts, SIZE_SDA)
                        trades.append({
                            "strategy": name, "address": address,
                            "opened_at": position["opened_at"], "closed_at": ts.isoformat(),
                            "entry_price_sda": position["entry_price_sda"],
                            "quote_gap_minutes": round((ts-last).total_seconds()/60, 2),
                            **stale_result,
                        })
                    else:
                        trades.append({
                            "strategy": name, "address": address,
                            "opened_at": position["opened_at"], "closed_at": ts.isoformat(),
                            "entry_price_sda": position["entry_price_sda"],
                            "exit_price_sda": None, "pnl_sda": -SIZE_SDA,
                            "roi_pct": -100.0, "reason": "STALE_TIMEOUT",
                            "quote_gap_minutes": round((ts-last).total_seconds()/60,2),
                            "valid_for_learning": False,
                            "learning_exclusion_reasons": ["stale_timeout_no_observed_exit_quote"],
                            "notional_sda": SIZE_SDA,
                            "model": "conservative_risk_writeoff_no_observed_fill",
                            **({"v32_breakout_at_entry": position.get("v32_breakout_at_entry")}
                               if name in BREAKOUT_EVIDENCE_MODES else {}),
                            **({"v33_flow_at_entry": position.get("v33_flow_at_entry")}
                               if name == FLOW_V33_RESEARCH_MODE else {}),
                            **({"v33_cost_breakout_at_entry": position.get("v33_cost_breakout_at_entry")}
                               if name == COST_BREAKOUT_V33_MODE else {}),
                        })
                    del positions[address]
                    closed += 1
                continue
            stamp, price = quote
            gap = (stamp - last).total_seconds() / 60
            duration = (stamp - start).total_seconds() / 3600
            entry = positive(position.get("entry_price_sda"))
            if entry is None:
                raise ValueError("Corrupted forward shadow entry price")
            if name == TREND_RUNNER_RESEARCH_MODE:
                # Never replay an already stale/future transaction as if it
                # could have executed at the current scanner snapshot.
                if stamp > ts or (ts-stamp).total_seconds() > 300:
                    continue
                action, runner_reason = apply_observed_quote(
                    position, stamp, price, gap, SIZE_SDA, FEE_AND_SLIPPAGE
                )
                position["last_quote_at"] = stamp.isoformat()
                if action in ("hold", "partial"):
                    continue
                if action != "close":
                    raise ValueError("Unexpected Trend Runner action")
                result = runner_close_record(
                    position, stamp, price, runner_reason, gap,
                    SIZE_SDA, FEE_AND_SLIPPAGE
                )
                trades.append({
                    "strategy": name, "address": address,
                    "opened_at": position["opened_at"],
                    "closed_at": stamp.isoformat(),
                    **result,
                })
                del positions[address]
                closed += 1
                continue
            position["last_quote_at"] = stamp.isoformat()
            position["peak_price_sda"] = max(price, positive(position.get("peak_price_sda")) or entry)
            move = price / entry - 1
            trailing = price / position["peak_price_sda"] - 1
            target = V33_TARGET if name == COST_BREAKOUT_V33_MODE else TAKE_PCT
            trail = V33_TRAIL if name == COST_BREAKOUT_V33_MODE else TRAIL_PCT
            maximum_hours = V33_MAX_HOURS if name == COST_BREAKOUT_V33_MODE else MAX_HOURS
            reason = ("STOP" if move <= STOP_PCT else
                      "TAKE" if move >= target else
                      "TRAIL" if duration >= 0.25 and trailing <= -trail else
                      "TIME" if duration >= maximum_hours else None)
            if not reason:
                continue
            net = round((v33_net_sda(move) if name == COST_BREAKOUT_V33_MODE
                         else exit_profit(entry, price)), 6)
            # Gapped or delayed observed prices are recorded but excluded from
            # trustworthy model selection.
            exclusion_reasons = []
            if gap > MAX_QUOTE_GAP_MIN:
                exclusion_reasons.append("quote_gap_over_30m")
            if duration > maximum_hours + 0.5:
                exclusion_reasons.append("holding_time_over_max_plus_30m")
            if not (0.2 <= price/entry <= 4.0):
                exclusion_reasons.append("implausible_quote_jump")
            valid = not exclusion_reasons
            trades.append({
                "strategy": name, "address": address,
                "opened_at": position["opened_at"], "closed_at": stamp.isoformat(),
                "entry_price_sda": entry, "exit_price_sda": price,
                "pnl_sda": net, "roi_pct": round(net / SIZE_SDA * 100, 5),
                "reason": reason, "quote_gap_minutes": round(gap, 2),
                "valid_for_learning": valid,
                "learning_exclusion_reasons": exclusion_reasons,
                "notional_sda": SIZE_SDA,
                "model": "observed_quote_paper_not_executable_fill",
                **({"v32_macd_at_entry": position.get("v32_macd_at_entry")}
                   if name in MACD_RESEARCH_MODES else {}),
                **({"v32_breakout_at_entry": position.get("v32_breakout_at_entry")}
                   if name in BREAKOUT_EVIDENCE_MODES else {}),
                **({"v33_flow_at_entry": position.get("v33_flow_at_entry")}
                   if name == FLOW_V33_RESEARCH_MODE else {}),
                **({"v33_cost_breakout_at_entry": position.get("v33_cost_breakout_at_entry")}
                   if name == COST_BREAKOUT_V33_MODE else {}),
            })
            del positions[address]
            closed += 1
        # Refractory period prevents a volatile token from producing an
        # artificial stream of repeated simulated entries.
        if len(positions) < MAX_POSITIONS and (
                name not in dynamic_policies or name in active_dynamic):
            # Retired dynamic arms evaluate exits, but cannot open new entries.
            # Rank only this research lane toward the *earlier* breakouts.
            # All existing paper/Shadow policies retain their score ordering.
            choices = (sorted(
                ranked, key=lambda c: (
                    not (isinstance(c.get("v32_breakout"), dict)
                         and c["v32_breakout"].get("eligible") is True),
                    abs((finite((c.get("v32_breakout") or {}).get("breakout_pct")) or 0)-2.0),
                    -(finite((c.get("v32_breakout") or {}).get("volume_multiple")) or 0),
                    -(finite(c.get("score")) or 0),
                ))
                if name == BREAKOUT_RESEARCH_MODE else ranked)
            for c in choices:
                address = str(c.get("address") or "").lower()
                if not address or address in positions or not eligible(
                        c, filters, name.startswith('research_watch'), name):
                    continue
                quote = quotes.get(address)
                entry = positive(c.get("price_sda"))
                if quote is None or entry is None:
                    continue
                stamp, quote_price = quote
                # Avoid treating a stale or unrelated quote as an execution.
                if stamp > ts or (ts - stamp).total_seconds() > 300:
                    continue
                # Candidate and market prices are contemporaneous.
                if abs(quote_price / entry - 1) > 0.015:
                    continue
                previous_entry = when(last_entry.get(address))
                if previous_entry and (ts - previous_entry).total_seconds() < COOLDOWN_HOURS * 3600:
                    continue
                positions[address] = {
                    "opened_at": ts.isoformat(), "entry_price_sda": entry,
                    "last_quote_at": stamp.isoformat(), "peak_price_sda": entry,
                    **({"runner_legs": [], "runner_largest_quote_gap_min": 0.0,
                        "runner_entry_reference": "baseline_shadow_entry_gates"}
                       if name == TREND_RUNNER_RESEARCH_MODE else {}),
                    **({"v32_macd_at_entry": c.get("v32_macd")}
                       if name in MACD_RESEARCH_MODES else {}),
                    **({"v32_breakout_at_entry": c.get("v32_breakout")}
                       if name in BREAKOUT_EVIDENCE_MODES else {}),
                    **({"v33_flow_at_entry": c.get("v33_flow")}
                       if name == FLOW_V33_RESEARCH_MODE else {}),
                    **({"v33_cost_breakout_at_entry": c.get("v33_cost_breakout")}
                       if name == COST_BREAKOUT_V33_MODE else {}),
                }
                last_entry[address] = ts.isoformat()
                opened += 1
                break  # max one entry per strategy per scanner cycle
        if len(trades) > MAX_HISTORY:
            del trades[:-MAX_HISTORY]
        # The last entry timestamps can be pruned without changing positions.
        for key, value in list(last_entry.items()):
            d = when(value)
            if d and (ts - d).total_seconds() > 86400:
                del last_entry[key]
    diagnostic["missed_pump_learning"] = {
        "research_only": True,
        "active_policy_count": sum(
            name.startswith("research_miss_") and name in active_dynamic
            for name in dynamic_policies),
        "forward_qualified": {
            name: policy_qualified.get(name, 0)
            for name in dynamic_policies if name.startswith("research_miss_")},
        "paper_champion_unchanged": True,
    }
    diagnostic["strategy_evolution"] = {
        "active_experiments":len(active_dynamic),
        "tracked_experiments":len(dynamic_policies),
        "qualified":{name:policy_qualified.get(name,0) for name in dynamic_policies},
        "research_only":True, "paper_champion_unchanged":True,
    }
    runner_state = data["strategies"][TREND_RUNNER_RESEARCH_MODE]
    diagnostic["trend_runner_research"] = {
        "entry_reference": "same_entry_filters_as_baseline",
        "open": len(runner_state["positions"]),
        "open_after_first_take": sum(
            bool(p.get("runner_legs")) for p in runner_state["positions"].values()),
        "closed_trade_rows": len(runner_state["trades"]),
        "valid_closed": sum(
            t.get("valid_for_learning") is True for t in runner_state["trades"]),
        "partial_legs_are_not_closed_trades": True,
        "paper_only": True,
    }
    data["last_snapshot_at"] = ts.isoformat()
    data["updated_at"] = ts.isoformat()
    data["runs"] = int(data.get("runs", 0)) + 1
    data["last_diagnostic"] = diagnostic
    # Rolling counts show whether a lane was temporarily empty or persistently
    # blocked; history is bounded for small durable checkpoint files.
    history = data.get("diagnostic_history", [])
    if not isinstance(history, list):
        history = []
    history.append(diagnostic)
    data["diagnostic_history"] = history[-72:]
    return {"status": "processed", "at": ts.isoformat(), "opened": opened, "closed": closed,
            "qualified": policy_qualified, "quote_ready": policy_quote_ready,
            "quotes": len(quotes), "candidates": len(ranked),
            "complete_feature_rows": feature_complete,
            "entry_lane_rows": entry_lanes, "watch_lane_rows": watch_lanes,
            "policies": len(POLICIES),
            "macd_research_paper_only": True,
            "breakout_research_paper_only": True,
            "trend_runner_research_paper_only": True,
            "v33_flow_research_paper_only": True}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default=str(STATE))
    ap.add_argument("--market", default="market_analysis.json")
    ap.add_argument("--funnel", default="v30_execution_state.json")
    args = ap.parse_args()
    state_path = Path(args.state)
    state = load_state(state_path)
    market = json.loads(Path(args.market).read_text(encoding="utf-8"))
    execution = json.loads(Path(args.funnel).read_text(encoding="utf-8"))
    funnels = execution.get("funnels", [])
    if not isinstance(funnels, list) or not funnels:
        raise ValueError("No V30 funnel snapshots")
    outcome = step(state, market, funnels[-1])
    if outcome["status"] == "processed":
        state_path.parent.mkdir(parents=True, exist_ok=True)
        temp = state_path.with_suffix(".tmp")
        temp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(state_path)
    print("FORWARD SHADOW AGENT: " + json.dumps(outcome))


if __name__ == "__main__":
    main()
