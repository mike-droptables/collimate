# SPDX-License-Identifier: MIT
"""Repo-wide pytest setup, shared by ``tests/`` and ``tests_live/``."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

# Test scratch goes to the repo's git-ignored ``tmp/``, not the machine's
# /tmp (often a small RAM-backed tmpfs). Pytest still numbers its runs and
# keeps only the last three there, so concurrent suites stay safe.
_TEMPROOT = Path(__file__).resolve().parent / "tmp"
_TEMPROOT.mkdir(exist_ok=True)
os.environ.setdefault("PYTEST_DEBUG_TEMPROOT", str(_TEMPROOT))


@pytest.fixture(autouse=True)
def _no_bridge_left_installed():
    """Fail the test that leaves a run bridge installed, and clear it. The
    bridge is process-global: a later test in the same worker would run
    against it (``tests._agent_run.in_cell`` reuses an installed bridge, its
    workspace included), and fail far from the test that leaked it."""
    from collimate import native
    yield
    leaked = native.current_bridge()
    if leaked is not None:
        native._clear_bridge()
        pytest.fail(f"the test left a run bridge installed (workspace {leaked.workspace})")


@pytest.fixture
def native_bridge(tmp_path):
    """Bind a host-free bridge for either suite, then clear and close it."""
    from collimate import native
    from collimate.testing import make_test_bridge

    bridge = make_test_bridge(tmp_path)
    native.set_bridge(bridge)
    try:
        yield bridge
    finally:
        native._clear_bridge()
        bridge.close()
