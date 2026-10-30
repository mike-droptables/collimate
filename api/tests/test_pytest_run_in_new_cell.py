"""Run in a new cell clones accepted SQL files under a fresh logical ID.

Binary references and surviving pins cross; tombstoned paths and run folders do
not. The source accepted revisions remain unchanged. Real runner preparation,
manager stand-ins, and actual run/lineage receipts verify the contract.
"""
from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

import pytest

from conftest import install_runner
from run_spawn import wait_spawned
from database import get_db_connection

CHANNEL = str(uuid.uuid5(uuid.NAMESPACE_URL, "tests:run-in-new-cell:channel"))


@pytest.fixture
def client(api_app):
    from fastapi.testclient import TestClient
    from models import Channel
    from state import state
    with TestClient(api_app) as http:
        channel = Channel(id=uuid.UUID(CHANNEL), name='Starting')
        state.channels = [channel]
        state.active_channel_id = channel.id
        get_db_connection(str(channel.id)).close()
        http.channel_id = CHANNEL
        yield http


def _accepted(cell):
    from persistence import snapshot_files
    with get_db_connection(CHANNEL) as conn:
        return snapshot_files(conn, str(cell.id), include_deleted=True)


@pytest.fixture
def started(monkeypatch):
    """Record managers instead of spawning, and capture broadcasts."""
    import events
    import routes.runs.create as create

    managers: dict = {}
    broadcasts: list = []

    class _Manager:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            self.process = None
            managers[self.run_id] = self

        async def start(self):
            pass

    async def _no_reap(*_a):
        pass

    monkeypatch.setattr(create, "ProcessManager", _Manager)
    monkeypatch.setattr(create, "_reap_process", _no_reap)
    monkeypatch.setattr(events, "broadcast", lambda ch, ev: broadcasts.append(ev))
    yield managers, broadcasts
    # The stand-ins never finish: take them out of the registry so a later
    # test's channel rename or delete does not wait on them.
    from run_registry import active_managers
    for run_id in managers:
        active_managers.pop(run_id, None)


def _blob(raw: bytes) -> str:
    h = hashlib.sha256(raw).hexdigest()
    path = Path(".collimate", "objects", h[:2], h[2:])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return h


def _source_cell(client):
    """A canvas cell with accepted text, binary, a run folder and a tombstone."""
    from models import VirtualFile
    from state import state

    stack_id = client.post(f"/api/stacks/{client.channel_id}/create").json()["main_stacks"][-1]["id"]
    ch = next(c for c in state.channels if c.id == state.active_channel_id)
    stack = next(s for s in ch.main_stacks if str(s.id) == stack_id)
    cell = stack.cells[0]
    cell.name = "Notes"
    png = b"\x89PNG\r\n\x1a\n\x00\xff binary"
    cell.files = [
        VirtualFile(name="keep.md", content="keep"),
        VirtualFile(name="gone.md", content="tombstoned"),
        VirtualFile(name="image.png", content="", language="binary", blob_hash=_blob(png)),
        VirtualFile(name="_runs/agent-aaaaaaaa/chat/main.json", content="[]"),
    ]
    cell.pinned = "keep.md"
    cid = str(cell.id)
    from persistence import initialize_files, mutate_files
    with get_db_connection(str(ch.id)) as conn:
        initialize_files(conn, cid, cell.files)
    receipt = mutate_files(str(ch.id), cid, dict(operation_id=str(uuid.uuid4()), mutations=[
        dict(path='added.md', kind='put', content_text='accepted added text', base_revision=0,
             writer_id='clone-fixture', edit_seq=1),
        dict(path='gone.md', kind='delete', base_revision=1, writer_id='clone-fixture', edit_seq=1),
    ]), actor='host')
    assert receipt['outcome'] == 'accepted'
    return ch, stack, cell, png


