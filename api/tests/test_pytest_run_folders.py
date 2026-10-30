"""Run folders: the per-run lock band ``_runs/<label>/`` of a cell.

Covers the path predicates (run_folders.py), the host's partition of a run's
emit (its own folder yes, its own run card and every other ``_runs/`` folder
no), the name rule (only the run that created a cell may name it), sealing,
the run card and the run-end / startup sweeps, accepted conditional
mutations around run folders, the ``reveal_file`` op, and the lock order (an
emit racing a run start on the same cell finishes). Synthetic cells, runs
and labels throughout.
"""
from __future__ import annotations

import asyncio
import json
import threading
import types
import uuid
from pathlib import Path

import pytest
import yaml

from database import get_db_connection
from run_folders import (
    in_folder,
    is_run_path,
    label_of,
    run_card_path,
    run_folder,
    mint_run_label,
    strip_run_paths,
)

CHANNEL = str(uuid.uuid5(uuid.NAMESPACE_URL, "tests:run-folders:channel"))
OTHER = "other-bbbbbbbb"


# ---------------------------------------------------------------------------
# Predicates
# ---------------------------------------------------------------------------

def test_labels_folders_and_cards():
    rid = "3f9a2c1d-0000-4000-8000-000000000000"
    assert mint_run_label("agent--claude-code", rid) == "agent--claude-code-3f9a2c1d"
    assert mint_run_label(None, rid) == "run-3f9a2c1d"
    assert run_folder("x-3f9a2c1d") == "_runs/x-3f9a2c1d"
    assert run_card_path("x-3f9a2c1d") == "_runs/x-3f9a2c1d/x-3f9a2c1d.run"


def test_host_run_paths_follow_shared_sdk_fixture__F1238():
    from pathlib import Path
    import collimate
    from run_folders import normalize
    fixture = json.loads((Path(collimate.__file__).resolve().parents[2] / "shared/run-path-contract.json").read_text())
    for row in fixture["paths"]:
        assert normalize(row["path"]) == row["normalized"]
        assert is_run_path(row["path"]) == row["is_run_path"]
        assert label_of(row["path"]) == row["label"]
    for row in fixture["labels"]:
        if row["valid"]:
            assert run_card_path(row["label"]) == row["card"]


@pytest.mark.parametrize("path,run,label", [
    ("_runs/a-11111111/chat/x.chat", True, "a-11111111"),
    ("_runs", True, None),
    ("/_runs/a-11111111/f", True, "a-11111111"),
    ("notes/../_runs/a-11111111/f", True, "a-11111111"),
    ("notes/_runs/a-11111111/f", False, None),
    ("_runsx/a", False, None),
    ("notes.md", False, None),
])
def test_run_path_predicates_normalize_like_hydration(path, run, label):
    assert is_run_path(path) is run
    assert label_of(path) == label


def test_in_folder():
    assert in_folder("_runs/a-11111111/panel/status.html", "a-11111111")
    assert not in_folder("_runs/a-111111112/x", "a-11111111")
    assert not in_folder("_runs/a-11111111", "a-11111111")


def test_strip_run_paths_keeps_the_shape_it_was_given():
    assert strip_run_paths({"a.md": 1, "_runs/x-11111111/f": 2}) == {"a.md": 1}
    vf = types.SimpleNamespace(name="_runs/x-11111111/f")
    keep = types.SimpleNamespace(name="b.md")
    assert strip_run_paths([vf, keep]) == [keep]
    assert strip_run_paths([{"filepath": "_runs/y/z"}, {"filepath": "c"}]) == [{"filepath": "c"}]
    assert strip_run_paths(["_runs/q", "d"]) == ["d"]


# ---------------------------------------------------------------------------
# A real ProcessManager on a real cell
# ---------------------------------------------------------------------------

@pytest.fixture
def client(api_app):
    from fastapi.testclient import TestClient
    with TestClient(api_app) as client:
        from models import Channel
        from state import state,persist_state,flush_persist_state
        from database import get_db_connection
        channel=Channel(id=uuid.UUID(CHANNEL), name='Starting')
        state.channels=[channel]
        state.active_channel_id=channel.id
        get_db_connection(str(channel.id)).close()
        persist_state()
        flush_persist_state()
        client.channel_id = CHANNEL
        yield client


