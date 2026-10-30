"""Channel delete and rename: deleting a channel must not unlink CAS blobs
other channels still use. Delete refuses active runs; rename keeps UUID addressing."""

import os

from fake_manager import FakeManager


def test_delete_channel_keeps_blob_referenced_by_other_channel_current_file__F0349(client):
    import cas
    import database
    from state import state, channel_db_name

    keeper = next(c for c in state.channels if c.id == state.active_channel_id)
    r = client.post("/api/channels", params={"name": "Doomed"})
    assert r.status_code == 200, r.text
    doomed_id = r.json()["id"]
    doomed = next(c for c in state.channels if str(c.id) == doomed_id)

    h = cas.hash_and_store_bytes(b"\x89PNG shared binary bytes")
    assert h

    from models import VirtualFile
    from persistence import initialize_files
    # A current accepted binary body is shared across two channel databases.
    for channel, cell_id in ((doomed, "doomed-cell"), (keeper, "keeper-cell")):
        with database.get_db_connection(channel_db_name(channel)) as conn:
            initialize_files(conn, cell_id, [VirtualFile(name="a.png", blob_hash=h)])

    r = client.delete(f"/api/channels/{doomed_id}")
    assert r.status_code == 200, r.text
    assert os.path.isfile(cas.blob_path(h)), "blob still referenced by an accepted file was unlinked"


# ─── UUID names are metadata; only delete interrupts run storage ────────────

class _LiveRun:
    """What the refusal reads off a registered run: its immutable channel UUID."""

    def __init__(self, channel: str):
        self.channel = channel
        self.cell_id = None


def _db_path(slug: str) -> str:
    return os.path.join(".collimate", "channels", f"{slug}.db")


def _second_channel(client, name: str):
    from state import state
    r = client.post("/api/channels", params={"name": name})
    assert r.status_code == 200, r.text
    return next(c for c in state.channels if str(c.id) == r.json()["id"])