def test_run_in_new_cell_clones_the_exact_file_state(client, started):
    managers, broadcasts = started
    runner, _gen = install_runner(generator="clone_probe", runner="clone-probe")
    ch, stack, source, png = _source_cell(client)
    before_source = _accepted(source)
    source_index = [str(s.id) for s in ch.main_stacks].index(str(stack.id))

    r = client.post(f"/api/channels/{CHANNEL}/runs",
                    json={"runner": runner, "cell_id": str(source.id), "new_cell": True})
    assert r.status_code == 200, r.text
    body = r.json()
    wait_spawned(body["run_id"])
    assert body["cell_id"] != str(source.id)

    # A new standard stack right of the source's, holding one cell.
    new_stack = ch.main_stacks[source_index + 1]
    assert str(new_stack.id) == body["stack_id"] and new_stack.stack_type == "standard"
    [clone] = new_stack.cells
    assert str(clone.id) == body["cell_id"] and clone.name == "Notes"
    assert clone.pinned == "keep.md"

    files = {f.name: f for f in clone.files}
    label = body["label"]
    assert set(files) == {"keep.md", "added.md", "image.png", f"_runs/{label}/{label}.run"}
    assert files["added.md"].content == "accepted added text"
    assert files["image.png"].blob_hash == hashlib.sha256(png).hexdigest()

    # The run is on the clone; source accepted revisions and tombstone are unchanged.
    [manager] = managers.values()
    assert manager.cell_id == body['cell_id']
    assert _accepted(source) == before_source
    assert {f.name for f in source.files} == {
        'keep.md', 'added.md', 'image.png', '_runs/agent-aaaaaaaa/chat/main.json'}
    clone_files = {f['path']: f for f in _accepted(clone)}
    assert clone_files['image.png']['encoding'] == 'binary'
    assert clone_files['image.png']['body_hash'] == hashlib.sha256(png).hexdigest()
    read = client.get(f'/api/channels/{ch.id}/cells/{clone.id}/files/image.png',
                      params={'revision': clone_files['image.png']['file_revision']})
    assert read.status_code == 200 and read.content == png

    [event] = [e for e in broadcasts if e["type"] == "run_started"]
    assert event["cell_id"] == body["cell_id"] and event["stack_id"] == body["stack_id"]


def test_a_pin_on_a_file_the_clone_drops_is_not_copied(client, started):
    runner, _gen = install_runner(generator="clone_pin", runner="clone-pin")
    _ch, _stack, source, _png = _source_cell(client)
    source.pinned = "_runs/agent-aaaaaaaa/chat/main.json"
    r = client.post(f"/api/channels/{CHANNEL}/runs",
                    json={"runner": runner, "cell_id": str(source.id), "new_cell": True})
    assert r.status_code == 200, r.text
    from cell_placement import find_cell
    from state import get_channel_by_id
    assert find_cell(get_channel_by_id(CHANNEL), r.json()["cell_id"])[1].pinned is None


def test_a_plain_run_needs_no_clone(client, started):
    runner, _gen = install_runner(generator="clone_none", runner="clone-none")
    ch, _stack, source, _png = _source_cell(client)
    stacks = len(ch.main_stacks)
    r = client.post(f"/api/channels/{CHANNEL}/runs",
                    json={"runner": runner, "cell_id": str(source.id)})
    assert r.status_code == 200, r.text
    assert r.json()["cell_id"] == str(source.id) and "stack_id" not in r.json()
    assert len(ch.main_stacks) == stacks


def test_new_cell_on_an_unknown_cell_is_refused_without_a_clone(client, started):
    runner, _gen = install_runner(generator="clone_ghost", runner="clone-ghost")
    from state import state
    ch = next(c for c in state.channels if c.id == state.active_channel_id)
    stacks = len(ch.main_stacks)
    r = client.post(f"/api/channels/{CHANNEL}/runs",
                    json={"runner": runner, "cell_id": str(uuid.uuid4()), "new_cell": True})
    assert r.status_code == 400
    assert len(ch.main_stacks) == stacks