@pytest.fixture
def cell(client):
    """A fresh canvas cell id."""
    return client.post(f"/api/stacks/{client.channel_id}/create").json()["main_stacks"][-1]["cells"][0]["id"]


def _seed_run_row(run_id: str, cell_id: str, runner_id: str) -> None:
    import annotations
    conn = get_db_connection(CHANNEL)
    try:
        conn.execute("INSERT INTO runs (id, prompt, status, runner_id) VALUES (?, 'x', 'running', ?)",
                     (run_id, runner_id))
        annotations.create_run_context(conn, cell_id, run_id)
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def live(tmp_path, monkeypatch):
    """Start (register) synthetic runs on cells, with the run row a version
    references and the run's annotation context. Everything registered is
    dropped afterwards."""
    from process_manager import ProcessManager
    import run_registry as registry
    from runner_permissions import UNRESTRICTED

    from conftest import add_capabilities_cell
    from models import Cell, VirtualFile
    from state import find_active_channel
    channel = find_active_channel()
    if channel is not None:
        add_capabilities_cell(channel, Cell(files=[
            VirtualFile(name='synthetic-surface/types.handler', content=yaml.safe_dump({
                'id': 'synthetic-surface', 'name': 'Synthetic', 'description': '',
                'guidance': '', 'examples': [], 'types': [{'ext': '.review_surface'}]}))]))
    runs: dict = {}
    monkeypatch.setattr(registry, "active_managers", runs)

    def start(cell_id: str, runner: str = "synth", *, created_cell: bool = False):
        rid = str(uuid.uuid4())
        _seed_run_row(rid, cell_id, runner)
        ws = tmp_path / rid
        ws.mkdir()
        mgr = ProcessManager(
            channel=CHANNEL, run_id=rid, app_dir=str(ws), workspace_dir=str(ws),
            entrypoint="x.py", generator_name="synth", cell_id=cell_id,
            runner_id=runner, runner_name=runner.title(), permissions=UNRESTRICTED,
        )
        mgr.created_cell = created_cell
        mgr._ipc_writer = None
        # What start() reads from a manifest: the UI file types these
        # synthetic runs write into their folders.
        mgr.ui_files = (".review_surface", ".html", ".json", ".webview", ".txt")
        runs[rid] = mgr
        return mgr

    def sibling(cell_id: str, label: str = OTHER):
        mgr = types.SimpleNamespace(run_id=f"run-{label}", cell_id=cell_id, label=label,
                                    channel=CHANNEL)
        runs[mgr.run_id] = mgr
        return mgr

    return types.SimpleNamespace(start=start, sibling=sibling, runs=runs)


def _memory(cell_id: str):
    from cell_placement import find_cell
    from state import get_channel_by_id
    return find_cell(get_channel_by_id(CHANNEL), cell_id)[1]


def _accepted_files(cell_id):
    from database import get_db_connection
    from persistence import snapshot_files
    from cas import read_blob
    with get_db_connection(CHANNEL) as conn:
        return {row['path']:read_blob(row['body_hash']).decode('utf-8',errors='replace') for row in snapshot_files(conn,cell_id)}


def _accept_file(cell_id,path,body):
    from database import get_db_connection
    from persistence import current_file,mutate_files
    from cas import store_bytes
    with get_db_connection(CHANNEL) as conn:row=current_file(conn,cell_id,path)
    mutation={'path':path,'writer_id':'host:surface-fixture','edit_seq':(row['revision'] if row else 0)+1,
              'base_revision':row['revision'] if row else 0,'kind':'delete' if body is None else 'put'}
    if body is not None:
        if isinstance(body,bytes):
            try:
                body.decode('utf-8')
                encoding='utf-8'
            except UnicodeDecodeError:encoding='binary'
            mutation.update(blob_hash=store_bytes(body),encoding=encoding)
        else:mutation['content_text']=body
    receipt=mutate_files(CHANNEL,cell_id,{'operation_id':str(uuid.uuid4()),'mutations':[mutation],'resolves':[]},actor='host')
    assert receipt['outcome']=='accepted',receipt


