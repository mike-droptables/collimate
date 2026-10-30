"""Run provenance: generator source-version stamping and input_0 edges."""
from __future__ import annotations

import json
import shutil
import time
import uuid

import pytest

from database import get_db_connection


@pytest.fixture
def channel(fresh_cwd, monkeypatch):
    from models import Channel
    from state import state, flush_persist_state
    registered = Channel(name='Provenance')
    from genesis import ensure_capabilities_stack
    ensure_capabilities_stack(registered)
    monkeypatch.setattr(state, 'channels', [registered])
    monkeypatch.setattr(state, 'active_channel_id', registered.id)
    get_db_connection(str(registered.id)).close()
    yield str(registered.id)
    flush_persist_state()


@pytest.fixture
def client(api_app):
    from fastapi.testclient import TestClient
    from models import Channel
    from state import state, persist_state, flush_persist_state
    with TestClient(api_app) as client:
        registered = Channel(name='Provenance')
        from genesis import ensure_capabilities_stack
        ensure_capabilities_stack(registered)
        state.channels = [registered]
        state.active_channel_id = registered.id
        get_db_connection(str(registered.id)).close()
        persist_state()
        flush_persist_state()
        yield client


def _channel_id():
    from state import state
    return str(state.active_channel_id)


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

def test_runs_table_has_provenance_columns(channel):
    conn = get_db_connection(channel)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(runs)").fetchall()}
    conn.close()
    assert {"generator_name", "generator_cell_id"} <= cols
    # Saved runner identity, frozen source version, and complete run snapshot.
    assert {"runner_id", "runner_cell_id", "runner_snapshot"} <= cols
    assert "relay_id" not in cols and "relay_tag" not in cols


# ---------------------------------------------------------------------------
# CellsMixin stamping (synthetic registered manager and ids)
# ---------------------------------------------------------------------------

def _manager(channel: str, workspace, *, gen_version: str | None, input_tip: str | None):
    from process_manager import ProcessManager
    from runner_permissions import UNRESTRICTED

    m = ProcessManager(channel=channel, run_id=str(uuid.uuid4()), app_dir=str(workspace),
                       workspace_dir=str(workspace), entrypoint='unused.py', generator_name='synth_gen',
                       cell_id=str(uuid.uuid4()), permissions=UNRESTRICTED,
                       generator_cell_version_id=gen_version, input_tip_id=input_tip)
    m._broadcast_run_events = lambda events: None
    m._broadcast_dag_commit = lambda *args: None
    return m


def _commit(manager, name):
    return manager._update_cell({'operation_id': str(uuid.uuid4()), 'writer_id': manager.run_id,
        'edit_seq': 1, 'base_revisions': {'out.txt': 0}, 'name': name,
        'artifacts': [{'type': 'memory', 'cell_path': 'out.txt', 'content': name}]})


def _seed_run_row(channel: str, run_id: str):
    conn = get_db_connection(channel)
    conn.execute("INSERT INTO runs (id, prompt, status) VALUES (?, '', 'running')", (run_id,))
    conn.commit()
    conn.close()


def _seed_cells_rows(channel: str, run_id: str, ids: list[str]):
    """Real cells rows for provenance targets — FKs are enforced, and the
    write path validates references against actual rows."""
    conn = get_db_connection(channel)
    for i, cid in enumerate(ids):
        conn.execute(
            "INSERT INTO cells (id, run_id, generator_name, sequence_num, "
            "version_name, source_type) "
            "VALUES (?, ?, 'genesis', ?, 'Initial', 'genesis')",
            (cid, run_id, 100 + i),
        )
    conn.commit()
    conn.close()


def _place_target(manager):
    """A running writer owns an actual canvas cell, not only a history row."""
    from models import Cell, Stack
    from state import state
    channel = next(ch for ch in state.channels if str(ch.id) == manager.channel)
    channel.main_stacks = [Stack(cells=[Cell(id=manager.cell_id)])]


