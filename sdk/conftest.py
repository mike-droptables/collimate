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