def _operation(manager,**data):
    from database import get_db_connection
    from persistence import current_file
    paths={a['cell_path'] for a in data.get('artifacts',[]) if a.get('category')!='log'}
    paths.update(data.get('deleted_files',[]))
    conn=get_db_connection(manager.channel)
    try:
        revisions={}
        for path in paths:
            row=current_file(conn,manager.cell_id,path)
            revisions[path]=row['revision'] if row else 0
    finally:
        conn.close()
    manager._fixture_seq=getattr(manager,'_fixture_seq',0)+1
    return dict(data,operation_id=str(uuid.uuid4()),writer_id=manager.run_id,
        edit_seq=manager._fixture_seq,base_revisions=revisions)


def _emit(manager,**data):
    return manager._emit_partial(**_operation(manager,**data))


def _mem(path: str, text: str) -> dict:
    return {"type": "memory", "cell_path": path, "content": text}


# ---------------------------------------------------------------------------
# The emit partition
# ---------------------------------------------------------------------------

def test_an_emit_writes_its_own_folder_and_nothing_else_under_runs__F0138(cell, live):
    run = live.start(cell)
    live.sibling(cell)
    own = f"{run.folder}/panel/status.review_surface"
    _version, refused = _emit(run,artifacts=[
        _mem("notes.md", "work"),
        _mem(own, "mine"),
        _mem(run_card_path(run.label), "forged card"),
        _mem(f"_runs/{OTHER}/tab-1.webview", "theirs"),
        _mem("_runs/dead-cccccccc/leftover.txt", "orphan"),
    ])
    assert sorted((r["path"], r["reason"], r["owner"]) for r in refused) == sorted([
        (run_card_path(run.label), "host_owned", run.label),
        (f"_runs/{OTHER}/tab-1.webview", "locked", OTHER),
        ("_runs/dead-cccccccc/leftover.txt", "locked", "dead-cccccccc"),
    ])
    for files in (_accepted_files(cell), _accepted_files(cell)):
        assert files.get("notes.md") == "work" and files.get(own) == "mine"
        assert not any(p.startswith(("_runs/other", "_runs/dead")) or p.endswith(".run")
                       for p in files)


def test_an_emit_writes_only_declared_ui_file_types_into_its_folder(cell, live):
    """The folder holds UI files only: an own-folder cell write whose suffix
    the manifest's ui_files does not declare is refused (``ui_type``); a log
    artifact never enters the cell, and a delete removes whatever is there."""
    run = live.start(cell)
    run.ui_files = (".chat",)
    pointer = f"{run.folder}/chat/conversation.chat"
    stray = f"{run.folder}/scratch/notes.md"
    _version, refused = _emit(run,artifacts=[
        _mem(pointer, "{}"),
        _mem(stray, "work"),
        {**_mem(f"{run.folder}/trace.md", "log"), "category": "log"},
        _mem("scratch/notes.md", "work"),
    ])
    assert refused == [{"path": stray, "reason": "ui_type", "owner": run.label}]
    files = _accepted_files(cell)
    assert files[pointer] == "{}" and files["scratch/notes.md"] == "work"
    assert stray not in files and f"{run.folder}/trace.md" not in files
    assert _emit(run,deleted_files=[pointer])[1] == []
    assert pointer not in _accepted_files(cell)


def test_a_run_that_declares_no_ui_files_writes_nothing_into_its_folder(cell, live):
    run = live.start(cell)
    run.ui_files = ()
    own = f"{run.folder}/panel/status.review_surface"
    assert _emit(run,artifacts=[_mem(own, "{}")])[1] == [
        {"path": own, "reason": "ui_type", "owner": run.label}]


def test_an_emit_cannot_delete_another_runs_accepted_file(cell,live):
    run=live.start(cell)
    theirs=live.start(cell,runner='other')
    other_file=f'{theirs.folder}/doc.html'
    _emit(theirs,artifacts=[_mem(other_file,'doc')])
    _accept_file(cell,'notes.md','accepted user notes')
    _,refused=_emit(run,artifacts=[_mem(other_file,'clobber'),_mem('notes.md','gen notes')],deleted_files=[other_file])
    assert {entry['path'] for entry in refused}=={other_file}
    assert _accepted_files(cell)[other_file]=='doc'
    assert _accepted_files(cell)['notes.md']=='gen notes'



