"""The channel's lineage (O04): one move per operation from cell_moves, the
cells they name with open/removed status, and the lineage_changed event.
Synthetic fixtures only."""
import json
from uuid import uuid4

def add_cell(name, files=None):
    from models import Cell, Stack, VirtualFile
    from state import get_active_channel
    cell = Cell(name=name, files=[VirtualFile(name=k, content=v) for k, v in (files or {}).items()])
    stack = Stack(name="Project", cells=[cell])
    get_active_channel().main_stacks.append(stack)
    return stack, cell


def test_lineage_groups_moves_per_operation_with_cell_status_O04(client, monkeypatch):
    import cell_files
    import cell_moves
    import versions
    from database import get_db_connection
    stack, frame = add_cell("Frame")
    _, lww = add_cell("LWW")
    _, oplog = add_cell("Op log")
    copies = [str(uuid4()), str(uuid4())]
    merged, part_a, part_b, clone = (str(uuid4()) for _ in range(4))
    conn = get_db_connection(client.channel_id)
    run_id = str(uuid4())
    conn.execute("INSERT INTO runs(id, prompt, status, kind, target_cell_id, runner_id, runner_snapshot) "
                 "VALUES(?, '', 'running', 'generator', ?, 'sower', ?)", (run_id, clone, json.dumps({"name": "Idea sower"})))
    # One Duplicate stack: a pair per copied cell under one op id.
    for source, copy in zip((str(lww.id), str(oplog.id)), copies):
        cell_moves.record_move(conn, op="duplicate", op_id="dup-op", from_cell=source, from_version=None, to_cell=copy)
    for i, source in enumerate((str(lww.id), str(oplog.id))):
        cell_moves.record_move(conn, op="merge", op_id="merge-op", from_cell=source, from_version=None,
                               to_cell=merged, ord=i)
    for i, out in enumerate((part_a, part_b)):
        cell_moves.record_move(conn, op="split", op_id="split-op", from_cell=merged, from_version=None,
                               to_cell=out, ord=i)
    cell_moves.record_move(conn, op="run_cell", op_id=run_id, from_cell=str(frame.id), from_version=None,
                           to_cell=clone)
    conn.commit()
    conn.close()
    assert client.post(f"/api/channels/{client.channel_id}/cells/{oplog.id}/remove").status_code == 200

    def forbidden(*_a, **_k):
        raise AssertionError("the lineage walked version chains or files")

    monkeypatch.setattr(versions, "iter_version_files", forbidden)
    monkeypatch.setattr(cell_files, "walk_versions", forbidden)
    response = client.get(f"/api/channels/{client.channel_id}/lineage")
    assert response.status_code == 200, response.text
    body = response.json()

    moves = body["moves"]
    assert [m["op"] for m in moves] == ["duplicate", "duplicate", "merge", "split", "run_cell"]
    assert all(m["at"].endswith("+00:00") for m in moves)
    assert [(m["from"], m["to"]) for m in moves[:2]] == [
        ([{"cell_id": str(lww.id), "version_id": None}], [{"cell_id": copies[0], "version_id": None}]),
        ([{"cell_id": str(oplog.id), "version_id": None}], [{"cell_id": copies[1], "version_id": None}]),
    ]
    merge, split, run_cell = moves[2:]
    assert [e["cell_id"] for e in merge["from"]] == [str(lww.id), str(oplog.id)]
    assert [e["cell_id"] for e in merge["to"]] == [merged] and merge["op_id"] == "merge-op"
    assert [e["cell_id"] for e in split["from"]] == [merged]
    assert [e["cell_id"] for e in split["to"]] == [part_a, part_b]
    assert run_cell["runner"] == {"runner_id": "sower", "runner_name": "Idea sower"}
    assert "runner" not in merge

    cells = {c["cell_id"]: c for c in body["cells"]}
    assert cells[str(lww.id)] == {"cell_id": str(lww.id), "name": "LWW", "status": "open",
                                  "stack_id": next(str(s.id) for s in client_stacks() if lww in s.cells)}
    assert cells[str(oplog.id)] == {"cell_id": str(oplog.id), "name": "Op log", "status": "removed"}
    assert cells[merged]["status"] == "removed" and "stack_id" not in cells[merged]
    assert {copies[0], copies[1], part_a, part_b, clone} <= set(cells)
    assert cells[str(frame.id)]["stack_id"] == str(stack.id)


def client_stacks():
    from state import get_active_channel
    return get_active_channel().main_stacks


def test_lineage_changed_is_sent_on_remove_restore_and_fork_O04(client, monkeypatch):
    import events
    from database import get_db_connection
    from versions import synthesize_genesis_row
    sent = []
    original = events.broadcast

    def record(channel, event):
        if event.get("type") == "lineage_changed":
            sent.append(event["cell_ids"])
        return original(channel, event)

    monkeypatch.setattr(events, "broadcast", record)
    stack, cell = add_cell("Notes", {"n.md": "n"})
    assert client.post(f"/api/stacks/{client.channel_id}/{stack.id}/remove").status_code == 200
    assert client.post(f"/api/channels/{client.channel_id}/cells/{cell.id}/restore").status_code == 200
    assert sent == [[str(cell.id)], [str(cell.id)]]

    conn = get_db_connection(client.channel_id)
    run_id = str(uuid4())
    conn.execute("INSERT INTO runs(id, prompt, status) VALUES(?, 'genesis-snapshot', 'completed')", (run_id,))
    synthesize_genesis_row(conn, str(cell.id), run_id, 1, [{"filepath": "n.md", "content": "n"}])
    conn.commit()
    conn.close()
    response = client.post(f"/api/channels/{client.channel_id}/cells/{cell.id}/fork", json={"target_cell_id": str(cell.id)})
    assert response.status_code == 200, response.text
    assert sent[-1] == [str(cell.id), response.json()["new_cell_id"]]


def test_run_status_carries_end_time_note_and_outcome_from_the_row_O04(client, monkeypatch):
    import events
    from database import get_db_connection
    from process_manager.streams import run_status_event
    conn = get_db_connection(client.channel_id)
    run_id = str(uuid4())
    conn.execute("INSERT INTO runs(id, prompt, status, kind, ended_at, note, outcome) "
                 "VALUES(?, '', 'failed', 'generator', '2026-10-06T14:05:00+00:00', 'probe failed', 'fail')",
                 (run_id,))
    conn.commit()
    conn.close()
    assert run_status_event(client.channel_id, run_id, "failed", "boom") == {
        "type": "run_status", "run_id": run_id, "status": "failed", "failure_reason": "boom",
        "ended_at": "2026-10-06T14:05:00+00:00", "note": "probe failed", "outcome": "fail"}
    assert run_status_event(client.channel_id, str(uuid4()), "stopped")["ended_at"] is None

    sent = []
    monkeypatch.setattr(events, "broadcast", lambda channel, event: sent.append(event))
    from routes.runs.control import _broadcast_status
    _broadcast_status(client.channel_id, run_id, "failed")
    assert sent[-1]["note"] == "probe failed" and sent[-1]["ended_at"] == "2026-10-06T14:05:00+00:00"
