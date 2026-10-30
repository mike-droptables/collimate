"""A run's annotation context across its life: created with the run row at
accept (the prompt seeded as its first, read message), sealed by a refused
start, by the run's end (with its annotations.json log) and by crash
recovery; listed as ``listening`` on /runs/active."""
from __future__ import annotations

import asyncio
import json
import shutil
import time

import pytest

import annotations as store
from conftest import install_runner
from database import get_db_connection
from fake_manager import FakeManager
from run_spawn import run_row, wait_spawned


def _cell(client) -> str:
    return client.post(f"/api/stacks/{client.channel_id}/create").json()["main_stacks"][-1]["cells"][0]["id"]


def _context(client, cell_id, run_id):
    conn = get_db_connection(client.channel_id)
    try:
        return store.get_context(conn, cell_id, run_id)
    finally:
        conn.close()


def _log_artifacts(channel, run_id) -> dict[str, bytes]:
    from cell_commits import blob_bytes
    conn = get_db_connection(channel)
    try:
        rows = conn.execute("SELECT filepath, blob_hash FROM run_artifacts WHERE run_id=? AND category='log'",
                            (run_id,)).fetchall()
    finally:
        conn.close()
    return {row["filepath"]: blob_bytes(row["blob_hash"]) for row in rows}


@pytest.fixture
def held_runs(client, monkeypatch):
    """Managers that never spawn and stay registered; teardown drops them."""
    import routes.runs.create as create
    import run_registry
    import served_surfaces
    from run_folders import mint_run_label, run_folder
    managers: dict[str, FakeManager] = {}

    class Manager(FakeManager):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.label = mint_run_label(self.runner_id, self.run_id)
            self.folder = run_folder(self.label)
            self.ui_files = ()
            managers[self.run_id] = self

        async def start(self):
            pass

    async def no_reaper(*_args):
        pass
    monkeypatch.setattr(create, "ProcessManager", Manager)
    monkeypatch.setattr(create, "_reap_process", no_reaper)
    yield managers
    for run_id in managers:
        run_registry.active_managers.pop(run_id, None)
        served_surfaces.release_run(run_id)


def _start(client, runner, cell_id):
    response = client.post(f"/api/channels/{client.channel_id}/runs", json={"runner": runner, "cell_id": cell_id})
    assert response.status_code == 200, response.text
    return response.json()["run_id"]


def test_accept_creates_the_run_context_and_seeds_a_non_empty_prompt(client, held_runs, monkeypatch):
    import routes.runs.create as create
    seen = []
    original = create._RunStart.spawn

    async def spawn(self):
        seen.append(_context(client, self.made["cell_id"], self.run_id))
        return await original(self)
    monkeypatch.setattr(create._RunStart, "spawn", spawn)
    runner, _ = install_runner(generator="annotated", runner="annotated",
                               definition={"prompt": "Tidy [notes](notes.md)"})
    cell_id = _cell(client)
    run_id = _start(client, runner, cell_id)
    wait_spawned(run_id)
    (before_spawn,) = seen
    assert before_spawn is not None and before_spawn["run_id"] == run_id
    (prompt,) = before_spawn["messages"]
    assert prompt["kind"] == "prompt" and prompt["author"] == "user" and prompt["body"] == "Tidy [notes](notes.md)"
    assert prompt["read_at"] == prompt["created_at"] and prompt["refs"] == ["notes.md"]
    assert before_spawn["label"] == f"annotated-{run_id[:8]}"


def test_accept_without_a_prompt_seeds_no_message(client, held_runs):
    runner, _ = install_runner(generator="silent", runner="silent")
    cell_id = _cell(client)
    run_id = _start(client, runner, cell_id)
    wait_spawned(run_id)
    context = _context(client, cell_id, run_id)
    assert context is not None and context["messages"] == [] and context["sealed"] is False


def test_a_failing_context_insert_leaves_no_run_row(client, held_runs, monkeypatch):
    def broken(*_args):
        raise RuntimeError("no context")
    monkeypatch.setattr(store, "create_run_context", broken)
    runner, _ = install_runner(generator="broken", runner="broken")
    cell_id = _cell(client)
    response = client.post(f"/api/channels/{client.channel_id}/runs", json={"runner": runner, "cell_id": cell_id})
    assert response.status_code == 500
    conn = get_db_connection(client.channel_id)
    try:
        assert conn.execute("SELECT COUNT(*) FROM runs WHERE kind='generator'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM annotation_contexts").fetchone()[0] == 0
    finally:
        conn.close()


