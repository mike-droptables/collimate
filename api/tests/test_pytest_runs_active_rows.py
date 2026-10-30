# SPDX-License-Identifier: MIT
"""The /runs/active rows: what a client needs to reattach to a channel's
live runs after a reload — the cell, the runner, the run folder and the live
phase. Synthetic managers throughout."""
from __future__ import annotations

import asyncio
import uuid

import pytest

from fake_manager import FakeManager

CHANNEL = str(uuid.uuid5(uuid.NAMESPACE_URL, 'active-rows:primary'))
OTHER_CHANNEL = str(uuid.uuid5(uuid.NAMESPACE_URL, 'active-rows:other'))


@pytest.fixture(autouse=True)
def registered_channels(fresh_cwd, monkeypatch):
    import state
    from database import get_db_connection
    from models import AppState, Channel
    primary = Channel(id=CHANNEL, name='Primary')
    other = Channel(id=OTHER_CHANNEL, name='Other')
    monkeypatch.setattr(state, 'state', AppState(channels=[primary, other], active_channel_id=primary.id))
    get_db_connection(CHANNEL).close()


def _Mgr(**over):
    """A live run as /runs/active reads it."""
    fields = dict(
        channel=CHANNEL, cell_id="cell-1", runner_id="synth", runner_name="Synth",
        label="synth-aaaaaaaa", folder="_runs/synth-aaaaaaaa",
        generator_name="synthetic-gen", current_phase=None,
        current_phase_message="",
    )
    fields.update(over)
    return FakeManager(**fields)


def test_active_runs_rows_name_the_cell_runner_label_and_folder__D22():
    """Each row is what the run light, the overlay and the top status need:
    the cell, the runner, the label and the run folder. Another channel's
    runs are left out."""
    from routes.runs import queries

    orig = queries.active_managers
    queries.active_managers = {
        "r1": _Mgr(),
        "r2": _Mgr(cell_id="cell-2", label="synth-bbbbbbbb", folder="_runs/synth-bbbbbbbb"),
        "other": _Mgr(channel=OTHER_CHANNEL),
    }
    try:
        rows = asyncio.run(queries.list_active_runs(CHANNEL))
    finally:
        queries.active_managers = orig
    assert [r["run_id"] for r in rows] == ["r1", "r2"]
    assert rows[0] == {
        "run_id": "r1", "cell_id": "cell-1", "cell_name": "", "runner_id": "synth",
        "runner_name": "Synth", "label": "synth-aaaaaaaa",
        "folder": "_runs/synth-aaaaaaaa", "generator_name": "synthetic-gen",
        "started_at": None, "listening": False, "phase": "", "phase_message": "", "stopping": False,
        "surfaces": [], "effective_grant": None, "adopted": False, "harness": None, "harness_label": None,
        "cli_locus": None,
    }


def test_active_runs_rows_carry_the_effective_grant_and_its_adoption():
    """A harness-bound run's row says the grant it runs under and whether
    its harness's minimum raised it, and for which harness — what the
    Status view's adopted-grant line reads. A queued run carries the same
    from its StartingRun."""
    from collimate.harness_catalog import HARNESSES
    from routes.runs import create, queries

    grant = {"update_packages": False, "docker": "full", "internet": "full",
             "internet_allow": [], "commands": "deny"}
    orig_active, orig_starting = queries.active_managers, create._starting
    queries.active_managers = {
        "r1": _Mgr(effective_grant=grant, adopted=True, harness="qwen-code"),
        "r2": _Mgr(cell_id="cell-2", effective_grant={**grant, "docker": "none"}, adopted=False,
                   harness="pydantic-ai"),
    }
    create._starting = {"r3": create.StartingRun(
        run_id="r3", channel=CHANNEL, cell_id="cell-3", runner_id="synth", runner_name="Synth",
        label="synth-cccccccc", folder="_runs/synth-cccccccc", generator_name="synthetic-gen",
        effective_grant=grant, adopted=True, harness="codex")}
    try:
        rows = {r["run_id"]: r for r in asyncio.run(queries.list_active_runs(CHANNEL))}
    finally:
        queries.active_managers, create._starting = orig_active, orig_starting
    assert (rows["r1"]["effective_grant"], rows["r1"]["adopted"], rows["r1"]["harness"]) == (grant, True, "qwen-code")
    assert rows["r1"]["harness_label"] == HARNESSES["qwen-code"]["label"]
    assert rows["r2"]["adopted"] is False and rows["r2"]["effective_grant"]["docker"] == "none"
    assert (rows["r3"]["phase"], rows["r3"]["adopted"], rows["r3"]["harness"]) == ("queued", True, "codex")