def test_versioned_commit_stamps_and_walkers_ignore_input_edges(channel, tmp_path):
    from versions import find_tip

    gen_version = str(uuid.uuid4())
    input_tip = str(uuid.uuid4())
    m = _manager(channel, tmp_path, gen_version=gen_version, input_tip=input_tip)
    _seed_run_row(channel, m.run_id)
    _seed_cells_rows(channel, m.run_id, [gen_version, input_tip, m.cell_id])
    _place_target(m)

    # A commit mints a fresh version row on the cell's chain.
    _commit(m, 'v1')
    conn = get_db_connection(channel)
    v1 = conn.execute(
        "SELECT id, generator_cell_id FROM cells WHERE run_id=? AND version_name='v1'",
        (m.run_id,),
    ).fetchone()
    assert v1["generator_cell_id"] == gen_version
    edge = conn.execute(
        "SELECT parent_cell_id FROM cell_edges WHERE child_cell_id=? AND parent_key='input_0'",
        (v1["id"],),
    ).fetchone()
    assert edge["parent_cell_id"] == input_tip
    conn.close()

    # Version-tip walking is inert to the provenance edges: the tip of the
    # cell's chain is v1 (via previous_version), and the input's own
    # "chain" is untouched (its tip stays itself — input_0 is not a version).
    conn = get_db_connection(channel)
    assert find_tip(conn, m.cell_id) == v1["id"]
    assert find_tip(conn, input_tip) == input_tip
    conn.close()


def test_modify_run_version_edge_wins_over_input_edge(channel, tmp_path):
    """A run on a cell hydrates from that same cell (input_0), so the input
    tip equals the previous_version parent. cell_edges' (parent, child)
    primary key admits one edge per pair — the previous_version edge must
    claim it (walkers filter on it; an input_0 there would sever the version
    chain and make the modify output invisible to history + hydration)."""
    from versions import find_tip

    gen_version = str(uuid.uuid4())
    target = str(uuid.uuid4())  # the cell being modified = input 0 = chain tip
    m = _manager(channel, tmp_path, gen_version=gen_version, input_tip=target)
    m.cell_id = target
    _seed_run_row(channel, m.run_id)
    _seed_cells_rows(channel, m.run_id, [gen_version, target])
    _place_target(m)

    _commit(m, 'v2')

    conn = get_db_connection(channel)
    edge = conn.execute(
        "SELECT parent_key, child_cell_id FROM cell_edges WHERE parent_cell_id=?",
        (target,),
    ).fetchone()
    new_tip = find_tip(conn, target)
    conn.close()

    assert edge["parent_key"] == "previous_version"
    assert new_tip == edge["child_cell_id"] != target


def test_dangling_provenance_ids_are_dropped_not_fatal(channel, tmp_path):
    """Provenance must never break an emit: ids without backing cells rows
    (legacy callers, rows deleted mid-run) stamp as NULL / skip their edge
    instead of tripping the enforced foreign keys."""
    m = _manager(
        channel, tmp_path,
        gen_version=str(uuid.uuid4()),   # no cells row behind it
        input_tip=str(uuid.uuid4()),  # ditto
    )
    _seed_run_row(channel, m.run_id)
    _seed_cells_rows(channel, m.run_id, [m.cell_id])
    _place_target(m)

    _commit(m, 'v1')

    conn = get_db_connection(channel)
    row = conn.execute(
        "SELECT id, generator_cell_id FROM cells WHERE run_id=? AND version_name='v1'",
        (m.run_id,),
    ).fetchone()
    # Only the version edge lands; the dangling input tip stamps no input_0 edge.
    n_input_edges = conn.execute(
        "SELECT COUNT(*) FROM cell_edges WHERE child_cell_id=? AND parent_key LIKE 'input_%'",
        (row["id"],),
    ).fetchone()[0]
    conn.close()
    assert row["generator_cell_id"] is None
    assert n_input_edges == 0


@pytest.mark.parametrize('body', [None, '', 'accepted input'])
def test_freeze_synthesizes_typed_genesis_for_never_versioned_input(channel, body):
    """Provenance freezes accepted current bytes, including empty inputs."""
    from versions import freeze_cell_versions, version_file_entries
    from persistence import initialize_files

    run_id = str(uuid.uuid4())
    _seed_run_row(channel, run_id)
    cell_id = str(uuid.uuid4())

    if body is not None:
        from models import VirtualFile
        with get_db_connection(channel) as conn:
            initialize_files(conn, cell_id, [VirtualFile(name='input.txt', content=body, language='plaintext')])
    resolved = freeze_cell_versions(channel, run_id, [cell_id])
    assert resolved[cell_id] == cell_id

    conn = get_db_connection(channel)
    row = conn.execute(
        "SELECT source_type, version_name FROM cells WHERE id=?", (cell_id,),
    ).fetchone()
    entries = version_file_entries(conn, cell_id)
    if body is None:
        assert entries == []
    else:
        from cas import read_blob
        assert len(entries) == 1 and entries[0]['encoding'] == 'utf-8'
        assert read_blob(entries[0]['body_hash']) == body.encode()
    conn.close()
    assert row is not None
    assert row["version_name"] == "Initial"


