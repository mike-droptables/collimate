"""Run start checkpoints, stop/reattach lifecycle and accepted-file inspection.

Synthetic managers drive stop and status routes. Synthetic runners exercise
start and frozen workspace preparation without relying on shipped generators.
"""
from __future__ import annotations

from fake_manager import FakeManager
import hashlib
import uuid
from pathlib import Path

import pytest

from conftest import install_runner
from database import get_db_connection


CHANNEL = str(uuid.uuid5(uuid.NAMESPACE_URL, "tests:runs-lifecycle:channel"))


@pytest.fixture
def client(api_app):
    from fastapi.testclient import TestClient
    from models import Channel
    from state import state
    with TestClient(api_app) as http:
        channel = Channel(id=uuid.UUID(CHANNEL), name="Starting")
        from genesis import ensure_capabilities_stack
        ensure_capabilities_stack(channel)
        state.channels = [channel]
        state.active_channel_id = channel.id
        get_db_connection(CHANNEL).close()
        http.channel_id = CHANNEL
        yield http


def _operation():
    return dict(operation_id=str(uuid.uuid4()), writer_id='test-run', edit_seq=1, base_revisions={})



class _StubManager(FakeManager):
    """Minimal stand-in for a ProcessManager, registered into
    active_managers so /runs/active can find it and /stop_run can
    inspect it without real subprocess plumbing.

    This mirrors the pattern used by routes/cells.py's _InlineChatRun
    (see cells.py line ~546): anything that gets stored in
    active_managers only needs to walk like a manager, not actually be
    one. We add the attributes each codepath reaches for."""

    def __init__(self, *, channel, run_id, cell_id,
                 generator_name="stub_gen", process=None):
        super().__init__()
        self.channel = channel
        self.run_id = run_id
        self.cell_id = cell_id
        self.runner_id = "stub"
        self.runner_name = "Stub"
        self.label = f"stub-{run_id[:8]}"
        self.folder = f"_runs/{self.label}"
        self.generator_name = generator_name
        self.process = process
        # /runs/active reads these on the stub
        self._pending_user_messages = []
        self._pending_renderer_actions = []


class _FakeDeadProcess:
    """A process object that's already exited — pushes stop_run into
    the ``already_finished`` branch so we can test the DB status write
    without signaling anything."""
    pid = -1
    returncode = 0

    async def wait(self):
        return self.returncode


class _FakeLiveProcess:
    """A process object that looks alive to stop_run. We give it a
    nonexistent PID so SIGTERM/SIGKILL attempts no-op via the
    ProcessLookupError branch in _kill_pg, but the rest of the stop
    path still runs (DB update + run_event insert)."""
    pid = 2**30  # extremely unlikely to match any real process
    returncode = None

    async def wait(self):
        # Called by the escalation task. Return immediately.
        return 0


def _cas_write(content: str) -> str:
    raw = content.encode("utf-8")
    h = hashlib.sha256(raw).hexdigest()
    d = Path(".collimate") / "objects" / h[:2]
    d.mkdir(parents=True, exist_ok=True)
    (d / h[2:]).write_bytes(raw)
    return h


def _seed_cell(files: dict[str, str] | None = None) -> str:
    """Insert a single cells row with CAS-backed files and return its id."""
    conn = get_db_connection(CHANNEL)
    try:
        run_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO runs (id, prompt, status) VALUES (?, 'seed', 'completed')",
            (run_id,),
        )
        cid = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO cells "
            "(id, run_id, generator_name, sequence_num, "
            " version_name, source_type) "
            "VALUES (?, ?, 'seed', 1, 'seed', 'generator')",
            (cid, run_id),
        )
        accepted = []
        for path, content in (files or {"main.py": "x\n"}).items():
            blob = _cas_write(content)
            accepted.append({"path": path, "body_hash": blob,
                             "encoding": "utf-8", "language": "text"})
            conn.execute(
                "INSERT OR IGNORE INTO blobs (hash, size) VALUES (?, ?)",
                (blob, len(content.encode("utf-8"))),
            )
            conn.execute(
                "INSERT INTO cell_files (cell_id, filepath, blob_hash, encoding, language) "
                "VALUES (?, ?, ?, 'utf-8', 'text')",
                (cid, path, blob),
            )
        from persistence import initialize_files
        initialize_files(conn, cid, accepted)
        from versions import initialize_file_snapshot
        initialize_file_snapshot(conn, cid)
        conn.commit()
        return cid
    finally:
        conn.close()


def _place_on_canvas(cid: str) -> None:
    """Put a seeded cell on the canvas so a single-shaped run can take it as
    its input — create_run's existence check reads state, not just the DB. The
    accepted SQL rows carry its current files."""
    from state import state, persist_state
    from models import Cell, Stack

    ch = next(c for c in state.channels if c.id == state.active_channel_id)
    ch.main_stacks.append(Stack(cells=[Cell(id=uuid.UUID(cid))]))
    from persistence import publish_files
    publish_files(ch, cid, {}, persist=False)
    persist_state()