def test_the_ack_names_what_was_refused(cell, live):
    run = live.start(cell)
    sent: list = []

    async def capture(body, _request):
        sent.append(body)
    run._send_ack = capture
    asyncio.run(run._op_update_cell(_operation(run,**{
        "artifacts": [_mem("_runs/dead-cccccccc/x", "y"), _mem("ok.md", "fine")],
        "metadata": {"name": "Named"},
    })))
    [ack] = sent
    assert ack["op"] == "ack_update" and ack["ok"] is True and ack["cell_id"]
    assert ack["refused"] == [
        {"path": "_runs/dead-cccccccc/x", "reason": "locked", "owner": "dead-cccccccc"}]


def test_a_sealed_run_writes_nothing(cell, live):
    run = live.start(cell)
    run.sealed = True
    with pytest.raises(RuntimeError, match="ended"):
        _emit(run,artifacts=[_mem(f"{run.folder}/late.txt", "late")])
    assert f"{run.folder}/late.txt" not in _accepted_files(cell)


@pytest.mark.parametrize("created_cell", [False, True])
@pytest.mark.parametrize("sibling", [False, True])
def test_only_the_creating_run_may_name_a_cell__F1645_F1653_F0138(cell, live, created_cell, sibling):
    _memory(cell).name = "My title"
    run = live.start(cell, created_cell=created_cell)
    if sibling:
        live.sibling(cell)
    _emit(run,cell_name="Runner title")
    assert _memory(cell).name == ("Runner title" if created_cell else "My title")
    assert run._last_emit_cell_name == ("Runner title" if created_cell else None)


def test_replayed_created_run_name_does_not_overwrite_later_user_rename(client,cell,live):
    run=live.start(cell,created_cell=True)
    request=_operation(run,artifacts=[],metadata={'name':'Runner title'})
    acknowledgements=[]
    async def capture(body,_request):acknowledgements.append(body)
    run._send_ack=capture
    asyncio.run(run._op_update_cell(dict(request)))
    assert _memory(cell).name=='Runner title'
    assert client.patch(f'/api/channels/{CHANNEL}/cells/{cell}/meta',json={'name':'User title'}).status_code==200
    asyncio.run(run._op_update_cell(dict(request)))
    assert _memory(cell).name=='User title'
    assert len(acknowledgements)==2 and all(a['ok'] for a in acknowledgements)
    assert acknowledgements[0]['operation_id']==acknowledgements[1]['operation_id']==request['operation_id']
    assert acknowledgements[0]['files']==acknowledgements[1]['files']==[]


@pytest.mark.parametrize("created_cell", [False, True])
def test_an_unnamed_update_never_renames_the_cell__F0110(client, cell, live, created_cell):
    """F0110: ``gen.commit()`` without a name, like the flush after ``run()``
    returns, sends no ``metadata.name``, and the cell keeps the name its user
    gave it. A named update is accepted only on a cell this run created. The version is labelled with the run's last name, else
    with its generator id, never with the tip's label (here the run-start
    flush's "Manual Edit")."""
    from versions import find_tip

    def rename(name: str) -> None:
        assert client.patch(f"/api/channels/{CHANNEL}/cells/{cell}/meta", json={"name": name}).status_code == 200

    def tip_label() -> str:
        conn = get_db_connection(CHANNEL)
        try:
            return conn.execute("SELECT version_name FROM cells WHERE id=?",
                                (find_tip(conn, cell),)).fetchone()["version_name"]
        finally:
            conn.close()

    rename("Research notes")
    run = live.start(cell, created_cell=created_cell)
    sent: list = []

    async def capture(body, _request):
        sent.append(body)
    run._send_ack = capture

    def update(files: dict | None = None, name: str | None = None) -> None:
        asyncio.run(run._op_update_cell(_operation(run,**{
            "artifacts": [_mem(path, text) for path, text in (files or {}).items()],
            "metadata": {} if name is None else {"name": name},
        })))

    _accept_file(cell, "notes.md", "the user's edit")
    from cell_files import checkpoint_cells
    checkpoint_cells(CHANNEL,[cell],"checkpoint")
    assert "notes.md" in _accepted_files(cell)

    update({"notes.md": "accepted edit"})
    assert _memory(cell).name == "Research notes"
    assert tip_label() == "synth"

    update(name="Report")
    assert _memory(cell).name == ("Report" if created_cell else "Research notes")
    rename("Mine")
    update({"notes.md": "final"})
    assert _memory(cell).name == "Mine"
    assert tip_label() == ("Report" if created_cell else "synth")
    assert _accepted_files(cell)["notes.md"] == "final"
    assert [ack["ok"] for ack in sent] == [True, True, True]


