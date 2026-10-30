"""The shipped entrypoints must bootstrap the shared runtime before importing it."""
import subprocess
import sys

from tests._workspace import GENERATORS


def test_agent_entrypoint_imports_in_real_isolated_process_P4F19(tmp_path):
    entrypoint = GENERATORS / 'agent/agent/agent.py'
    result = subprocess.run(
        [sys.executable, '-I', '-c',
         'import runpy,sys; runpy.run_path(sys.argv[1], run_name="startup_probe")', str(entrypoint)],
        cwd=tmp_path, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
