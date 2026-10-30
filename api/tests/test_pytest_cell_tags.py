"""Cell tags (TG-01, TG-02, TG-03): the rules, the vocabulary, the remove
action and how Duplicate, Merge and Split carry tags. Synthetic cells only."""
from __future__ import annotations

import pytest

from test_pytest_options_identity import CHANNEL as CH, accept_file, client as client, make_stack, merge, split
from test_pytest_removed_shelf import add_cell, lineage_events as lineage_events, shelf


def _live(cell_id):
    from cell_placement import find_cell
    from state import get_active_channel
    return find_cell(get_active_channel(), str(cell_id))[1]


def _record(cell_id):
    import removed_cells
    from database import get_db_connection
    conn = get_db_connection(CH)
    try:
        return removed_cells.get(conn, str(cell_id))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("  draft  ", "draft"),
    ("needs\t \n review", "needs review"),
    ("Ünïcode ok", "Ünïcode ok"),
    ("x" * 48, "x" * 48),
    ("x" * 49, None),
    ("", None),
    ("   \n\t ", None),
    ("bell\x07", None),
    ("nul\x00here", None),
    ("del\x7f", None),
    (None, None),
    (7, None),
])
def test_normalize_tag_trims_collapses_and_refuses_TG01(raw, expected):
    from cell_tags import normalize_tag
    assert normalize_tag(raw) == expected


def test_normalize_tags_keeps_first_spelling_order_and_cap_TG01():
    from cell_tags import TAGS_PER_CELL_MAX, normalize_tags
    assert normalize_tags(["Draft", " review ", "DRAFT", "draft", "Later"]) == ["Draft", "review", "Later"]
    assert normalize_tags([]) == []
    assert len(normalize_tags([f"t{i}" for i in range(TAGS_PER_CELL_MAX)])) == TAGS_PER_CELL_MAX
    # Duplicates do not count toward the cap.
    assert len(normalize_tags([f"t{i}" for i in range(TAGS_PER_CELL_MAX)] + ["T0"])) == TAGS_PER_CELL_MAX
    for bad in ([f"t{i}" for i in range(TAGS_PER_CELL_MAX + 1)], "draft", None, {"draft": 1},
                ["ok", 3], ["ok", ""], ["ok", "x" * 49], ["ok", "a\x00b"]):
        with pytest.raises(ValueError):
            normalize_tags(bad)


def test_apply_tag_action_adds_then_removes_casefold_TG03():
    from cell_tags import TAGS_PER_CELL_MAX, apply_tag_action
    assert apply_tag_action(["Draft", "review"], ["  Done ", "DRAFT"], ["Review"]) == ["Draft", "Done"]
    assert apply_tag_action(["Draft"], [], []) == ["Draft"]
    # Add then remove: a tag in both ends up removed.
    assert apply_tag_action([], ["same"], ["SAME"]) == []
    full = [f"t{i}" for i in range(TAGS_PER_CELL_MAX)]
    assert apply_tag_action(full, ["one more"], ["t0"]) == [*full[1:], "one more"]
    with pytest.raises(ValueError):
        apply_tag_action(full, ["one more"], [])
    with pytest.raises(ValueError):
        apply_tag_action([], ["bad\x00"], [])


def test_union_tags_dedupes_in_order_and_caps_TG01():
    from cell_tags import TAGS_PER_CELL_MAX, union_tags
    assert union_tags([["a", "B"], ["b", "c"], [], ["A", "d"]]) == ["a", "B", "c", "d"]
    many = union_tags([[f"x{i}" for i in range(20)], [f"y{i}" for i in range(20)]])
    assert len(many) == TAGS_PER_CELL_MAX and many[-1] == "y11"


# ---------------------------------------------------------------------------
# The vocabulary
# ---------------------------------------------------------------------------

def _shown_capabilities():
    """The active channel's Capabilities stack, ensured and shown on the
    board after its other stacks."""
    from cell_placement import capabilities_stack
    from genesis import ensure_capabilities_stack
    from state import get_active_channel
    ch = get_active_channel()
    ensure_capabilities_stack(ch)
    stack = capabilities_stack(ch)
    if stack in ch.nav_stacks:
        ch.nav_stacks.remove(stack)
        ch.main_stacks.append(stack)
    return stack


