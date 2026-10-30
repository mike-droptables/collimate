"""The run-node data model (channel migration v5): ``runs.kind`` /
``runs.target_cell_id`` / ``run_artifacts``, plus the retention paths that
must respect the new table — the CAS GC live set and channel-delete
refcounting live in their own suites; this file pins the schema itself and
the GC integration."""

import os
import uuid


def _channel_id(label):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, 'https://collimate.test/channels/' + label))


def _body_hash(body):
    from cas import store_bytes
    return store_bytes(body.encode('utf-8'))


def _blob(conn, h):
    conn.execute("INSERT OR IGNORE INTO blobs (hash, size) VALUES (?, 4)", (h,))


def _artifact(conn, run_id, category, filepath, h):
    _blob(conn, h)
    conn.execute(
        "INSERT INTO run_artifacts (run_id, category, filepath, blob_hash) "
        "VALUES (?, ?, ?, ?)",
        (run_id, category, filepath, h),
    )


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def test_v5_schema_present(fresh_cwd):
    from database import get_db_connection

    conn = get_db_connection(_channel_id("schema_ch"))
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(runs)")}
        assert "kind" in cols
        assert "target_cell_id" in cols
        tables = {
            r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "run_artifacts" in tables
        # kind defaults to 'system' — bookkeeping INSERTs that don't name it
        # never read as generator runs.
        conn.execute(
            "INSERT INTO runs (id, prompt, status) VALUES ('r-sys', 'checkpoint', 'completed')"
        )
        row = conn.execute("SELECT kind FROM runs WHERE id='r-sys'").fetchone()
        assert row["kind"] == "system"
    finally:
        conn.close()


def test_run_artifacts_category_check(fresh_cwd):
    import sqlite3

    from database import get_db_connection

    conn = get_db_connection(_channel_id("check_ch"))
    try:
        conn.execute("INSERT INTO runs (id, prompt, status) VALUES ('r1', '', 'completed')")
        _blob(conn, _body_hash("a"))
        conn.execute(
            "INSERT INTO run_artifacts (run_id, category, filepath, blob_hash) "
            "VALUES ('r1', 'config', 'prompt.md', ?)",
            (_body_hash("a"),),
        )
        try:
            conn.execute(
                "INSERT INTO run_artifacts (run_id, category, filepath, blob_hash) "
                "VALUES ('r1', 'file', 'x', ?)",
                (_body_hash("a"),),
            )
            raised = False
        except sqlite3.IntegrityError:
            raised = True
        assert raised, "category CHECK must reject anything but config/log"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CAS GC integration
# ---------------------------------------------------------------------------

def test_gc_live_set_includes_run_artifact_blobs(fresh_cwd):
    from cas_gc import _live_blobs_in_channel
    from database import get_db_connection

    conn = get_db_connection(_channel_id("gc_ch"))
    try:
        conn.execute(
            "INSERT INTO runs (id, prompt, status, kind, target_cell_id) "
            "VALUES ('r1', 'p', 'completed', 'generator', 'cell-A')"
        )
        h = _body_hash("d")
        _artifact(conn, "r1", "config", "prompt.md", h)
        conn.commit()

        live = _live_blobs_in_channel(conn)
        assert h in live, "config/log blobs must be in the GC live set"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Node discriminator backfill
# ---------------------------------------------------------------------------

def test_v5_backfill_marks_runner_runs_and_drops_reserved_rows(fresh_cwd, tmp_path):
    """Drive the v5 backfill against a DB frozen at v4: a runner run gains
    kind='generator', and legacy .collreserved cell_files rows are dropped."""
    import sqlite3

    from migrations import CHANNEL_MIGRATIONS, apply

    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        # Bring the DB to v4, then seed pre-v5 shapes: a runner run whose
        # chain is root(manual_edit) -> v1(generator emit), PLUS a JIT-flush
        # row on the generator SOURCE cell under the same run_id (which must
        # NOT win the target backfill), and a bookkeeping checkpoint run.
        apply(conn, CHANNEL_MIGRATIONS[:4])
        conn.execute(
            "INSERT INTO runs (id, prompt, status, runner_id) "
            "VALUES ('r-runner', 'p', 'completed', 'one-shot-runner')"
        )
        conn.execute(
            "INSERT INTO runs (id, prompt, status) VALUES ('r-ckpt', 'checkpoint', 'completed')"
        )
        for cid, seq, st in (
            ("root", 90, "manual_edit"),     # the visible canvas id
            ("v1", 1, "generator"),          # the run's emitted version
            ("src-jit", 91, "manual_edit"),  # JIT flush on the SOURCE cell
        ):
            conn.execute(
                "INSERT INTO cells (id, run_id, sequence_num, source_type) "
                "VALUES (?, 'r-runner', ?, ?)",
                (cid, seq, st),
            )
        conn.execute(
            "INSERT INTO cell_edges (parent_cell_id, child_cell_id, parent_key) "
            "VALUES ('root', 'v1', 'previous_version')"
        )
        conn.execute("INSERT INTO blobs (hash, size) VALUES (?, 1)", (_body_hash("e"),))
        conn.execute(
            "INSERT INTO cell_files (cell_id, filepath, blob_hash) VALUES "
            "('v1', '.collreserved/logs.txt', ?)", (_body_hash("e"),),
        )
        conn.execute(
            "INSERT INTO cell_files (cell_id, filepath, blob_hash) VALUES "
            "('v1', 'main.py', ?)", (_body_hash("e"),),
        )
        conn.commit()

        apply(conn, CHANNEL_MIGRATIONS[:5])

        rows = {
            r["id"]: (r["kind"], r["target_cell_id"])
            for r in conn.execute("SELECT id, kind, target_cell_id FROM runs")
        }
        assert rows["r-runner"] == ("generator", "root"), rows
        assert rows["r-ckpt"][0] == "system"
        paths = {
            r["filepath"] for r in conn.execute("SELECT filepath FROM cell_files")
        }
        assert paths == {"main.py"}
    finally:
        conn.close()


def test_create_run_target_stamp_shape(fresh_cwd):
    """The INSERT-side contract: kind='generator' + target_cell_id land in
    the same statement as the run row (the durable run→cell link must exist
    before any emit — a crash-before-first-commit run still resolves)."""
    from database import get_db_connection

    run_id = str(uuid.uuid4())
    conn = get_db_connection(_channel_id("stamp_ch"))
    try:
        conn.execute(
            "INSERT INTO runs (id, prompt, status, phase, generator_name, "
            "runner_id, kind, target_cell_id) "
            "VALUES (?, 'p', 'running', 'queued', 'g', 'runner', "
            "'generator', 'cell-X')",
            (run_id,),
        )
        row = conn.execute(
            "SELECT kind, target_cell_id FROM runs WHERE id=?", (run_id,)
        ).fetchone()
        assert row["kind"] == "generator"
        assert row["target_cell_id"] == "cell-X"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Mid-run settings updates land as Config artifacts
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# /history — run-node grouping
# ---------------------------------------------------------------------------

def _seed_history_fixture(conn, visible):
    """root(genesis) -> v1,v2 (run A, generator, targets `visible`)
    -> v3 (checkpoint/system). Plus run B: generator, targets `visible`,
    zero versions (crashed before first emit) with a log artifact. Plus
    run C: generator, targets a DIFFERENT cell — its JIT-flush version on
    this chain must group as a files node, not a run node."""
    conn.execute(
        "INSERT INTO runs (id, prompt, status, kind, target_cell_id) "
        "VALUES ('runA', 'do the thing', 'completed', 'generator', ?)",
        (visible,),
    )
    conn.execute(
        "INSERT INTO runs (id, prompt, status) VALUES ('ckpt', 'checkpoint', 'completed')"
    )
    conn.execute(
        "INSERT INTO runs (id, prompt, status, kind, target_cell_id, created_at) "
        "VALUES ('runB', 'crashed', 'failed', 'generator', ?, '2999-01-01 00:00:00')",
        (visible,),
    )
    conn.execute(
        "INSERT INTO runs (id, prompt, status, kind, target_cell_id) "
        "VALUES ('runC', 'other target', 'completed', 'generator', 'elsewhere')"
    )
    rows = [
        (visible, "ckpt", 90, "root", "genesis"),
        ("v1", "runA", 1, "first emit", "generator"),
        ("v2", "runA", 2, "second emit", "generator"),
        ("vC", "runC", 1, "jit flush", "manual_edit"),
        ("v3", "ckpt", 1, "Manual Edit", "manual_edit"),
    ]
    for cid, rid, seq, name, st in rows:
        conn.execute(
            "INSERT INTO cells (id, run_id, generator_name, sequence_num, "
            "version_name, source_type) VALUES (?, ?, 'g', ?, ?, ?)",
            (cid, rid, seq, name, st),
        )
    for parent, child in ((visible, "v1"), ("v1", "v2"), ("v2", "vC"), ("vC", "v3")):
        conn.execute(
            "INSERT INTO cell_edges (parent_cell_id, child_cell_id, parent_key) "
            "VALUES (?, ?, 'previous_version')",
            (parent, child),
        )
    _artifact(conn, "runA", "config", "run.yaml", _body_hash("1"))
    _artifact(conn, "runA", "log", "logs.txt", _body_hash("2"))
    _artifact(conn, "runB", "log", "logs.txt", _body_hash("3"))
    conn.commit()


def test_history_groups_run_nodes_files_nodes_and_zero_version_runs(client):
    from database import get_db_connection

    visible = "hist-cell"
    conn = get_db_connection(client.channel_id)
    try:
        _seed_history_fixture(conn, visible)
    finally:
        conn.close()

    r = client.get(f"/api/channels/{client.channel_id}/cells/{visible}/history")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_versions"] == 5

    by_id = {n["node_id"]: n for n in body["nodes"]}
    # Run A: one node holding BOTH its versions, with config + log. A run
    # node's id names its first version (runs may interleave on a cell).
    a = by_id["run:runA:v1"]
    assert a["kind"] == "run"
    assert [v["cell_id"] for v in a["versions"]] == ["v1", "v2"]
    assert any(c["path"] == "run.yaml" for c in a["config"])
    assert any(l["path"] == "logs.txt" for l in a["logs"])
    assert a["run"]["prompt"] == "do the thing"

    # Run B: zero versions, still a node, log present.
    b = by_id["run:runB"]  # no version yet: the bare run id
    assert b["versions"] == []
    assert any(l["path"] == "logs.txt" for l in b["logs"])
    assert b["run"]["status"] == "failed"

    # Run C targeted a different cell: its version here is a FILES node
    # entry, and no run:runC node exists.
    assert not any(nid.startswith("run:runC") for nid in by_id)
    files_nodes = [n for n in body["nodes"] if n["kind"] == "files"]
    files_versions = [v["cell_id"] for n in files_nodes for v in n["versions"]]
    assert "vC" in files_versions
    # The root + the checkpoint are files-node versions too.
    assert visible in files_versions and "v3" in files_versions

    # vC and v3 are consecutive non-run versions -> ONE files node.
    vc_node = next(n for n in files_nodes if "vC" in [v["cell_id"] for v in n["versions"]])
    assert [v["cell_id"] for v in vc_node["versions"]] == ["vC", "v3"]

    # Newest-first ordering: the zero-version run B carries the newest
    # timestamp so it heads the rail — while `current` still points at the
    # node holding the chain TIP (v3), not at the head node.
    assert body["nodes"][0]["node_id"] == "run:runB"
    assert body["current"]["node_id"] == vc_node["node_id"]
    assert body["current"]["version_cell_id"] == "v3"


def test_history_splits_interleaved_runs_into_distinct_nodes(client):
    """Two runs live on one cell commit in turn (A, B, A, B): each maximal
    stretch of one run's versions is its own node, and every node id is
    unique (``run:<run id>:<first version id>``) so the frontend's selection
    keys never collide."""
    from database import get_db_connection

    visible = "interleaved-cell"
    conn = get_db_connection(client.channel_id)
    try:
        for rid in ("runX", "runY"):
            conn.execute(
                "INSERT INTO runs (id, prompt, status, kind, target_cell_id) "
                "VALUES (?, '', 'running', 'generator', ?)",
                (rid, visible),
            )
        conn.execute(
            "INSERT INTO runs (id, prompt, status) VALUES ('seed', 'genesis', 'completed')"
        )
        chain = [
            (visible, "seed", "genesis"), ("x1", "runX", "generator"),
            ("y1", "runY", "generator"), ("x2", "runX", "generator"),
            ("x3", "runX", "generator"), ("y2", "runY", "generator"),
        ]
        for seq, (cid, rid, st) in enumerate(chain):
            conn.execute(
                "INSERT INTO cells (id, run_id, generator_name, sequence_num, "
                "version_name, source_type) VALUES (?, ?, 'g', ?, ?, ?)",
                (cid, rid, seq, cid, st),
            )
        for (parent, _r, _s), (child, _r2, _s2) in zip(chain, chain[1:]):
            conn.execute(
                "INSERT INTO cell_edges (parent_cell_id, child_cell_id, parent_key) "
                "VALUES (?, ?, 'previous_version')",
                (parent, child),
            )
        conn.commit()
    finally:
        conn.close()

    body = client.get(f"/api/channels/{client.channel_id}/cells/{visible}/history").json()
    runs = [n for n in body["nodes"] if n["kind"] == "run"]
    assert [n["node_id"] for n in runs] == [
        "run:runY:y2", "run:runX:x2", "run:runY:y1", "run:runX:x1",
    ]
    assert [[v["cell_id"] for v in n["versions"]] for n in runs] == [
        ["y2"], ["x2", "x3"], ["y1"], ["x1"],
    ]
    ids = [n["node_id"] for n in body["nodes"]]
    assert len(ids) == len(set(ids))
    assert body["current"] == {"node_id": "run:runY:y2", "version_cell_id": "y2"}


def test_history_run_node_carries_the_runner_name_it_started_under_O09(client):
    """A run node's ``run`` names its runner by the name in the run's
    ``runner_snapshot`` (the name it started under), next to the id; a run
    with no snapshot sends ``runner_name: None``."""
    import json
    from database import get_db_connection

    visible = "named-runner-cell"
    conn = get_db_connection(client.channel_id)
    try:
        conn.execute(
            "INSERT INTO runs (id, prompt, status, kind, target_cell_id, runner_id, "
            "runner_snapshot) VALUES ('runN', '', 'completed', 'generator', ?, 'r-1', ?)",
            (visible, json.dumps({"name": "Draft writer"})),
        )
        conn.execute(
            "INSERT INTO runs (id, prompt, status, kind, target_cell_id, runner_id, created_at) "
            "VALUES ('runM', '', 'failed', 'generator', ?, 'r-2', '2999-01-01 00:00:00')",
            (visible,),
        )
        conn.execute(
            "INSERT INTO cells (id, run_id, generator_name, sequence_num, version_name, "
            "source_type) VALUES (?, 'runN', 'g', 0, 'v', 'generator')",
            (visible,),
        )
        conn.commit()
    finally:
        conn.close()

    body = client.get(f"/api/channels/{client.channel_id}/cells/{visible}/history").json()
    by_id = {n["node_id"]: n for n in body["nodes"]}
    named = by_id[f"run:runN:{visible}"]["run"]
    assert (named["runner_id"], named["runner_name"]) == ("r-1", "Draft writer")
    bare = by_id["run:runM"]["run"]
    assert (bare["runner_id"], bare["runner_name"]) == ("r-2", None)


def test_run_artifacts_endpoint(client):
    from database import get_db_connection

    conn = get_db_connection(client.channel_id)
    try:
        conn.execute(
            "INSERT INTO runs (id, prompt, status, kind, target_cell_id) "
            "VALUES ('runD', '', 'completed', 'generator', 'cellD')"
        )
        _artifact(conn, "runD", "config", "prompt.md", _body_hash("4"))
        _artifact(conn, "runD", "log", "logs.txt", _body_hash("5"))
        conn.commit()
    finally:
        conn.close()

    r = client.get(f"/api/channels/{client.channel_id}/runs/runD/artifacts")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["run_id"] == "runD" and body["status"] == "completed"
    cats = {(a["category"], a["path"]) for a in body["artifacts"]}
    assert cats == {("config", "prompt.md"), ("log", "logs.txt")}

    assert client.get(f"/api/channels/{client.channel_id}/runs/nope/artifacts").status_code == 404


# ---------------------------------------------------------------------------
# Regression: accepted-file refresh and live-files read paths must be callable
# (their errors are swallowed into the run log at the call sites, so a broken
# import inside them is otherwise invisible to every suite).
# ---------------------------------------------------------------------------

def test_accepted_refresh_and_live_files_paths_are_callable(fresh_cwd, tmp_path, monkeypatch):
    import uuid as _uuid
    from collections import deque

    from database import get_db_connection
    from process_manager import ProcessManager
    from models import AppState, Cell, Channel, Stack, VirtualFile
    from persistence import initialize_files
    import state

    cell = Cell(files=[VirtualFile(name='kept.txt', content='tip content'),
                       VirtualFile(name='new.txt', content='accepted content')])
    source = Channel(name='Live reads', main_stacks=[Stack(cells=[cell])])
    from genesis import ensure_capabilities_stack
    ensure_capabilities_stack(source)
    monkeypatch.setattr(state, 'state', AppState(channels=[source], active_channel_id=source.id))
    channel = str(source.id)
    run_id = str(_uuid.uuid4())
    cell_id = str(cell.id)
    conn = get_db_connection(channel)
    conn.execute(
        "INSERT INTO runs (id, prompt, status, kind, target_cell_id) "
        "VALUES (?, '', 'running', 'generator', ?)", (run_id, cell_id),
    )
    conn.execute(
        "INSERT INTO cells (id, run_id, generator_name, sequence_num, "
        "version_name, source_type) VALUES (?, ?, 'g', 1, 'v', 'generator')",
        (cell_id, run_id),
    )
    initialize_files(conn, cell_id, cell.files)
    conn.commit()
    conn.close()

    m = ProcessManager.__new__(ProcessManager)
    m.label = "test-run"
    m.channel = channel
    m.run_id = run_id
    m.cell_id = cell_id
    m.workspace_dir = str(tmp_path / "ws")
    os.makedirs(m.workspace_dir, exist_ok=True)
    m.log_buffer = deque()

    # Live reads capture accepted SQL refs and decode their immutable bodies.
    files = m._compute_cell_live_files()
    assert files.get("kept.txt") == b"tip content"
    assert files.get("new.txt") == b"accepted content"

    m._flush_chat_files_to_workspace()
    assert os.path.isfile(os.path.join(m.workspace_dir, "kept.txt"))
    assert os.path.isfile(os.path.join(m.workspace_dir, "new.txt"))
    # No swallowed error left a complaint in the log buffer.
    assert not [e for e in m.log_buffer if "cannot import" in str(e)]


def test_accepted_tombstone_removes_the_file_from_live_files_and_workspace(
        fresh_cwd, tmp_path, monkeypatch):
    """An accepted deletion removes a clean workspace file and keeps its revision."""
    import uuid as _uuid
    from collections import deque

    from database import get_db_connection
    from process_manager import ProcessManager
    from models import AppState, Cell, Channel, Stack, VirtualFile
    from persistence import initialize_files, mutate_files
    import state

    cell = Cell(files=[VirtualFile(name='kept.txt', content='tip content'),
                       VirtualFile(name='gone.txt', content='old')])
    source = Channel(name='Live deletion', main_stacks=[Stack(cells=[cell])])
    from genesis import ensure_capabilities_stack
    ensure_capabilities_stack(source)
    monkeypatch.setattr(state, 'state', AppState(channels=[source], active_channel_id=source.id))
    channel = str(source.id)
    run_id = str(_uuid.uuid4())
    cell_id = str(cell.id)
    conn = get_db_connection(channel)
    conn.execute(
        "INSERT INTO runs (id, prompt, status, kind, target_cell_id) "
        "VALUES (?, '', 'running', 'generator', ?)", (run_id, cell_id),
    )
    conn.execute(
        "INSERT INTO cells (id, run_id, generator_name, sequence_num, "
        "version_name, source_type) VALUES (?, ?, 'g', 1, 'v', 'generator')",
        (cell_id, run_id),
    )
    initialize_files(conn, cell_id, cell.files)
    conn.commit()
    conn.close()

    m = ProcessManager.__new__(ProcessManager)
    m.label = "test-run"
    m.channel = channel
    m.run_id = run_id
    m.cell_id = cell_id
    m.workspace_dir = str(tmp_path / "ws")
    os.makedirs(m.workspace_dir, exist_ok=True)
    m.log_buffer = deque()

    m._flush_chat_files_to_workspace()
    gone = os.path.join(m.workspace_dir, 'gone.txt')
    assert open(gone).read() == 'old'
    receipt = mutate_files(channel, cell_id, {'operation_id':str(uuid.uuid4()),
        'mutations':[{'path':'gone.txt','base_revision':1,'writer_id':'user',
                      'edit_seq':1,'kind':'delete'}],'resolves':[]})
    assert receipt['outcome'] == 'accepted'
    files = m._compute_cell_live_files()
    assert files.get("kept.txt") == b"tip content"
    assert "gone.txt" not in files

    assert m._compute_cell_live_hashes()['gone.txt'] == {
        'body_hash':None,'file_revision':2,'encoding':'utf-8','deleted':True}
    with open(gone, 'w') as fh:
        fh.write('unsaved candidate')
    m._flush_chat_files_to_workspace()
    assert open(gone).read() == 'unsaved candidate'
    os.unlink(gone)
    outside = tmp_path / 'outside.txt'
    outside.write_text('old')
    os.symlink(outside, gone)
    m._flush_chat_files_to_workspace()
    assert os.path.islink(gone)
    assert outside.read_text() == 'old'
    os.unlink(gone)
    with open(gone, 'w') as fh:
        fh.write('old')
    m._flush_chat_files_to_workspace()
    assert os.path.isfile(os.path.join(m.workspace_dir, "kept.txt"))
    assert not os.path.lexists(gone)
    with open(gone, 'w') as fh:
        fh.write('unsaved candidate')
    m._flush_chat_files_to_workspace()
    assert open(gone).read() == 'unsaved candidate'


def test_a_runs_card_version_precedes_its_freeze_and_heads_its_run_node_GN03(client, monkeypatch):
    """The accept commits the run card ("Run started") before it freezes
    the cell, so that version is the run's frozen input (run.yaml
    ``input_version_ids``), part of its History run node, and the cell's
    current version when the run starts."""
    import yaml
    import routes.runs.create as create
    import run_registry
    from cas import read_blob
    from conftest import install_runner
    from database import get_db_connection
    from fake_manager import FakeManager
    from run_spawn import wait_spawned

    class Manager(FakeManager):
        async def start(self):
            pass

    async def no_reaper(*_args):
        pass

    monkeypatch.setattr(create, "ProcessManager", Manager)
    monkeypatch.setattr(create, "_reap_process", no_reaper)
    runner, _ = install_runner(generator="card_probe", runner="card-probe")
    cell = client.post(f"/api/stacks/{client.channel_id}/create").json()["main_stacks"][-1]["cells"][0]["id"]
    response = client.post(f"/api/channels/{client.channel_id}/runs", json={"runner": runner, "cell_id": cell})
    assert response.status_code == 200, response.text
    run_id = response.json()["run_id"]
    wait_spawned(run_id)
    run_registry.active_managers.pop(run_id, None)

    conn = get_db_connection(client.channel_id)
    try:
        [card_version] = [r[0] for r in conn.execute(
            "SELECT id FROM cells WHERE run_id=? AND version_name='Run started'", (run_id,))]
        blob = conn.execute("SELECT blob_hash FROM run_artifacts WHERE run_id=? AND filepath='run.yaml'",
                            (run_id,)).fetchone()[0]
    finally:
        conn.close()
    assert yaml.safe_load(read_blob(blob))["input_version_ids"] == [card_version]
    body = client.get(f"/api/channels/{client.channel_id}/cells/{cell}/history").json()
    [node] = [n for n in body["nodes"] if n["kind"] == "run" and n["node_id"].startswith(f"run:{run_id}:")]
    assert node["versions"][-1]["cell_id"] == card_version
    assert body["current"] == {"node_id": node["node_id"], "version_cell_id": card_version}
