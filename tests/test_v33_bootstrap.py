import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools.strategy_evolution_agent import initial_state

ROOT = Path(__file__).resolve().parents[1]


class EvolutionBootstrapTests(unittest.TestCase):
    def test_bootstrap_before_checkpoint_without_touching_paper_ledger(self):
        bash=(ROOT/"run_scanner_v18.sh").read_text(encoding="utf-8")
        marker="ensure_json_state() {\n  python - <<'PY'\n"
        self.assertIn(marker,bash)
        code=bash.split(marker,1)[1].split("\nPY\n",1)[0]
        self.assertIn("strategy_lab/evolution_state.json",code)
        self.assertIn('strategy_lab/evolution_state.json',bash.split('STATE_FILES="',1)[1].split('"',1)[0])
        with tempfile.TemporaryDirectory() as directory:
            env={**os.environ,"PYTHONPATH":str(ROOT)}
            for _ in range(2):
                outcome=subprocess.run([sys.executable,"-c",code],cwd=directory,
                                       env=env,capture_output=True,text=True)
                self.assertEqual(outcome.returncode,0,outcome.stderr)
                path=Path(directory)/"strategy_lab/evolution_state.json"
                state=json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(state,initial_state())
                self.assertFalse((Path(directory)/"paper_stats.json").exists())
                self.assertFalse((Path(directory)/"positions.json").exists())


if __name__=="__main__":
    unittest.main()
