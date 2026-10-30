"""Removed cells come back under their own identity; restoring never
duplicates history. The shelf listing itself is test_pytest_removed_shelf.py.
The cell search over the catalog is gone (K02)."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest


@pytest.fixture
def client(api_app):
    """Real API lifespan with a synthetic channel; no shipped definitions needed."""
    from fastapi.testclient import TestClient
    from database import get_db_connection
    from models import Channel
    from state import state, persist_state, flush_persist_state

    with TestClient(api_app, raise_server_exceptions=False) as http:
        channel = Channel(name='Starting')
        from genesis import ensure_capabilities_stack
        ensure_capabilities_stack(channel)
        state.channels = [channel]
        state.active_channel_id = channel.id
        get_db_connection(str(channel.id)).close()
        persist_state()
        flush_persist_state()
        http.channel_id = str(channel.id)
        yield http


def add_cell(name="Research", files=None, *, channel=None, **metadata):
    from models import Cell, Stack, VirtualFile
    from state import get_active_channel
    ch = channel or get_active_channel()
    cell = Cell(name=name, files=[VirtualFile(name=k, content=v) for k, v in (files or {}).items()], **metadata)
    stack = Stack(name="Project", cells=[cell])
    ch.main_stacks.append(stack)
    from database import get_db_connection
    from persistence import initialize_files
    with get_db_connection(str(ch.id)) as conn:
        initialize_files(conn, str(cell.id), cell.files)
    return ch, stack, cell


def shelf(client, channel_id=None, **params):
    response = client.get(f"/api/channels/{channel_id or client.channel_id}/cells/removed", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def accepted_files(client, cell_id):
    from database import get_db_connection
    from persistence import snapshot_files
    conn = get_db_connection(client.channel_id)
    try:
        return {f['path']: f for f in snapshot_files(conn, str(cell_id))}
    finally:
        conn.close()


def mutate(client, cell, path, *, kind='put', base=None, **body):
    from database import get_db_connection
    from persistence import current_file
    with get_db_connection(client.channel_id) as conn:
        row = current_file(conn, str(cell.id), path)
        revision = row['revision'] if row else 0
    return client.post(f'/api/channels/{client.channel_id}/cells/{cell.id}/files/mutate', json={
        'operation_id': str(uuid4()), 'mutations': [{
            'path': path, 'kind': kind, 'base_revision': revision if base is None else base,
            'writer_id': 'removed-cell-test', 'edit_seq': revision + 1, **body,
        }],
    })


def restore(client, cell, **body):
    return client.post(f"/api/channels/{client.channel_id}/cells/{cell.id}/restore", json=body)


def test_restore_refuses_cells_in_the_layout_and_the_old_routes_are_gone_O04(client):
    from cell_placement import capabilities_stack
    from models import Cell
    ch, stack, first = add_cell(icon="BookOpen")
    second = Cell(name="Hidden below")
    stack.cells.append(second)
    source = Cell(name="Source")
    capabilities_stack(ch).cells.append(source)
    for cell in (first, second, source):
        assert restore(client, cell).status_code == 409
    assert [str(c.id) for c in stack.cells] == [str(first.id), str(second.id)]
    assert client.post(f"/api/channels/{client.channel_id}/cells/{first.id}/open").status_code in (404, 405)
    assert "/api/channels/{channel}/cells/{cell_id}/open" not in client.get("/openapi.json").json()["paths"]


def test_cell_search_route_is_gone_K02(client):
    import cell_catalog
    path = "/api/channels/{channel}/cell_search"
    assert all(getattr(route, "path", "") != path for route in client.app.routes)
    assert client.get(f"/api/channels/{client.channel_id}/cell_search").status_code in (404, 405)
    assert not hasattr(cell_catalog, "search_cells")


def test_remove_materializes_blank_cell_and_preserves_metadata(client):
    from database import get_db_connection
    ch, stack, cell = add_cell("Blank notebook", icon="Notebook", pinned="note.md", settings={"view": "notes"})
    response = client.post(f"/api/channels/{client.channel_id}/cells/{cell.id}/remove")
    assert response.status_code == 200, response.text
    entry = shelf(client)[0]
    assert entry["cell_id"] == str(cell.id) and entry["reason"] == "removed"
    assert entry["name"] == cell.name and entry["icon"] == cell.icon
    conn = get_db_connection(client.channel_id)
    before = conn.execute("SELECT count(*) FROM cells").fetchone()[0]
    response = restore(client, cell)
    assert response.status_code == 200, response.text
    assert response.json()["cell_id"] == str(cell.id)
    restored = next(c for s in ch.main_stacks for c in s.cells if c.id == cell.id)
    assert restored.files == [] and restored.created_at == cell.created_at
    assert restored.icon == cell.icon and restored.settings == cell.settings and restored.pinned == cell.pinned
    assert conn.execute("SELECT count(*) FROM cells").fetchone()[0] == before
    conn.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: restore(client, cell), range(2)))
    assert all(r.status_code == 409 for r in responses)
    assert len([c for s in ch.main_stacks for c in s.cells if c.id == cell.id]) == 1


@pytest.mark.parametrize("binary", [b"\x00\xff\x89PNG", b"ASCII bytes stored as binary"])
def test_remove_stack_preserves_accepted_text_and_explicit_binary(client, binary):
    from cas import hash_and_store_bytes
    ch, stack, cell = add_cell(files={"edited.txt": "before", "removed.txt": "delete me", "untouched.txt": "keep"})
    assert mutate(client, cell, 'edited.txt', content_text='autosaved').status_code == 200
    assert mutate(client, cell, 'removed.txt', kind='delete').status_code == 200
    digest = hash_and_store_bytes(binary)
    assert mutate(client, cell, 'image.bin', blob_hash=digest, encoding='binary').status_code == 200
    response = client.post(f"/api/stacks/{client.channel_id}/{stack.id}/remove")
    assert response.status_code == 200, response.text
    assert [e["cell_id"] for e in shelf(client)] == [str(cell.id)]
    assert accepted_files(client, cell.id) == {}
    response = restore(client, cell)
    assert response.status_code == 200, response.text
    assert any(c.id == cell.id for s in ch.main_stacks for c in s.cells)
    files = accepted_files(client, cell.id)
    assert set(files) == {"edited.txt", "untouched.txt", "image.bin"}
    for path, raw in [('edited.txt', b'autosaved'), ('untouched.txt', b'keep'), ('image.bin', binary)]:
        response = client.get(f'/api/channels/{client.channel_id}/cells/{cell.id}/files/{path}')
        assert response.status_code == 200 and response.content == raw
    assert files['image.bin']['body_hash'] == digest and files['image.bin']['encoding'] == 'binary'
    assert (Path(".collimate/objects") / digest[:2] / digest[2:]).read_bytes() == binary


def test_restore_keeps_binary_reference_and_metadata_only_response(client):
    from cas import hash_and_store_bytes
    ch, stack, cell = add_cell(files={"notes.md": "keep"})
    digest = hash_and_store_bytes(b"\x00\xff large binary")
    assert mutate(client, cell, 'image.bin', blob_hash=digest, encoding='binary').status_code == 200
    assert client.post(f"/api/stacks/{client.channel_id}/{stack.id}/remove").status_code == 200
    response = restore(client, cell)
    assert response.status_code == 200, response.text
    assert any(c.id == cell.id for s in ch.main_stacks for c in s.cells)
    assert accepted_files(client, cell.id)['image.bin']['body_hash'] == digest
    metadata = next(f for stack in response.json()['channel']['main_stacks'] for c in stack['cells']
                    if c['id'] == str(cell.id) for f in c['files'] if f['name'] == 'image.bin')
    assert metadata['body_hash'] == digest and metadata['encoding'] == 'binary'
    assert 'content' not in metadata and 'blob_hash' not in metadata


def test_restore_refuses_late_mutations_after_removal__P0F12(client):
    ch, stack, cell = add_cell(files={"notes.md": "old", "gone.md": "remove later"})
    assert client.post(f"/api/stacks/{client.channel_id}/{stack.id}/remove").status_code == 200
    assert mutate(client, cell, 'notes.md', base=1, content_text='late save').status_code == 404
    assert mutate(client, cell, 'gone.md', base=1, kind='delete').status_code == 404
    assert restore(client, cell).status_code == 200
    assert any(c.id == cell.id for s in ch.main_stacks for c in s.cells)
    for path, text in {'notes.md': 'old', 'gone.md': 'remove later'}.items():
        assert client.get(f'/api/channels/{client.channel_id}/cells/{cell.id}/files/{path}').text == text


def seed_history(channel_id, *, name="Historical", source=False):
    from database import get_db_connection
    from cell_commits import cell_commit_lock
    from versions import synthesize_genesis_row, write_version_from_overlay
    from models import VirtualFile
    root, run_id = str(uuid4()), str(uuid4())
    conn = get_db_connection(channel_id)
    conn.execute("INSERT INTO runs(id,prompt,status) VALUES(?, '', 'completed')", (run_id,))
    synthesize_genesis_row(conn, root, run_id, 1,
                          [VirtualFile(name="package/choice.generator" if source else "notes.md", content="old")])
    conn.execute("UPDATE cells SET version_name=? WHERE id=?", (name, root))
    with cell_commit_lock(root):
        tip = write_version_from_overlay(conn, root, run_id, [("notes.md", "set", "latest")],
                                          version_name="Second version", source_type="manual_edit",
                                          generator_name="manual_edit")
    conn.commit()
    conn.close()
    return root, tip


def test_restore_needs_a_removed_record_O04(client):
    """A version chain the channel never recorded as removed (no
    removed_cells row) is not restorable, whatever its versions."""
    root, tip = seed_history(client.channel_id)
    for cid in (root, tip, str(uuid4())):
        assert client.post(f"/api/channels/{client.channel_id}/cells/{cid}/restore").status_code == 404
    assert shelf(client) == []


def test_restore_is_channel_scoped_without_creating_ghost_db(client):
    from models import Channel
    from state import state
    ch, stack, cell = add_cell()
    assert client.post(f"/api/stacks/{client.channel_id}/{stack.id}/remove").status_code == 200
    from database import get_db_connection
    other = Channel(name="Other")
    from genesis import ensure_capabilities_stack
    ensure_capabilities_stack(other)
    state.channels.append(other)
    get_db_connection(str(other.id)).close()
    _, _, other_cell = add_cell("Other only", channel=other)
    state.active_channel_id = other.id
    assert shelf(client, str(other.id)) == []
    assert client.post(f"/api/channels/{other.id}/cells/{cell.id}/restore").status_code == 404
    assert client.post(f"/api/channels/{other.id}/cells/{other_cell.id}/restore").status_code == 409
    assert client.get("/api/channels/unknown/cells/removed").status_code == 404
    assert client.post(f"/api/channels/unknown/cells/{cell.id}/restore").status_code == 404
    assert not Path(".collimate/channels/unknown.db").exists()
    response = restore(client, cell)
    assert response.status_code == 200 and response.json()["channel"]["id"] == str(ch.id)
    assert state.active_channel_id == other.id


@pytest.mark.parametrize("operation", ["stack", "cell"])
def test_removal_failure_keeps_canvas_and_accepted_files(client, monkeypatch, operation):
    import cas
    from database import get_db_connection
    ch, stack, cell = add_cell(files={"notes.md": "keep"})
    assert mutate(client, cell, 'notes.md', content_text='new work').status_code == 200
    before = accepted_files(client, cell.id)
    def fail_read(_digest):
        raise OSError('synthetic storage read error')
    monkeypatch.setattr(cas, 'require_blob', fail_read)
    remove_url = (f"/api/stacks/{client.channel_id}/{stack.id}/remove" if operation == "stack"
                  else f"/api/channels/{client.channel_id}/cells/{cell.id}/remove")
    response = client.post(remove_url)
    assert stack in ch.main_stacks and stack.cells == [cell]
    assert accepted_files(client, cell.id) == before
    conn = get_db_connection(client.channel_id)
    assert conn.execute("SELECT count(*) FROM cells WHERE id=?", (str(cell.id),)).fetchone()[0] == 0
    conn.close()
    assert response.status_code == 409, response.text


def test_missing_content_cannot_restore(client):
    from database import get_db_connection
    from versions import version_file_entries
    ch, stack, cell = add_cell(files={"notes.md": "kept"})
    assert client.post(f"/api/stacks/{client.channel_id}/{stack.id}/remove").status_code == 200
    conn = get_db_connection(client.channel_id)
    removed = shelf(client)[0]
    digest = version_file_entries(conn, removed["tip_version_id"])[0]["body_hash"]
    conn.close()
    (Path(".collimate/objects") / digest[:2] / digest[2:]).unlink()
    response = restore(client, cell)
    assert response.status_code == 409, response.text
    assert [e["cell_id"] for e in shelf(client)] == [str(cell.id)]


def test_concurrent_restore_creates_only_one_canvas_cell(client):
    ch, stack, cell = add_cell(files={"notes.md": "same logical cell"})
    assert client.post(f"/api/stacks/{client.channel_id}/{stack.id}/remove").status_code == 200
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: restore(client, cell), range(2)))
    assert sorted(r.status_code for r in responses) == [200, 409]
    assert len([c for s in ch.main_stacks for c in s.cells if c.id == cell.id]) == 1


def test_ordinary_cell_containing_generator_code_remains_restorable(client):
    ch, stack, cell = add_cell("Generator notes", files={"example/choice.generator": "id: example"})
    assert client.post(f"/api/stacks/{client.channel_id}/{stack.id}/remove").status_code == 200
    assert [item["cell_id"] for item in shelf(client)] == [str(cell.id)]
    assert restore(client, cell).status_code == 200


def test_previously_mounted_history_version_refuses_removed_cell_mutation__P0F12(client):
    from uuid import UUID
    root, tip = seed_history(client.channel_id)
    ch, stack, cell = add_cell("Renamed historical cell", id=UUID(tip), icon="History",
                               settings={"view": "history"}, files={"notes.md": "latest"})
    assert client.post(f"/api/stacks/{client.channel_id}/{stack.id}/remove").status_code == 200
    entries = shelf(client)
    assert len(entries) == 1 and entries[0]["cell_id"] == tip and entries[0]["tip_version_id"] == tip
    assert entries[0]["name"] == cell.name and entries[0]["icon"] == cell.icon
    assert mutate(client, cell, 'notes.md', base=1, content_text='late historical save').status_code == 404
    assert client.post(f"/api/channels/{client.channel_id}/cells/{root}/restore").status_code == 404
    response = client.post(f"/api/channels/{client.channel_id}/cells/{tip}/restore")
    assert response.status_code == 200, response.text
    assert response.json()["cell_id"] == tip
    restored = next(c for s in ch.main_stacks for c in s.cells if str(c.id) == tip)
    assert restored.settings == cell.settings and restored.icon == cell.icon
    assert client.get(f'/api/channels/{client.channel_id}/cells/{tip}/files/notes.md').text == 'latest'
    assert shelf(client) == []


def test_unreadable_content_does_not_restore_as_empty_file(client, monkeypatch):
    ch, stack, cell = add_cell(files={"notes.md": "kept"})
    assert client.post(f"/api/stacks/{client.channel_id}/{stack.id}/remove").status_code == 200
    import cas
    def fail_cas(_digest):
        raise OSError("synthetic storage read error")
    monkeypatch.setattr(cas, 'require_blob', fail_cas)
    response = restore(client, cell)
    assert response.status_code == 409, response.text
    assert [e["cell_id"] for e in shelf(client)] == [str(cell.id)]


def test_remove_writes_removed_cells_rows_with_stack_size_and_restore_forgets_O03(client):
    """Remove records each cell in the removed_cells table (reason
    'removed', the stack's size at removal, UTC ISO times), never in
    ui_state; restoring the cell deletes its record."""
    import removed_cells
    from database import get_db_connection
    from models import Cell
    ch, trio, first = add_cell("First")
    trio.cells += [Cell(name="Second"), Cell(name="Third")]
    _, pair, solo = add_cell("Pair one", channel=ch)
    pair.cells.append(Cell(name="Pair two"))
    assert client.post(f"/api/channels/{client.channel_id}/cells/{first.id}/remove").status_code == 200
    assert client.post(f"/api/stacks/{client.channel_id}/{pair.id}/remove").status_code == 200
    conn = get_db_connection(client.channel_id)
    try:
        records = {r.name: r for r in removed_cells.list_removed(conn)}
        assert {name: (r.stack_cell_count, r.reason, r.stack_name) for name, r in records.items()} == {
            "First": (3, "removed", "Project"), "Pair one": (2, "removed", "Project"),
            "Pair two": (2, "removed", "Project")}
        assert all(r.removed_at.endswith("+00:00") for r in records.values())
        assert records["First"].cell_id == str(first.id)
        assert conn.execute("SELECT COUNT(*) FROM ui_state WHERE key='removed_cells'").fetchone()[0] == 0
    finally:
        conn.close()
    assert restore(client, first).status_code == 200
    conn = get_db_connection(client.channel_id)
    try:
        assert removed_cells.get(conn, str(first.id)) is None
        assert removed_cells.get(conn, str(solo.id)) is not None
    finally:
        conn.close()
    assert {e["name"] for e in shelf(client)} == {"Pair one", "Pair two"}