def _mutate(client, cell_id, path, text=None, *, delete=False):
    from persistence import current_file
    conn = get_db_connection(CHANNEL)
    try:
        current = current_file(conn, cell_id, path)
    finally:
        conn.close()
    revision = current['revision'] if current else 0
    mutation = dict(path=path, writer_id='user-lifecycle', edit_seq=revision + 1,
                    base_revision=revision, kind='delete' if delete else 'put')
    if not delete:
        mutation['content_text'] = text
    response = client.post(f'/api/channels/{CHANNEL}/cells/{cell_id}/files/mutate',
                           json=dict(operation_id=str(uuid.uuid4()), mutations=[mutation]))
    assert response.status_code == 200, response.text
    assert response.json()['outcome'] == 'accepted'
    return response


def _seed_run(status: str = "running") -> str:
    """Insert a runs row and return its id."""
    conn = get_db_connection(CHANNEL)
    try:
        rid = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO runs (id, prompt, status) VALUES (?, 'seed', ?)",
            (rid, status),
        )
        conn.commit()
        return rid
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# create_run: error paths
# ---------------------------------------------------------------------------

def test_create_run_unknown_runner_returns_404(client):
    r = client.post(
        f"/api/channels/{CHANNEL}/runs",
        json={"runner": "__does_not_exist__", "cell_id": str(uuid.uuid4())},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# create_run: freeze accepted current files
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('delete,added', [(False, True), (True, True), (True, False)])
def test_create_run_checkpoints_accepted_files_and_tombstones(client, delete, added):
    from versions import tip_file_map
    from persistence import snapshot_files
    runner, _gen = install_runner(generator='lifecycle-input', runner='lifecycle-input')
    parent = _seed_cell({'keep.txt': 'k', 'doomed.txt': 'd'})
    _place_on_canvas(parent)
    if delete:
        _mutate(client, parent, 'doomed.txt', delete=True)
    if added:
        _mutate(client, parent, 'notes.md', 'accepted content')
    conn = get_db_connection(CHANNEL)
    before = snapshot_files(conn, parent, include_deleted=True)
    conn.close()
    response = client.post(f'/api/channels/{CHANNEL}/runs', json={'runner': runner, 'cell_id': parent})
    assert response.status_code == 200, response.text
    from run_spawn import wait_spawned
    wait_spawned(response.json()['run_id'])
    label = response.json()['label']
    conn = get_db_connection(CHANNEL)
    try:
        edge = conn.execute("SELECT child_cell_id FROM cell_edges WHERE parent_cell_id=? AND parent_key='previous_version'", (parent,)).fetchone()
        assert edge is not None
        # The run's input version: the accepted files and its run card,
        # committed in one version before the freeze.
        files = tip_file_map(conn, edge['child_cell_id'])
        assert set(files) == ({'keep.txt', f'_runs/{label}/{label}.run'}
                              | ({'doomed.txt'} if not delete else set()) | ({'notes.md'} if added else set()))
        assert [f for f in snapshot_files(conn, parent, include_deleted=True) if not f['path'].startswith('_runs/')] == before
    finally:
        conn.close()


def test_first_emit_after_checkpoint_lands_on_a_fresh_sequence(client, monkeypatch, tmp_path):
    """The JIT flush can mint a Manual Edit cell under the run_id before the
    generator's first ``update_cell`` (seq=1 for a fresh run). The emit's
    genesis stub and its version row must both take fresh sequence numbers
    rather than colliding with it on the ``(run_id, sequence_num)`` UNIQUE
    constraint — the user-visible symptom of a collision is a merge run
    that never finishes.
    """
    from process_manager import ProcessManager
    from runner_permissions import UNRESTRICTED
    from state import state as app_state
    from models import Stack, Cell

    run_id = str(uuid.uuid4())
    placeholder_id = str(uuid.uuid4())

    # Seed a run + a Manual-Edit-style cell (the JIT flush analogue) so
    # the (run_id, seq=1) slot is already taken before the first emit.
    conn = get_db_connection(CHANNEL)
    try:
        conn.execute(
            "INSERT INTO runs (id, prompt, status) VALUES (?, '', 'running')",
            (run_id,),
        )
        manual_edit_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO cells (id, run_id, generator_name, sequence_num, "
            "version_name, source_type) "
            "VALUES (?, ?, 'manual_edit', 1, 'Manual Edit', 'manual_edit')",
            (manual_edit_id, run_id),
        )
        conn.commit()
    finally:
        conn.close()

    # The in-memory apply looks the cell up in state.channels — wire one up
    # just for this test.
    ch = next(c for c in app_state.channels if str(c.id) == CHANNEL)
    placeholder_cell = Cell(id=uuid.UUID(placeholder_id), name="…")
    ch.main_stacks.append(Stack(cells=[placeholder_cell]))
    prev_active = app_state.active_channel_id
    app_state.active_channel_id = ch.id
    try:
        manager = ProcessManager(
            channel=CHANNEL,
            run_id=run_id,
            app_dir=str(tmp_path / "app"),
            workspace_dir=str(tmp_path / "ws"),
            entrypoint="x.py",
            generator_name="test_gen",
            cell_id=placeholder_id,
            permissions=UNRESTRICTED,
        )

        manager._update_cell({
            "artifacts": [{"type": "memory", "cell_path": "result.txt", "content": "output"}],
            "name": "Output", **_operation(), "base_revisions": {"result.txt": 0},
        })
    finally:
        app_state.active_channel_id = prev_active

    conn = get_db_connection(CHANNEL)
    try:
        rows = conn.execute(
            "SELECT id, sequence_num, source_type FROM cells WHERE run_id=? "
            "ORDER BY sequence_num",
            (run_id,),
        ).fetchall()
        edges = conn.execute(
            "SELECT parent_cell_id FROM cell_edges "
            "WHERE parent_key='previous_version' AND parent_cell_id=?",
            (placeholder_id,),
        ).fetchall()
    finally:
        conn.close()
    by_id = {r["id"]: r for r in rows}
    assert by_id[placeholder_id]["source_type"] == "genesis"
    assert by_id[manual_edit_id]["sequence_num"] == 1
    seqs = [r["sequence_num"] for r in rows]
    assert len(seqs) == len(set(seqs)) == 3, (
        f"expected genesis stub + Manual Edit + version row on distinct seqs, got {seqs}"
    )
    assert len(edges) == 1, "the emit must chain a version row onto the placeholder"


def test_a_runs_emits_never_touch_the_cell_pin(client, tmp_path):
    """The cell's pin is the user's alone: a run shows a file with
    reveal_file, and an emit neither sets nor clears the pin (a ``pinned``
    in the metadata is not read)."""
    import asyncio
    from models import Cell, Stack
    from process_manager import ProcessManager
    from runner_permissions import UNRESTRICTED
    from state import channel_db_name, state as app_state

    ch = next(c for c in app_state.channels if channel_db_name(c) == CHANNEL)
    cell = Cell(name="Web", pinned="notes.md")
    ch.main_stacks.append(Stack(cells=[cell]))
    run_id = _seed_run()
    manager = ProcessManager(
        channel=CHANNEL, run_id=run_id, app_dir=str(tmp_path / "app"),
        workspace_dir=str(tmp_path / "ws"), entrypoint="x.py",
        generator_name="test_gen", cell_id=str(cell.id), permissions=UNRESTRICTED,
    )
    manager._ipc_writer = None
    asyncio.run(manager._op_update_cell({"artifacts": [], "metadata": {"pinned": ""}, **_operation()}))
    manager._emit_partial(cell_name="Web", **_operation())
    assert cell.pinned == "notes.md"


# ---------------------------------------------------------------------------
# stop_run
# ---------------------------------------------------------------------------

def test_stop_run_on_unknown_run_marks_stopped(client):
    """stop_run on an id that's never been registered hits the
    already_finished branch, updates the runs table, and returns
    stopped."""
    rid = _seed_run(status="running")
    r = client.delete(f"/api/channels/{CHANNEL}/runs/{rid}")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "stopped"
    assert body["stop_type"] == "already_finished"

    conn = get_db_connection(CHANNEL)
    try:
        row = conn.execute(
            "SELECT status FROM runs WHERE id=?", (rid,),
        ).fetchone()
    finally:
        conn.close()
    assert row and row["status"] == "stopped"


def test_stop_run_on_live_stub_writes_stop_event__F0376(client):
    """With a stub manager backed by a fake live process, stop_run
    goes down the signal path. The actual kill no-ops (bogus PID) but
    the DB state still transitions and a stop event lands in
    run_events."""
    from run_registry import active_managers

    rid = _seed_run(status="running")
    mgr = _StubManager(
        channel=CHANNEL,
        run_id=rid,
        cell_id=str(uuid.uuid4()),
        process=_FakeLiveProcess(),
    )
    active_managers[rid] = mgr

    try:
        r = client.delete(f"/api/channels/{CHANNEL}/runs/{rid}")
    finally:
        active_managers.pop(rid, None)

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "stopped"
    assert body["stop_type"] == "gracefully_stopped"

    conn = get_db_connection(CHANNEL)
    try:
        row = conn.execute(
            "SELECT status FROM runs WHERE id=?", (rid,),
        ).fetchone()
        events = conn.execute(
            "SELECT event_type, payload FROM run_events WHERE run_id=?",
            (rid,),
        ).fetchall()
    finally:
        conn.close()
    assert row and row["status"] == "stopped"
    types = [e["event_type"] for e in events]
    assert "stop" in types, f"expected a stop event in {types}"


def test_stop_run_on_dead_stub_short_circuits__F0376(client):
    """A stub manager whose process has already exited
    (returncode != None) takes the already_finished branch: no stop event
    is written."""
    from run_registry import active_managers

    rid = _seed_run(status="running")
    mgr = _StubManager(
        channel=CHANNEL,
        run_id=rid,
        cell_id=str(uuid.uuid4()),
        process=_FakeDeadProcess(),
    )
    active_managers[rid] = mgr

    try:
        r = client.delete(f"/api/channels/{CHANNEL}/runs/{rid}")
    finally:
        active_managers.pop(rid, None)

    assert r.status_code == 200
    assert r.json()["stop_type"] == "already_finished"

    conn = get_db_connection(CHANNEL)
    try:
        events = conn.execute(
            "SELECT event_type FROM run_events WHERE run_id=?",
            (rid,),
        ).fetchall()
    finally:
        conn.close()
    # The already-finished path skips run_events insertion.
    assert all(e["event_type"] != "stop" for e in events)


def test_stop_run_on_ended_run_keeps_its_terminal_status(client):
    """A Stop that lands after the run already failed must not rewrite
    its history to 'stopped'."""
    rid = _seed_run(status="failed")
    r = client.delete(f"/api/channels/{CHANNEL}/runs/{rid}")
    assert r.status_code == 200
    assert r.json()["stop_type"] == "already_finished"

    conn = get_db_connection(CHANNEL)
    try:
        row = conn.execute("SELECT status FROM runs WHERE id=?", (rid,)).fetchone()
    finally:
        conn.close()
    assert row["status"] == "failed"


@pytest.mark.parametrize("force", [False, True])
def test_stop_run_never_removes_the_run_from_the_registry__F0321(client, force):
    """Only the reaper removes a run, once its run-end cleanup (finalize,
    the run-folder sweep) is done: until then the stopped run still guards
    its cell and run folder. Neither the force branch nor the graceful
    branch's SIGKILL escalation drops it."""
    import time
    from run_registry import active_managers
    from run_registry import lock_owner, running_cell_ids

    rid = _seed_run(status="running")
    cell_id = str(uuid.uuid4())
    mgr = _StubManager(channel=CHANNEL, run_id=rid, cell_id=cell_id,
                       process=_FakeLiveProcess())
    active_managers[rid] = mgr
    try:
        r = client.delete(f"/api/channels/{CHANNEL}/runs/{rid}"
                          + ("?force=true" if force else ""))
        time.sleep(0.2)  # the graceful branch's escalation task settles
        assert r.status_code == 200
        assert active_managers.get(rid) is mgr
        assert cell_id in running_cell_ids()
        assert lock_owner(cell_id, f"{mgr.folder}/out.txt") is mgr
    finally:
        active_managers.pop(rid, None)


@pytest.mark.parametrize("final_status", [None, "completed"])
def test_stop_run_on_an_exited_run_leaves_its_status_to_the_cleanup__F0321(
        client, monkeypatch, final_status):
    """A Stop on a run whose process already exited, still in its run-end
    cleanup, neither drops it from the registry nor rewrites its status:
    the cleanup's finalize records how the run ended. Once the status is
    final, the Stop re-broadcasts it (a missed run_status would leave the
    frontend wedged on "Force Stop")."""
    import events
    from run_registry import active_managers

    sent: list = []
    monkeypatch.setattr(events, "broadcast", lambda ch, ev: sent.append(ev))
    rid = _seed_run(status="running" if final_status is None else final_status)
    mgr = _StubManager(channel=CHANNEL, run_id=rid, cell_id=str(uuid.uuid4()),
                       process=_FakeDeadProcess())
    mgr.final_status = final_status
    active_managers[rid] = mgr
    try:
        r = client.delete(f"/api/channels/{CHANNEL}/runs/{rid}")
        assert active_managers.get(rid) is mgr
    finally:
        active_managers.pop(rid, None)
    assert r.json() == {"status": "stopped", "stop_type": "already_finished"}
    conn = get_db_connection(CHANNEL)
    try:
        row = conn.execute("SELECT status FROM runs WHERE id=?", (rid,)).fetchone()
    finally:
        conn.close()
    assert row["status"] == ("running" if final_status is None else final_status)
    runs_status = [e for e in sent if e.get("type") == "run_status"]
    if final_status is None:
        assert runs_status == []
    else:
        assert runs_status == [{"type": "run_status", "run_id": rid, "status": "completed",
                                "failure_reason": None, "ended_at": None, "note": None,
                                "outcome": None}]


@pytest.mark.parametrize("force", [False, True])
def test_stop_run_on_an_exited_run_signals_its_leftover_group__F0321(client, force):
    """A run whose process exited while a child it started is still alive in
    its process group (holding the run's stdout, so the run end is still in
    its bounded drain): Stop and Force Stop signal the group, the child
    dies, and the run stays registered for its reaper."""
    import os
    import signal
    import subprocess
    import time
    from run_registry import active_managers

    sh = subprocess.Popen(
        ["sh", "-c", "sleep 60 & echo $!; exit 0"],
        stdout=subprocess.PIPE, start_new_session=True,
    )
    child_pid = int(sh.stdout.readline().decode().strip())
    sh.wait()

    class _Exited:
        pid = sh.pid
        returncode = sh.returncode

    rid = _seed_run(status="running")
    mgr = _StubManager(channel=CHANNEL, run_id=rid, cell_id=str(uuid.uuid4()),
                       process=_Exited())
    active_managers[rid] = mgr
    try:
        r = client.delete(f"/api/channels/{CHANNEL}/runs/{rid}"
                          + ("?force=true" if force else ""))
        assert r.status_code == 200, r.text
        assert r.json() == {
            "status": "stopped",
            "stop_type": "force_stopped" if force else "gracefully_stopped",
        }
        assert active_managers.get(rid) is mgr
        for _ in range(100):
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            pytest.fail("the leftover child in the run's group survived the stop")
    finally:
        active_managers.pop(rid, None)
        try:
            os.kill(child_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        sh.stdout.close()


def test_stop_run_keeps_the_status_of_a_run_that_finalized_meanwhile__F0390(client):
    """The signal path's status write only overrides a row still 'running':
    a run that finished on its own before the write keeps its status, and
    no stop event is recorded against it."""
    from run_registry import active_managers

    rid = _seed_run(status="completed")
    mgr = _StubManager(channel=CHANNEL, run_id=rid, cell_id=str(uuid.uuid4()),
                       process=_FakeLiveProcess())
    active_managers[rid] = mgr
    try:
        r = client.delete(f"/api/channels/{CHANNEL}/runs/{rid}")
    finally:
        active_managers.pop(rid, None)
    assert r.json()["stop_type"] == "gracefully_stopped"
    conn = get_db_connection(CHANNEL)
    try:
        row = conn.execute("SELECT status FROM runs WHERE id=?", (rid,)).fetchone()
        stops = conn.execute(
            "SELECT 1 FROM run_events WHERE run_id=? AND event_type='stop'", (rid,),
        ).fetchall()
    finally:
        conn.close()
    assert row["status"] == "completed"
    assert stops == []


def _finalize_exit(channel: str, run_id: str, returncode: int) -> str | None:
    """The run's own finalize (FinalizationMixin._finalize_run_status, the
    real status write) for a process that exited with ``returncode``;
    returns the status it read back."""
    from types import SimpleNamespace
    from process_manager.finalization import FinalizationMixin

    class _Exited(FinalizationMixin):
        timeout_reason = None
        final_status = None
        log_buffer: list = []

        def _broadcast_run_status(self, *_a):
            pass

        def _apply_phase(self, *_a):
            pass

        def _flush_log_buffer(self):
            pass

    run = _Exited()
    run.channel, run.run_id = channel, run_id
    run.process = SimpleNamespace(returncode=returncode)
    run._finalize_run_status()
    return run.final_status


def test_stop_run_records_stopped_before_a_prompt_exit_is_finalized__F0390(
        client, monkeypatch):
    """A run that exits the instant it is signalled is finalized from its
    exit moments later, by its real finalize, which writes 'failed' for a
    killed run. The Stop records 'stopped' before it signals, so the
    finalize finds nothing to do and the run reads 'stopped' with its stop
    event."""
    import os
    from run_registry import active_managers

    rid = _seed_run(status="running")
    proc = _FakeLiveProcess()
    real_getpgid = os.getpgid
    finalized: list = []

    def exit_and_finalize(pid):
        if pid != proc.pid:
            return real_getpgid(pid)
        finalized.append(_finalize_exit(CHANNEL, rid, -9))
        raise ProcessLookupError(pid)
    monkeypatch.setattr(os, "getpgid", exit_and_finalize)

    active_managers[rid] = _StubManager(channel=CHANNEL, run_id=rid,
                                        cell_id=str(uuid.uuid4()), process=proc)
    try:
        r = client.delete(f"/api/channels/{CHANNEL}/runs/{rid}?force=true")
    finally:
        active_managers.pop(rid, None)
    assert r.json()["stop_type"] == "force_stopped"
    conn = get_db_connection(CHANNEL)
    try:
        row = conn.execute("SELECT status FROM runs WHERE id=?", (rid,)).fetchone()
        stops = conn.execute(
            "SELECT payload FROM run_events WHERE run_id=? AND event_type='stop'", (rid,),
        ).fetchall()
    finally:
        conn.close()
    assert finalized == ["stopped"]
    assert row["status"] == "stopped"
    assert [s["payload"] for s in stops] == ['{"stop_type": "force_stopped"}']


def _record_signals(monkeypatch, pid: int) -> list:
    """Record every signal a stop sends to ``pid`` (the process group or the
    bare pid) instead of sending it."""
    import os
    sent: list = []
    real_getpgid, real_killpg, real_kill = os.getpgid, os.killpg, os.kill
    monkeypatch.setattr(os, "getpgid", lambda p: p if p == pid else real_getpgid(p))
    monkeypatch.setattr(
        os, "killpg", lambda g, sig: sent.append(sig) if g == pid else real_killpg(g, sig))
    monkeypatch.setattr(
        os, "kill", lambda p, sig: sent.append(sig) if p == pid else real_kill(p, sig))
    return sent


def test_stop_run_signals_a_run_whose_status_write_failed__F0390(client, monkeypatch):
    """The status write before the signal is best-effort: a channel db that
    refuses it never keeps the run from being stopped."""
    import signal
    import routes.runs.control as control
    from run_registry import active_managers

    def locked(*_a, **_k):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(control, "mark_run_stopped", locked)
    rid = _seed_run(status="running")
    proc = _FakeLiveProcess()
    sent = _record_signals(monkeypatch, proc.pid)
    active_managers[rid] = _StubManager(channel=CHANNEL, run_id=rid,
                                        cell_id=str(uuid.uuid4()), process=proc)
    try:
        r = client.delete(f"/api/channels/{CHANNEL}/runs/{rid}")
    finally:
        active_managers.pop(rid, None)
    assert r.status_code == 200, r.text
    assert r.json()["stop_type"] == "gracefully_stopped"
    assert sent == [signal.SIGTERM]


@pytest.mark.parametrize("force", [False, True])
def test_stop_run_never_signals_a_process_reaped_during_its_status_write__F0390(
        client, monkeypatch, force):
    """A run that exits (and is reaped) while the Stop writes its status is
    not signalled: its pid may already belong to another process."""
    import routes.runs.control as control
    from run_registry import active_managers

    rid = _seed_run(status="running")
    proc = _FakeLiveProcess()
    real_mark = control.mark_run_stopped

    def reaped_meanwhile(*args, **kwargs):
        proc.returncode = 0
        return real_mark(*args, **kwargs)
    monkeypatch.setattr(control, "mark_run_stopped", reaped_meanwhile)
    sent = _record_signals(monkeypatch, proc.pid)
    active_managers[rid] = _StubManager(channel=CHANNEL, run_id=rid,
                                        cell_id=str(uuid.uuid4()), process=proc)
    try:
        r = client.delete(f"/api/channels/{CHANNEL}/runs/{rid}"
                          + ("?force=true" if force else ""))
    finally:
        active_managers.pop(rid, None)
    assert r.status_code == 200, r.text
    assert sent == []


def test_a_force_stop_after_a_graceful_stop_is_recorded_as_forced__F0390(client):
    """The second Stop finds the row already 'stopped' by the first; it is
    still the user's stop of a live run, so its event is recorded and the
    run's end reads "Stopped (forced)"."""
    from run_registry import active_managers

    rid = _seed_run(status="running")
    active_managers[rid] = _StubManager(channel=CHANNEL, run_id=rid,
                                        cell_id=str(uuid.uuid4()),
                                        process=_FakeLiveProcess())
    try:
        assert client.delete(f"/api/channels/{CHANNEL}/runs/{rid}").status_code == 200
        assert client.delete(
            f"/api/channels/{CHANNEL}/runs/{rid}?force=true").status_code == 200
    finally:
        active_managers.pop(rid, None)
    conn = get_db_connection(CHANNEL)
    try:
        stops = [r["payload"] for r in conn.execute(
            "SELECT payload FROM run_events WHERE run_id=? AND event_type='stop' ORDER BY id",
            (rid,))]
    finally:
        conn.close()
    assert stops == ['{"stop_type": "gracefully_stopped"}', '{"stop_type": "force_stopped"}']


async def test_the_reaper_removes_a_run_whose_cleanup_crashed__F0321(monkeypatch):
    """The reaper is the only thing that removes a run from the registry, so
    a run-end cleanup that raised (a finalize that hit 'database is locked')
    must not leave the run registered: its cell would stay refused for
    removal, merge and checkpoint, and its channel for rename and delete,
    until the server restarted."""
    import asyncio
    import routes.runs.helpers as helpers
    from run_registry import active_managers
    from run_registry import running_cell_ids

    monkeypatch.setattr(helpers, "_reap_run_containers", lambda rid: None)

    async def crashed_cleanup():
        raise RuntimeError("database is locked")

    rid = str(uuid.uuid4())
    cell_id = str(uuid.uuid4())
    mgr = _StubManager(channel=CHANNEL, run_id=rid, cell_id=cell_id,
                       process=_FakeDeadProcess())
    mgr._cleanup_task = asyncio.ensure_future(crashed_cleanup())
    active_managers[rid] = mgr
    try:
        await helpers._reap_process(rid, mgr)
        assert rid not in active_managers
        assert cell_id not in running_cell_ids()
    finally:
        active_managers.pop(rid, None)


def test_stop_run_on_unspawned_manager_aborts_spawn__F0376(client):
    """A manager registered but not spawned yet (start_run still inside
    manager.start()) is told to abort its spawn. Its registry entry stays
    for start_run's unwind to pop, and the row is marked stopped."""
    from run_registry import active_managers

    rid = _seed_run(status="running")
    mgr = _StubManager(
        channel=CHANNEL,
        run_id=rid,
        cell_id=str(uuid.uuid4()),
        process=None,
    )
    aborted: list[bool] = []
    mgr.abort_spawn = lambda: aborted.append(True)
    active_managers[rid] = mgr

    try:
        r = client.delete(f"/api/channels/{CHANNEL}/runs/{rid}")
        still_registered = active_managers.get(rid) is mgr
    finally:
        active_managers.pop(rid, None)

    assert r.status_code == 200
    assert r.json() == {"status": "stopped", "stop_type": "aborted_spawn"}
    assert aborted == [True]
    assert still_registered

    conn = get_db_connection(CHANNEL)
    try:
        row = conn.execute("SELECT status FROM runs WHERE id=?", (rid,)).fetchone()
    finally:
        conn.close()
    assert row["status"] == "stopped"


def test_force_stop_unregisters_terminal_sessions(client, monkeypatch):
    """A force stop tears down the run's terminal sessions eagerly, not
    only after the run-end cleanup's drain."""
    from sessions import terminal as terminal_sessions
    from run_registry import active_managers

    calls: list[str] = []
    monkeypatch.setattr(terminal_sessions, "unregister_sessions_for_run", calls.append)

    rid = _seed_run(status="running")
    mgr = _StubManager(
        channel=CHANNEL,
        run_id=rid,
        cell_id=str(uuid.uuid4()),
        process=_FakeLiveProcess(),
    )
    active_managers[rid] = mgr

    try:
        r = client.delete(f"/api/channels/{CHANNEL}/runs/{rid}?force=true")
    finally:
        active_managers.pop(rid, None)

    assert r.status_code == 200
    assert r.json()["stop_type"] == "force_stopped"
    assert calls == [rid]


# ---------------------------------------------------------------------------
# list_runs + /runs/{id}
# ---------------------------------------------------------------------------

def test_list_runs_returns_newest_first(client):
    """list_runs orders by created_at DESC. We pin explicit timestamps
    on three runs so the ordering isn't ambiguous under SQLite's
    1-second CURRENT_TIMESTAMP resolution."""
    conn = get_db_connection(CHANNEL)
    try:
        r_old = str(uuid.uuid4())
        r_mid = str(uuid.uuid4())
        r_new = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO runs (id, prompt, status, created_at) "
            "VALUES (?, 'old', 'completed', '2020-01-01 00:00:00')", (r_old,),
        )
        conn.execute(
            "INSERT INTO runs (id, prompt, status, created_at) "
            "VALUES (?, 'mid', 'completed', '2022-01-01 00:00:00')", (r_mid,),
        )
        conn.execute(
            "INSERT INTO runs (id, prompt, status, created_at) "
            "VALUES (?, 'new', 'completed', '2024-01-01 00:00:00')", (r_new,),
        )
        conn.commit()
    finally:
        conn.close()

    r = client.get(f"/api/channels/{CHANNEL}/runs")
    assert r.status_code == 200
    ids = [row["id"] for row in r.json()]
    for rid in (r_old, r_mid, r_new):
        assert rid in ids
    # DESC by created_at: newest comes before oldest.
    assert ids.index(r_new) < ids.index(r_mid) < ids.index(r_old)


def test_get_run_returns_404_for_unknown(client):
    r = client.get(f"/api/channels/{CHANNEL}/runs/{uuid.uuid4()}")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# list_active_runs: filters by channel
# ---------------------------------------------------------------------------

def test_list_active_runs_filters_by_channel__F0376(client):
    """/runs/active returns only managers whose .channel matches the
    path channel. Managers for other channels are filtered out."""
    from run_registry import active_managers

    rid_here = _seed_run()
    rid_other = _seed_run()
    here = _StubManager(
        channel=CHANNEL,
        run_id=rid_here,
        cell_id=str(uuid.uuid4()),
    )
    elsewhere = _StubManager(
        channel=str(uuid.uuid4()),
        run_id=rid_other,
        cell_id=str(uuid.uuid4()),
    )
    active_managers[rid_here] = here
    active_managers[rid_other] = elsewhere

    try:
        r = client.get(f"/api/channels/{CHANNEL}/runs/active")
    finally:
        active_managers.pop(rid_here, None)
        active_managers.pop(rid_other, None)

    assert r.status_code == 200
    ids = {row["run_id"] for row in r.json()}
    assert rid_here in ids
    assert rid_other not in ids


def test_list_active_runs_flags_a_run_whose_end_is_under_way__F0321(client):
    """A stopped or finished run stays listed until its run-end cleanup is
    done, flagged ``stopping`` so a reload does not attach it as live."""
    from run_registry import active_managers

    rows = {}
    for key in ("live", "stop_requested", "finalized"):
        rid = _seed_run()
        mgr = _StubManager(channel=CHANNEL, run_id=rid, cell_id=str(uuid.uuid4()))
        if key == "stop_requested":
            mgr.stop_requested = True
        if key == "finalized":
            mgr.final_status = "completed"
        active_managers[rid] = mgr
        rows[rid] = key
    try:
        r = client.get(f"/api/channels/{CHANNEL}/runs/active")
    finally:
        for rid in rows:
            active_managers.pop(rid, None)
    flags = {rows[row["run_id"]]: row["stopping"] for row in r.json() if row["run_id"] in rows}
    assert flags == {"live": False, "stop_requested": True, "finalized": True}


def test_list_active_runs_reports_a_final_status_as_the_phase__F0321(client):
    """A run's finalize sets its final status before it applies the matching
    terminal phase. /runs/active read in between reports that status as the
    phase, so a reload never attaches the run as stopping with a live phase
    after its last run_status has gone out."""
    from run_registry import active_managers

    rows = {}
    for key, final in (("live", None), ("finalized", "stopped")):
        rid = _seed_run()
        mgr = _StubManager(channel=CHANNEL, run_id=rid, cell_id=str(uuid.uuid4()))
        mgr.current_phase = "running"
        mgr.stop_requested = final is not None
        mgr.final_status = final
        active_managers[rid] = mgr
        rows[rid] = key
    try:
        r = client.get(f"/api/channels/{CHANNEL}/runs/active")
    finally:
        for rid in rows:
            active_managers.pop(rid, None)
    phases = {rows[row["run_id"]]: row["phase"] for row in r.json() if row["run_id"] in rows}
    assert phases == {"live": "running", "finalized": "stopped"}


# ---------------------------------------------------------------------------
# events_since
# ---------------------------------------------------------------------------

def test_events_since_returns_strictly_after_since_id(client):
    """/runs/{id}/events_since returns rows ordered by id, and only
    those with id > since_id."""
    rid = _seed_run()
    conn = get_db_connection(CHANNEL)
    try:
        for payload in ("a", "b", "c"):
            conn.execute(
                "INSERT INTO run_events (run_id, event_type, payload) "
                "VALUES (?, 'stdout', ?)",
                (rid, payload),
            )
        conn.commit()
        # Grab the id of the middle event so we can filter around it.
        mid = conn.execute(
            "SELECT id FROM run_events WHERE run_id=? AND payload='b'",
            (rid,),
        ).fetchone()["id"]
    finally:
        conn.close()

    r = client.get(
        f"/api/channels/{CHANNEL}/runs/{rid}/events_since",
        params={"since_id": mid},
    )
    assert r.status_code == 200
    body = r.json()
    # Only 'c' should remain.
    payloads = [e["payload"] for e in body["events"]]
    assert payloads == ["c"]

    # Also sanity-check since_id=0 returns everything ordered by id ASC.
    r0 = client.get(
        f"/api/channels/{CHANNEL}/runs/{rid}/events_since",
        params={"since_id": 0},
    )
    assert [e["payload"] for e in r0.json()["events"]] == ["a", "b", "c"]


def test_events_since_for_unknown_run_returns_empty(client):
    r = client.get(
        f"/api/channels/{CHANNEL}/runs/{uuid.uuid4()}/events_since",
        params={"since_id": 0},
    )
    assert r.status_code == 200
    assert r.json()["events"] == []


# ---------------------------------------------------------------------------
# Cell inspection endpoint (list_cell_files) — small smoke tests
# ---------------------------------------------------------------------------

def test_list_cell_files_returns_rows_with_blob_metadata(client):
    """The live inventory exposes immutable, body-free revision descriptors."""
    cid = _seed_cell({"a.py": "print(1)\n", "b.md": "hi"})
    r = client.get(f"/api/channels/{CHANNEL}/cells/{cid}/files")
    assert r.status_code == 200
    rows = r.json()['files']
    by_path = {r['path']: r for r in rows}
    assert set(by_path.keys()) == {"a.py", "b.md"}
    for file in rows:
        assert len(file['body_hash']) == 64 and file['file_revision'] == 1
        assert not file['deleted'] and 'content' not in file


# ---------------------------------------------------------------------------
# Audit fixes — regression coverage
# ---------------------------------------------------------------------------

def test_events_since_returns_is_terminal_flag(client):
    """events_since must include is_terminal so the frontend has a
    single source of truth (it used to re-derive from !is_active &&
    status != 'running', which didn't always agree with the SSE path)."""
    rid = _seed_run(status="completed")
    r = client.get(
        f"/api/channels/{CHANNEL}/runs/{rid}/events_since?since_id=0"
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "completed"
    assert body["is_terminal"] is True
    assert body["is_active"] is False


def test_events_since_is_terminal_false_while_running(client):
    rid = _seed_run(status="running")
    r = client.get(
        f"/api/channels/{CHANNEL}/runs/{rid}/events_since?since_id=0"
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "running"
    assert body["is_terminal"] is False


def test_finalize_run_status_does_not_overwrite_stopped(client, monkeypatch):
    """The conditional UPDATE in _finalize_run_status must no-op when
    the runs row already reached a non-'running' status — without this,
    a finalize racing with a /stop handler would silently overwrite
    the real stopped status with 'completed' or 'failed'."""
    from process_manager.finalization import FinalizationMixin

    class _Dummy(FinalizationMixin):
        def __init__(self, channel, run_id):
            self.channel = channel
            self.run_id = run_id
            # fake exited-cleanly process so candidate would be 'completed'
            self.process = type("P", (), {"returncode": 0})()
            self.cell_id = None
            self.settings_history = []
            self.workspace_dir = "/tmp/__does_not_exist__"
            self.timeout_reason = None

        def _broadcast_run_status(self, _s, _r=None):
            pass

        def _apply_phase(self, _p, _m=""):
            # Phase mirroring lives on StreamsMixin in the real composition;
            # this stand-in only exercises the status-overwrite guard.
            pass

        def _flush_log_buffer(self):
            pass

    rid = _seed_run(status="stopped")
    d = _Dummy(CHANNEL, rid)
    d._finalize_run_status()

    conn = get_db_connection(CHANNEL)
    try:
        row = conn.execute("SELECT status FROM runs WHERE id=?", (rid,)).fetchone()
    finally:
        conn.close()
    assert row["status"] == "stopped", (
        "finalize overwrote a stopped run with the candidate status"
    )
