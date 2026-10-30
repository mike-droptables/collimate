"""Annotation contexts are keyed by a cell's logical id, so every copy of a
cell starts with an empty one (like ``_runs/``, they never cross) while
Remove and Restore keep the cell's own."""
from __future__ import annotations

import uuid

from test_pytest_options_identity import CHANNEL, client as client, make_stack, merge, seed_version, split
from test_pytest_merge_split import declaration


def note(client, cell_id, body="keep this"):
    response = client.post(f"/api/channels/{CHANNEL}/cells/{cell_id}/annotations", json={
        "operation_id": str(uuid.uuid4()), "context_id": "cell", "body": body, "mode": "queued"})
    assert response.status_code == 200, response.text


def cell_messages(client, cell_id):
    response = client.get(f"/api/channels/{CHANNEL}/cells/{cell_id}/annotations/cell")
    assert response.status_code == 200, response.text
    return [m["body"] for m in response.json()["messages"]]


def _noted_stack(*names):
    ch, stack, ids = make_stack([(name, {f"{name}.md": name}) for name in names or ("Work",)])
    return ch, stack, ids


def test_duplicate_cell_starts_with_an_empty_cell_context(client):
    _ch, _stack, (cell_id,) = _noted_stack()
    note(client, cell_id)
    response = client.post(f"/api/channels/{CHANNEL}/cells/{cell_id}/duplicate", json={})
    assert response.status_code == 200, response.text
    assert cell_messages(client, response.json()["new_cell_id"]) == []
    assert cell_messages(client, cell_id) == ["keep this"]


def test_duplicate_stack_and_duplicate_cell_in_stack_start_empty(client):
    ch, stack, (cell_id,) = _noted_stack()
    note(client, cell_id)
    assert client.post(f"/api/stacks/{CHANNEL}/{stack.id}/duplicate_cell_in_stack/0").status_code == 200
    assert cell_messages(client, str(stack.cells[1].id)) == []
    assert client.post(f"/api/stacks/{CHANNEL}/{stack.id}/duplicate_stack").status_code == 200
    index = next(i for i, s in enumerate(ch.main_stacks) if s.id == stack.id)
    for copy in ch.main_stacks[index + 1].cells:
        assert cell_messages(client, str(copy.id)) == []


def test_fork_starts_empty(client):
    _ch, _stack, (cell_id,) = _noted_stack()
    seed_version(CHANNEL, cell_id)
    note(client, cell_id)
    response = client.post(f"/api/channels/{CHANNEL}/cells/{cell_id}/fork", json={"target_cell_id": cell_id})
    assert response.status_code == 200, response.text
    assert cell_messages(client, response.json()["new_cell_id"]) == []


def test_merge_output_and_split_outputs_start_empty(client):
    _ch, stack, ids = _noted_stack("A", "B")
    for cell_id in ids:
        note(client, cell_id)
    merged = merge(client, stack)
    assert merged not in ids and cell_messages(client, merged) == []
    _ch, stack, (source,) = make_stack([("Source", {"pick.options_stack": declaration(), "choice/a.txt": "A"})])
    note(client, source)
    response = split(client, source)
    assert response.status_code == 200, response.text
    outputs = [str(c.id) for c in stack.cells if str(c.id) != source]
    assert outputs and all(cell_messages(client, cell_id) == [] for cell_id in outputs)


def test_run_in_new_cell_clone_starts_empty(client):
    from cell_commits import clone_cell_for_run
    _ch, _stack, (cell_id,) = _noted_stack()
    note(client, cell_id)
    clone, _new_stack = clone_cell_for_run(CHANNEL, cell_id)
    assert clone != cell_id and cell_messages(client, clone) == []


def test_export_writes_no_annotation_files(client):
    _ch, _stack, (cell_id,) = _noted_stack()
    note(client, cell_id)
    response = client.get(f"/api/channels/{CHANNEL}/export/manifest", params={"scope": "cell", "cell_id": cell_id})
    assert response.status_code == 200, response.text
    assert [entry["path"] for entry in response.json()["entries"]] == ["Work/Work.md"]


def test_remove_then_restore_keeps_the_cell_context(client):
    _ch, stack, (cell_id,) = _noted_stack()
    note(client, cell_id)
    assert client.post(f"/api/stacks/{CHANNEL}/{stack.id}/remove").status_code == 200
    assert client.get(f"/api/channels/{CHANNEL}/cells/{cell_id}/annotations").status_code == 404
    assert client.post(f"/api/channels/{CHANNEL}/cells/{cell_id}/restore").status_code == 200
    assert cell_messages(client, cell_id) == ["keep this"]
