"""The shipped entrypoints must bootstrap the shared runtime before importing it."""
from pathlib import Path
import subprocess
import sys

import gen

from tests._workspace import GENERATORS


def test_agent_entrypoint_imports_in_real_isolated_process_P4F19(tmp_path):
    entrypoint = GENERATORS / 'agent/agent/agent.py'
    result = subprocess.run(
        # -I ignores PYTHONPATH, so the SDK packages' directory (an installed
        # distribution's site-packages, or sdk/src on the source-only leg) is
        # named outright; nothing else reaches the process.
        [sys.executable, '-I', '-c',
         'import runpy,sys; sys.path.insert(0, sys.argv[2]); '
         'runpy.run_path(sys.argv[1], run_name="startup_probe")', str(entrypoint),
         str(Path(gen.__file__).resolve().parents[1])],
        cwd=tmp_path, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
