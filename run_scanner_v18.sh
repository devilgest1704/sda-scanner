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
OPTIMIZER_INTERVAL="${SCANNER_OPTIMIZER_INTERVAL_SECONDS:-3600}"

if ! [[ "$INTERVAL" =~ ^[0-9]+$ ]] || [ "$INTERVAL" -lt 1 ]; then
  echo "Invalid SCANNER_INTERVAL_SECONDS=$INTERVAL" >&2
  exit 1
fi
if ! [[ "$CHECKPOINT" =~ ^[0-9]+$ ]] || [ "$CHECKPOINT" -lt 30 ]; then
  echo "Invalid SCANNER_STATE_CHECKPOINT_SECONDS=$CHECKPOINT" >&2
  exit 1
fi

if ! [[ "$OPTIMIZER_INTERVAL" =~ ^[0-9]+$ ]] || [ "$OPTIMIZER_INTERVAL" -lt 900 ]; then
  echo "Invalid SCANNER_OPTIMIZER_INTERVAL_SECONDS=$OPTIMIZER_INTERVAL" >&2
  exit 1
fi

STATE_FILES="state.json whale_state.json whale_data.json whale_history.json portfolio_data.json market_analysis.json token_metadata.json liquidity_data.json paper_stats.json sidra_swap_discovery.json pending_signals.json positions.json v30_optimizer_trades.json wallet_data.json v30_pump_state.json v30_execution_state.json v30_no_buy_diagnostic.json strategy_lab/candidate_outcomes.json strategy_lab/missed_pump_learning.json strategy_lab/forward_shadow_state.json strategy_lab/evolution_state.json decision_state_v15.json paper_auto_state.json telegram_menu_state.json paper_buy_guard_state.json real_trade_state.json v25_candidate_state.json v25_learner_state.json v25_shadow_learning_v2.json v25_shadow_learning_summary.json v25_shadow_learning_false_negative_report.json v25_shadow_learning_event_report.json v30_pump_state.json strategy_lab/temporal_paper_trial.json strategy_lab/temporal_paper_trial_report.json"

ensure_json_state() {
  python - <<'PY'
import json
from pathlib import Path

files = '''state.json whale_state.json whale_data.json whale_history.json portfolio_data.json market_data.json market_analysis.json token_metadata.json liquidity_data.json paper_stats.json sidra_swap_discovery.json pending_signals.json positions.json v30_optimizer_trades.json wallet_data.json decision_state_v15.json paper_auto_state.json telegram_menu_state.json paper_buy_guard_state.json real_trade_state.json v25_candidate_state.json v25_learner_state.json v25_shadow_learning_v2.json v25_shadow_learning_summary.json v25_shadow_learning_false_negative_report.json v25_shadow_learning_event_report.json v30_pump_state.json'''.split()

# Never reset the accounting source-of-truth, even if a scanner stage
# wrote corrupt JSON. The dedicated paper_state_guard fails closed below.
files = [f for f in files if f not in ("positions.json", "paper_stats.json")]

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

# A new research ledger is structurally validated and must NEVER be treated
# as a generic {} scanner state. Create it BEFORE the first Git checkpoint.
from tools.strategy_evolution_agent import initial_state
evolution = Path("strategy_lab/evolution_state.json")
if not evolution.exists():
    evolution.parent.mkdir(parents=True,exist_ok=True)
    evolution.write_text(json.dumps(initial_state(),indent=2)+"\n",encoding="utf-8")
PY
}

