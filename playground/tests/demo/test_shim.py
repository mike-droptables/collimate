"""The shim's pure seams, checked against the repo's own app code.

The shim runs inside pyodide, but two of its contracts with the app can be
verified here, so a main-code change that breaks them fails CI instead of a
visitor's boot: the demo runner's copy of ``ProcessManager.start``'s
post-spawn wiring, and the request guard's verdicts.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SHIM_DIR = Path(__file__).resolve().parents[2] / "shim"
if str(SHIM_DIR) not in sys.path:
    sys.path.insert(0, str(SHIM_DIR))

from collimate_playground import boot, demo_runner  # noqa: E402


def test_native_start_still_carries_the_wiring_the_demo_runner_copies():
    from process_manager.manager import ProcessManager
    demo_runner.check_native_wiring(ProcessManager.start)


def test_the_wiring_check_fails_loud_on_drift():
    async def start(self):
        self._stdout_task = asyncio.create_task(self._drain_stdout())  # noqa: F821

    with pytest.raises(RuntimeError, match="_microbatch_writer"):
        demo_runner.check_native_wiring(start)


@pytest.mark.parametrize("method,path,status", [
    ("POST", "/api/channels", 400),
    ("DELETE", "/api/channels/demo", 400),
    ("PUT", "/api/channels/demo/rename", 400),
    ("GET", "/api/accounts", 200),
    ("POST", "/api/accounts", 501),
])
def test_guard_answers_the_machine_bound_routes(method, path, status):
    verdict = boot.guard_verdict(method, path)
    assert verdict is not None and verdict[0] == status


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/channels"),
    ("GET", "/api/channels/demo/state/stream"),
    ("PUT", "/api/channels/demo/settings"),
    ("GET", "/api/state"),
    ("GET", "/api/runners"),
    ("GET", "/api/syncthing"),
])
def test_guard_forwards_everything_else(method, path):
    assert boot.guard_verdict(method, path) is None


def test_the_origin_guard_takes_the_pages_hostname():
    """boot() trusts the page's hostname in the app's origin guard; the
    guard must still keep its trusted names in a set."""
    from auth_middleware import origin_guard
    assert isinstance(origin_guard.trusted_hosts, set)