def test_channel_tags_unions_live_and_removed_with_counts_TG02(client):
    from conftest import add_capabilities_cell
    from models import Cell
    from state import get_active_channel
    _, _, one = add_cell("One", tags=["Alpha", "beta"])
    _, _, two = add_cell("Two", tags=["alpha"])
    _, _, gone = add_cell("Gone", tags=["ALPHA", "gamma"])
    add_cell("Plain")
    # A capability cell's tags count like any other, the stack hidden or not.
    add_capabilities_cell(get_active_channel(), Cell(name="Capability", tags=["cap", "beta"]))
    assert client.post(f"/api/channels/{CH}/cells/{gone.id}/remove").status_code == 200
    response = client.get(f"/api/channels/{CH}/tags")
    assert response.status_code == 200, response.text
    assert response.json() == [
        {"tag": "Alpha", "live": 2, "removed": 1},
        {"tag": "beta", "live": 2, "removed": 0},
        {"tag": "cap", "live": 1, "removed": 0},
        {"tag": "gamma", "live": 0, "removed": 1},
    ]
    # Restored, the removed record's tags count as live again.
    assert client.post(f"/api/channels/{CH}/cells/{gone.id}/restore").status_code == 200
    assert {e["tag"]: (e["live"], e["removed"]) for e in client.get(f"/api/channels/{CH}/tags").json()} == {
        "Alpha": (3, 0), "beta": (2, 0), "cap": (1, 0), "gamma": (1, 0)}
    assert one.tags == ["Alpha", "beta"] and two.tags == ["alpha"]


# ---------------------------------------------------------------------------
# Remove with a zone's action
# ---------------------------------------------------------------------------

def test_remove_action_tags_the_record_only_and_restore_brings_them_back_TG03(client):
    _, stack, cell = add_cell("Triage", {"a.md": "a"}, tags=["Keep", "drop"])
    response = client.post(f"/api/channels/{CH}/cells/{cell.id}/remove",
                           json={"add_tags": ["  needs   review "], "remove_tags": ["DROP"]})
    assert response.status_code == 200, response.text
    # The live cell object was never changed; only the record took the action.
    assert cell.tags == ["Keep", "drop"]
    assert _record(cell.id).tags == ["Keep", "needs review"]
    [entry] = shelf(client)
    assert entry["tags"] == ["Keep", "needs review"]
    restored = client.post(f"/api/channels/{CH}/cells/{cell.id}/restore")
    assert restored.status_code == 200, restored.text
    assert _live(cell.id).tags == ["Keep", "needs review"]


def test_remove_with_an_invalid_action_refuses_and_keeps_the_cell_TG03(client):
    from cell_tags import TAGS_PER_CELL_MAX
    _, stack, cell = add_cell("Full", tags=[f"t{i}" for i in range(TAGS_PER_CELL_MAX)])
    for body in ({"add_tags": ["one more"]}, {"add_tags": ["bad\x00tag"]}, {"remove_tags": [""]},
                 {"add_tags": ["ok", 3]}, {"remove_tags": [None]}):
        response = client.post(f"/api/channels/{CH}/cells/{cell.id}/remove", json=body)
        assert response.status_code == 400, response.text
    assert _live(cell.id) is cell and _record(cell.id) is None and shelf(client) == []
    # An empty action is a plain remove.
    assert client.post(f"/api/channels/{CH}/cells/{cell.id}/remove",
                       json={"add_tags": [], "remove_tags": []}).status_code == 200
    assert _record(cell.id).tags == cell.tags


def test_a_move_onto_and_off_capabilities_keeps_the_tags_TG03(client):
    capabilities = _shown_capabilities()
    _, stack, cell = add_cell("Mover", tags=["Keep", "client"])
    from state import get_active_channel
    ch = get_active_channel()

    def move(source, target, mode):
        response = client.post(f"/api/stacks/{CH}/{source.id}/move_cell_to/{target}/{mode}",
                               params={"cell_id": str(cell.id)})
        assert response.status_code == 200, response.text

    move(stack, ch.main_stacks.index(capabilities), "stack")
    assert cell in capabilities.cells and cell.tags == ["Keep", "client"]
    # On Capabilities the tags still change through …/meta, and a remove
    # (with or without a zone's action) is refused.
    response = client.patch(f"/api/channels/{CH}/cells/{cell.id}/meta", json={"tags": ["Keep", "later"]})
    assert response.status_code == 200, response.text
    response = client.post(f"/api/channels/{CH}/cells/{cell.id}/remove", json={"add_tags": ["x"]})
    assert response.status_code == 400, response.text
    assert _record(cell.id) is None
    move(capabilities, len(ch.main_stacks), "insert")
    assert cell not in capabilities.cells and _live(cell.id).tags == ["Keep", "later"]


# ---------------------------------------------------------------------------
# A removed cell's tags, edited on the shelf
# ---------------------------------------------------------------------------

def _put_removed_tags(client, cell_id, body):
    return client.put(f"/api/channels/{CH}/cells/removed/{cell_id}/tags", json=body)