def test_a_cli_runs_row_says_where_its_harness_cli_runs():
    """A CLI run's row carries where its harness CLI runs (``host``: this
    machine, or ``container``), from its placement record — what the Status
    view's "This machine" chip reads; any other run carries None."""
    from routes.runs import create, queries

    grant = {"update_packages": False, "docker": "none", "internet": "full",
             "internet_allow": [], "commands": "ask"}
    placement = {"harness": "claude-code", "surface": "terminal", "runtime": "host", "adopted": False,
                 "raised": [], "runner_grant": grant, "effective_grant": grant}
    assert create.cli_locus(placement) == "host"
    assert create.cli_locus({**placement, "runtime": "docker"}) == "container"
    assert create.cli_locus({**placement, "surface": "chat"}) is None and create.cli_locus(None) is None
    card = create._card_placement(placement)
    assert (card["surface"], card["runtime"]) == ("terminal", "host")
    orig_active, orig_starting = queries.active_managers, create._starting
    queries.active_managers = {"r1": _Mgr(effective_grant=grant, adopted=False, harness="claude-code",
                                          cli_locus="host")}
    create._starting = {"r2": create.StartingRun(
        run_id="r2", channel=CHANNEL, cell_id="cell-2", runner_id="synth", runner_name="Synth",
        label="synth-bbbbbbbb", folder="_runs/synth-bbbbbbbb", generator_name="synthetic-gen",
        effective_grant=grant, adopted=False, harness="antigravity", cli_locus="host")}
    try:
        rows = {r["run_id"]: r for r in asyncio.run(queries.list_active_runs(CHANNEL))}
    finally:
        queries.active_managers, create._starting = orig_active, orig_starting
    assert rows["r1"]["cli_locus"] == "host" and rows["r2"]["cli_locus"] == "host"


def test_active_runs_carries_the_phase_for_reload_reseed__F0376():
    """A missed run_phase broadcast is repaired on reload: the active-runs
    listing carries the manager's live phase and its message."""
    from routes.runs import queries

    orig = queries.active_managers
    queries.active_managers = {
        "r1": _Mgr(current_phase="awaiting_input", current_phase_message="session open"),
        "r2": _Mgr(cell_id="cell-2"),
    }
    try:
        result = {r["run_id"]: r for r in asyncio.run(queries.list_active_runs(CHANNEL))}
        assert result["r1"]["phase"] == "awaiting_input"
        assert result["r1"]["phase_message"] == "session open"
        assert result["r2"]["phase"] == "" and result["r2"]["phase_message"] == ""
    finally:
        queries.active_managers = orig


def test_an_accepted_run_still_spawning_is_a_queued_row_with_starting_surfaces_GN03(monkeypatch):
    """Between the accept and the registration a run is listed after the
    registered ones, ``queued``, with the surfaces it claimed as starting;
    once registered it is listed once, from its manager; a Stop that
    reaches it while queued lists it stopping."""
    import run_registry
    import served_surfaces
    from routes.runs import create, queries

    monkeypatch.setattr(queries, "active_managers", {"r1": _Mgr()})
    monkeypatch.setattr(create, "active_managers", queries.active_managers)
    monkeypatch.setattr(create, "_starting", {"r2": create.StartingRun(
        run_id="r2", channel=CHANNEL, cell_id="cell-2", runner_id="synth", runner_name="Synth",
        label="synth-bbbbbbbb", folder="_runs/synth-bbbbbbbb", generator_name="synthetic-gen")})
    pointer = "_runs/synth-bbbbbbbb/chat/conversation.chat"
    served_surfaces.claim_starting(CHANNEL, "cell-2", "r2", [pointer], "synth-bbbbbbbb", label="Synth")
    run_registry.reserve("r2", "cell-2")
    try:
        rows = asyncio.run(queries.list_active_runs(CHANNEL))
        assert [r["run_id"] for r in rows] == ["r1", "r2"]
        assert rows[1]["phase"] == "queued" and rows[1]["stopping"] is False
        assert rows[1]["surfaces"] == [{"path": pointer, "label": "Synth", "state": "starting"}]
        everywhere = asyncio.run(queries.list_active_runs_everywhere())
        assert [r["run_id"] for r in everywhere[0]["runs"]] == ["r1", "r2"]

        queries.active_managers["r2"] = _Mgr(cell_id="cell-2", label="synth-bbbbbbbb",
                                             folder="_runs/synth-bbbbbbbb")
        assert [r["run_id"] for r in asyncio.run(queries.list_active_runs(CHANNEL))] == ["r1", "r2"]
        del queries.active_managers["r2"]

        # Stop all (its hold cancelled) or a per-run Stop lists it stopping.
        run_registry.cancel_holds()
        assert asyncio.run(queries.list_active_runs(CHANNEL))[1]["stopping"] is True
    finally:
        run_registry.release("r2")
        served_surfaces.release_run("r2")
