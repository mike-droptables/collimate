"""Run ownership/deadline regressions alongside the existing auxiliary unit gate."""
from pathlib import Path
import subprocess


def test_supervisor_lifecycle_and_budgets__P4F09():
    path = Path(__file__).resolve().parents[1] / "runtime" / "verifier-supervisor.test.mjs"
    subprocess.run(["node", "--test", str(path)], check=True, timeout=30)
