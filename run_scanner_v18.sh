#!/usr/bin/env bash
set -euo pipefail

# Continuous scanner runner.
# One GitHub Actions job owns the scanner and starts a new scan as soon as the
# previous one finishes (or after the configured interval). This avoids the
# cron -> queued/pending workflow problem.
#
# SCANNER_CONTINUOUS=true  -> keep scanning until the job is stopped.
# SCANNER_INTERVAL_SECONDS -> target cadence between scan starts (default 60).
# SCANNER_STATE_CHECKPOINT_SECONDS -> push runtime state periodically (default 300).
#
# The scanner is paper-only. No source code is rewritten at runtime.

INTERVAL="${SCANNER_INTERVAL_SECONDS:-60}"
CHECKPOINT="${SCANNER_STATE_CHECKPOINT_SECONDS:-300}"
CONTINUOUS="${SCANNER_CONTINUOUS:-true}"

if ! [[ "$INTERVAL" =~ ^[0-9]+$ ]] || [ "$INTERVAL" -lt 1 ]; then
  echo "Invalid SCANNER_INTERVAL_SECONDS=$INTERVAL" >&2
  exit 1
fi
if ! [[ "$CHECKPOINT" =~ ^[0-9]+$ ]] || [ "$CHECKPOINT" -lt 30 ]; then
  echo "Invalid SCANNER_STATE_CHECKPOINT_SECONDS=$CHECKPOINT" >&2
  exit 1
fi

STATE_FILES="state.json whale_state.json whale_data.json whale_history.json portfolio_data.json market_analysis.json token_metadata.json liquidity_data.json paper_stats.json sidra_swap_discovery.json pending_signals.json positions.json v30_optimizer_trades.json wallet_data.json v30_pump_state.json v30_execution_state.json strategy_lab/candidate_outcomes.json strategy_lab/forward_shadow_state.json decision_state_v15.json paper_auto_state.json telegram_menu_state.json paper_buy_guard_state.json real_trade_state.json v25_candidate_state.json v25_learner_state.json v25_shadow_learning_v2.json v25_shadow_learning_summary.json v25_shadow_learning_false_negative_report.json v25_shadow_learning_event_report.json v30_pump_state.json"

ensure_json_state() {
  python - <<'PY'
import json
from pathlib import Path

files = '''state.json whale_state.json whale_data.json whale_history.json portfolio_data.json market_data.json market_analysis.json token_metadata.json liquidity_data.json paper_stats.json sidra_swap_discovery.json pending_signals.json positions.json v30_optimizer_trades.json wallet_data.json decision_state_v15.json paper_auto_state.json telegram_menu_state.json paper_buy_guard_state.json real_trade_state.json v25_candidate_state.json v25_learner_state.json v25_shadow_learning_v2.json v25_shadow_learning_summary.json v25_shadow_learning_false_negative_report.json v25_shadow_learning_event_report.json v30_pump_state.json'''.split()

for name in files:
    p = Path(name)
    if not p.exists() or p.stat().st_size == 0:
        p.write_text('{}\n', encoding='utf-8')
        continue
    try:
        json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        print(f'WARNING: resetting malformed state file: {name}')
        p.write_text('{}\n', encoding='utf-8')
PY
}

checkpoint_state() {
  if [ -z "${GITHUB_ACTIONS:-}" ]; then
    return 0
  fi

  git config user.name 'sda-scanner-bot'
  git config user.email 'scanner@github.local'

  git add $STATE_FILES
  # Never let a large local market_data blob leak into the checkpoint via a prior index state.
  git reset -q HEAD -- market_data.json 2>/dev/null || true
  if git diff --staged --quiet; then
    return 0
  fi

  git commit -m 'Update SDA scanner state'

  for attempt in 1 2 3 4 5; do
    if git push origin HEAD:main; then
      echo "STATE CHECKPOINT: push succeeded on attempt $attempt."
      return 0
    fi

    if [ "$attempt" = "5" ]; then
      echo "STATE CHECKPOINT: push failed after 5 attempts; keeping scanner alive for next checkpoint." >&2
      git reset -q HEAD -- market_data.json 2>/dev/null || true
      return 0
    fi

    # Runtime generates market_data.json but it is intentionally not versioned
    # by checkpoints. Stash all remaining worktree changes so they cannot block
    # rebasing the committed state onto a concurrently updated main.
    git stash push -u -m scanner-checkpoint-rebase >/dev/null 2>&1 || true
    git fetch origin main
    if ! git rebase origin/main; then
      echo "STATE CHECKPOINT: rebase conflict; aborting state push but keeping scanner alive." >&2
      git rebase --abort || true
      git stash pop >/dev/null 2>&1 || true
      return 0
    fi
    git stash pop >/dev/null 2>&1 || true
    git reset -q HEAD -- market_data.json 2>/dev/null || true
    sleep 2
  done
}

