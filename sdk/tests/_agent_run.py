# SPDX-License-Identifier: MIT
"""A host-free run in a named cell, for the agent engine's tests.

The engine keys a harness's per-cell home (``acp_client.cell_key``) by the
run's cell, which ``gen.run_info()`` reads from the installed bridge. A test
that only needs "some run in cell X" calls :func:`in_cell`: it sets the cell
on the run already installed, or installs a fresh host-free run there for
the test's duration.
"""
from __future__ import annotations


def in_cell(request, monkeypatch, workspace, cell_id: str):
    """Run the rest of the test inside a host-free run of cell ``cell_id``
    (workspace ``workspace`` when a new run is installed). Returns the run's
    bridge."""
    from collimate import native
    from collimate.testing import make_test_bridge

    current = native.current_bridge()
    if current is not None:
        monkeypatch.setattr(current, "cell_id", cell_id)
        return current
    workspace.mkdir(parents=True, exist_ok=True)
    bridge = make_test_bridge(workspace, cell_id=cell_id)
    monkeypatch.setattr(native, "_bridge", bridge)
    request.addfinalizer(bridge.close)
    return bridge
