"""Stack operations around the built-in cards at the head of Capabilities.

The channel's Capabilities stack leads with the app's built-in cards
(``Cell.builtin``): every layout operation on a card is refused with one
message, a cell moved within or onto the stack lands after them, and the
outline leaves them out. Each case shows the channel's own Capabilities
stack on the board next to one synthetic capability cell; nothing here
reads a shipped generator or renderer.
"""
from __future__ import annotations

import pytest

REFUSAL = "Built-in packages are part of the app"


def _channel(channel_id):
    from state import state
    return next(c for c in state.channels if str(c.id) == str(channel_id))


@pytest.fixture
def capabilities(client):
    """The client's channel with its Capabilities stack shown on the board
    after one standard stack, holding the cards and one capability cell
    ``Mine``; answers ``(client, capabilities, standard)``."""
    from cell_placement import capabilities_stack
    from conftest import add_capabilities_cell
    from models import Cell, Stack

    ch = _channel(client.channel_id)
    add_capabilities_cell(ch, Cell(name="Mine"))
    stack = capabilities_stack(ch)
    ch.nav_stacks.remove(stack)
    standard = Stack(name="Work", cells=[Cell(name="Board")])
    ch.main_stacks[:] = [standard, stack]
    return client, stack, standard


def _names(stack):
    return [c.name for c in stack.cells]


def _refused(response, detail=REFUSAL):
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == detail


def test_a_card_cannot_be_removed_moved_forked_or_edited(capabilities):
    client, stack, _standard = capabilities
    ch_id = client.channel_id
    card = stack.cells[0]
    assert card.builtin
    before = _names(stack)
    _refused(client.post(f"/api/channels/{ch_id}/cells/{card.id}/remove"))
    _refused(client.post(f"/api/stacks/{ch_id}/{stack.id}/move_cell_to/0/stack",
                         params={"cell_id": str(card.id), "tgt_cell_index": 1}))
    _refused(client.post(f"/api/stacks/{ch_id}/{stack.id}/move_cell_to/0/insert",
                         params={"cell_id": str(card.id)}))
    _refused(client.post(f"/api/channels/{ch_id}/cells/{card.id}/duplicate"))
    _refused(client.post(f"/api/channels/{ch_id}/cells/{card.id}/fork", json={"target_cell_id": str(card.id)}))
    _refused(client.patch(f"/api/channels/{ch_id}/cells/{card.id}/meta", json={"name": "Mine now"}))
    assert _names(stack) == before and len(_channel(ch_id).main_stacks) == 2


def test_merge_split_and_in_stack_duplicate_never_take_a_card(capabilities):
    client, stack, standard = capabilities
    ch_id = client.channel_id
    before = _names(stack)
    response = client.post(f"/api/stacks/{ch_id}/{stack.id}/move_stack_to/0/stack")
    assert response.status_code == 200, response.text
    _refused(client.post(f"/api/channels/{ch_id}/merge", json={"stack_id": str(stack.id)}),
             "Capabilities cells are not merged")
    _refused(client.post(f"/api/channels/{ch_id}/split", json={"cell_id": str(stack.cells[0].id)}),
             "Capabilities cells are not split")
    _refused(client.post(f"/api/stacks/{ch_id}/{stack.id}/duplicate_cell_in_stack/0"),
             "Capabilities cells are duplicated into a new stack")
    assert _names(stack) == before and _names(standard) == ["Board"]


def test_a_client_cannot_set_builtin(capabilities):
    client, stack, _standard = capabilities
    mine = stack.cells[-1]
    response = client.patch(f"/api/channels/{client.channel_id}/cells/{mine.id}/meta",
                            json={"name": "Renamed", "builtin": True})
    assert response.status_code == 200, response.text
    assert (mine.name, mine.builtin) == ("Renamed", False)
    imported = client.post(f"/api/channels/{client.channel_id}/import", json={"stacks": [
        {"name": "In", "cells": [{"name": "Card?", "builtin": True}]}]})
    assert imported.status_code == 422, imported.text


def test_a_cell_moved_within_capabilities_lands_after_the_cards(capabilities):
    from builtin_packages import builtin_cells
    from conftest import add_capabilities_cell
    from models import Cell

    client, stack, _standard = capabilities
    two = Cell(name="Two")
    add_capabilities_cell(_channel(client.channel_id), two)
    response = client.post(f"/api/stacks/{client.channel_id}/{stack.id}/move_cell_to/1/stack",
                           params={"cell_id": str(two.id), "tgt_cell_index": 0})
    assert response.status_code == 200, response.text
    cards = len(builtin_cells())
    assert all(c.builtin for c in stack.cells[:cards])
    assert _names(stack)[cards:] == ["Two", "Mine"]


def test_the_outline_leaves_the_cards_out(capabilities):
    client, stack, standard = capabilities
    outline = client.get(f"/api/channels/{client.channel_id}/outline").json()
    mine = stack.cells[-1]
    assert outline["capabilities_cells"] == [
        {"id": str(mine.id), "name": "Mine"}]
    assert [s["id"] for s in outline["main_stacks"]] == [str(standard.id)]


def test_no_route_adds_into_capabilities_by_kind(client):
    for kind in ("generator", "renderer", "capabilities"):
        response = client.post(f"/api/channels/{client.channel_id}/stacks/{kind}/add",
                               json={"suggestion_id": "x", "folder": "y"})
        assert response.status_code in (404, 405), response.text