def test_removed_cell_tags_are_set_on_the_record_and_restore_brings_them_back_TG07(client, lineage_events):
    _, _, cell = add_cell("Shelved", {"a.md": "a"}, tags=["Keep"])
    assert client.post(f"/api/channels/{CH}/cells/{cell.id}/remove").status_code == 200
    removed_at = _record(cell.id).removed_at
    lineage_events.clear()
    response = _put_removed_tags(client, cell.id, {"tags": ["Keep", "  needs   review ", "KEEP"]})
    assert response.status_code == 200, response.text
    # The same normalisation as a live cell's list: collapsed, casefold-unique.
    assert response.json() == {"cell_id": str(cell.id), "tags": ["Keep", "needs review"]}
    record = _record(cell.id)
    assert record.tags == ["Keep", "needs review"] and record.removed_at == removed_at
    # The shelf's change event, so every open overview rereads its shelf.
    assert lineage_events == [[str(cell.id)]]
    [entry] = shelf(client)
    assert entry["tags"] == ["Keep", "needs review"]
    assert {e["tag"]: e["removed"] for e in client.get(f"/api/channels/{CH}/tags").json()} == {
        "Keep": 1, "needs review": 1}
    assert [e["cell_id"] for e in shelf(client, q="review")] == [str(cell.id)]
    # Taking every tag off is a list like any other.
    assert _put_removed_tags(client, cell.id, {"tags": ["needs review"]}).json()["tags"] == ["needs review"]
    restored = client.post(f"/api/channels/{CH}/cells/{cell.id}/restore")
    assert restored.status_code == 200, restored.text
    assert _live(cell.id).tags == ["needs review"]


def test_removed_cell_tags_refuse_bad_lists_live_cells_and_unknown_ids_TG07(client, lineage_events):
    from cell_tags import TAGS_PER_CELL_MAX
    _, _, cell = add_cell("Shelved", tags=["Keep"])
    assert client.post(f"/api/channels/{CH}/cells/{cell.id}/remove").status_code == 200
    lineage_events.clear()
    for body in ({"tags": ["ok", 3]}, {"tags": ["bad\x00tag"]}, {"tags": [""]},
                 {"tags": [f"t{i}" for i in range(TAGS_PER_CELL_MAX + 1)]}):
        response = _put_removed_tags(client, cell.id, body)
        assert response.status_code == 400, response.text
    assert _put_removed_tags(client, cell.id, {"tags": None}).status_code == 422
    assert _record(cell.id).tags == ["Keep"] and lineage_events == []
    # A cell on the canvas changes its tags through …/meta.
    _, _, live = add_cell("Live", tags=["x"])
    assert _put_removed_tags(client, live.id, {"tags": ["y"]}).status_code == 409
    assert live.tags == ["x"]
    # No record.
    from uuid import uuid4
    assert _put_removed_tags(client, uuid4(), {"tags": ["y"]}).status_code == 404


# ---------------------------------------------------------------------------
# Duplicate, Merge, Split
# ---------------------------------------------------------------------------

def test_every_duplicate_route_copies_tags_TG01(client):
    from state import get_active_channel
    ch, stack, cell = add_cell("Source", {"a.md": "a"}, tags=["Draft", "client"])
    response = client.post(f"/api/channels/{CH}/cells/{cell.id}/duplicate")
    assert response.status_code == 200, response.text
    copy = _live(response.json()["new_cell_id"])
    assert copy.tags == ["Draft", "client"] and copy.tags is not cell.tags

    response = client.post(f"/api/stacks/{CH}/{stack.id}/duplicate_cell_in_stack/0")
    assert response.status_code == 200, response.text
    [copy_id] = response.json()["duplicated_cell_ids"]
    assert _live(copy_id).tags == ["Draft", "client"]

    response = client.post(f"/api/stacks/{CH}/{stack.id}/duplicate_stack")
    assert response.status_code == 200, response.text
    ch = get_active_channel()
    copies = ch.main_stacks[[s.id for s in ch.main_stacks].index(stack.id) + 1].cells
    assert [c.tags for c in copies] == [["Draft", "client"], ["Draft", "client"]]


def test_merge_unions_tags_and_split_revival_adopts_the_records_TG01(client):
    _, stack, ids = make_stack([("First", {"a.txt": "A"}), ("Second", {"b.txt": "B"}), ("Third", {"c.txt": "C"})])
    for cell, tags in zip(stack.cells, (["Draft", "x"], ["draft", "y"], ["z"])):
        cell.tags = tags
    source = merge(client, stack)
    merged = _live(source)
    assert merged.tags == ["Draft", "x", "y", "z"]
    merged.tags = ["merged only"]
    # The Third option's bytes change, so its output is fresh, not revived,
    # and takes none of the Third record's tags.
    accept_file(source, "Third/c.txt", "changed")
    response = split(client, source)
    assert response.status_code == 200, response.text
    outputs = response.json()["new_cell_ids"]
    assert outputs[:2] == ids[:2] and outputs[2] != ids[2]
    assert [_live(cid).tags for cid in outputs] == [["Draft", "x"], ["draft", "y"], []]