checkpoint_state() {
  if [ -z "${GITHUB_ACTIONS:-}" ]; then
    return 0
  fi

  git config user.name 'sda-scanner-bot'
  git config user.email 'scanner@github.local'

  # Produce a small observed-state NO-BUY audit at each durable scanner
  # checkpoint (not per token). Never relax PAPER entry risk or interrupt
  # the current position ledger if telemetry is temporarily unreadable.
  if ! python -m tools.no_buy_diagnostic --output v30_no_buy_diagnostic.json; then
    echo "::warning::NO BUY checkpoint diagnosis unavailable; state/stop processing continues." >&2
  fi

  # Newly introduced research files may not exist in older checkouts.
  # Stage only existing or already tracked paths; never terminate the PAPER
  # risk worker because an optional state file is not yet initialized.
  for state_file in $STATE_FILES; do
    if [ -e "$state_file" ] || git ls-files --error-unmatch "$state_file" >/dev/null 2>&1; then
      git add -- "$state_file"
    fi
  done
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
      echo "::error::STATE CHECKPOINT: push failed after 5 attempts; stopping to protect unpublished PAPER state." >&2
      git reset -q HEAD -- market_data.json 2>/dev/null || true
      return 1
    fi

    # Runtime generates market_data.json but it is intentionally not versioned
    # by checkpoints. Stash all remaining worktree changes so they cannot block
    # rebasing the committed state onto a concurrently updated main.
    if ! git stash push -u -m scanner-checkpoint-rebase; then
      echo '::error::STATE CHECKPOINT: could not stash runtime state; stopping PAPER writer.' >&2
      return 1
    fi
    if ! git fetch origin main; then
      echo '::error::STATE CHECKPOINT: fetch failed; restoring runtime state.' >&2
      if ! git stash pop; then
        echo '::error::STATE CHECKPOINT: stash retained for recovery.' >&2
      fi
      return 1
    fi
    # Concurrent research/agent commits frequently change telemetry JSON.
    # Rebase the scanner's checkpoint with the scanner snapshot winning on
    # conflicting state files. This is only allowed if no newer PAPER ledger
    # exists upstream; otherwise stop and require explicit reconciliation.
    if ! python - <<'PY'
import json
import subprocess
import sys
from pathlib import Path

def read_at(ref, path):
    try:
        return json.loads(subprocess.check_output(
            ["git", "show", f"{ref}:{path}"], stderr=subprocess.DEVNULL))
    except (subprocess.CalledProcessError, ValueError):
        return None

def count_closed(data):
    if not isinstance(data, dict):
        return None
    for key in ("closed_trades",):
        value = data.get(key)
        if isinstance(value, int):
            return value
    return None

local = read_at("HEAD", "paper_stats.json")
remote = read_at("origin/main", "paper_stats.json")
lc, rc = count_closed(local), count_closed(remote)
if lc is None or rc is None or lc < rc:
    print("::error::CHECKPOINT: paper closed-trade count unavailable or upstream is newer; refuse automatic merge")
    sys.exit(1)
if lc == rc and local.get("realized_pnl_sda") != remote.get("realized_pnl_sda"):
    print("::error::CHECKPOINT: same close count but differing realized P/L; refuse automatic merge")
    sys.exit(1)
# A newer upstream position ledger may have same closed count but changed
# open positions. Never auto-resolve divergent positions or paper ledgers.
base = subprocess.check_output(["git", "merge-base", "HEAD", "origin/main"], text=True).strip()
for path in ("positions.json", "paper_stats.json"):
    old = read_at(base, path)
    upstream = read_at("origin/main", path)
    local_now = read_at("HEAD", path)
    if upstream != old and upstream != local_now:
        print(f"::error::CHECKPOINT: upstream {path} diverged; refuse automatic merge")
        sys.exit(1)
PY
    then
      echo "::error::STATE CHECKPOINT: unsafe concurrent PAPER state; stopping writer." >&2
      if ! git stash pop; then
        echo "::error::STATE CHECKPOINT: runtime stash retained for manual recovery." >&2
      fi
      return 1
    fi
    # The scanner owns its runtime state; upstream changes to research JSON
    # must not silently override the scanner's snapshot. Only the guarded
    # PAPER ledger may be published after a concurrent main update.
    if ! git -c merge.directoryRenames=false rebase -X theirs origin/main; then
      echo "::error::STATE CHECKPOINT: rebase conflict; stopping to protect unpublished PAPER positions and trade history." >&2
      git rebase --abort || true
      git stash pop >/dev/null 2>&1 || true
      return 1
    fi
    if ! git stash pop; then
      echo '::error::STATE CHECKPOINT: stash restore failed; stopping PAPER writer.' >&2
      return 1
    fi
    if [ -n "$(git ls-files -u)" ]; then
      echo '::error::STATE CHECKPOINT: unresolved index entries; stopping PAPER writer.' >&2
      return 1
    fi
    git reset -q HEAD -- market_data.json 2>/dev/null || true
    sleep 2
  done
}

