"""Synthetic metadata publication, durable replay and journal contracts."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pytest


@pytest.fixture
def workspace(fresh_cwd, monkeypatch):
    import state
    import cell_metadata
    import user_validation
    from database import get_db_connection
    from models import AppState, Channel, Stack, Cell
    cell = Cell(name="Original")
    stack = Stack(name="Column", cells=[cell])
    channel = Channel(name="Synthetic workspace", main_stacks=[stack])
    monkeypatch.setattr(state, "state", AppState(channels=[channel], active_channel_id=channel.id))
    monkeypatch.setattr(state, "_persistence_writer", None)
    monkeypatch.setattr(state, "broadcast_state_update", lambda: None)
    monkeypatch.setattr(user_validation, "notify_user_validation", lambda: None)
    monkeypatch.setattr(cell_metadata, "_announce", lambda *args: None)
    conn = get_db_connection(str(channel.id))
    conn.close()
    state.persist_state()
    yield SimpleNamespace(channel=channel, stack=stack, cell=cell, cid=str(cell.id), ch=str(channel.id))
    state.stop_persistence_writer()


def _rows(sql, params=()):
    from database import get_global_db_connection
    conn = get_global_db_connection()
    try:
        return [dict(row) for row in conn.execute(sql, params)]
    finally:
        conn.close()


def _layout():
    return json.loads(_rows("SELECT value FROM ui_state WHERE key='app_state'")[0]["value"])


def _rename(ws, name, **extra):
    from cell_metadata import publish_cell_metadata
    from cell_lineage import user_actor
    return publish_cell_metadata(ws.ch, ws.cid, {"name": name, **extra},
                                 source_type="manual_edit", actor=user_actor())


def _intent(ws, name="Run title"):
    from cell_metadata import prepare_cell_metadata
    operation = str(uuid4())
    digest = hashlib.sha256(operation.encode()).hexdigest()
    args = (ws.ch, ws.cid, operation, digest, name)
    kwargs = {"source_type": "generator", "actor": {
        "kind": "runner", "runner_id": "synthetic-runner", "runner_name": "Captured runner"}}
    prepare_cell_metadata(*args, **kwargs)
    return args, kwargs


def _file_receipt(ws, args, *, outcome="accepted", refused=None):
    from database import get_db_connection
    receipt = {"operation_id": args[2], "outcome": outcome, "files": [], "conflicts": []}
    if refused:
        receipt["refused"] = refused
    conn = get_db_connection(ws.ch)
    try:
        conn.execute("INSERT INTO file_operations VALUES (?,?,?,?)",
                     (args[2], ws.cid, args[3], json.dumps(receipt)))
        conn.commit()
    finally:
        conn.close()


def test_cell_rename_commits_literal_names_and_explicit_actor_without_file_history(workspace):
    from cell_journal import journal
    from database import get_db_connection
    ws = workspace
    _rename(ws, "Second")
    _rename(ws, "Third")
    events = [e for e in journal(ws.ch, ws.cid, before=None, limit=100) if e["kind"] == "cell_renamed"]
    assert {(e["old_name"], e["new_name"]) for e in events} == {("Original", "Second"), ("Second", "Third")}
    assert all(e["by"]["kind"] == "user" and e["cells"][0]["name"] == "Third" for e in events)
    assert all(e["version_id"] is None and e["files"] == [] for e in events)
    assert _layout()["channels"][0]["main_stacks"][0]["cells"][0]["name"] == "Third"
    conn = get_db_connection(ws.ch)
    try:
        assert conn.execute("SELECT COUNT(*) FROM cells").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM file_revisions").fetchone()[0] == 0
    finally:
        conn.close()


def test_a_metadata_noop_creates_no_rename_event(workspace):
    ws = workspace
    _rename(ws, "Original", icon="star")
    assert _rows("SELECT * FROM cell_rename_events") == []
    assert ws.cell.icon == "star"


def test_a_capabilities_cell_rename_records_its_event(workspace):
    from cell_placement import CAPABILITIES_STACK_TYPE
    ws = workspace
    ws.stack.stack_type = CAPABILITIES_STACK_TYPE
    _rename(ws, "Capability title")
    assert [(e["old_name"], e["new_name"]) for e in _rows("SELECT * FROM cell_rename_events")] == [
        ("Original", "Capability title")]
    assert ws.cell.name == "Capability title"


def test_rename_sql_failure_exposes_neither_layout_nor_event(workspace):
    from database import get_global_db_connection
    ws = workspace
    before = _layout()
    conn = get_global_db_connection()
    conn.execute("CREATE TRIGGER reject_rename BEFORE INSERT ON cell_rename_events "
                 "BEGIN SELECT RAISE(ABORT,'journal unavailable'); END")
    conn.commit()
    conn.close()
    with pytest.raises(sqlite3.IntegrityError, match="journal unavailable"):
        _rename(ws, "Uncommitted")
    assert ws.cell.name == "Original" and _layout() == before
    assert _rows("SELECT * FROM cell_rename_events") == []


def test_pending_name_finishes_after_failure_and_replay_never_overwrites_later_name(workspace, monkeypatch):
    import cell_metadata
    ws = workspace
    args, kwargs = _intent(ws)
    _file_receipt(ws, args)
    with monkeypatch.context() as patch:
        patch.setattr(cell_metadata, "_event", lambda *args: (_ for _ in ()).throw(OSError("publication failed")))
        with pytest.raises(OSError, match="publication failed"):
            cell_metadata.finish_cell_metadata(*args, **kwargs)
    assert ws.cell.name == "Original"
    assert _rows("SELECT status FROM cell_metadata_operations")[0]["status"] == "pending"
    result = cell_metadata.finish_cell_metadata(*args, **kwargs)
    assert result["status"] == "applied" and ws.cell.name == "Run title"
    _rename(ws, "User title")
    assert cell_metadata.finish_cell_metadata(*args, **kwargs) == result
    assert ws.cell.name == "User title"
    events = _rows("SELECT * FROM cell_rename_events")
    assert len(events) == 2
    run = next(e for e in events if e["source_type"] == "generator")
    assert json.loads(run["actor_json"]) == kwargs["actor"]


def test_pending_intent_is_not_rebased_across_metadata_aba(workspace):
    from cell_metadata import prepare_cell_metadata, finish_cell_metadata
    ws = workspace
    args, kwargs = _intent(ws)
    _rename(ws, "Other")
    _rename(ws, "Original")
    prepare_cell_metadata(*args, **kwargs)
    _file_receipt(ws, args)
    assert finish_cell_metadata(*args, **kwargs)["status"] == "superseded"
    assert ws.cell.name == "Original"
    assert len(_rows("SELECT * FROM cell_rename_events")) == 2


def test_failed_lifecycle_attempt_conservatively_invalidates_pending_name(workspace):
    from cell_metadata import advance_cell_metadata_generations, finish_cell_metadata
    from cell_commits import cell_commit_lock
    ws = workspace
    args, kwargs = _intent(ws)
    with cell_commit_lock(ws.cid):
        advance_cell_metadata_generations(ws.ch, [ws.cid])
        # The subsequent lifecycle transaction fails without changing membership.
    _file_receipt(ws, args)
    assert finish_cell_metadata(*args, **kwargs)["status"] == "superseded"
    assert ws.cell.name == "Original"
    assert _rows("SELECT * FROM cell_rename_events") == []


@pytest.mark.parametrize("outcome,refused,status", [
    ("accepted", [{"path": "bad.txt", "reason": "handler"}], "applied"),
    ("conflict", None, "refused"), ("superseded", None, "refused"),
])
def test_name_eligibility_uses_exact_operation_outcome(workspace, outcome, refused, status):
    from cell_metadata import finish_cell_metadata
    args, kwargs = _intent(workspace)
    _file_receipt(workspace, args, outcome=outcome, refused=refused)
    assert finish_cell_metadata(*args, **kwargs)["status"] == status
    assert workspace.cell.name == ("Run title" if status == "applied" else "Original")


def test_changed_retry_input_and_missing_intent_are_refused(workspace):
    from cell_metadata import prepare_cell_metadata, finish_cell_metadata
    args, kwargs = _intent(workspace)
    with pytest.raises(ValueError, match="different input"):
        prepare_cell_metadata(*(*args[:4], "Changed"), **kwargs)
    missing = (*args[:2], str(uuid4()), *args[3:])
    _file_receipt(workspace, missing)
    with pytest.raises(ValueError, match="no prepared metadata intent"):
        finish_cell_metadata(*missing, **kwargs)
    assert workspace.cell.name == "Original"


def test_queued_layout_and_concurrent_unrelated_change_cannot_erase_name(workspace, monkeypatch):
    import concurrent.futures
    import threading
    import cell_metadata
    import state
    ws = workspace
    state.start_persistence_writer(delay=3600)
    entered, release = threading.Event(), threading.Event()
    original = cell_metadata.write_layout_state
    def paused(conn, snapshot):
        entered.set()
        assert release.wait(5)
        return original(conn, snapshot)
    monkeypatch.setattr(cell_metadata, "write_layout_state", paused)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(_rename, ws, "Committed name")
        assert entered.wait(5)
        assert ws.cell.name == "Original"
        assert state.state_lock.acquire(timeout=0.2)
        try:
            ws.stack.name = "Concurrent column"
        finally:
            state.state_lock.release()
        state.persist_state()
        release.set()
        pending.result(timeout=5)
    assert ws.cell.name == "Committed name" and ws.stack.name == "Concurrent column"
    state.flush_persist_state()
    saved = _layout()["channels"][0]["main_stacks"][0]
    assert saved["name"] == "Concurrent column"
    assert saved["cells"][0]["name"] == "Committed name"


def test_explicit_channel_delete_removes_only_its_global_metadata(workspace):
    import state
    from cell_metadata import finish_cell_metadata, remove_channel_layout
    from database import get_db_connection
    from models import Channel, Stack, Cell
    ws = workspace
    other = Channel(name="Retained channel", main_stacks=[Stack(cells=[Cell(name="Retained cell")])])
    state.state.channels.append(other)
    conn = get_db_connection(str(other.id))
    conn.close()
    survivor = SimpleNamespace(ch=str(other.id), cid=str(other.main_stacks[0].cells[0].id))
    _rename(survivor, "Other name")
    _intent(survivor)
    args, kwargs = _intent(ws)
    _file_receipt(ws, args)
    finish_cell_metadata(*args, **kwargs)
    remove_channel_layout(ws.ch)
    assert [ch["id"] for ch in _layout()["channels"]] == [survivor.ch]
    for table in ("cell_rename_events", "cell_metadata_heads", "cell_metadata_operations"):
        assert _rows(f"SELECT * FROM {table} WHERE channel_id=?", (ws.ch,)) == []
        assert len(_rows(f"SELECT * FROM {table} WHERE channel_id=?", (survivor.ch,))) == 1


def test_empty_channel_delete_waits_for_import_publication(workspace, monkeypatch):
    import concurrent.futures
    import threading
    from contextlib import contextmanager
    import cell_commits
    import state
    from cell_metadata import remove_channel_layout
    from models import Cell, Stack
    ws = workspace
    ws.channel.main_stacks.clear()
    state.persist_state()
    barrier = "channel-publication:" + ws.ch
    attempted = threading.Event()
    original = cell_commits.cell_commit_lock
    def observed(cell_id):
        if cell_id != barrier:
            return original(cell_id)
        @contextmanager
        def waiting():
            attempted.set()
            with original(cell_id):
                yield
        return waiting()
    monkeypatch.setattr(cell_commits, "cell_commit_lock", observed)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        with original(barrier):
            deletion = pool.submit(remove_channel_layout, ws.ch)
            assert attempted.wait(5)
            assert not deletion.done()
            assert ws.channel in state.state.channels
            # Import publishes its new IDs before releasing the barrier. The
            # subsequent delete must capture and remove that complete state.
            with state.state_lock:
                ws.channel.main_stacks.append(Stack(cells=[Cell(name="Imported")]))
        deletion.result(timeout=5)
    assert state.state.channels == [] and _layout()["channels"] == []
    assert _rows("SELECT * FROM cell_metadata_heads WHERE channel_id=?", (ws.ch,)) == []


def test_same_second_rename_page_keeps_boundary_and_literal_history(workspace, monkeypatch):
    import cell_metadata
    from cell_journal import journal
    monkeypatch.setattr(cell_metadata, "now_iso", lambda: "2040-01-01T12:00:00+00:00")
    _rename(workspace, "Second")
    _rename(workspace, "Third")
    page = journal(workspace.ch, None, before=None, limit=1)
    assert len(page) == 2
    assert all(e["kind"] == "cell_renamed" for e in page)
    older = journal(workspace.ch, None, before=page[-1]["t"], limit=1)
    assert all(e["kind"] != "cell_renamed" for e in older)


def test_restart_completes_only_durable_pending_operations_once(workspace, monkeypatch):
    import state
    from cell_metadata import recover_pending_cell_metadata
    from genesis import parse_saved_app_state
    ws = workspace
    args, kwargs = _intent(ws)
    _file_receipt(ws, args)
    # Reconstruct layout, so recovery cannot rely on the previous Cell instance.
    restored = parse_saved_app_state(json.dumps(_layout()))
    monkeypatch.setattr(state, "state", restored)
    assert recover_pending_cell_metadata() == 1
    assert restored.channels[0].main_stacks[0].cells[0].name == "Run title"
    assert recover_pending_cell_metadata() == 0
    assert len(_rows("SELECT * FROM cell_rename_events")) == 1
    _intent(ws, "Never committed")
    assert recover_pending_cell_metadata() == 0
    assert restored.channels[0].main_stacks[0].cells[0].name == "Run title"


def test_offline_reset_invalidation_uses_loaded_snapshot_without_live_registry(workspace, monkeypatch):
    import state
    from cell_metadata import advance_snapshot_generations, finish_cell_metadata
    from models import AppState
    args, kwargs = _intent(workspace)
    snapshot = _layout()
    with monkeypatch.context() as patch:
        patch.setattr(state, "state", AppState())
        advance_snapshot_generations(snapshot, workspace.ch, [workspace.cid])
    _file_receipt(workspace, args)
    assert finish_cell_metadata(*args, **kwargs)["status"] == "superseded"
    assert workspace.cell.name == "Original"


def _tag(ws, tags):
    from cell_metadata import publish_cell_metadata
    from cell_lineage import user_actor
    return publish_cell_metadata(ws.ch, ws.cid, {"tags": tags}, source_type="manual_edit", actor=user_actor())


def _head(ws):
    return _rows("SELECT generation, signature FROM cell_metadata_heads WHERE channel_id=? AND cell_id=?",
                 (ws.ch, ws.cid))


def test_tags_publish_normalized_without_rename_lineage_or_generation_TG01(workspace, monkeypatch):
    import cell_metadata
    ws = workspace
    _rename(ws, "Named")
    announced = []
    monkeypatch.setattr(cell_metadata, "_announce", lambda channel, cell_id, renamed: announced.append(renamed))
    head, events, before = _head(ws), _rows("SELECT * FROM cell_rename_events"), ws.cell.updated_at
    assert len(head) == 1 and len(events) == 1
    result = _tag(ws, ["  Draft ", "client  work", "DRAFT"])
    assert result["tags"] == ["Draft", "client work"] and ws.cell.tags == ["Draft", "client work"]
    assert set(result) == {"id", "name", "icon", "settings", "pinned", "tags", "updated_at"}
    assert ws.cell.updated_at > before and result["updated_at"] == ws.cell.updated_at.isoformat()
    assert _layout()["channels"][0]["main_stacks"][0]["cells"][0]["tags"] == ["Draft", "client work"]
    # Tags are unsigned: the metadata generation, rename history and lineage stay put.
    assert _head(ws) == head and _rows("SELECT * FROM cell_rename_events") == events
    assert announced == [False]
    _tag(ws, [])
    assert ws.cell.tags == [] and _head(ws) == head
    with pytest.raises(ValueError):
        _tag(ws, ["ok", "bad\x00"])
    assert ws.cell.tags == []


def test_a_tag_edit_does_not_supersede_a_pending_run_name_TG01(workspace):
    from cell_metadata import finish_cell_metadata
    ws = workspace
    args, kwargs = _intent(ws)
    _tag(ws, ["during run"])
    _file_receipt(ws, args)
    assert finish_cell_metadata(*args, **kwargs)["status"] == "applied"
    assert ws.cell.name == "Run title" and ws.cell.tags == ["during run"]


@pytest.fixture
def meta_client(api_app):
    from fastapi.testclient import TestClient
    from database import get_db_connection
    from models import Cell, Channel, Stack
    from state import state
    with TestClient(api_app) as client:
        cell = Cell(name="Tagged")
        channel = Channel(name="Synthetic tags", main_stacks=[Stack(cells=[cell])])
        from genesis import ensure_capabilities_stack
        ensure_capabilities_stack(channel)
        state.channels = [channel]
        state.active_channel_id = channel.id
        get_db_connection(str(channel.id)).close()
        yield client, f"/api/channels/{channel.id}/cells/{cell.id}/meta", cell


def test_patch_meta_tags_a_capabilities_cell_like_any_other_TG01(meta_client):
    from cell_placement import capabilities_stack
    from models import Cell
    from state import state
    client, route, cell = meta_client
    capability = Cell(name="Capability")
    capabilities_stack(state.channels[0]).cells.append(capability)
    capability_route = route.replace(str(cell.id), str(capability.id))
    response = client.patch(capability_route, json={"tags": ["x", " X "]})
    assert response.status_code == 200, response.text
    assert response.json()["tags"] == ["x"] and capability.tags == ["x"]
    assert client.patch(capability_route, json={"name": "Renamed"}).status_code == 200
    assert capability.name == "Renamed"


def test_patch_meta_takes_the_full_tag_list_TG01(meta_client):
    from cell_tags import TAGS_PER_CELL_MAX
    client, route, cell = meta_client
    response = client.patch(route, json={"tags": ["Later", " later ", "  urgent\tfix "]})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tags"] == ["Later", "urgent fix"] and cell.tags == ["Later", "urgent fix"]
    assert set(body) == {"id", "name", "icon", "settings", "pinned", "tags", "updated_at"}
    for bad in (None, ["x" * 49], ["a\x07b"], [""], ["ok", 3],
                [f"t{i}" for i in range(TAGS_PER_CELL_MAX + 1)]):
        response = client.patch(route, json={"tags": bad})
        assert response.status_code == 400, (bad, response.text)
    assert cell.tags == ["Later", "urgent fix"]
    assert client.patch(route, json={"tags": []}).json()["tags"] == [] and cell.tags == []
