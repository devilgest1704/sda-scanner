"""Integration test: two independent writers racing on a temporary bare Git remote."""
import json
import subprocess
import tempfile
from pathlib import Path

def git(c, *args):
    return subprocess.run(["git", "-C", str(c), *args], check=True, text=True, capture_output=True).stdout.strip()

def write(c, name, obj):
    p = Path(c) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj) + "\n")

def test_concurrent_checkpoint():
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        remote, writer, agent = t / "remote.git", t / "writer", t / "agent"
        subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
        subprocess.run(["git", "clone", "-q", str(remote), str(writer)], check=True)
        for path in (writer,):
            git(path, "config", "user.email", "test@example.com")
            git(path, "config", "user.name", "Test")
        write(writer, "paper_stats.json", {"closed_trades": 552, "realized_pnl_sda": -1465.5})
        write(writer, "positions.json", {"positions": []})
        write(writer, "telemetry.json", {"origin": "base"})
        git(writer, "add", ".")
        git(writer, "commit", "-qm", "base")
        git(writer, "push", "-q", "origin", "HEAD:main")
        git(writer, "checkout", "-q", "-B", "main")
        subprocess.run(["git", "clone", "-q", "--branch", "main", str(remote), str(agent)], check=True)
        git(agent, "config", "user.email", "test@example.com")
        git(agent, "config", "user.name", "Agent")
        write(agent, "telemetry.json", {"origin": "agent"})
        git(agent, "add", ".")
        git(agent, "commit", "-qm", "agent telemetry")
        git(agent, "push", "-q", "origin", "HEAD:main")
        write(writer, "telemetry.json", {"origin": "scanner"})
        git(writer, "add", ".")
        git(writer, "commit", "-qm", "scanner checkpoint")
        git(writer, "fetch", "-q", "origin", "main")
        git(writer, "-c", "merge.directoryRenames=false", "rebase", "-X", "theirs", "origin/main")
        assert json.loads((writer / "telemetry.json").read_text())["origin"] == "scanner"
        git(writer, "push", "-q", "origin", "HEAD:main")
        git(agent, "pull", "-q", "--rebase", "origin", "main")
        write(agent, "paper_stats.json", {"closed_trades": 553, "realized_pnl_sda": -1470.0})
        git(agent, "add", ".")
        git(agent, "commit", "-qm", "agent ledger")
        git(agent, "push", "-q", "origin", "HEAD:main")
        git(writer, "fetch", "-q", "origin", "main")
        local = json.loads(git(writer, "show", "HEAD:paper_stats.json"))
        upstream = json.loads(git(writer, "show", "origin/main:paper_stats.json"))
        assert local["closed_trades"] < upstream["closed_trades"]

if __name__ == "__main__":
    test_concurrent_checkpoint()
    print("PASS: telemetry rebase and newer-ledger detection")