# Scanner checkpoint is the reliable heartbeat. GitHub scheduled workflows
# may be delayed or skipped. Dispatch the independent optimizer after a
# successful scan checkpoint; never run the optimizer in this paper writer,
# never block paper exits, and never dispatch more often than its cadence.
last_optimizer_probe=0
maybe_dispatch_optimizer() {
  if [ -z "${GITHUB_ACTIONS:-}" ] || [ "$CONTINUOUS" != "true" ]; then
    return 0
  fi
  local now last_run last_epoch age
  now="$(date +%s)"
  if [ -z "${GH_TOKEN:-}" ] || [ -z "${GITHUB_REPOSITORY:-}" ]; then
    echo "::warning::AUTONOMOUS OPTIMIZER: missing Actions token or repository; schedule remains fallback."
    return 0
  fi
  if ! last_run="$(timeout 30s gh run list \
    --workflow autonomous_paper_agent.yml \
    --repo "$GITHUB_REPOSITORY" --branch main --limit 30 \
    --json createdAt,event \
    --jq '[.[] | select(.event == "schedule" or .event == "workflow_dispatch") | .createdAt] | first // empty')"; then
    echo "::warning::AUTONOMOUS OPTIMIZER: unable to query recent runs; retry next probe."
    return 0
  fi
  if [ -n "$last_run" ]; then
    if ! last_epoch="$(date -u -d "$last_run" +%s)"; then
      echo "::warning::AUTONOMOUS OPTIMIZER: invalid last run timestamp; retry next probe."
      return 0
    fi
    age=$((now - last_epoch))
    if [ "$age" -ge 0 ] && [ "$age" -lt "$OPTIMIZER_INTERVAL" ]; then
      echo "AUTONOMOUS OPTIMIZER: last scheduled/manual run ${age}s ago; next check later."
      return 0
    fi
  fi
  if timeout 30s gh workflow run autonomous_paper_agent.yml \
      --repo "$GITHUB_REPOSITORY" --ref main; then
    echo "AUTONOMOUS OPTIMIZER: hourly recovery run dispatched."
  else
    echo "::warning::AUTONOMOUS OPTIMIZER: dispatch failed; retry next probe."
  fi
  return 0
}


# Daily V31 research can be skipped by GitHub cron. Keep it separate from
# the hourly optimizer. Never do research in the scanner's paper writer.
maybe_dispatch_v31_research() {
  if [ -z "${GITHUB_ACTIONS:-}" ] || [ "$CONTINUOUS" != "true" ]; then
    return 0
  fi
  if [ -z "${GH_TOKEN:-}" ] || [ -z "${GITHUB_REPOSITORY:-}" ]; then
    echo "::warning::V31 RESEARCH: no Actions access; relying on scheduled run."
    return 0
  fi
  local runs gate_rc
  if ! runs="$(timeout 30s gh run list \
    --workflow strategy-lab.yml \
    --repo "$GITHUB_REPOSITORY" --branch main --limit 30 \
    --json createdAt,event,status,conclusion)"; then
    echo "::warning::V31 RESEARCH: run history unavailable; retry next probe."
    return 0
  fi
  gate_rc=0
  printf '%s\n' "$runs" | python -m tools.v31_schedule_guard || gate_rc=$?
  if [ "$gate_rc" -eq 0 ]; then
    if timeout 30s gh workflow run strategy-lab.yml \
        --repo "$GITHUB_REPOSITORY" --ref main; then
      echo "V31 RESEARCH: missing daily research workflow dispatched."
    else
      echo "::warning::V31 RESEARCH: dispatch failed; retry after next probe."
    fi
  elif [ "$gate_rc" -eq 2 ]; then
    echo "::warning::V31 RESEARCH: run data malformed; no dispatch."
  fi
  return 0
}

