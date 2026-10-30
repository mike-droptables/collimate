"""Invariants of the channel's one Capabilities stack.

- Every channel has exactly one, headed by the app's built-in cards; a
  channel created without packages holds it hidden, with only the cards.
- It keeps its name, is hidden rather than removed, is never duplicated,
  and its cells are not removed (they move off first).
- Only Capabilities hides to the top bar; restoring shows it on the board.
- ``genesis.ensure_capabilities_stack`` creates it when missing and treats
  a second one as a bug.

Synthetic layouts only — nothing here depends on shipped generators,
renderers, or bundle contents.
"""

from __future__ import annotations

import pytest


def _ch():
    from state import state
    return next(c for c in state.channels if c.id == state.active_channel_id)


def _shown_capabilities():
    """Show the active channel's Capabilities stack on the board, as the
    only board stack besides one standard stack; answers both."""
    from cell_placement import capabilities_stack
    from models import Cell, Stack
    ch = _ch()
    stack = capabilities_stack(ch)
    standard = Stack(name="A", cells=[Cell(name="A-0")])
    ch.nav_stacks.remove(stack)
    ch.main_stacks[:] = [standard, stack]
    return stack, standard


def _card_ids(channel_id: str) -> list[str]:
    from builtin_packages import builtin_cell_id, builtin_cells
    return [builtin_cell_id(channel_id, spec.spec_id) for spec in builtin_cells()]


def test_new_channel_holds_one_hidden_capabilities_stack_of_the_cards(client):
    r = client.post("/api/channels", params={"name": "No Picks"}, json={"config": {"packages": None}})
    assert r.status_code == 200, r.text
    ch = r.json()
    assert ch["main_stacks"] == []
    (stack,) = ch["nav_stacks"]
    assert (stack["name"], stack["stack_type"]) == ("Capabilities", "capabilities")
    assert [c["id"] for c in stack["cells"]] == _card_ids(ch["id"])
    assert all(c["builtin"] for c in stack["cells"])


def test_capabilities_keeps_its_name_shown_or_hidden(client):
    from cell_placement import capabilities_stack
    stack = capabilities_stack(_ch())
    r = client.put(f"/api/stacks/{client.channel_id}/{stack.id}/rename", params={"name": "Sneaky"})
    assert r.status_code == 400, r.text
    stack, _ = _shown_capabilities()
    r = client.put(f"/api/stacks/{client.channel_id}/{stack.id}/rename", params={"name": "Sneaky"})
    assert r.status_code == 400, r.text
    assert capabilities_stack(_ch()).name == "Capabilities"


def test_rename_standard_stack_ok(client):
    _, standard = _shown_capabilities()
    r = client.put(f"/api/stacks/{client.channel_id}/{standard.id}/rename", params={"name": "Renamed"})
    assert r.status_code == 200, r.text
    assert _ch().main_stacks[0].name == "Renamed"


def test_capabilities_is_not_removed_or_duplicated_and_its_cells_stay(client):
    from conftest import add_capabilities_cell
    from cell_placement import capabilities_stack, capabilities_stack_cells
    from models import Cell
    stack, _ = _shown_capabilities()
    cell = Cell(name="Synthetic capability")
    add_capabilities_cell(_ch(), cell)
    r = client.post(f"/api/stacks/{client.channel_id}/{stack.id}/remove")
    assert r.status_code == 400, r.text
    r = client.post(f"/api/stacks/{client.channel_id}/{stack.id}/duplicate_stack")
    assert r.status_code == 400, r.text
    r = client.post(f"/api/channels/{client.channel_id}/cells/{cell.id}/remove")
    assert r.status_code == 400, r.text
    ch = _ch()
    assert [s.stack_type for s in ch.main_stacks] == ["standard", "capabilities"]
    assert capabilities_stack(ch) is stack and capabilities_stack_cells(ch) == [cell]


def test_only_capabilities_hides_and_restore_shows_it(client):
    from cell_placement import capabilities_stack
    stack, standard = _shown_capabilities()
    r = client.post(f"/api/stacks/{client.channel_id}/{standard.id}/minimize")
    assert r.status_code == 400, r.text
    r = client.post(f"/api/stacks/{client.channel_id}/{stack.id}/minimize")
    assert r.status_code == 200, r.text
    ch = _ch()
    assert [s.id for s in ch.main_stacks] == [standard.id] and [s.id for s in ch.nav_stacks] == [stack.id]
    r = client.post(f"/api/stacks/{client.channel_id}/{stack.id}/restore_minimized", params={"after": str(standard.id)})
    assert r.status_code == 200, r.text
    ch = _ch()
    assert [s.id for s in ch.main_stacks] == [standard.id, stack.id] and ch.nav_stacks == []
    assert ch.main_selection_id == stack.id and capabilities_stack(ch) is stack


def test_ensure_creates_one_stack_heads_it_with_the_cards_and_refuses_two():
    from cell_placement import CAPABILITIES_STACK_TYPE, capabilities_stack, capabilities_stack_cells
    from genesis import ensure_capabilities_stack
    from models import Cell, Channel, Stack
    from state import channel_db_name
    ch = Channel(name="Bare", main_stacks=[Stack(name="A", cells=[Cell(name="A-0")])])
    ensure_capabilities_stack(ch)
    stack = capabilities_stack(ch)
    assert ch.nav_stacks == [stack] and stack.name == "Capabilities"
    cards = _card_ids(channel_db_name(ch))
    assert [str(c.id) for c in stack.cells] == cards
    # Shown on the board, renamed and with the cards out of place: the call
    # keeps the stack where it is and puts its name and cards back.
    user = Cell(name="Synthetic capability")
    stack.name, stack.cells = "Other", [user, *stack.cells]
    ch.nav_stacks.remove(stack)
    ch.main_stacks.append(stack)
    ensure_capabilities_stack(ch)
    assert capabilities_stack(ch) is stack and ch.nav_stacks == [] and stack.name == "Capabilities"
    assert [str(c.id) for c in stack.cells] == [*cards, str(user.id)]
    assert capabilities_stack_cells(ch) == [user]
    ch.nav_stacks.append(Stack(name="Capabilities", stack_type=CAPABILITIES_STACK_TYPE))
    with pytest.raises(ValueError, match="more than one Capabilities stack"):
        ensure_capabilities_stack(ch)
