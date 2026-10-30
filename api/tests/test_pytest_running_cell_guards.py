"""Layout guards for running cells.

A "running cell" is the cell of a live run — the one ``active_managers``
tracks via ``cell_id`` (several runs may share it), a stopping run included
— or of a start still holding its cell (``run_registry.reserve``). While a
run holds it:

  * the stack holding it refuses removal (409),
  * the cell itself refuses removal (409),
  * the cell cannot move onto Capabilities (409), while a cell on
    Capabilities moves off freely (no run ever holds one there),
  * cells without a live run stay freely removable.

The fake manager is a SimpleNamespace (``conftest.fake_run``) — the guards
only read ``cell_id``, so no real ProcessManager is needed.
"""

from __future__ import annotations

import pytest


def _add_stack_with_cells(n: int = 1):
    """Append a synthetic stack with n cells to the active channel.
    Returns (stack, cells)."""
    from state import state
    from models import Stack, Cell

    ch = next(c for c in state.channels if c.id == state.active_channel_id)
    cells = [Cell(name=f"cell-{i}") for i in range(n)]
    stack = Stack(name="t", cells=cells)
    ch.main_stacks.append(stack)
    return stack, cells


def test_remove_stack_409_when_it_holds_a_running_cell(client, fake_run):
    from state import state

    stack, (running,) = _add_stack_with_cells(1)
    fake_run(str(running.id))

    r = client.post(f"/api/stacks/{client.channel_id}/{stack.id}/remove")
    assert r.status_code == 409, r.text

    ch = next(c for c in state.channels if c.id == state.active_channel_id)
    assert any(s.id == stack.id for s in ch.main_stacks)


def test_remove_cell_409_for_running_placeholder(client, fake_run):
    stack, (running, _side) = _add_stack_with_cells(2)
    fake_run(str(running.id))

    r = client.post(f"/api/channels/{client.channel_id}/cells/{running.id}/remove")
    assert r.status_code == 409, r.text
    assert len(stack.cells) == 2


def test_remove_cell_allows_other_cell_while_run_is_active(client, fake_run):
    """A run on one cell does not lock the other cells in its stack."""
    stack, (running, side) = _add_stack_with_cells(2)
    fake_run(str(running.id))

    r = client.post(f"/api/channels/{client.channel_id}/cells/{side.id}/remove")
    assert r.status_code == 200, r.text
    assert [c.id for c in stack.cells] == [running.id]


def test_remove_stack_succeeds_once_run_is_gone(client, fake_run):
    from state import state

    stack, (running,) = _add_stack_with_cells(1)
    fake_run(str(running.id))
    from run_registry import active_managers
    active_managers.pop("test-run-guard", None)

    r = client.post(f"/api/stacks/{client.channel_id}/{stack.id}/remove")
    assert r.status_code == 200, r.text

    ch = next(c for c in state.channels if c.id == state.active_channel_id)
    assert all(s.id != stack.id for s in ch.main_stacks)


@pytest.fixture
def shown_capabilities(client):
    """The active channel's Capabilities stack shown on the board after a
    synthetic two-cell stack; answers ``(stack, capabilities)``."""
    from cell_placement import capabilities_stack
    from state import state

    stack, _cells = _add_stack_with_cells(2)
    ch = next(c for c in state.channels if c.id == state.active_channel_id)
    capabilities = capabilities_stack(ch)
    ch.nav_stacks.remove(capabilities)
    ch.main_stacks.append(capabilities)
    return stack, capabilities


def _onto_capabilities(client, stack, capabilities, cell):
    from state import state
    ch = next(c for c in state.channels if c.id == state.active_channel_id)
    target = next(i for i, s in enumerate(ch.main_stacks) if s is capabilities)
    return client.post(f"/api/stacks/{client.channel_id}/{stack.id}/move_cell_to/{target}/stack",
                       params={"cell_id": str(cell.id)})


@pytest.mark.parametrize("hold", ["active", "stopping", "starting"])
def test_a_held_cell_cannot_move_onto_capabilities(client, fake_run, shown_capabilities, hold):
    import run_registry

    stack, capabilities = shown_capabilities
    running = stack.cells[0]
    before = list(capabilities.cells)
    if hold == "starting":
        run_registry.reserve("test-start-hold", str(running.id))
    else:
        fake_run(str(running.id))
        if hold == "stopping":
            run_registry.mark_stop_requested(run_registry.active_managers.get("test-run-guard"))
    try:
        r = _onto_capabilities(client, stack, capabilities, running)
    finally:
        run_registry.release("test-start-hold")
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == f"Stop the run in ‘{running.name}’ before moving it onto Capabilities"
    assert capabilities.cells == before and stack.cells[0] is running


def test_a_cell_without_a_run_moves_onto_capabilities_beside_a_running_one(client, fake_run, shown_capabilities):
    stack, capabilities = shown_capabilities
    running, idle = stack.cells
    fake_run(str(running.id))
    r = _onto_capabilities(client, stack, capabilities, idle)
    assert r.status_code == 200, r.text
    assert capabilities.cells[-1] is idle and stack.cells == [running]


def test_a_cell_moves_off_capabilities_with_a_run_elsewhere(client, fake_run, shown_capabilities):
    from conftest import add_capabilities_cell
    from models import Cell
    from state import state

    stack, capabilities = shown_capabilities
    capability = Cell(name="Synthetic capability")
    ch = next(c for c in state.channels if c.id == state.active_channel_id)
    add_capabilities_cell(ch, capability)
    fake_run(str(stack.cells[0].id))
    r = client.post(f"/api/stacks/{client.channel_id}/{capabilities.id}/move_cell_to/0/stack",
                    params={"cell_id": str(capability.id), "tgt_cell_index": 2})
    assert r.status_code == 200, r.text
    assert stack.cells[-1] is capability and capability not in capabilities.cells


@pytest.mark.parametrize("running", [True, False])
def test_a_sole_cell_dropped_into_its_own_stack_beside_capabilities_stays_put(client, fake_run, running):
    """``move_cell_to/{own index}/stack`` for a stack's only cell names that
    stack, not the neighbour its removal would shift in: with Capabilities
    right of it, the cell (running or not) stays where it is and Capabilities
    is unchanged."""
    from cell_placement import capabilities_stack
    from state import state

    lone, (cell,) = _add_stack_with_cells(1)
    ch = next(c for c in state.channels if c.id == state.active_channel_id)
    capabilities = capabilities_stack(ch)
    ch.nav_stacks.remove(capabilities)
    ch.main_stacks.append(capabilities)
    if running:
        fake_run(str(cell.id))
    before_ids = [s.id for s in ch.main_stacks]
    before_caps = list(capabilities.cells)
    own = before_ids.index(lone.id)
    assert ch.main_stacks[own + 1] is capabilities

    r = client.post(f"/api/stacks/{client.channel_id}/{lone.id}/move_cell_to/{own}/stack",
                    params={"cell_id": str(cell.id), "tgt_cell_index": 999})
    assert r.status_code == 200, r.text
    assert [s.id for s in ch.main_stacks] == before_ids
    assert lone.cells == [cell] and capabilities.cells == before_caps
    assert [s["id"] for s in r.json()["main_stacks"]] == [str(i) for i in before_ids]