# ---------------------------------------------------------------------------
# Lock order: an emit racing a start on the same cell finishes
# ---------------------------------------------------------------------------

def test_emit_and_accepted_checkpoint_lock_order_finishes(cell,live):
    from cell_files import checkpoint_cells
    run=live.start(cell)
    errors=[]
    def emits():
        try:
            for index in range(15):_emit(run,artifacts=[_mem('emitted.txt',str(index))])
        except Exception as exc:errors.append(exc)
    def checkpoints():
        try:
            for index in range(15):
                _accept_file(cell,'user.txt',f'user {index}')
                checkpoint_cells(CHANNEL,[cell],"checkpoint")
        except Exception as exc:errors.append(exc)
    threads=[threading.Thread(target=emits),threading.Thread(target=checkpoints)]
    for thread in threads:thread.start()
    for thread in threads:thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads),'Accepted emit/checkpoint deadlocked'
    assert errors==[]
    with get_db_connection(CHANNEL) as conn:
        assert not conn.execute("SELECT parent_cell_id FROM cell_edges WHERE parent_key='previous_version' GROUP BY parent_cell_id HAVING COUNT(*)>1").fetchall()
    assert _accepted_files(cell)['emitted.txt']=='14' and _accepted_files(cell)['user.txt']=='user 14'



# ---------------------------------------------------------------------------
# Draft promotion around run folders
# ---------------------------------------------------------------------------

def test_user_arbitrary_run_path_put_and_delete_are_refused_live_or_inactive(cell,live,client):
    from persistence import current_file
    run=live.start(cell)
    path=f'{run.folder}/panel/status.review_surface'
    _emit(run,artifacts=[_mem(path,'pending')])
    for active in (True,False):
        if not active:live.runs.pop(run.run_id)
        with get_db_connection(CHANNEL) as conn:revision=current_file(conn,cell,path)['revision']
        for kind in ('put','delete'):
            mutation={'path':path,'writer_id':'user','edit_seq':1,'base_revision':revision,'kind':kind}
            if kind=='put':mutation['content_text']='forged'
            response=client.post(f'/api/channels/{CHANNEL}/cells/{cell}/files/mutate',json={'operation_id':str(uuid.uuid4()),'mutations':[mutation],'resolves':[]})
            assert response.status_code==409,response.text
        assert _accepted_files(cell)[path]=='pending'



def test_a_version_write_without_the_cell_lock_is_refused(cell):
    from versions import write_version_from_overlay
    conn = get_db_connection(CHANNEL)
    try:
        with pytest.raises(RuntimeError, match="commit lock"):
            write_version_from_overlay(conn, cell, "r", [("a", "set", "b")],
                                        version_name="x", source_type="manual_edit",
                                        generator_name="x")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The run card and the sweeps
# ---------------------------------------------------------------------------