maybe_run_strategy_evolution() {
  # Every hour: diagnose actual losing trades, invent bounded hypotheses,
  # review existing forward outcomes and enroll new PAPER-only Shadow arms.
  # Run in the scanner workspace so new experiment policy is visible to
  # forward_shadow_agent WITHOUT restarting the worker.
  local now last_age
  now="$(date +%s)"
  last_age="$(python - <<'PY'
import json
from datetime import datetime,timezone
from pathlib import Path
p=Path("strategy_lab/evolution_state.json")
try:
    x=json.loads(p.read_text(encoding="utf-8"))
    t=datetime.fromisoformat(str(x["updated_at"]).replace("Z","+00:00"))
    print(max(0,int((datetime.now(timezone.utc)-t).total_seconds())))
except (OSError,ValueError,KeyError,TypeError,AttributeError):
    print(86400)
PY
)"
  if [ "$last_age" -lt 3600 ]; then
    return 0
  fi
  if python -m tools.strategy_evolution_agent; then
    echo "EVIDENCE AGENT: refreshed hypotheses from paper outcomes."
  else
    echo "::warning::EVIDENCE AGENT: hypothesis refresh failed; existing paper/Shadow policies retained."
  fi
  return 0
}

maybe_dispatch_research_and_optimizer() {
  maybe_run_strategy_evolution
  maybe_dispatch_optimizer
  maybe_dispatch_v31_research
  return 0
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
  # Read-only risk telemetry; must never interrupt exits or checkpoint safety.
  if ! python -m tools.paper_loss_circuit; then
    echo "::warning::24h paper risk circuit telemetry failed; continue existing stop/exit checks." >&2
  fi
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
  # Isolated PAPER experimental lane follows only newly-observed temporal
  # recovery Shadow signals. Its ledger is separate from positions.json.
  # Preserve main paper stop/exit evaluation even on trial failures.
  if ! python -m tools.temporal_paper_trial; then
    echo "::warning::Temporal PAPER trial unavailable; Champion ledger and stops unchanged." >&2
  fi
  run_timed_stage training_data python tools/v30_collect_training_data.py
  # Research-only: join this cycle's fresh market quotes to prior candidate
  # decisions, and persist the archive with the scanner checkpoint.
  # Failure must not interrupt paper exits or the next scan.
  if ! python tools/candidate_price_tracker.py; then
    echo "::warning::Candidate outcome tracking failed; scanner continues."
  fi

  ensure_json_state
  # Each completed scan must agree with the published paper accounting and
  # retain every earlier closed trade observed by this worker.
  # If it doesn't, STOP the worker before any checkpoint can overwrite
  # the last valid committed position ledger.
  python -m tools.paper_state_guard

  ended="$(date +%s)"
  elapsed=$((ended - started))
  echo "⏱️ SDA SCAN COMPLETE: ${elapsed}s"
  if [ "$elapsed" -gt 120 ]; then
    echo "⚠️ PAPER RISK: scan duration ${elapsed}s exceeded 120s; paper stops were not evaluated continuously." >&2
  fi
}

# Before any scanner writer starts, check the committed paper ledger.
# Missing/corrupt positions.json or paper_stats.json MUST never silently
# become {}. The per-worker sentinel then protects closed-trade monotonicity.
python -m tools.paper_state_guard

# Compile once per worker, not once per minute.
python -m py_compile market_scanner.py liquidity_scanner.py main.py engine.py technical_analysis.py telegram_dashboard.py paper_engine_v19.py market_analysis_export.py v26_pump_hunter.py v30_pump_hunter.py tools/temporal_paper_trial.py
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
    if [ $((now - last_optimizer_probe)) -ge 900 ]; then
      last_optimizer_probe="$now"
      # Network API calls run in the background: never delay the next paper
      # scan or risk/stop processing while checking the optimizer cadence.
      maybe_dispatch_research_and_optimizer &
    fi
  fi
done
