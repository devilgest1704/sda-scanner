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

STATE_FILES="state.json whale_state.json whale_data.json whale_history.json portfolio_data.json market_data.json market_analysis.json token_metadata.json liquidity_data.json paper_stats.json sidra_swap_discovery.json pending_signals.json positions.json wallet_data.json decision_state_v15.json paper_auto_state.json telegram_menu_state.json paper_buy_guard_state.json real_trade_state.json v25_candidate_state.json v25_learner_state.json v25_shadow_learning_v2.json v25_shadow_learning_summary.json v25_shadow_learning_false_negative_report.json v25_shadow_learning_event_report.json"

ensure_json_state() {
  python - <<'PY'
import json
from pathlib import Path

files = '''state.json whale_state.json whale_data.json whale_history.json portfolio_data.json market_data.json market_analysis.json token_metadata.json liquidity_data.json paper_stats.json sidra_swap_discovery.json pending_signals.json positions.json wallet_data.json decision_state_v15.json paper_auto_state.json telegram_menu_state.json paper_buy_guard_state.json real_trade_state.json v25_candidate_state.json v25_learner_state.json v25_shadow_learning_v2.json v25_shadow_learning_summary.json v25_shadow_learning_false_negative_report.json v25_shadow_learning_event_report.json'''.split()

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
      echo "STATE CHECKPOINT: push failed after 5 attempts." >&2
      return 1
    fi

    git fetch origin main
    if ! git rebase origin/main; then
      echo "STATE CHECKPOINT: rebase conflict; aborting rather than touching strategy/source." >&2
      git rebase --abort || true
      return 1
    fi
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

  python whale_scanner.py
  python market_scanner.py
  python technical_analysis.py
  python market_analysis_export.py
  python token_metadata.py
  python liquidity_scanner.py

  TELEGRAM_TOKEN="" python paper_engine_v19.py
  TELEGRAM_TOKEN="" python paper_stats.py

  ensure_json_state

  ended="$(date +%s)"
  elapsed=$((ended - started))
  echo "⏱️ SDA SCAN COMPLETE: ${elapsed}s"
}

# Compile once per worker, not once per minute.
python -m py_compile market_scanner.py liquidity_scanner.py main.py engine.py technical_analysis.py telegram_dashboard.py paper_engine_v19.py market_analysis_export.py v26_pump_hunter.py
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