# ---------------------------------------------------------------------------
# E2E — real generator subprocess
# ---------------------------------------------------------------------------

e2e = pytest.mark.skipif(
    shutil.which("uv") is None,
    reason="uv not available on PATH; provenance E2E needs it",
)


def _install_runner_gen(name: str, body: str, definition: dict | None = None) -> tuple[str, str, str]:
    """Install a generator and saved runner; return ids and committed source tip."""
    from conftest import install_runner
    from state import state
    from versions import find_tip

    runner_id, gen_id = install_runner(
        generator=name, runner=name, entrypoint_body=body, definition=definition,
    )
    ch = next(c for c in state.channels if c.id == state.active_channel_id)
    for stack in ch.nav_stacks:
        for cell in stack.cells:
            if any(file.name == f"{name}/choice.generator" for file in cell.files):
                conn = get_db_connection(_channel_id())
                try:
                    source_version = find_tip(conn, str(cell.id))
                finally:
                    conn.close()
                return runner_id, gen_id, source_version
    raise AssertionError(f"runner fixture cell for {name!r} not found")


def _install_input_cell(name: str) -> str:
    from state import state, persist_state, flush_persist_state
    from models import VirtualFile, Cell, Stack
    from persistence import initialize_files

    ch = next(c for c in state.channels if c.id == state.active_channel_id)
    cell = Cell(
        id=uuid.uuid4(), name=name,
        files=[VirtualFile(name="seed.txt", content="synthetic input", language="text")],
    )
    with get_db_connection(str(ch.id)) as conn:
        initialize_files(conn, str(cell.id), cell.files)
    ch.main_stacks.append(Stack(name=f"{name}Stack", cells=[cell], stack_type="standard"))
    persist_state()
    flush_persist_state()
    return str(cell.id)


def _run_to_terminal(client, runner: str, input_cell_id: str,
                     timeout_s: float = 90.0) -> tuple[str, str]:
    """A runner run on ``input_cell_id``, which it writes in place.
    Returns (run_id, cell_id)."""
    r = client.post(
        f"/api/channels/{_channel_id()}/runs",
        json={"runner": runner, "cell_id": input_cell_id},
    )
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    deadline = time.monotonic() + timeout_s
    last: dict = {}
    while time.monotonic() < deadline:
        ev = client.get(f"/api/channels/{_channel_id()}/runs/{run_id}/events_since?since_id=0")
        assert ev.status_code == 200, ev.text
        last = ev.json()
        if last.get("is_terminal"):
            break
        time.sleep(0.2)
    assert last.get("is_terminal"), f"run never terminated: {last.get('status')}"
    return run_id, input_cell_id


_SIMPLE = (
    "# /// script\n# requires-python = \">=3.11\"\n# dependencies = []\n# ///\n"
    "import gen\n"
    "async def run():\n"
    "    gen.update_file('out.txt', 'done')\n"
    "    await gen.commit()\n"
    "if __name__ == '__main__':\n"
    "    gen.main(run)\n"
)


