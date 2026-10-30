"""A cell dropped onto a stack lands at the stack position it was dropped at.

The overview's columns show each stack in its own order, and a card held
over a column opens its gap where the pointer points: above the first card,
between two, or below the last. The drop sends ``move_cell_to/{i}/stack``
with the moving cell's ``cell_id`` and ``tgt_cell_index``, the position the cell takes in the target stack
once it is in (for a move within one stack, counted after the cell has left
its own place).
"""
import pytest


@pytest.fixture
def board(client):
    from database import get_db_connection
    from models import Cell, Channel, Stack
    from state import state

    a = Stack(name="Alpha", cells=[Cell(name="A0"), Cell(name="A1")])
    b = Stack(name="Beta", cells=[Cell(name="B0"), Cell(name="B1"), Cell(name="B2")])
    channel = Channel(name="Order", main_stacks=[a, b])
    state.channels.append(channel)
    get_db_connection(str(channel.id)).close()
    return channel


def _move(client, channel, stack, target, src, tgt):
    """Move the cell at ``src`` of ``stack``, addressed by its id."""
    return client.post(
        f"/api/stacks/{channel.id}/{stack.id}/move_cell_to/{target}/stack",
        params={"cell_id": str(stack.cells[src].id), "tgt_cell_index": tgt},
    )


def _cells(body):
    return [[c["name"] for c in s["cells"]] for s in body["main_stacks"]]


@pytest.mark.parametrize("tgt, beta", [
    (0, ["A0", "B0", "B1", "B2"]),
    (1, ["B0", "A0", "B1", "B2"]),
    (2, ["B0", "B1", "A0", "B2"]),
    (3, ["B0", "B1", "B2", "A0"]),
])
def test_move_cell_into_another_stack_lands_at_the_top_middle_or_bottom_polish(client, board, tgt, beta):
    a, b = board.main_stacks
    res = _move(client, board, a, 1, 0, tgt)
    assert res.status_code == 200, res.text
    assert _cells(res.json()) == [["A1"], beta]
    assert res.json()["main_selection_id"] == str(b.id)


@pytest.mark.parametrize("src, tgt, beta", [
    (0, 2, ["B1", "B2", "B0"]),
    (2, 0, ["B2", "B0", "B1"]),
    (0, 1, ["B1", "B0", "B2"]),
    (1, 1, ["B0", "B1", "B2"]),
])
def test_move_cell_within_its_stack_takes_the_position_counted_after_it_leaves_polish(client, board, src, tgt, beta):
    a, b = board.main_stacks
    res = _move(client, board, b, 1, src, tgt)
    assert res.status_code == 200, res.text
    assert _cells(res.json()) == [["A0", "A1"], beta]


def test_move_cell_past_the_end_of_the_stack_lands_at_its_bottom_polish(client, board):
    a, b = board.main_stacks
    res = _move(client, board, a, 1, 0, 99)
    assert _cells(res.json()) == [["A1"], ["B0", "B1", "B2", "A0"]]
