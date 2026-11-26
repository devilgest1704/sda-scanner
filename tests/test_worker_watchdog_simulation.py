import os
import subprocess
import tempfile
import textwrap
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _watchdog_script(head_branch="main", event_type="push"):
    workflow = (ROOT / ".github" / "workflows" / "scanner_watchdog.yml").read_text(encoding="utf-8")
    marker = "      - name: Dispatch scanner only when no worker is active"
    section = workflow.split(marker, 1)[1]
    run_marker = "        run: |\n"
    section = section.split(run_marker, 1)[1]
    script = section.split("\n      - name:", 1)[0]
    script = textwrap.dedent(script)
    return (script
        .replace("${{ github.event_name }}", "workflow_run")
        .replace("${{ github.event.workflow_run.head_branch }}", head_branch)
        .replace("${{ github.event.workflow_run.event }}", event_type)
        .replace("${{ github.repository }}", "devilgest1704/sda-scanner"))


def _run_watchdog(tmp_path, *, active="0", head_branch="main", event_type="push"):
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    dispatch_log = tmp_path / "dispatch.log"
    marker = tmp_path / "dispatched"

    commands = {
        "git": '#!/bin/sh\necho mainsha\n',
        "timeout": '#!/bin/sh\nshift 2\nexec "$@"\n',
        "sleep": '#!/bin/sh\nexit 0\n',
        "gh": '''#!/bin/sh
case "$1 $2" in
  "run list")
    case "$*" in
      *"--json databaseId,status,headSha,createdAt"*) exit 0 ;;
      *"--json databaseId,status,headSha"*) exit 0 ;;
      *"--json status"*)
        if [ "${FAKE_ACTIVE:-0}" = "1" ]; then
          echo 1
        elif [ -f "$DISPATCH_MARKER" ]; then
          echo 1
        else
          echo 0
        fi
        ;;
      *) echo "unexpected run list arguments" >&2; exit 90 ;;
    esac
    ;;
  "workflow run")
    echo dispatch >> "$DISPATCH_LOG"
    touch "$DISPATCH_MARKER"
    exit 0
    ;;
  *) echo "unexpected gh invocation: $*" >&2; exit 91 ;;
esac
''',
    }
    for name, body in commands.items():
        path = fakebin / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)

    script = _watchdog_script(head_branch=head_branch, event_type=event_type)
    syntax = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
    assert syntax.returncode == 0, syntax.stderr
    env = dict(os.environ)
    env.update({
        "PATH": str(fakebin) + os.pathsep + env.get("PATH", ""),
        "FAKE_ACTIVE": active,
        "DISPATCH_LOG": str(dispatch_log),
        "DISPATCH_MARKER": str(marker),
        "GH_TOKEN": "test-token",
    })
    result = subprocess.run(["bash", "-e", "-c", script], cwd=tmp_path, env=env,
                            text=True, capture_output=True, timeout=15)
    count = len(dispatch_log.read_text(encoding="utf-8").splitlines()) if dispatch_log.exists() else 0
    return result, count


def test_watchdog_dispatches_one_replacement_when_no_worker_exists(tmp_path):
    result, dispatch_count = _run_watchdog(tmp_path)
    assert result.returncode == 0, result.stderr + result.stdout
    assert "Scanner restart verified." in result.stdout
    assert dispatch_count == 1


def test_watchdog_does_not_dispatch_when_worker_is_already_active(tmp_path):
    result, dispatch_count = _run_watchdog(tmp_path, active="1")
    assert result.returncode == 0, result.stderr + result.stdout
    assert "already active/pending" in result.stdout
    assert dispatch_count == 0


def test_watchdog_ignores_non_main_workflow_completions(tmp_path):
    result, dispatch_count = _run_watchdog(tmp_path, head_branch="feature/recovery", event_type="pull_request")
    assert result.returncode == 0, result.stderr + result.stdout
    assert "Ignoring non-production workflow completion" in result.stdout
    assert dispatch_count == 0