def test_the_card_and_the_run_end_sweep(cell, live):
    """The card is the run's first locked file; the sweep removes the whole
    folder, writes accepted tombstones for its files
    and keeps the prior accepted bytes in the run's Logs — unless the run logged that
    path itself."""
    from cell_commits import run_card, sweep_run_folder, write_run_start

    run = live.start(cell)
    card = run_card(run_id=run.run_id, runner_id="synth", runner_name="Synth",
                    generator="synth", label=run.label)
    write_run_start(CHANNEL, cell, card, {})
    card_path = run_card_path(run.label)
    written = json.loads(_accepted_files(cell)[card_path])
    assert set(written) == {"v", "run_id", "runner", "generator", "label",
                            "folder", "started_at"}
    assert written["folder"] == run.folder
    _emit(run,artifacts=[_mem(f"{run.folder}/chat/main.json", "[]"),
                                 _mem("keep.md", "keep")])
    _accept_file(cell, f"{run.folder}/panel/status.txt", "accepted\n")
    conn = get_db_connection(CHANNEL)
    try:
        from run_artifacts import record_artifact_bytes
        record_artifact_bytes(conn, run.run_id, "log", f"{run.folder}/chat/main.json", b"logged")
        conn.commit()
    finally:
        conn.close()

    assert sweep_run_folder(CHANNEL, cell, run.run_id, run.label)
    for files in (_accepted_files(cell), _accepted_files(cell)):
        assert not any(p.startswith(run.folder + "/") for p in files)
        assert files["keep.md"] == "keep"
    assert all(not path.startswith(run.folder+"/") for path in _accepted_files(cell))
    conn = get_db_connection(CHANNEL)
    try:
        logs = {r["filepath"]: r["blob_hash"] for r in conn.execute(
            "SELECT filepath, blob_hash FROM run_artifacts WHERE run_id=? AND category='log'",
            (run.run_id,))}
    finally:
        conn.close()
    from cell_commits import blob_bytes
    assert blob_bytes(logs[f"{run.folder}/chat/main.json"]) == b"logged"
    assert card_path in logs
    assert f"{run.folder}/panel/status.txt" in logs
    # Idempotent: nothing left, nothing written.
    assert sweep_run_folder(CHANNEL, cell, run.run_id, run.label) is None


def test_the_startup_sweep_clears_dead_folders_from_tip_memory_and_accepted_files(cell, live):
    """After a crash the restored state and the tip still hold a dead run's
    folder: the startup sweep removes it everywhere, so the next emit on the
    cell cannot re-commit it; a live run's folder stays."""
    from cell_commits import sweep_orphan_run_folders

    run = live.start(cell)
    alive = live.start(cell, runner="alive")
    _emit(alive,artifacts=[_mem(f"{alive.folder}/a.txt", "live")])
    dead = "_runs/dead-cccccccc/orphan.txt"
    # The dead run's file in the tip (a version without the run) ...
    from cell_commits import commit_cell_change
    commit_cell_change(CHANNEL, cell, run.run_id, writes={dead: "orphan"})
    # ... in metadata, and an accepted file under another dead folder.
    _accept_file(cell,"_runs/dead-dddddddd/mem.txt","m")
    _accept_file(cell, "_runs/dead-eeeeeeee/accepted.txt", "d")
    live.runs.pop(run.run_id)

    assert sweep_orphan_run_folders() >= 1
    for files in (_accepted_files(cell), _accepted_files(cell)):
        assert not any(p.startswith("_runs/dead-") for p in files)
        assert files[f"{alive.folder}/a.txt"] == "live"
    assert not any(path.startswith("_runs/dead-") for path in _accepted_files(cell))
    _emit(alive,artifacts=[_mem("after.md", "x")])
    assert not any(p.startswith("_runs/dead-") for p in _accepted_files(cell))


# ---------------------------------------------------------------------------
# reveal_file
# ---------------------------------------------------------------------------

def test_reveal_file_broadcasts_only_a_committed_path(cell, live, monkeypatch):
    import events
    run = live.start(cell)
    _emit(run,artifacts=[_mem(f"{run.folder}/tab-1.webview", "{}")])
    seen: list = []
    monkeypatch.setattr(events, "broadcast", lambda ch, ev: seen.append(ev))
    asyncio.run(run._op_reveal_file({"path": f"{run.folder}/tab-1.webview"}))
    asyncio.run(run._op_reveal_file({"path": f"{run.folder}/tab-2.webview"}))
    assert seen == [{"type": "reveal_file", "run_id": run.run_id, "cell_id": cell,
                     "path": f"{run.folder}/tab-1.webview"}]


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

def test_a_history_run_node_is_keyed_by_run_and_first_version():
    from cell_commits import run_node_id
    assert run_node_id("r1", "v1") == "run:r1:v1"
    assert run_node_id("r1", "v7") != run_node_id("r1", "v1")
    assert run_node_id("r1", None) == "run:r1"