@e2e
def test_run_provenance_stamps_end_to_end(client):
    # A single in-place runner run rewrites its one input cell. The runner and
    # its target generator share one cell, so a run freezes BOTH to that cell.
    runner_id, gen_name, gen_cell_id = _install_runner_gen("prov_simple", _SIMPLE)
    input_id = _install_input_cell("ProvInput")

    run_id, cell_id = _run_to_terminal(client, runner_id, input_id)
    # cell_id == input_id — the run rewrote the cell in place.

    conn = get_db_connection(_channel_id())
    run_row = conn.execute(
        "SELECT generator_name, generator_cell_id, "
        "runner_id, runner_cell_id, kind, target_cell_id "
        "FROM runs WHERE id=?",
        (run_id,),
    ).fetchone()
    # The run committed onto the input cell's version chain — every native
    # emit (the first included) mints a new version row, so the output lives
    # at the chain TIP while the pre-run version stays immutable.
    from versions import find_tip
    tip = find_tip(conn, input_id)
    from versions import tip_file_map
    files = set(tip_file_map(conn, tip))
    conn.close()

    assert run_row["generator_name"] == gen_name
    # The end-to-end provenance stamp: the run pinned the generator's exact
    # SOURCE VERSION created when the complete accepted runner source was saved.
    assert run_row["generator_cell_id"] == gen_cell_id
    # Runner provenance: which runner ran, and its frozen source version —
    # the SAME cell/version as the generator (they share a cell).
    assert run_row["runner_id"] == runner_id
    assert run_row["runner_cell_id"] == gen_cell_id
    # Run-node stamps: a real runner run is kind='generator' and records the
    # cell it ran ON — the durable run→cell link the history rail groups by.
    assert run_row["kind"] == "generator"
    assert run_row["target_cell_id"] == cell_id

    # Config was captured at kickoff (run.yaml is always present) and the
    # run log was projected into run_artifacts at finalization. The log may
    # land a beat after terminal status (finalization runs off the wait
    # task), so poll briefly.
    conn2 = get_db_connection(_channel_id())
    try:
        cfg = conn2.execute(
            "SELECT 1 FROM run_artifacts WHERE run_id=? AND category='config' "
            "AND filepath='run.yaml'", (run_id,),
        ).fetchone()
        assert cfg, "run.yaml config artifact missing"
        deadline = time.monotonic() + 15.0
        log_row = None
        while time.monotonic() < deadline and log_row is None:
            log_row = conn2.execute(
                "SELECT 1 FROM run_artifacts WHERE run_id=? AND category='log' "
                "AND filepath='logs.txt'", (run_id,),
            ).fetchone()
            if log_row is None:
                time.sleep(0.2)
        assert log_row, "logs.txt artifact never committed"
    finally:
        conn2.close()

    assert "out.txt" in files

    # /history on the in-place cell resolves, and the run got a RUN node
    # (kind='generator' + target stamp -> the grouping the rail renders)
    # carrying its config and, once finalization lands, its log.
    v = client.get(f"/api/channels/{_channel_id()}/cells/{cell_id}/history")
    assert v.status_code == 200, v.text
    h = v.json()
    run_nodes = [n for n in h["nodes"] if n["kind"] == "run"]
    ours = [n for n in run_nodes if n["run"]["run_id"] == run_id]
    assert len(ours) == 1, h["nodes"]
    node = ours[0]
    assert node["node_id"] == f"run:{run_id}:{node['versions'][0]['cell_id']}"
    assert any(c["path"] == "run.yaml" for c in node["config"])
    assert node["versions"], "the run's emitted version groups under its node"


# ---------------------------------------------------------------------------
# Runner attribution the journal and the shelf read (cell_lineage.runners_of):
# the runner's id from the run row, its name from the run's own snapshot.
# ---------------------------------------------------------------------------

def test_runner_attribution_reads_the_run_rows_snapshot_name_O02(client):
    from cell_lineage import runners_of

    run_id = str(uuid.uuid4())
    cell_id = str(uuid.uuid4())
    snapshot = {
        "runner": "demo-runner",
        "name": "Capture a website",
        "generator": "demo-gen",
        "folder_hashes": {"folder_a": {"grab.js": "a" * 64}},
    }
    conn = get_db_connection(_channel_id())
    # runs.runner_cell_id → cells(id) and cells.run_id → runs(id) are a circular
    # FK; insert the run first (runner_cell_id NULL), then the cell, then point
    # runner_cell_id at it.
    conn.execute(
        "INSERT INTO runs (id, prompt, status, runner_id, "
        "runner_snapshot, generator_name) "
        "VALUES (?, 'go', 'completed', ?, ?, 'demo-gen')",
        (run_id, "demo-runner", json.dumps(snapshot)),
    )
    conn.execute(
        "INSERT INTO cells (id, run_id, generator_name, sequence_num, "
        "version_name, source_type) "
        "VALUES (?, ?, 'demo-gen', 0, 'Capture', 'generator')",
        (cell_id, run_id),
    )
    conn.execute("UPDATE runs SET runner_cell_id=? WHERE id=?", (cell_id, run_id))
    conn.commit()
    try:
        assert runners_of(conn, [run_id]) == {
            run_id: {"runner_id": "demo-runner", "runner_name": "Capture a website"},
        }
    finally:
        conn.close()


def test_a_system_run_has_no_runner_attribution_O02(client):
    from cell_lineage import runners_of

    run_id = str(uuid.uuid4())
    conn = get_db_connection(_channel_id())
    conn.execute(
        "INSERT INTO runs (id, prompt, status, generator_name) "
        "VALUES (?, '', 'completed', 'system')",
        (run_id,),
    )
    conn.commit()
    try:
        assert runners_of(conn, [run_id]) == {run_id: {"runner_id": None, "runner_name": None}}
    finally:
        conn.close()
