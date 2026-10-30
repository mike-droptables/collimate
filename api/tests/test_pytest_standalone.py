"""Standalone behavior joins normal pytest while larger scripts retain their harness."""

import os
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPTS = (
    "test_audit_regressions.py",
    "test_channel_ops.py",
    "test_new_sdk_flow.py",
)


def _run_script(script, scratch):
    scratch.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "TMPDIR": str(scratch),
        "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
        "COLLIMATE_ACCOUNTS_CONFIG": str(scratch / "accounts.json"),
    }
    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=scratch,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"{script.name} exited {result.returncode}\n{result.stdout}\n{result.stderr}"
    )
    return result.stdout


@pytest.mark.parametrize("script", SCRIPTS)
def test_standalone_behavior_in_normal_pytest__F1904(script, tmp_path):
    output = _run_script(Path(__file__).with_name(script), tmp_path / "standalone")
    assert output.strip(), "The harness must execute and report its real checks"


def test_standalone_nonzero_exit_is_a_real_pytest_failure__F1904(tmp_path):
    script = tmp_path / "failure-control.py"
    script.write_text('import sys\nprint("synthetic assertion failed")\nsys.exit(3)\n')
    with pytest.raises(AssertionError, match="synthetic assertion failed"):
        _run_script(script, tmp_path / "failure-scratch")
