"""Publish runtime state JSON to main without touching the worker's checkout.

The commit is built with a temporary index on top of the freshly fetched remote
main. HEAD, the index and every executable file of the running worker stay
exactly as they were at startup: no pull, rebase, reset, stash or checkout.
A state file changed concurrently on main by another writer is a conflict:
nothing is pushed and the next checkpoint / a restarted worker resolves it.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from tools.worker_code_pin import EXECUTION_CONFIG, is_code_path

STATE_SUFFIXES = (".json", ".jsonl")
RECORD_NAME = "sda_state_checkpoint.json"
BOT_ENV = {"GIT_AUTHOR_NAME": "sda-scanner-bot", "GIT_AUTHOR_EMAIL": "scanner@github.local",
           "GIT_COMMITTER_NAME": "sda-scanner-bot", "GIT_COMMITTER_EMAIL": "scanner@github.local"}


class StateUnchanged(RuntimeError):
    pass


def _git(args, cwd, env=None, check=True):
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                            env={**os.environ, **(env or {})}, timeout=120)
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, ["git", *args],
                                            result.stdout, result.stderr)
    return result


def is_state_path(path):
    path = str(path).replace("\\", "/")
    return (path.endswith(STATE_SUFFIXES) and not is_code_path(path)
            and path not in EXECUTION_CONFIG and not path.startswith((".git/", ".github/")))


def expand(paths, cwd):
    root = Path(cwd)
    files = []
    for item in paths:
        target = root / item
        if target.is_dir():
            files += [p.relative_to(root).as_posix() for p in sorted(target.rglob("*"))
                      if p.is_file() and p.suffix in STATE_SUFFIXES]
        elif target.is_file():
            files.append(Path(item).as_posix())
    for path in files:
        if not is_state_path(path):
            raise ValueError(f"Refusing to publish non-state path through state checkpoint: {path}")
    return sorted(set(files))


def _blob(rev, path, cwd):
    result = _git(["rev-parse", "--verify", "--quiet", f"{rev}:{path}"], cwd, check=False)
    return result.stdout.strip() or None


def _record_file(cwd):
    return Path(_git(["rev-parse", "--absolute-git-dir"], cwd).stdout.strip()) / RECORD_NAME


def _load_record(cwd):
    try:
        value = json.loads(_record_file(cwd).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_record(cwd, record):
    path = _record_file(cwd)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def push_state(paths, message="Update SDA scanner state", *, cwd=None, remote="origin",
               branch="main", attempts=5, require_changed=(), sleep=time.sleep):
    cwd = str(Path(cwd or ".").resolve())
    files = expand(paths, cwd)
    record = _load_record(cwd)
    local = {p: _git(["hash-object", "-w", "--", p], cwd).stdout.strip() for p in files}
    base = {p: record.get(p) or _blob("HEAD", p, cwd) for p in files}
    changed = [p for p in files if local[p] != base[p]]
    missing = [p for p in require_changed if Path(p).as_posix() not in changed]
    if missing:
        raise StateUnchanged(f"State not changed since last durable checkpoint: {', '.join(missing)}")
    if not changed:
        return {"status": "NOOP", "paths": []}
    for attempt in range(1, attempts + 1):
        if _git(["fetch", "--quiet", remote, branch], cwd, check=False).returncode != 0:
            sleep(2)
            continue
        remote_sha = _git(["rev-parse", "FETCH_HEAD"], cwd).stdout.strip()
        publish, conflicts = {}, []
        for path in changed:
            theirs = _blob(remote_sha, path, cwd)
            if theirs == local[path]:
                continue
            if theirs != base[path]:
                conflicts.append(path)
            else:
                publish[path] = local[path]
        if conflicts:
            return {"status": "CONFLICT", "paths": conflicts, "remote": remote_sha}
        if not publish:
            record.update({p: local[p] for p in changed})
            _save_record(cwd, record)
            return {"status": "NOOP", "paths": []}
        with tempfile.TemporaryDirectory() as tmp:
            env = {"GIT_INDEX_FILE": str(Path(tmp) / "index"), **BOT_ENV}
            _git(["read-tree", remote_sha], cwd, env)
            for path, blob in publish.items():
                _git(["update-index", "--add", "--cacheinfo", f"100644,{blob},{path}"], cwd, env)
            tree = _git(["write-tree", "--missing-ok"], cwd, env).stdout.strip()
        commit = _git(["commit-tree", tree, "-p", remote_sha, "-m", message], cwd, BOT_ENV).stdout.strip()
        if _git(["push", "--quiet", remote, f"{commit}:refs/heads/{branch}"], cwd, check=False).returncode == 0:
            record.update({p: local[p] for p in changed})
            _save_record(cwd, record)
            return {"status": "PUSHED", "commit": commit, "paths": sorted(publish), "attempt": attempt}
        sleep(2)
    return {"status": "FAILED", "paths": changed}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--message", default="Update SDA scanner state")
    parser.add_argument("paths", nargs="+")
    args = parser.parse_args(argv)
    result = push_state(args.paths, args.message)
    status = result["status"]
    if status == "PUSHED":
        print(f"STATE CHECKPOINT: pushed {len(result['paths'])} state file(s) on attempt {result['attempt']}.")
    elif status == "NOOP":
        print("STATE CHECKPOINT: no state changes.")
    elif status == "CONFLICT":
        print("::warning::STATE CHECKPOINT: state changed concurrently on main "
              f"({', '.join(result['paths'])}); nothing pushed, worker code unchanged.", file=sys.stderr)
    else:
        print("::warning::STATE CHECKPOINT: push failed after retries; worker code unchanged.", file=sys.stderr)
    return 0 if status in ("PUSHED", "NOOP") else 1


if __name__ == "__main__":
    sys.exit(main())
