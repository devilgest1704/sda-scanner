"""Pin the long-running scanner worker to CI-approved executable code.

The worker starts on the newest code revision whose REAL trading tests are
green and keeps it for its whole life. New code only reaches production through
a controlled worker restart (scanner watchdog). Runtime state JSON is published
separately by tools/state_checkpoint.py without touching the checkout.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

TEST_WORKFLOW = "real-bridge-tests.yml"
CODE_SUFFIXES = (".py", ".sh", ".yml", ".yaml")
CODE_NAMES = {"requirements.txt", "vercel.json"}
CODE_PATHSPEC = [f":(glob)**/*{suffix}" for suffix in CODE_SUFFIXES] + [
    f":(glob)**/{name}" for name in sorted(CODE_NAMES)]
# Bot-generated PAPER configs: pinned for the worker's life, adopted only on restart.
EXECUTION_CONFIG = {"v30_agent_config.json", "paper_champion_config.json"}
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".pytest_cache"}
RECORD_NAME = "sda_worker_code.json"
STATUS_FILE = "real_worker_status.json"
LEGACY_STATUS_GRACE_SECONDS = 900


def is_code_path(path):
    name = str(path).replace("\\", "/").rsplit("/", 1)[-1]
    return name in CODE_NAMES or name.endswith(CODE_SUFFIXES)


def is_pinned_path(path):
    return is_code_path(path) or str(path).replace("\\", "/") in EXECUTION_CONFIG


def _now():
    return datetime.now(timezone.utc).isoformat()


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          check=True).stdout.strip()


def tree_entries(rev, predicate, cwd=None):
    out = subprocess.run(["git", "ls-tree", "-r", "-z", "--full-tree", rev], cwd=cwd,
                         capture_output=True, check=True).stdout.decode("utf-8")
    entries = {}
    for item in filter(None, out.split("\0")):
        meta, path = item.split("\t", 1)
        _mode, kind, blob = meta.split()
        if kind == "blob" and predicate(path):
            entries[path] = blob
    return entries


def blob_hash(content):
    return hashlib.sha1(b"blob %d\0" % len(content) + content).hexdigest()


def disk_entries(root, predicate=is_pinned_path):
    root = Path(root)
    entries = {}
    for directory, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in files:
            path = Path(directory, name)
            rel = path.relative_to(root).as_posix()
            if predicate(rel) and path.is_file() and not path.is_symlink():
                entries[rel] = blob_hash(path.read_bytes())
    return entries


def fingerprint(entries):
    digest = hashlib.sha256()
    for path in sorted(entries):
        digest.update(f"{path}\0{entries[path]}\n".encode("utf-8"))
    return digest.hexdigest()


def code_fingerprint(rev, cwd=None):
    return fingerprint(tree_entries(rev, is_code_path, cwd))


def expected_entries(code_rev, config_rev, cwd=None):
    entries = tree_entries(code_rev, is_code_path, cwd)
    entries.update(tree_entries(config_rev, lambda p: p in EXECUTION_CONFIG, cwd))
    return entries


def _api(url, token):
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}",
        "User-Agent": "sda-scanner-worker-code-pin"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def test_runs(repo, token, fetch=None):
    """REAL trading test runs for pushes to main (raises when unavailable: fail closed)."""
    if not repo or not token:
        raise RuntimeError("GITHUB_REPOSITORY and GH_TOKEN are required to verify CI approval")
    url = (f"https://api.github.com/repos/{repo}/actions/workflows/{TEST_WORKFLOW}"
           "/runs?branch=main&event=push&per_page=100")
    data = (fetch or _api)(url, token)
    runs = data.get("workflow_runs") if isinstance(data, dict) else None
    if not isinstance(runs, list):
        raise RuntimeError("Unexpected GitHub Actions response while verifying CI approval")
    return [run for run in runs if isinstance(run, dict) and run.get("head_sha")]


def resolve_approval(rev, runs, cwd=None, max_commits=200):
    """Newest code revision reachable from ``rev`` whose REAL trading tests passed."""
    approved, pending, failed = set(), set(), set()
    for run in runs:
        try:
            fp = code_fingerprint(run["head_sha"], cwd)
        except subprocess.CalledProcessError:
            continue
        if run.get("status") != "completed":
            pending.add(fp)
        elif run.get("conclusion") == "success":
            approved.add(fp)
        else:
            failed.add(fp)
    head_fp = code_fingerprint(rev, cwd)
    if head_fp in approved:
        state = "APPROVED"
    elif head_fp in pending:
        state = "PENDING"
    elif head_fp in failed:
        state = "FAILED"
    else:
        state = "UNTESTED"
    commits = git("log", "--format=%H", f"-{max_commits}", rev, "--", *CODE_PATHSPEC, cwd=cwd).split()
    for sha in commits:
        fp = code_fingerprint(sha, cwd)
        if fp in approved:
            return {"approved_sha": sha, "approved_fingerprint": fp,
                    "head_code_fingerprint": head_fp, "head_state": state}
    return {"approved_sha": None, "approved_fingerprint": None,
            "head_code_fingerprint": head_fp, "head_state": state}


def record_path(cwd=None):
    return Path(git("rev-parse", "--absolute-git-dir", cwd=cwd)) / RECORD_NAME


def load_record(cwd=None):
    try:
        return json.loads(record_path(cwd).read_text(encoding="utf-8"))
    except (OSError, ValueError, subprocess.CalledProcessError):
        return None


def overlay(approved_sha, cwd=None):
    """Replace unapproved executable files in the fresh checkout with approved ones."""
    head = tree_entries("HEAD", is_code_path, cwd)
    target = tree_entries(approved_sha, is_code_path, cwd)
    changed = sorted(p for p in set(head) | set(target) if head.get(p) != target.get(p))
    for path in changed:
        if path in target:
            git("checkout", approved_sha, "--", path, cwd=cwd)
        else:
            Path(cwd or ".", path).unlink(missing_ok=True)
    return changed


def pin(cwd=None, repo=None, token=None, run_id=None, fetch=None):
    """Startup only: choose approved code, materialise it, and record the pin."""
    root = Path(cwd or ".").resolve()
    head = git("rev-parse", "HEAD", cwd=root)
    approval = resolve_approval("HEAD", test_runs(repo, token, fetch), cwd=root)
    approved = approval["approved_sha"]
    if not approved:
        raise RuntimeError("No CI-approved executable code revision found; refusing to start REAL worker")
    replaced = [] if approval["head_state"] == "APPROVED" else overlay(approved, cwd=root)
    expected = fingerprint(expected_entries(approved, "HEAD", cwd=root))
    actual = fingerprint(disk_entries(root))
    if actual != expected:
        raise RuntimeError("Worker checkout does not match the approved code revision; refusing to start")
    record = {
        "worker_code_sha": approved, "worker_head_sha": head,
        "worker_code_approval": ("APPROVED" if not replaced
                                 else f"PINNED_TO_LAST_APPROVED (main code {approval['head_state']})"),
        "worker_code_fingerprint": approval["approved_fingerprint"],
        "worker_pinned_fingerprint": expected, "worker_started_at": _now(),
        "worker_run_id": str(run_id) if run_id else None, "overlaid_paths": replaced,
    }
    path = record_path(root)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return record


def verify(cwd=None):
    """Compare the code currently on disk/HEAD with the code pinned at startup."""
    root = Path(cwd or ".").resolve()
    record = load_record(root)
    if not record:
        return {"code_integrity": "UNPINNED", "code_checked_at": _now()}
    problems = []
    head = git("rev-parse", "HEAD", cwd=root)
    if head != record.get("worker_head_sha"):
        problems.append(f"HEAD moved {record.get('worker_head_sha')} -> {head}")
    current = disk_entries(root)
    if fingerprint(current) != record.get("worker_pinned_fingerprint"):
        expected = expected_entries(record["worker_code_sha"], record["worker_head_sha"], cwd=root)
        diff = sorted(p for p in set(expected) | set(current) if expected.get(p) != current.get(p))
        problems.append("executable files changed: " + ", ".join(diff[:20]))
    return {"code_integrity": "CODE_SHA_DRIFT" if problems else "OK",
            "code_drift": problems, "code_checked_at": _now()}


def _flag(name):
    value = os.environ.get(name)
    if value is None or value.strip().lower() not in ("true", "false"):
        return None
    return value.strip().lower() == "true"


def _read_json(root, name):
    try:
        value = json.loads((root / name).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _freshness(value, max_age=900):
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds()
        return age >= 0 and age <= max_age
    except (TypeError, ValueError, OverflowError):
        return False


def write_status(cwd=None, output=STATUS_FILE):
    """Worker heartbeat for the REAL dashboard including the pinned code identity."""
    root = Path(cwd or ".").resolve()
    status = {"source": "github-actions-real-worker",
              "enabled": _flag("REAL_TRADING_ENABLED"), "dry_run": _flag("REAL_TRADING_DRY_RUN"),
              "updated_at": _now()}
    ledger = _read_json(root, "real_trade_state.json")
    accounting = _read_json(root, "real_accounting_state.json")
    status["lastWorkerHeartbeat"] = status["updated_at"]
    status["ledger_updated_at"] = ledger.get("ledgerUpdatedAt") or ledger.get("updated_at")
    bridge = _read_json(root, "real_paper_bridge_state.json")
    wallet_sync = bridge.get("wallet_sync") if isinstance(bridge.get("wallet_sync"), dict) else {}
    status["lastSyncAttemptAt"] = wallet_sync.get("lastSyncAttemptAt") or ledger.get("lastSyncAttemptAt")
    status["lastSuccessfulWalletSync"] = wallet_sync.get("lastSuccessfulWalletSync") or ledger.get("lastSuccessfulWalletSync")
    status["lastSuccessfulReconciliation"] = wallet_sync.get("lastSuccessfulReconciliation") or ledger.get("lastSuccessfulReconciliation")
    status["lastBlockchainDataAt"] = wallet_sync.get("lastBlockchainDataAt") or ledger.get("lastBlockchainDataAt")
    status["ledgerUpdatedAt"] = wallet_sync.get("ledgerUpdatedAt") or ledger.get("ledgerUpdatedAt") or ledger.get("updated_at")
    status["lastSyncError"] = wallet_sync.get("lastSyncError") or ledger.get("lastSyncError")
    status["syncStatus"] = wallet_sync.get("syncStatus") or ledger.get("syncStatus") or "UNKNOWN"
    whale = _read_json(root, "whale_state.json")
    market_data = _read_json(root, "market_data.json")
    market_source = _read_json(root, "market_source_state.json")
    market = _read_json(root, "market_analysis.json")
    paper = _read_json(root, "paper_stats.json")
    bridge_health = bridge.get("sell_health") if isinstance(bridge.get("sell_health"), dict) else {}
    lane = _read_json(root, "strategy_lab/lane_research_state.json")

    whale_stamp = whale.get("last_successful_whale_at") or whale.get("updated_at")
    market_data_stamp = (market_source.get("last_successful_market_at")
                         or market_data.get("updated_at"))
    market_analysis_stamp = market.get("updated_at")
    whale_health = whale.get("whale_source_health")
    if whale_health != "SOURCE_UNAVAILABLE":
        whale_health = "HEALTHY" if _freshness(whale_stamp) else "STALE"
    market_source_health = market_source.get("source_health")
    if market_source_health == "SOURCE_UNAVAILABLE":
        market_health = "SOURCE_UNAVAILABLE"
    else:
        market_health = ("HEALTHY" if _freshness(market_data_stamp) and _freshness(market_analysis_stamp)
                         else "STALE")
    sell_stamp = bridge.get("last_real_sell_evaluation_at") or bridge_health.get("at")
    sell_total = len(ledger.get("positions", {})) if isinstance(ledger.get("positions"), dict) else 0
    sell_evaluated = int(bridge.get("real_positions_evaluated", bridge_health.get("evaluated", 0)) or 0)
    sell_recent = _freshness(sell_stamp)
    sell_failed = bridge_health.get("health") == "SELL_ERROR"
    sell_protection = ("ACTIVE" if sell_recent and not sell_failed and sell_evaluated >= sell_total
                       else "FAILING" if sell_recent and sell_failed else "UNKNOWN")
    reconciliation_stamp = (wallet_sync.get("lastSuccessfulReconciliation")
                             or ledger.get("lastSuccessfulReconciliation")
                             or bridge.get("last_real_reconciliation_at"))
    sync_status = wallet_sync.get("syncStatus") or ledger.get("syncStatus")
    if sync_status == "FRESH":
        reconciliation_health = "ACTIVE" if _freshness(reconciliation_stamp) else "FAILING"
    elif sync_status in {"STALE", "FAILED", "DEGRADED", "IN_PROGRESS"}:
        reconciliation_health = "FAILING"
    else:
        # Legacy workers have no snapshot health object. Preserve the old
        # reconciliation timestamp for diagnostics, but BUY admission still
        # requires syncStatus=FRESH in the scanner.
        reconciliation_health = "ACTIVE" if _freshness(reconciliation_stamp) else "UNKNOWN"

    status.update({
        "last_successful_scan_at": paper.get("updated_at"),
        "last_paper_evaluation_at": paper.get("updated_at"),
        "last_successful_whale_at": whale_stamp,
        "last_successful_market_at": market_data_stamp,
        "last_successful_market_analysis_at": market_analysis_stamp,
        "last_real_sell_evaluation_at": sell_stamp,
        "last_real_reconciliation_at": reconciliation_stamp,
        "lastSyncAttemptAt": status.get("lastSyncAttemptAt"),
        "lastSuccessfulWalletSync": status.get("lastSuccessfulWalletSync"),
        "lastSuccessfulReconciliation": reconciliation_stamp,
        "lastBlockchainDataAt": status.get("lastBlockchainDataAt"),
        "ledgerUpdatedAt": status.get("ledgerUpdatedAt"),
        "lastSyncError": status.get("lastSyncError"),
        "syncStatus": status.get("syncStatus"),
        "last_lane_research_update_at": lane.get("updated_at") or lane.get("last_run_at"),
        "data_source_health": {"market": market_health, "whale": whale_health},
        "market_source_health": market_health,
        "whale_source_health": whale_health,
        "real_sell_protection": sell_protection,
        "real_positions_evaluated": sell_evaluated,
        "real_positions_total": sell_total,
        "manual_reconciliation": reconciliation_health,
    })
    try:
        import real_trading_config as cfg
        status.update({"real_buy_size_sda": cfg.position_sda(),
                       "real_capital_cap_sda": cfg.TRADING_WALLET_CAPITAL_SDA,
                       "real_max_open_positions": cfg.MAX_OPEN_POSITIONS})
    except Exception:
        pass
    record = load_record(root) or {}
    for key in ("worker_code_sha", "worker_head_sha", "worker_code_approval", "worker_started_at",
                "worker_run_id", "worker_pinned_fingerprint"):
        status[key] = record.get(key)
    status.update(verify(root))
    status["health"] = "CODE_SHA_DRIFT" if status["code_integrity"] == "CODE_SHA_DRIFT" else "OK"
    if status["health"] == "CODE_SHA_DRIFT" or sell_protection != "ACTIVE":
        status["worker_health"] = "CRITICAL_FAILURE"
    elif (reconciliation_health != "ACTIVE" or whale_health != "HEALTHY"
          or market_health != "HEALTHY"):
        status["worker_health"] = "BUY_DATA_DEGRADED"
    else:
        status["worker_health"] = "HEALTHY"
    path = root / output
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(status, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return status


def watchdog_decision(run_id, run_sha, run_age, cwd=None, repo=None, token=None, fetch=None):
    """'replace' only when CI-approved code/config differs from what the worker runs."""
    root = Path(cwd or ".").resolve()
    approval = resolve_approval("HEAD", test_runs(repo, token, fetch), cwd=root)
    if not approval["approved_sha"]:
        return "keep", "no CI-approved code revision on main"
    expected = fingerprint(expected_entries(approval["approved_sha"], "HEAD", cwd=root))
    try:
        status = json.loads((root / STATUS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        status = {}
    if str(status.get("worker_run_id")) == str(run_id) and status.get("worker_pinned_fingerprint"):
        worker = status["worker_pinned_fingerprint"]
    elif int(run_age) < LEGACY_STATUS_GRACE_SECONDS:
        return "keep", "worker has not published its pinned code yet"
    else:
        # Worker started before code pinning existed: it runs its own run commit.
        worker = fingerprint(expected_entries(run_sha, run_sha, cwd=root))
    if worker == expected:
        return "keep", f"worker runs approved code {approval['approved_sha'][:12]}"
    return "replace", (f"approved code {approval['approved_sha'][:12]} "
                       f"(main code {approval['head_state']}) differs from worker")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("pin")
    sub.add_parser("sha")
    status = sub.add_parser("status")
    status.add_argument("--output", default=STATUS_FILE)
    watch = sub.add_parser("watchdog")
    watch.add_argument("--run-id", required=True)
    watch.add_argument("--run-sha", required=True)
    watch.add_argument("--run-age", type=int, required=True)
    same = sub.add_parser("same-code")
    same.add_argument("rev_a")
    same.add_argument("rev_b")
    args = parser.parse_args(argv)
    repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if args.command == "pin":
        record = pin(repo=repo, token=token, run_id=os.environ.get("GITHUB_RUN_ID"))
        print(f"WORKER_CODE_SHA={record['worker_code_sha']} ({record['worker_code_approval']})")
        for path in record["overlaid_paths"]:
            print(f"::warning::Unapproved main code not used by this worker: {path}")
        return 0
    if args.command == "sha":
        print((load_record() or {}).get("worker_code_sha") or "UNPINNED")
        return 0
    if args.command == "status":
        result = write_status(output=args.output)
        if result["code_integrity"] == "CODE_SHA_DRIFT":
            print(f"::error::CODE_SHA_DRIFT {json.dumps(result['code_drift'])[:1500]}", file=sys.stderr)
        print(f"WORKER CODE: {result.get('worker_code_sha')} integrity={result['code_integrity']}")
        return 0
    if args.command == "watchdog":
        decision, reason = watchdog_decision(args.run_id, args.run_sha, args.run_age, repo=repo, token=token)
        print(reason, file=sys.stderr)
        print(decision)
        return 0
    if args.command == "same-code":
        return 0 if code_fingerprint(args.rev_a) == code_fingerprint(args.rev_b) else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