def test_failed_update_reports_failure_without_stale_version__F0097(cell, live, monkeypatch):
    run = live.start(cell)
    replies = []

    async def capture(body, _request):
        replies.append(body)

    run._send_ack = capture
    asyncio.run(run._op_update_cell(_operation(run,**{"artifacts": [_mem("ok.md", "first")]})))
    assert replies[-1]["ok"] and replies[-1]["cell_id"]
    committed_version = replies[-1]["cell_id"]

    def fail(data):
        raise RuntimeError("commit failed")

    monkeypatch.setattr(run, "_update_cell", fail)
    asyncio.run(run._op_update_cell(_operation(run,**{"artifacts": [_mem("failed.md", "lost")]})))
    assert replies[-1] == {"op": "ack_update", "ok": False, "cell_id": "",
                           "error": "commit failed", "refused": []}
    assert run._own_commit_paths == {"ok.md": committed_version}
    assert "failed.md" not in _accepted_files(cell)


def test_commit_returns_its_id_and_broadcasts_canvas_target__F0115(cell, live, monkeypatch):
    from versions import find_tip
    from process_manager.ipc import IPCMixin
    import inspect

    run = live.start(cell)
    broadcasts = []
    monkeypatch.setattr(run, "_broadcast_dag_commit", lambda cell_id, files: broadcasts.append(cell_id))
    version, refused = _emit(run,artifacts=[_mem("one.md", "one")])
    assert refused == [] and version != cell
    with get_db_connection(CHANNEL) as conn:
        assert find_tip(conn, cell) == version
    assert broadcasts == [cell]
    assert not hasattr(run, "_last_cell_id")
    parameter = inspect.signature(IPCMixin._broadcast_dag_commit).parameters["cell_id"]
    assert parameter.default is inspect.Parameter.empty


def test_commit_failure_reply_echoes_its_request_id__F0153(cell, live, monkeypatch):
    run = live.start(cell)
    lines = []

    async def drain():
        pass

    run._ipc_writer = types.SimpleNamespace(write=lines.append, drain=drain)

    def fail(data):
        raise RuntimeError("commit unavailable")

    monkeypatch.setattr(run, "_update_cell", fail)
    asyncio.run(run._handle_ipc(json.dumps({
        "op": "update_cell", "req_id": "failed-update", "artifacts": [_mem("notes.md", "text")],
    })))
    assert [json.loads(line) for line in lines] == [{
        "op": "ack_update", "ok": False, "cell_id": "", "refused": [],
        "error": "commit unavailable", "req_id": "failed-update",
    }]


def test_generator_commit_paths_are_the_actual_delta__F0308_F1716(cell, live, monkeypatch):
    import events

    run = live.start(cell)
    sent = []
    monkeypatch.setattr(events, "broadcast", lambda channel, event: sent.append(event))
    _emit(run,artifacts=[_mem("keep.md", "unchanged"), _mem("gone.md", "old")])
    sent.clear()
    _emit(run,artifacts=[
        _mem("keep.md", "unchanged"), _mem("new.md", "new"),
        _mem(f"_runs/{OTHER}/no.md", "refused"),
        {**_mem("trace.md", "trace"), "category": "log"},
    ], deleted_files=["gone.md"])
    [event] = [e for e in sent if e["type"] == "dag_commit"]
    assert event["cell_id"] == cell
    assert event["files"] == ["gone.md", "keep.md", "new.md"]


@pytest.mark.parametrize('accepted,delete',[('visible edit',False),('committed',False),(None,False),(b'different binary',False),(b'committed',False),('accepted-only',True)])
def test_file_receipts_reach_renderers_even_when_hash_is_unchanged__F0308_F1716(cell,live,monkeypatch,accepted,delete):
    import events
    run=live.start(cell)
    _emit(run,artifacts=[_mem('same.md','committed')])
    path='accepted-only.md' if delete else 'same.md'
    _accept_file(cell,path,accepted)
    seen=[]
    monkeypatch.setattr(events,'broadcast',lambda channel,event:seen.append(event))
    if delete:_emit(run,deleted_files=[path])
    else:_emit(run,artifacts=[_mem(path,'committed')])
    receipt=next(event for event in seen if event['type']=='file_changes')
    assert next(file for file in receipt['files'] if file['path']==path)['deleted']==delete
    assert (path not in _accepted_files(cell))==delete



