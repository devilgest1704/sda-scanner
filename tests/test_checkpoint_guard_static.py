#!/usr/bin/env python3
"""Offline regression checks for the scanner checkpoint safety guard."""
import subprocess
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'run_scanner_v18.sh'

def test_shell_syntax():
    subprocess.run(['bash', '-n', str(SCRIPT)], check=True)

def test_checkpoint_safety_guards_present():
    text = SCRIPT.read_text()
    assert 'unsafe concurrent PAPER state' in text
    assert 'git stash pop' in text
    assert 'git fetch origin main' in text
    assert 'git -c merge.directoryRenames=false rebase -X theirs origin/main' in text
    assert 'return 1' in text

if __name__ == '__main__':
    test_shell_syntax()
    test_checkpoint_safety_guards_present()
    print('checkpoint guard static checks passed')