def test_run_start_keeps_input_artifact_list_with_scalar_manager__F0216(client, started):
    import yaml
    from cell_commits import blob_bytes

    managers, _ = started
    runner, _ = install_runner(generator="scalar_probe", runner="scalar-probe")
    _, _, source, _ = _source_cell(client)
    response = client.post(f"/api/channels/{CHANNEL}/runs",
                           json={"runner": runner, "cell_id": str(source.id)})
    assert response.status_code == 200, response.text
    wait_spawned(response.json()["run_id"])
    manager = managers[response.json()["run_id"]]
    assert isinstance(manager.input_tip_id, str)
    conn = get_db_connection(CHANNEL)
    try:
        artifact = conn.execute(
            "SELECT blob_hash FROM run_artifacts WHERE run_id=? AND category='config' "
            "AND filepath='run.yaml'", (manager.run_id,),
        ).fetchone()
        assert artifact is not None
        recipe = yaml.safe_load(blob_bytes(artifact["blob_hash"]))
        assert recipe["input_version_ids"] == [manager.input_tip_id]
        assert conn.execute("SELECT 1 FROM cells WHERE id=?", (manager.input_tip_id,)).fetchone()
    finally:
        conn.close()


def _run_cell_moves():
    conn = get_db_connection(CHANNEL)
    try:
        return [tuple(r) for r in conn.execute(
            "SELECT op, op_id, from_cell, from_version, to_cell, to_version, ord FROM cell_moves ORDER BY id")]
    finally:
        conn.close()


@pytest.mark.parametrize("versioned", [False, True])
def test_run_in_new_cell_records_the_lineage_O03(client, started, versioned):
    _managers, broadcasts = started
    runner, _gen = install_runner(generator="lineage_probe", runner="lineage-probe")
    _ch, _stack, source, _png = _source_cell(client)
    tip = None
    if versioned:
        checkpoint = client.post(f'/api/channels/{CHANNEL}/cells/{source.id}/checkpoint')
        assert checkpoint.status_code == 200, checkpoint.text
        from versions import find_tip
        with get_db_connection(CHANNEL) as conn:
            tip = find_tip(conn, str(source.id))
            assert conn.execute('SELECT 1 FROM cells WHERE id=?', (tip,)).fetchone()

    r = client.post(f"/api/channels/{CHANNEL}/runs",
                    json={"runner": runner, "cell_id": str(source.id), "new_cell": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert _run_cell_moves() == [
        ("run_cell", body["run_id"], str(source.id), tip, body["cell_id"], None, 0)]
    [event] = [e for e in broadcasts if e["type"] == "lineage_changed"]
    assert event["cell_ids"] == [str(source.id), body["cell_id"]]


def test_a_plain_run_records_no_lineage_O03(client, started):
    _managers, broadcasts = started
    runner, _gen = install_runner(generator="lineage_plain", runner="lineage-plain")
    _ch, _stack, source, _png = _source_cell(client)
    r = client.post(f"/api/channels/{CHANNEL}/runs", json={"runner": runner, "cell_id": str(source.id)})
    assert r.status_code == 200, r.text
    assert _run_cell_moves() == []
    assert not [e for e in broadcasts if e["type"] == "lineage_changed"]


def test_a_failed_new_cell_start_unwinds_the_clone_and_its_lineage_O03(client, started, monkeypatch):
    import routes.runs.create as create
    runner, _gen = install_runner(generator="lineage_fail", runner="lineage-fail")
    ch, _stack, source, _png = _source_cell(client)
    stacks = len(ch.main_stacks)

    class _Failing(create.ProcessManager):
        stop_requested = False

        async def start(self):
            raise RuntimeError("synthetic spawn failure")

    monkeypatch.setattr(create, "ProcessManager", _Failing)
    r = client.post(f"/api/channels/{CHANNEL}/runs",
                    json={"runner": runner, "cell_id": str(source.id), "new_cell": True})
    assert r.status_code == 200, r.text
    wait_spawned(r.json()["run_id"])
    assert len(ch.main_stacks) == stacks
    assert _run_cell_moves() == []
    conn = get_db_connection(CHANNEL)
    try:
        row = conn.execute("SELECT status, ended_at FROM runs WHERE kind='generator'").fetchone()
    finally:
        conn.close()
    assert row["status"] == "failed" and row["ended_at"].endswith("+00:00")