run_one_scan() {
  local started ended elapsed
  started="$(date +%s)"

  echo
  echo "════════════════════════════════════════════════════════════"
  echo "🚀 SDA SCAN START $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
  echo "════════════════════════════════════════════════════════════"

  # Supabase can return transient 5xx/522 errors. The whale scanner
  # retries those internally; if they still fail, skip this whole scan and
  # keep the long-lived worker alive for the next cadence.
  set +e
  local whale_started whale_elapsed
  whale_started="$(date +%s)"
  python whale_scanner.py
  local whale_rc=$?
  whale_elapsed=$(( $(date +%s) - whale_started ))
  echo "⏱️ SCAN STAGE whale_scanner: ${whale_elapsed}s (exit=${whale_rc})"
  set -e
  if [ "$whale_rc" -eq 75 ]; then
    echo "⚠️ WHALE SCAN SKIPPED: transient Supabase failure; worker remains alive."
    return 0
  elif [ "$whale_rc" -ne 0 ]; then
    echo "❌ WHALE SCANNER FAILED: exit $whale_rc" >&2
    return "$whale_rc"
  fi

  # Keep a single writer for positions.json. Do not launch a parallel exit worker.
  # Stage durations expose delays before the paper stop-loss evaluation.
  run_timed_stage() {
    local stage="$1"
    shift
    local stage_started stage_elapsed
    stage_started="$(date +%s)"
    "$@"
    stage_elapsed=$(( $(date +%s) - stage_started ))
    echo "⏱️ SCAN STAGE ${stage}: ${stage_elapsed}s"
    if [ "$stage_elapsed" -gt 120 ]; then
      echo "⚠️ SCAN STAGE SLOW: ${stage} took ${stage_elapsed}s (>120s)." >&2
    fi
  }

  run_timed_stage market_scanner python market_scanner.py
  run_timed_stage technical_analysis python technical_analysis.py
  run_timed_stage market_analysis_export python market_analysis_export.py
  run_timed_stage token_metadata python token_metadata.py
  run_timed_stage liquidity_scanner python liquidity_scanner.py

  run_timed_stage paper_engine env TELEGRAM_TOKEN="" python paper_engine_v19.py
  run_timed_stage paper_stats env TELEGRAM_TOKEN="" python paper_stats.py
  run_timed_stage scan_funnel python -c 'import v30_pump_hunter as v30; v30.write_scan_funnel()'
  # Make silent telemetry failures visible in Actions logs. This is a
  # diagnostic warning only; it must not stop paper risk/exit processing.
  python - <<'PY'
import json
from pathlib import Path
try:
    data = json.loads(Path("v30_execution_state.json").read_text(encoding="utf-8"))
    latest = (data.get("funnels") or [])[-1]
    snapshots = latest.get("candidate_snapshots")
    tokens = latest.get("tokens", 0)
    print(f"CANDIDATE TELEMETRY: tokens={tokens} snapshots={len(snapshots) if isinstance(snapshots,list) else 'MISSING'} at={latest.get('at')}")
    if tokens and not isinstance(snapshots, list):
        print("::warning::V30 candidate snapshots missing from latest funnel")
    elif tokens and not snapshots:
        print("::warning::V30 funnel saw tokens but saved no candidate snapshots")
except Exception as exc:
    print("::warning::V30 candidate telemetry state invalid:", repr(exc))
PY
  # Forward paper learning: independent virtual policy portfolios updated
  # from contemporaneous candidates and later verified per-token quotes.
  # Never prevent existing paper stop/exit evaluation on a telemetry failure.
  if ! python -m tools.forward_shadow_agent; then
    echo "::warning::Forward paper learning skipped this scan; existing state preserved."
  fi
  run_timed_stage training_data python tools/v30_collect_training_data.py
  # Research-only: join this cycle's fresh market quotes to prior candidate
  # decisions, and persist the archive with the scanner checkpoint.
  # Failure must not interrupt paper exits or the next scan.
  if ! python tools/candidate_price_tracker.py; then
    echo "::warning::Candidate outcome tracking failed; scanner continues."
  fi

  ensure_json_state

  ended="$(date +%s)"
  elapsed=$((ended - started))
  echo "⏱️ SDA SCAN COMPLETE: ${elapsed}s"
  if [ "$elapsed" -gt 120 ]; then
    echo "⚠️ PAPER RISK: scan duration ${elapsed}s exceeded 120s; paper stops were not evaluated continuously." >&2
  fi
}

# Compile once per worker, not once per minute.
python -m py_compile market_scanner.py liquidity_scanner.py main.py engine.py technical_analysis.py telegram_dashboard.py paper_engine_v19.py market_analysis_export.py v26_pump_hunter.py v30_pump_hunter.py
find . -type d -name __pycache__ -prune -exec rm -rf {} +

echo "🔄 CONTINUOUS SDA SCANNER"
echo "Target interval: ${INTERVAL}s"
echo "State checkpoint: ${CHECKPOINT}s"

last_checkpoint="$(date +%s)"
next_start="$(date +%s)"

while true; do
  now="$(date +%s)"
  if [ "$now" -lt "$next_start" ]; then
    sleep "$((next_start - now))"
  fi

  run_started="$(date +%s)"
  run_one_scan
  run_finished="$(date +%s)"

  if [ "$CONTINUOUS" != "true" ]; then
    checkpoint_state
    exit 0
  fi

  # Keep cadence based on scan start time. If a scan takes longer than the
  # interval, do not queue/sleep: start the next scan immediately.
  next_start=$((run_started + INTERVAL))
  if [ "$run_finished" -gt "$next_start" ]; then
    echo "⚠️ SCAN OVERRUN: $((run_finished-run_started))s > ${INTERVAL}s; next scan starts immediately."
    next_start="$run_finished"
  fi

  now="$(date +%s)"
  if [ $((now - last_checkpoint)) -ge "$CHECKPOINT" ]; then
    checkpoint_state
    last_checkpoint="$now"
  fi
done