def test_rename_preserves_database_while_a_run_is_live__F0318__F0349(client):
    from run_registry import active_managers
    from state import state, channel_db_name
    ch = next(c for c in state.channels if c.id == state.active_channel_id)
    channel_id = channel_db_name(ch)
    before = os.stat(_db_path(channel_id))
    active_managers["live-run"] = _LiveRun(channel_id)
    try:
        response = client.put(f"/api/channels/{ch.id}/rename", params={"name": "Renamed"})
        assert response.status_code == 200, response.text
        assert ch.name == "Renamed"
        assert channel_db_name(ch) == channel_id
        after = os.stat(_db_path(channel_id))
        assert (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
        assert not os.path.exists(_db_path("renamed"))
    finally:
        active_managers.pop("live-run", None)


def test_delete_is_refused_while_a_run_is_live__F0318__F0349(client):
    """Delete would unlink the db (and blobs) a live run still writes. A run
    in ANOTHER channel does not block it."""
    from run_registry import active_managers
    from state import state, channel_db_name

    doomed = _second_channel(client, "Doomed")
    slug = channel_db_name(doomed)
    active_managers["other-run"] = _LiveRun(str(next(c.id for c in state.channels if c.id != doomed.id)))
    active_managers["child-run"] = _LiveRun(slug)
    try:
        r = client.delete(f"/api/channels/{doomed.id}")
        assert r.status_code == 409, r.text
        assert any(c.id == doomed.id for c in state.channels)
        assert os.path.isfile(_db_path(slug))
    finally:
        active_managers.pop("child-run", None)

    try:
        r = client.delete(f"/api/channels/{doomed.id}")
        assert r.status_code == 200, r.text
    finally:
        active_managers.pop("other-run", None)
    assert not any(c.id == doomed.id for c in state.channels)
    assert not os.path.exists(_db_path(slug))


def test_delete_is_refused_while_a_starting_hold_holds_a_cell__F0318(client):
    """A start holding a cell (its run row written, its manager not yet
    registered) counts as live on the cell's channel."""
    from models import Cell, Stack
    import run_registry as registry

    doomed = _second_channel(client, "Doomed")
    cell = Cell()
    doomed.main_stacks.append(Stack(cells=[cell]))
    registry.reserve("starting-run", str(cell.id))
    try:
        assert client.delete(f"/api/channels/{doomed.id}").status_code == 409
        r = client.put(f"/api/channels/{doomed.id}/rename", params={"name": "Kept"})
        assert r.status_code == 200
    finally:
        registry.release("starting-run")
    assert client.delete(f"/api/channels/{doomed.id}").status_code == 200


def test_a_start_under_way_blocks_delete_and_allows_rename__F0318(client, monkeypatch):
    """A start keeps the channel alive through its awaits; names may change."""
    import threading
    import uuid

    import routes.runs.create as create
    from runners import RunnerError
    from state import channel_db_name

    doomed = _second_channel(client, "Doomed")
    slug = channel_db_name(doomed)
    reached, release = threading.Event(), threading.Event()

    def blocked_resolve(*_args, **_kwargs):
        # The start's first await: it holds here, mid-start.
        reached.set()
        release.wait(10)
        raise RunnerError(404, "no such runner")
    monkeypatch.setattr(create, "resolve_runner_run", blocked_resolve)

    started: dict = {}
    thread = threading.Thread(target=lambda: started.setdefault("r", client.post(
        f"/api/channels/{doomed.id}/runs",
        json={"runner": "synthetic", "cell_id": str(uuid.uuid4())})))
    thread.start()
    try:
        assert reached.wait(10)
        r = client.put(f"/api/channels/{doomed.id}/rename", params={"name": "Renamed"})
        assert r.status_code == 200, r.text
        assert client.delete(f"/api/channels/{doomed.id}").status_code == 409
        assert os.path.isfile(_db_path(slug))
    finally:
        release.set()
        thread.join(10)
    assert started["r"].status_code == 404

    r = client.put(f"/api/channels/{doomed.id}/rename", params={"name": "Renamed"})
    assert r.status_code == 200, r.text
    assert os.path.isfile(_db_path(slug))
    assert client.delete(f"/api/channels/{doomed.id}").status_code == 200


class _ExitingRun(FakeManager):
    """A live run whose reaper finishes while a Stop is still writing: its
    log flush (the Stop's second db hop) takes it out of the registry, then
    holds until the test releases it."""

    def __init__(self, channel: str, run_id: str, reached, release):
        from types import SimpleNamespace

        super().__init__()

        self.channel = channel
        self.run_id = run_id
        self.cell_id = None
        self.process = SimpleNamespace(pid=2**30, returncode=None)
        self._reached = reached
        self._release = release

    def _flush_log_buffer(self):
        from run_registry import active_managers
        active_managers.pop(self.run_id, None)
        self._reached.set()
        self._release.wait(10)


def test_a_stop_under_way_blocks_delete_and_allows_rename__F0318(client):
    """Stop holds the UUID database through its final write after reaping."""
    import threading
    import uuid

    import database
    from run_registry import active_managers
    from state import channel_db_name

    doomed = _second_channel(client, "Doomed")
    slug = channel_db_name(doomed)
    rid = str(uuid.uuid4())
    conn = database.get_db_connection(slug)
    conn.execute("INSERT INTO runs (id, prompt, status) VALUES (?, 'x', 'running')", (rid,))
    conn.commit()
    conn.close()
    reached, release = threading.Event(), threading.Event()
    active_managers[rid] = _ExitingRun(slug, rid, reached, release)

    stopped: dict = {}
    thread = threading.Thread(target=lambda: stopped.setdefault("r", client.delete(
        f"/api/channels/{doomed.id}/runs/{rid}", params={"force": "true"})))
    thread.start()
    try:
        assert reached.wait(10)
        assert rid not in active_managers
        r = client.put(f"/api/channels/{doomed.id}/rename", params={"name": "Renamed"})
        assert r.status_code == 200, r.text
        assert client.delete(f"/api/channels/{doomed.id}").status_code == 409
    finally:
        release.set()
        thread.join(10)
        active_managers.pop(rid, None)
    assert stopped["r"].status_code == 200, stopped["r"].text
    conn = database.get_db_connection(slug)
    try:
        assert conn.execute("SELECT status FROM runs WHERE id=?", (rid,)).fetchone()[0] == "stopped"
        assert conn.execute(
            "SELECT payload FROM run_events WHERE run_id=? AND event_type='stop'", (rid,),
        ).fetchall()[0][0] == '{"stop_type": "force_stopped"}'
    finally:
        conn.close()

    r = client.put(f"/api/channels/{doomed.id}/rename", params={"name": "Renamed"})
    assert r.status_code == 200, r.text
    assert os.path.isfile(_db_path(slug))


def test_a_stop_on_an_unknown_channel_creates_no_db__F0318(client):
    """A Stop from a stale tab, after its channel was deleted,
    answers 404 and never creates a stray db under the old name."""
    r = client.delete("/api/channels/gone_channel/runs/abc")
    assert r.status_code == 404, r.text
    assert not os.path.exists(_db_path("gone_channel"))