def test_host_commit_paths_include_prefix_deletions__F0308(cell, live, monkeypatch):
    import events
    from cell_commits import commit_cell_change

    run = live.start(cell)
    sent = []
    monkeypatch.setattr(events, "broadcast", lambda channel, event: sent.append(event))
    commit_cell_change(CHANNEL, cell, run.run_id, writes={
        "keep.md": "unchanged", "old/a.md": "a", "old/b.md": "b",
    })
    sent.clear()
    commit_cell_change(CHANNEL, cell, run.run_id, writes={"keep.md": "unchanged"},
                       delete_prefix="old")
    [event] = [e for e in sent if e["type"] == "dag_commit"]
    assert event["files"] == ["keep.md", "old/a.md", "old/b.md"]


@pytest.mark.parametrize('operation_id', [None, '', 7, True, 'x' * 257])
@pytest.mark.parametrize('named', [False, True])
def test_invalid_ipc_operation_id_leaves_no_receipt_or_metadata_intent(cell, live, monkeypatch, operation_id, named):
    from database import get_global_db_connection
    run = live.start(cell, created_cell=True)
    request = _operation(run, metadata={'name': 'Refused name'} if named else {})
    request['operation_id'] = operation_id
    staged = []
    monkeypatch.setattr(run, '_hash_artifacts', lambda data: staged.append(data) or {})
    replies = []
    async def capture(body, _request):
        replies.append(body)
    run._send_ack = capture
    original_name = _memory(cell).name
    with get_db_connection(CHANNEL) as conn:
        before = [tuple(row) for row in conn.execute('SELECT * FROM file_operations WHERE cell_id=?', (cell,))]
    asyncio.run(run._op_update_cell(request))
    assert replies[-1]['ok'] is False
    assert 'operation_id' in replies[-1]['error']
    assert not staged
    assert _memory(cell).name == original_name
    with get_db_connection(CHANNEL) as conn:
        assert [tuple(row) for row in conn.execute('SELECT * FROM file_operations WHERE cell_id=?', (cell,))] == before
    conn = get_global_db_connection()
    try:
        assert conn.execute('SELECT COUNT(*) FROM cell_metadata_operations WHERE cell_id=?', (cell,)).fetchone()[0] == 0
    finally:
        conn.close()


def test_named_ipc_retry_finishes_metadata_after_file_receipt_committed(client, cell, live, monkeypatch):
    import cell_metadata
    from database import get_global_db_connection
    run = live.start(cell, created_cell=True)
    frozen = Path(run.workspace_dir) / "frozen-artifact.txt"
    frozen.write_text("accepted once")
    request = _operation(run, artifacts=[{
        "type": "workspace", "cell_path": "result.txt", "local_path": frozen.name,
    }], metadata={"name": "Recovered run title"})
    acknowledgements = []
    async def capture(body, _request):
        acknowledgements.append(body)
    run._send_ack = capture
    with monkeypatch.context() as patch:
        def unavailable(*args):
            raise OSError("injected metadata publication failure")
        patch.setattr(cell_metadata, "_event", unavailable)
        asyncio.run(run._op_update_cell(dict(request)))
    assert acknowledgements[0]["ok"] is False
    assert _memory(cell).name != "Recovered run title"
    frozen.unlink()
    with monkeypatch.context() as patch:
        def restaged(*args):
            raise AssertionError("durable receipt replay must not stage artifacts")
        patch.setattr(run, "_hash_artifacts", restaged)
        asyncio.run(run._op_update_cell(dict(request)))
    assert acknowledgements[1]["ok"] is True
    assert _memory(cell).name == "Recovered run title"
    conn = get_global_db_connection()
    try:
        events = conn.execute("SELECT old_name,new_name,source_type FROM cell_rename_events WHERE cell_id=?",
                              (cell,)).fetchall()
        assert len(events) == 1
        assert events[0]["new_name"] == "Recovered run title" and events[0]["source_type"] == "generator"
        receipt = conn.execute("SELECT status FROM cell_metadata_operations WHERE operation_id=?",
                               (request["operation_id"],)).fetchone()
        assert receipt["status"] == "applied"
    finally:
        conn.close()
    changed = dict(request, artifacts=[dict(request["artifacts"][0], local_path="another-snapshot")])
    asyncio.run(run._op_update_cell(changed))
    assert acknowledgements[2]["ok"] is False
    assert "different input" in acknowledgements[2]["error"]
