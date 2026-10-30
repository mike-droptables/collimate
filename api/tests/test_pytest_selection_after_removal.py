"""Where selection lands when a stack goes away.

When the selected stack disappears — removed outright, emptied by its
last cell being removed, or (Capabilities) hidden to the top bar —
selection moves to the stack on its **left**. If the
removed stack was leftmost, selection falls to the new leftmost (the old
right neighbor); if nothing is left, it clears.
"""

from __future__ import annotations


def _ch():
    from state import state
    return next(c for c in state.channels if c.id == state.active_channel_id)


def _layout(*specs):
    """Replace the active channel's main stacks with the given specs.

    Each spec is a name, or ``CAPABILITIES`` for the channel's one
    Capabilities stack, which moves onto the board at that place. Every
    other stack gets a single cell. Returns the stacks in order.
    """
    from cell_placement import capabilities_stack
    from genesis import ensure_capabilities_stack
    from models import Stack, Cell

    ch = _ch()
    ensure_capabilities_stack(ch)
    capabilities = capabilities_stack(ch)
    ch.main_stacks.clear()
    ch.nav_stacks[:] = [s for s in ch.nav_stacks if s is not capabilities]
    stacks = [capabilities if name is CAPABILITIES else Stack(name=name, cells=[Cell(name=f"{name}-0")])
              for name in specs]
    ch.main_stacks.extend(stacks)
    if capabilities not in stacks:
        ch.nav_stacks.append(capabilities)
    return stacks


CAPABILITIES = object()


def test_remove_middle_stack_selects_left_neighbor(client):
    a, b, c = _layout("A", "B", "C")
    ch = _ch()
    ch.main_selection_id = b.id

    r = client.post(f"/api/stacks/{client.channel_id}/{b.id}/remove")
    assert r.status_code == 200, r.text
    assert _ch().main_selection_id == a.id


def test_remove_leftmost_stack_selects_right_neighbor(client):
    a, b, c = _layout("A", "B", "C")
    ch = _ch()
    ch.main_selection_id = a.id

    r = client.post(f"/api/stacks/{client.channel_id}/{a.id}/remove")
    assert r.status_code == 200, r.text
    # No left neighbor — the old right neighbor is now leftmost.
    assert _ch().main_selection_id == b.id


def test_remove_only_stack_clears_selection(client):
    (a,) = _layout("A")
    ch = _ch()
    ch.main_selection_id = a.id

    r = client.post(f"/api/stacks/{client.channel_id}/{a.id}/remove")
    assert r.status_code == 200, r.text
    assert _ch().main_selection_id is None


def test_remove_unselected_stack_leaves_selection_alone(client):
    a, b, c = _layout("A", "B", "C")
    ch = _ch()
    ch.main_selection_id = c.id

    r = client.post(f"/api/stacks/{client.channel_id}/{a.id}/remove")
    assert r.status_code == 200, r.text
    assert _ch().main_selection_id == c.id


def test_remove_last_cell_empties_stack_selects_left_neighbor(client):
    a, b, c = _layout("A", "B", "C")
    ch = _ch()
    ch.main_selection_id = b.id

    # B holds a single cell — removing it empties (and so removes) the stack.
    r = client.post(f"/api/channels/{client.channel_id}/cells/{b.cells[0].id}/remove")
    assert r.status_code == 200, r.text
    assert all(s.id != b.id for s in _ch().main_stacks)
    assert _ch().main_selection_id == a.id


def test_hiding_capabilities_selects_left_neighbor(client):
    a, capabilities, c = _layout("A", CAPABILITIES, "C")
    ch = _ch()
    ch.main_selection_id = capabilities.id

    r = client.post(f"/api/stacks/{client.channel_id}/{capabilities.id}/minimize")
    assert r.status_code == 200, r.text
    ch = _ch()
    assert any(s.id == capabilities.id for s in ch.nav_stacks)
    assert all(s.id != capabilities.id for s in ch.main_stacks)
    assert ch.main_selection_id == a.id
