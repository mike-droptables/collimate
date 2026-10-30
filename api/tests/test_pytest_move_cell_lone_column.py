"""A stack's only cell moved to a new column moves the stack itself.

The overview and the main view send ``move_cell_to/{i}/insert`` for a cell
dropped between columns. When the cell is its stack's only one, the stack
IS the column being moved: it keeps its id, name and place in the order,
so the board sees a reorder (no column leaving while another arrives, no
layout re-anchoring). A cell leaving a stack that keeps other cells still
gets a new stack.
"""
import pytest


@pytest.fixture
def board(client):
    from database import get_db_connection
    from models import Cell, Channel, Stack
    from state import state

    a = Stack(name="Alpha", cells=[Cell(name="A")])
    b = Stack(name="Beta", cells=[Cell(name="B1"), Cell(name="B2")])
    c = Stack(name="Gamma", cells=[Cell(name="C")])
    d = Stack(name="Delta", cells=[Cell(name="D")])
    channel = Channel(name="Board", main_stacks=[a, b, c, d])
    state.channels.append(channel)
    get_db_connection(str(channel.id)).close()
    return channel


def _move(client, channel, stack, target, mode, src=0, tgt=0):
    """Move the cell at ``src`` of ``stack``, addressed by its id."""
    return client.post(
        f"/api/stacks/{channel.id}/{stack.id}/move_cell_to/{target}/{mode}",
        params={"cell_id": str(stack.cells[src].id), "tgt_cell_index": tgt},
    )


def _layout(body):
    return [(s["id"], s["name"], [c["name"] for c in s["cells"]]) for s in body["main_stacks"]]


def test_move_cell_lone_cell_to_the_next_slot_moves_its_stack_by_one(client, board):
    a, b, c, d = board.main_stacks
    res = _move(client, board, a, 2, "insert")
    assert res.status_code == 200, res.text
    assert _layout(res.json()) == [
        (str(b.id), "Beta", ["B1", "B2"]),
        (str(a.id), "Alpha", ["A"]),
        (str(c.id), "Gamma", ["C"]),
        (str(d.id), "Delta", ["D"]),
    ]
    assert res.json()["main_selection_id"] == str(a.id)


def test_move_cell_lone_cell_back_to_its_first_place_restores_the_order(client, board):
    a, b, c, d = board.main_stacks
    _move(client, board, a, 2, "insert")
    res = _move(client, board, a, 0, "insert")
    assert [s[0] for s in _layout(res.json())] == [str(a.id), str(b.id), str(c.id), str(d.id)]


def test_move_cell_lone_cell_leftward_and_to_the_end(client, board):
    a, b, c, d = board.main_stacks
    res = _move(client, board, d, 1, "insert")
    assert [s[0] for s in _layout(res.json())] == [str(a.id), str(d.id), str(b.id), str(c.id)]
    res = _move(client, board, a, 4, "insert")
    assert [s[0] for s in _layout(res.json())] == [str(d.id), str(b.id), str(c.id), str(a.id)]


def test_move_cell_from_a_multi_cell_stack_still_makes_a_new_stack(client, board):
    a, b, c, d = board.main_stacks
    res = _move(client, board, b, 2, "insert", src=1)
    layout = _layout(res.json())
    assert [s[2] for s in layout] == [["A"], ["B1"], ["B2"], ["C"], ["D"]]
    assert layout[1][0] == str(b.id)
    new_id = layout[2][0]
    assert new_id not in {str(s.id) for s in (a, b, c, d)}
    assert layout[2][1] == ""
    assert res.json()["main_selection_id"] == new_id


def test_move_cell_lone_cell_into_another_stack_still_removes_its_own(client, board):
    a, b, c, d = board.main_stacks
    res = _move(client, board, c, 1, "stack", tgt=2)
    assert _layout(res.json()) == [
        (str(a.id), "Alpha", ["A"]),
        (str(b.id), "Beta", ["B1", "B2", "C"]),
        (str(d.id), "Delta", ["D"]),
    ]
