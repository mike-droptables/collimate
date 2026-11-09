"""``gen.cell_state_dir()``: the cell's own state across runs, in its channel
folder (``$COLLIMATE_CELL_STATE``, set by the host for a run with a cell),
and the Agent harness keeping its sessions there."""
from __future__ import annotations

import stat
from pathlib import Path

import gen


def test_cell_state_dir_is_the_hosts_value(monkeypatch, tmp_path):
    monkeypatch.setenv("COLLIMATE_CELL_STATE", str(tmp_path / "cell-state" / "c1"))
    assert gen.cell_state_dir() == str(tmp_path / "cell-state" / "c1")


def test_a_run_without_a_cell_has_no_cell_state(monkeypatch):
    monkeypatch.delenv("COLLIMATE_CELL_STATE", raising=False)
    assert gen.cell_state_dir() is None
    monkeypatch.setenv("COLLIMATE_CELL_STATE", "  ")
    assert gen.cell_state_dir() is None


def test_the_agent_harness_keeps_its_state_in_the_cell_state_dir(monkeypatch, tmp_path):
    from _agent_shared import harness_homes
    cell_state = tmp_path / "Channels" / "chan" / "cell-state" / "cell-one"
    monkeypatch.setenv("COLLIMATE_CELL_STATE", str(cell_state))
    root, state = harness_homes._state_dir("codex")
    assert (root, state) == (str(cell_state), str(cell_state / "codex"))
    with harness_homes._state_lock(root, state):
        pass
    for level in (cell_state, cell_state / "codex"):
        assert stat.S_IMODE(level.stat().st_mode) == 0o700, level


def test_a_dev_run_gets_a_temporary_state_dir(monkeypatch):
    from _agent_shared import harness_homes
    monkeypatch.delenv("COLLIMATE_CELL_STATE", raising=False)
    monkeypatch.setattr(harness_homes, "_DEV_STATE", [])
    root, state = harness_homes._state_dir("goose")
    assert Path(root).name.startswith("collimate-agent-state-") and state == str(Path(root) / "goose")
    assert harness_homes._state_dir("goose") == (root, state)