def test_a_refused_start_seals_its_context_and_rings(client, monkeypatch):
    import events
    import process_manager.manager as pm
    seen = []
    real = events.broadcast
    monkeypatch.setattr(events, "broadcast", lambda channel, event: (seen.append(event), real(channel, event)))

    async def refuse(_context, _owner):
        raise RuntimeError("no leases today")
    monkeypatch.setattr(pm, "retain_account_leases", refuse)
    runner, _ = install_runner(generator="refused", runner="refused", definition={"prompt": "go"})
    cell_id = _cell(client)
    run_id = _start(client, runner, cell_id)
    wait_spawned(run_id)
    assert run_row(client.channel_id, run_id)["status"] == "failed"
    context = _context(client, cell_id, run_id)
    assert context["sealed"] is True
    assert {"type": "annotations_changed", "cell_id": cell_id, "context_id": run_id,
            "rev": context["rev"]} in seen
    assert json.loads(_log_artifacts(client.channel_id, run_id)["annotations.json"]) == context


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not available on PATH; this drives a real `uv run` generator")
def test_run_end_seals_after_the_inflight_wait_and_records_annotations_json(client):
    runner, _ = install_runner(generator="ended", runner="ended", definition={"prompt": "Say hi"})
    cell_id = _cell(client)
    run_id = _start(client, runner, cell_id)
    wait_spawned(run_id)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        context = _context(client, cell_id, run_id)
        if context["sealed"] and "annotations.json" in _log_artifacts(client.channel_id, run_id):
            break
        time.sleep(0.1)
    else:
        raise AssertionError("the run's context was never sealed")
    assert json.loads(_log_artifacts(client.channel_id, run_id)["annotations.json"]) == context
    assert [m["kind"] for m in context["messages"]] == ["prompt"]
    conn = get_db_connection(client.channel_id)
    try:
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(store.AnnotationRefused) as refused:
            store.run_post(conn, cell_id, run_id, label="x", message_id=store.mint_id(), body="late",
                           reply_to=None, operation_id="late", digest="d", live={run_id})
        conn.rollback()
    finally:
        conn.close()
    assert refused.value.code == "sealed"


def _seeded(channel, cell_id, run_id, *, prompt="", status="running"):
    conn = get_db_connection(channel)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO runs (id, prompt, status, runner_id) VALUES (?, ?, ?, 'synth')",
                     (run_id, prompt, status))
        store.create_run_context(conn, cell_id, run_id)
        if prompt:
            store.seed_prompt(conn, cell_id, run_id, prompt)
        conn.commit()
    finally:
        conn.close()


def test_a_context_without_messages_records_no_artifact(client):
    from process_manager.finalization import commit_run_annotations
    run_id = "33333333-0000-4000-8000-000000000003"
    _seeded(client.channel_id, "cell-x", run_id)
    bells = commit_run_annotations(client.channel_id, run_id)
    assert [(b.context_id, b.rev) for b in bells] == [(run_id, 1)]
    assert _context(client, "cell-x", run_id)["sealed"] is True
    assert "annotations.json" not in _log_artifacts(client.channel_id, run_id)
    assert commit_run_annotations(client.channel_id, run_id) == []
    assert commit_run_annotations(client.channel_id, "no-such-run") == []


def test_recover_interrupted_runs_seals_stale_contexts(client):
    from process_manager.finalization import recover_interrupted_runs
    stale = "44444444-0000-4000-8000-000000000004"
    _seeded(client.channel_id, "cell-y", stale, prompt="before the crash")
    recover_interrupted_runs()
    context = _context(client, "cell-y", stale)
    assert context["sealed"] is True
    assert run_row(client.channel_id, stale)["status"] == "interrupted"
    assert json.loads(_log_artifacts(client.channel_id, stale)["annotations.json"]) == context


def test_a_generator_log_artifact_named_annotations_json_is_dropped(tmp_path):
    from process_manager.artifacts import ArtifactsMixin

    stored: list = []

    class Run(ArtifactsMixin):
        workspace_dir = str(tmp_path)

        def _store_log_artifacts(self, entries):
            stored.extend(path for path, _local in entries)
    (tmp_path / "annotations.json").write_text("{}")
    (tmp_path / "logs.txt").write_text("forged")
    (tmp_path / "trace.log").write_text("ok")
    run = Run()
    run.log_buffer = []
    files = run._hash_artifacts({"artifacts": [
        {"type": "workspace", "cell_path": name, "category": "log"}
        for name in ("annotations.json", "logs.txt", "trace.log")]})
    assert files == {} and stored == ["trace.log"]
    assert [entry["payload"] for entry in run.log_buffer] == [
        f"[update_cell] dropped log artifact {name!r}: reserved for the host's run record"
        for name in ("annotations.json", "logs.txt")]


def test_runs_active_rows_carry_listening(client, monkeypatch):
    from routes.runs import queries
    run_id = "55555555-0000-4000-8000-000000000005"
    _seeded(client.channel_id, "cell-z", run_id)
    manager = FakeManager(channel=client.channel_id, run_id=run_id, cell_id="cell-z", label="synth-55555555")
    monkeypatch.setattr(queries, "active_managers", {run_id: manager})

    def listening():
        (row,) = asyncio.run(queries.list_active_runs(client.channel_id))
        return row["listening"]
    assert listening() is False
    conn = get_db_connection(client.channel_id)
    try:
        conn.execute("BEGIN IMMEDIATE")
        store.mark_listening(conn, "cell-z", run_id)
        conn.commit()
    finally:
        conn.close()
    assert listening() is True
