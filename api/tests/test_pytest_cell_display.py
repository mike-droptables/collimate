"""App display KV is strict, UUID-scoped and independent of file commits."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from uuid import uuid4

import pytest


@pytest.fixture
def display_client(api_app):
    from fastapi.testclient import TestClient
    from database import get_db_connection
    from models import Cell, Channel, Stack
    from state import state
    with TestClient(api_app) as client:
        cells = [Cell(name="Display A"), Cell(name="Display B")]
        channel = Channel(name="Display", main_stacks=[Stack(cells=cells)])
        state.channels = [channel]
        state.active_channel_id = channel.id
        get_db_connection(str(channel.id)).close()
        yield client, channel, cells


def descriptor(channel=None, cell=None):
    channel, cell = str(channel or uuid4()), str(cell or uuid4())
    tabs = [
        {"id": "file", "kind": "file", "path": "a.txt", "rendererId": "text"},
        {"id": "history", "kind": "history", "category": "config", "path": "skill/instructions.md"},
        {"id": "compare", "kind": "compare", "base": {"channel": channel, "cellId": cell, "fileName": "a.txt", "label": "Before"},
         "current": {"channel": channel, "cellId": cell, "fileName": "a.txt", "label": "After"}},
    ]
    return {"version": 1, "root": {"type": "split", "id": "split", "dir": "row", "children": [
        {"type": "leaf", "id": "left", "tabs": tabs, "activeTabId": "history"},
        {"type": "leaf", "id": "right", "tabs": [{"id": "empty", "kind": "empty"}], "activeTabId": "empty"},
    ]}, "activeLeafId": "left", "filesPane": {"collapsed": False, "side": "left", "width": 240,
        "height": None, "viewMode": "folder", "panelFilters": {"runsOnly": True, "sortByRecency": False}},
        "runsPane": {"open": True, "side": "bottom", "width": None, "height": 200}, "pinnedTabCompact": True,
        "history": {"nodeId": "run:stable-node", "versionId": str(uuid4()),
                    "sectionsOpen": {"config": True, "files": False, "logs": True}}}


def url(channel, cell=None):
    base = f"/api/channels/{channel.id}/cell_display"
    return base if cell is None else f"{base}/{cell.id}"


def test_display_roundtrip_all_tabs_and_delete_leave_file_authority_unchanged(display_client):
    client, channel, cells = display_client
    from database import get_db_connection
    with get_db_connection(str(channel.id)) as conn:
        before = {table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                  for table in ("cells", "cell_files", "working_files", "file_revisions", "blobs")}
    value = descriptor(channel.id, cells[0].id)
    assert client.put(url(channel, cells[0]), json=value).json() == {"ok": True, "cell_id": str(cells[0].id)}
    assert client.get(url(channel)).json() == {str(cells[0].id): value}
    with get_db_connection(str(channel.id)) as conn:
        assert before == {table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in before}
    assert client.delete(url(channel, cells[0])).json()["deleted"] is True
    assert client.delete(url(channel, cells[0])).json()["deleted"] is False
    assert client.get(url(channel)).json() == {}


@pytest.mark.parametrize("side", ["left", "top"])
def test_display_files_pane_docks_on_each_side_FP01(display_client, side):
    client, channel, cells = display_client
    value = descriptor(channel.id, cells[0].id)
    value["filesPane"].update(side=side, width=None, height=320)
    assert client.put(url(channel, cells[0]), json=value).status_code == 200
    assert client.get(url(channel)).json()[str(cells[0].id)]["filesPane"] == value["filesPane"]


@pytest.mark.parametrize("change", [
    lambda pane: pane.pop("side"),
    lambda pane: pane.update(side="center"),
    lambda pane: pane.update(side="right"),
    lambda pane: pane.update(side="bottom"),
    lambda pane: pane.update(side=None),
    lambda pane: pane.pop("width"),
    lambda pane: pane.pop("height"),
    lambda pane: pane.update(width=200.0),
    lambda pane: pane.update(height=180.5),
    lambda pane: pane.update(width=True),
    lambda pane: pane.update(height="180"),
    lambda pane: pane.update(width=0),
    lambda pane: pane.update(height=65536),
])
def test_display_files_pane_side_and_size_are_strict_FP01(display_client, change):
    client, channel, cells = display_client
    value = descriptor(channel.id, cells[0].id)
    change(value["filesPane"])
    assert client.put(url(channel, cells[0]), json=value).status_code == 422
    assert client.get(url(channel)).json() == {}


def test_display_files_pane_size_bounds_round_trip_FP01(display_client):
    client, channel, cells = display_client
    value = descriptor(channel.id, cells[0].id)
    for width, height in ((1, 65535), (65535, 1), (None, None)):
        value["filesPane"].update(width=width, height=height)
        assert client.put(url(channel, cells[0]), json=value).status_code == 200
        stored = client.get(url(channel)).json()[str(cells[0].id)]["filesPane"]
        assert (stored["width"], stored["height"]) == (width, height)


@pytest.mark.parametrize("side", ["right", "bottom"])
def test_display_runs_pane_docks_on_each_side(display_client, side):
    client, channel, cells = display_client
    value = descriptor(channel.id, cells[0].id)
    value["runsPane"].update(open=False, side=side, width=360, height=None)
    assert client.put(url(channel, cells[0]), json=value).status_code == 200
    assert client.get(url(channel)).json()[str(cells[0].id)]["runsPane"] == value["runsPane"]


@pytest.mark.parametrize("change", [
    lambda d: d.pop("runsPane"),
    lambda d: d.update(runsPane=None),
    lambda d: d["runsPane"].pop("open"),
    lambda d: d["runsPane"].pop("side"),
    lambda d: d["runsPane"].pop("width"),
    lambda d: d["runsPane"].pop("height"),
    lambda d: d["runsPane"].update(open=None),
    lambda d: d["runsPane"].update(open="true"),
    lambda d: d["runsPane"].update(side="left"),
    lambda d: d["runsPane"].update(side="top"),
    lambda d: d["runsPane"].update(side=None),
    lambda d: d["runsPane"].update(tab="status"),
    lambda d: d["runsPane"].update(width=0),
    lambda d: d["runsPane"].update(height=65536),
    lambda d: d["runsPane"].update(width=320.5),
    lambda d: d["runsPane"].update(height="200"),
    lambda d: d["runsPane"].update(runId="run-1"),
])
def test_display_runs_pane_is_strict(display_client, change):
    client, channel, cells = display_client
    value = descriptor(channel.id, cells[0].id)
    change(value)
    assert client.put(url(channel, cells[0]), json=value).status_code == 422
    assert client.get(url(channel)).json() == {}


def test_display_per_cell_commits_and_last_writer_wins(display_client):
    client, channel, cells = display_client
    first, second = descriptor(), descriptor()
    second["activeLeafId"] = "right"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda pair: client.put(url(channel, pair[0]), json=pair[1]),
                                zip(cells, [first, second])))
    assert [response.status_code for response in results] == [200, 200]
    assert client.get(url(channel)).json() == {str(cells[0].id): first, str(cells[1].id): second}
    assert client.put(url(channel, cells[0]), json=second).status_code == 200
    assert client.get(url(channel)).json()[str(cells[0].id)] == second


def test_display_requires_registered_uuid_and_live_cell_without_ghost_database(display_client):
    client, channel, cells = display_client
    from pathlib import Path
    missing = str(uuid4())
    assert client.get(f"/api/channels/{missing}/cell_display").status_code == 404
    assert client.put(f"/api/channels/{missing}/cell_display/{cells[0].id}", json=descriptor()).status_code == 404
    assert not Path(f".collimate/channels/{missing}.db").exists()
    assert client.get('/api/channels/Display/cell_display').status_code == 404
    assert client.put(f"{url(channel)}/{uuid4()}", json=descriptor()).status_code == 404
    assert client.put(url(channel, cells[0]), json=descriptor()).status_code == 200
    channel.name = "Renamed channel"
    assert str(cells[0].id) in client.get(url(channel)).json()


@pytest.mark.parametrize("change", [
    lambda d: d.update(width=500),
    lambda d: d["root"].update(sizes=[.5, .5]),
    lambda d: d["filesPane"].update(selection={"nodePath": "a.txt"}),
    lambda d: d["root"]["children"][0]["tabs"][0].update(content="bytes"),
    lambda d: d["root"]["children"][0]["tabs"][0].update(body_hash="a" * 64),
    lambda d: d["root"]["children"][0]["tabs"][0].update(rendererId=None),
    lambda d: d.update(version=True),
    lambda d: d.update(activeLeafId="missing"),
    lambda d: d["root"]["children"][0].update(activeTabId="empty"),
    lambda d: d["root"]["children"][1].update(id="left"),
    lambda d: d["root"]["children"][1]["tabs"][0].update(id="file"),
    lambda d: d["root"]["children"][0]["tabs"][-1]["base"].update(channel="Display"),
    # The sync surfaces are retired (TR-06): their tab kind is no display tab.
    lambda d: d["root"]["children"][0]["tabs"].append({"id": "sync", "kind": "sync", "surface": "local-browse"}),
    # The manage view lives on the Capabilities stack, not in a cell tab.
    lambda d: d["root"]["children"][0]["tabs"].append({"id": "manage", "kind": "manage"}),
])
def test_display_rejects_invalid_or_non_display_fields_without_overwriting(display_client, change):
    client, channel, cells = display_client
    good = descriptor()
    assert client.put(url(channel, cells[0]), json=good).status_code == 200
    invalid = deepcopy(good)
    change(invalid)
    assert client.put(url(channel, cells[0]), json=invalid).status_code == 422
    assert client.get(url(channel)).json() == {str(cells[0].id): good}


def test_display_structural_bounds_reject_without_truncating(display_client):
    client, channel, cells = display_client
    value = descriptor()
    leaf = value["root"]["children"][0]
    for i in range(33):
        leaf = {"type": "split", "id": f"depth-{i}", "dir": "row", "children": [leaf,
            {"type": "leaf", "id": f"empty-{i}", "tabs": [], "activeTabId": None}]}
    value["root"] = leaf
    assert client.put(url(channel, cells[0]), json=value).status_code == 422
    assert client.get(url(channel)).json() == {}


def test_display_invalid_stored_snapshot_is_visible(display_client):
    client, channel, cells = display_client
    from database import get_db_connection
    with get_db_connection(str(channel.id)) as conn:
        conn.execute("INSERT INTO ui_state(key,value) VALUES(?,?)", (f"cell_display:{cells[0].id}", '{broken'))
        conn.commit()
    response = client.get(url(channel))
    assert response.status_code == 409 and "invalid" in response.text


def test_display_listing_answers_after_the_manage_tab_upgrade(api_app, fresh_cwd):
    """A channel database stored before the Capabilities stack, whose cell
    display still holds a manage tab, upgrades on open and its displays list
    (one leftover manage tab would answer 409 for the whole channel)."""
    import json
    import sqlite3
    from pathlib import Path
    from fastapi.testclient import TestClient
    import migrations
    from models import Cell, Channel, Stack
    from state import state
    with TestClient(api_app) as client:
        cell = Cell(name="Display A")
        channel = Channel(name="Display", main_stacks=[Stack(cells=[cell])])
        value = descriptor(channel.id, cell.id)
        stored = deepcopy(value)
        left = stored["root"]["children"][0]
        left["tabs"].insert(1, {"id": "manage", "kind": "manage"})
        left["activeTabId"] = "manage"
        path = Path(".collimate/channels") / f"{channel.id}.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        old = sqlite3.connect(path)
        migrations.apply(old, [m for m in migrations.CHANNEL_MIGRATIONS if m[0] <= 27])
        old.execute("INSERT INTO ui_state(key,value) VALUES (?,?)", (f"cell_display:{cell.id}", json.dumps(stored)))
        old.commit()
        old.close()
        state.channels = [channel]
        state.active_channel_id = channel.id
        response = client.get(url(channel))
        assert response.status_code == 200, response.text
        left_after = response.json()[str(cell.id)]["root"]["children"][0]
        assert [tab["id"] for tab in left_after["tabs"]] == ["file", "history", "compare"]
        assert left_after["activeTabId"] == "history"


def test_display_put_needs_no_cell_or_state_lock(display_client):
    client, channel, cells = display_client
    from cell_commits import cell_commit_lock
    from state import state_lock
    with ThreadPoolExecutor(max_workers=1) as pool, cell_commit_lock(str(cells[0].id)), state_lock:
        response = pool.submit(client.put, url(channel, cells[0]), json=descriptor()).result(timeout=5)
    assert response.status_code == 200


def test_display_shelf_barrier_refuses_late_put_before_canvas_publication(display_client):
    client, channel, cells = display_client
    from database import get_db_connection
    import removed_cells
    value = descriptor()
    assert client.put(url(channel, cells[0]), json=value).status_code == 200
    with get_db_connection(str(channel.id)) as conn:
        removed_cells.remember(conn, cell=cells[0], stack_name="", stack_cell_count=2,
            removed_at="2026-10-09T00:00:00+00:00", reason="removed")
        conn.commit()
    assert client.put(url(channel, cells[0]), json=descriptor()).status_code == 409
    assert client.delete(url(channel, cells[0])).status_code == 409
    assert client.get(url(channel)).json() == {}


def seed_files(channel, cell, files):
    from database import get_db_connection
    from persistence import initialize_files
    with get_db_connection(str(channel.id)) as conn:
        initialize_files(conn, str(cell.id), files)
        conn.commit()


def test_remove_restore_carries_display_and_typed_bytes(display_client):
    from cas import store_bytes
    from database import get_db_connection
    from models import VirtualFile
    from persistence import snapshot_files, read_file
    import removed_cells
    client, channel, cells = display_client
    cell = cells[0]
    binary = b"valid UTF-8, still declared binary"
    seed_files(channel, cell, [VirtualFile(name="a.txt", content="accepted text"),
        VirtualFile(name="typed.bin", blob_hash=store_bytes(binary), content="", language="binary", encoding="binary")])
    value = descriptor(channel.id, cell.id)
    assert client.put(url(channel, cell), json=value).status_code == 200
    with get_db_connection(str(channel.id)) as conn:
        initial = {f['path']: f['file_revision'] for f in snapshot_files(conn, str(cell.id))}
    result = client.post(f"/api/channels/{channel.id}/cells/{cell.id}/remove")
    assert result.status_code == 200, result.text
    assert client.get(url(channel)).json() == {}
    with get_db_connection(str(channel.id)) as conn:
        assert removed_cells.get(conn, str(cell.id)).display == value
        assert snapshot_files(conn, str(cell.id)) == []
    assert client.put(url(channel, cell), json=value).status_code == 409
    restored = client.post(f"/api/channels/{channel.id}/cells/{cell.id}/restore")
    assert restored.status_code == 200, restored.text
    assert client.get(url(channel)).json()[str(cell.id)] == value
    with get_db_connection(str(channel.id)) as conn:
        assert removed_cells.get(conn, str(cell.id)) is None
        for entry in snapshot_files(conn, str(cell.id)):
            assert entry['file_revision'] > initial[entry['path']]
    metadata, raw = read_file(str(channel.id), str(cell.id), "typed.bin")
    assert metadata['encoding'] == 'binary' and raw == binary


def test_merge_split_revives_original_display_and_preserves_user_names(display_client):
    from models import VirtualFile
    from database import get_db_connection
    from persistence import snapshot_files
    import removed_cells
    client, channel, cells = display_client
    for i, cell in enumerate(cells):
        seed_files(channel, cell, [VirtualFile(name=f"{i}.txt", content=f"accepted {i}")])
        assert client.put(url(channel, cell), json=descriptor(channel.id, cell.id)).status_code == 200
    saved = client.get(url(channel)).json()
    stack = channel.main_stacks[0]
    merged = client.post(f"/api/channels/{channel.id}/merge", json={'stack_id': str(stack.id)})
    assert merged.status_code == 200, merged.text
    source = merged.json()['new_cell_id']
    assert client.get(url(channel)).json() == {}
    with get_db_connection(str(channel.id)) as conn:
        assert {c.cell_id: c.display for c in removed_cells.list_removed(conn)} == saved
    split = client.post(f"/api/channels/{channel.id}/split", json={'cell_id': source})
    assert split.status_code == 200, split.text
    assert split.json()['new_cell_ids'] == [str(c.id) for c in cells]
    assert client.get(url(channel)).json() == saved
    with get_db_connection(str(channel.id)) as conn:
        assert all(removed_cells.get(conn, str(c.id)) is None for c in cells)
        assert all(snapshot_files(conn, str(c.id)) for c in cells)


def test_removed_display_transfer_rolls_back_with_caller_transaction(display_client):
    from database import get_db_connection
    from cell_display import read_display
    import removed_cells
    client, channel, cells = display_client
    value = descriptor()
    assert client.put(url(channel, cells[0]), json=value).status_code == 200
    with get_db_connection(str(channel.id)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        removed_cells.remember(conn, cell=cells[0], stack_name='', stack_cell_count=2,
            removed_at='2026-10-09T00:00:00+00:00', reason='merged')
        assert read_display(conn, str(cells[0].id)) is None
        conn.rollback()
        assert read_display(conn, str(cells[0].id)) == value
        assert removed_cells.get(conn, str(cells[0].id)) is None


def test_remove_missing_accepted_body_refuses_and_preserves_live_display(display_client):
    from cas import blob_path
    from pathlib import Path
    from models import VirtualFile
    from database import get_db_connection
    from persistence import snapshot_files
    import removed_cells
    client, channel, cells = display_client
    seed_files(channel, cells[0], [VirtualFile(name='a.txt', content='missing body')])
    value = descriptor()
    assert client.put(url(channel, cells[0]), json=value).status_code == 200
    with get_db_connection(str(channel.id)) as conn:
        initial = snapshot_files(conn, str(cells[0].id))
    Path(blob_path(initial[0]['body_hash'])).unlink()
    response = client.post(f'/api/channels/{channel.id}/cells/{cells[0].id}/remove')
    assert response.status_code == 409, response.text
    assert client.get(url(channel)).json()[str(cells[0].id)] == value
    assert cells[0] in channel.main_stacks[0].cells
    with get_db_connection(str(channel.id)) as conn:
        assert snapshot_files(conn, str(cells[0].id)) == initial
        assert removed_cells.get(conn, str(cells[0].id)) is None


def test_reset_clears_only_replaced_cell_display_and_rollback_restores_it(display_client):
    from models import VirtualFile
    from database import get_db_connection
    from cell_display import read_display
    from reset_tool import load
    _commit_reset = load()._commit_reset
    client, channel, cells = display_client
    for cell in cells:
        seed_files(channel, cell, [VirtualFile(name='a.txt', content='initial')])
        assert client.put(url(channel, cell), json=descriptor()).status_code == 200
    saved = client.get(url(channel)).json()
    with get_db_connection(str(channel.id)) as conn:
        run = str(uuid4())
        conn.execute("INSERT INTO runs(id,status) VALUES(?,'completed')", (run,))
        assert not _commit_reset(conn, run, cells[1], [VirtualFile(name='a.txt', content='initial')], None)
        assert _commit_reset(conn, run, cells[0], [VirtualFile(name='a.txt', content='replacement')], None)
        assert read_display(conn, str(cells[0].id)) is None
        assert read_display(conn, str(cells[1].id)) == saved[str(cells[1].id)]
        conn.rollback()
        assert read_display(conn, str(cells[0].id)) == saved[str(cells[0].id)]
        conn.execute("INSERT INTO runs(id,status) VALUES(?,'completed')", (run,))
        assert _commit_reset(conn, run, cells[0], [VirtualFile(name='a.txt', content='replacement')], None)
        conn.commit()
    assert client.get(url(channel)).json() == {str(cells[1].id): saved[str(cells[1].id)]}


def test_removal_checkpoints_same_bytes_with_changed_encoding(display_client):
    from models import VirtualFile
    from cas import store_bytes
    from database import get_db_connection
    from persistence import snapshot_files, restore_snapshot, read_file
    from versions import synthesize_genesis_row
    client, channel, cells = display_client
    cell = cells[0]
    seed_files(channel, cell, [VirtualFile(name='a.txt', content='same bytes')])
    with get_db_connection(str(channel.id)) as conn:
        run = str(uuid4())
        conn.execute("INSERT INTO runs(id,status) VALUES(?,'completed')", (run,))
        synthesize_genesis_row(conn, str(cell.id), run, 1, snapshot_files(conn, str(cell.id)))
        restore_snapshot(conn, str(cell.id), [{'path': 'a.txt', 'body_hash': store_bytes(b'same bytes'),
                        'encoding': 'binary', 'language': 'binary'}])
        conn.commit()
    assert client.post(f'/api/channels/{channel.id}/cells/{cell.id}/remove').status_code == 200
    assert client.post(f'/api/channels/{channel.id}/cells/{cell.id}/restore').status_code == 200
    metadata, raw = read_file(str(channel.id), str(cell.id), 'a.txt')
    assert raw == b'same bytes' and metadata['encoding'] == 'binary'


def test_lifecycle_channel_writes_never_hold_global_state_lock(display_client, monkeypatch):
    from models import VirtualFile
    import database
    import routes.options
    from state import state_lock
    client, channel, cells = display_client
    for i, cell in enumerate(cells):
        seed_files(channel, cell, [VirtualFile(name=f'{i}.txt', content=f'body {i}')])
    real = database.get_db_connection
    writes = []

    class CheckedConnection:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def execute(self, sql, *args):
            if sql.lstrip().split()[0].upper() in {'BEGIN', 'INSERT', 'UPDATE', 'DELETE', 'REPLACE'}:
                assert not state_lock._is_owned(), sql
                writes.append(sql)
            return self.conn.execute(sql, *args)

        def executemany(self, sql, *args):
            assert not state_lock._is_owned(), sql
            writes.append(sql)
            return self.conn.executemany(sql, *args)

    def checked(*args, **kwargs):
        return CheckedConnection(real(*args, **kwargs))

    monkeypatch.setattr(database, 'get_db_connection', checked)
    monkeypatch.setattr(routes.options, 'get_db_connection', checked)
    stack = channel.main_stacks[0]
    merged = client.post(f'/api/channels/{channel.id}/merge', json={'stack_id': str(stack.id)})
    assert merged.status_code == 200, merged.text
    split = client.post(f'/api/channels/{channel.id}/split', json={'cell_id': merged.json()['new_cell_id']})
    assert split.status_code == 200, split.text
    assert client.post(f'/api/channels/{channel.id}/cells/{cells[0].id}/remove').status_code == 200
    assert client.post(f'/api/channels/{channel.id}/cells/{cells[0].id}/restore').status_code == 200
    assert len(writes) > 10
