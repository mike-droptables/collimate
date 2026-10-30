"""
Tests for the SDK own-cell update flow. Accepted
file-bearing updates record a version row (the first included) — the run's cell
id is an immutable genesis root.

Run from the repository root:
    python api/tests/test_new_sdk_flow.py

Covers:
  1. first update_cell mints a genesis root + version row
  2. repeated update_cell calls mint a root -> v1 -> v2 chain with carry-forward
  5. full chat flow: three emits extend the chain, the transcript carries forward
  6. the removed commit op falls through to the generic handler
  7. deleted_files removes files from the carried-forward set
  8. the run log is committed into run_artifacts on finalization
  9. category='log' artifacts route to run_artifacts, not the cell
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import uuid

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
API_DIR = os.path.dirname(THIS_DIR)
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)


def green(s): return f"\033[32m{s}\033[0m"
def red(s): return f"\033[31m{s}\033[0m"
def yellow(s): return f"\033[33m{s}\033[0m"


class TestRun:
    def __init__(self):
        self.passed = 0
        self.failed = 0

    def check(self, label, cond, detail=""):
        if cond:
            self.passed += 1
            print(f"  {green('PASS')} {label}")
        else:
            self.failed += 1
            print(f"  {red('FAIL')} {label}" + (f"  --  {detail}" if detail else ""))

    def section(self, name):
        print(f"\n{yellow('==')} {name}")


def _setup_tmpdir():
    tmpdir = tempfile.mkdtemp(prefix="test_new_sdk_")
    os.makedirs(os.path.join(tmpdir, ".collimate", "channels"), exist_ok=True)
    os.makedirs(os.path.join(tmpdir, ".collimate", "objects"), exist_ok=True)
    return tmpdir


def _reset_events_module():
    import events
    events._main_loop = None
    events._subscribers.clear()


def _make_manager(tmpdir, channel_id, run_id, run_cell_id, generator_name="test_gen"):
    """Build a ProcessManager without starting a subprocess."""
    from process_manager import ProcessManager
    from runner_permissions import UNRESTRICTED

    ws = tempfile.mkdtemp(prefix="ws_", dir=tmpdir)
    app_dir = tempfile.mkdtemp(prefix="app_", dir=tmpdir)

    manager = ProcessManager(
        channel=channel_id,
        run_id=run_id,
        app_dir=app_dir,
        workspace_dir=ws,
        entrypoint="test.py",
        generator_name=generator_name,
        cell_id=run_cell_id,
        created_cell=True,
        permissions=UNRESTRICTED,
    )
    manager.fixture_replies = []
    manager.fixture_edit_seq = 0

    async def receive_ack(reply, request):
        manager.fixture_replies.append(reply)

    manager._send_ack = receive_ack
    return manager


def _seed_run(channel_id, run_id):
    from database import get_db_connection
    conn = get_db_connection(channel_id)
    conn.execute(
        "INSERT INTO runs (id, prompt, status) VALUES (?, '', 'running')",
        (run_id,),
    )
    conn.commit()
    conn.close()


def _seed_state(channel_name, run_cell_id):
    """Seed in-memory state with a channel + the run's cell in a stack."""
    from models import Channel, Stack, Cell
    from state import state as app_state

    from genesis import ensure_capabilities_stack

    ch = Channel(name=channel_name.replace("_", " ").title())
    ensure_capabilities_stack(ch)
    run_cell = Cell(id=run_cell_id, name="Generating...")
    stk = Stack(cells=[run_cell])
    ch.main_stacks.append(stk)
    app_state.channels.append(ch)
    app_state.active_channel_id = ch.id
    return ch


async def _send_ipc(manager, payload):
    """Send the canonical SDK envelope with explicitly observed file bases."""
    from database import get_db_connection
    from persistence import snapshot_files

    request = json.loads(payload)
    if request['op'] == 'update_cell':
        with get_db_connection(manager.channel) as conn:
            current = {f['path']: f['file_revision'] for f in
                       snapshot_files(conn, manager.cell_id, include_deleted=True)}
        paths = set(request.get('deleted_files', []))
        for artifact in request.get('artifacts', []):
            artifact['encoding'] = 'utf-8'
            if artifact.get('category', 'cell') == 'cell':
                paths.add(artifact['cell_path'])
        manager.fixture_edit_seq += 1
        request.update(operation_id=str(uuid.uuid4()), writer_id='run:' + manager.run_id,
                       edit_seq=manager.fixture_edit_seq,
                       base_revisions={path: current.get(path, 0) for path in paths})
    await manager._handle_ipc(json.dumps(request))
    if request['op'] == 'update_cell':
        assert manager.fixture_replies[-1]['ok'], manager.fixture_replies[-1]


# ---------------------------------------------------------------------------
# Test 1: the first update_cell creates the genesis DB row
# ---------------------------------------------------------------------------

def test_update_cell_creates_genesis(t):
    t.section("first update_cell mints a genesis root + version row")

    tmpdir = _setup_tmpdir()
    old_cwd = os.getcwd()

    async def _run():
        os.chdir(tmpdir)
        _reset_events_module()
        from database import get_db_connection
        from events import capture_loop
        capture_loop()

        channel = "test_first_emit_genesis"
        run_id = str(uuid.uuid4())
        run_cell_id = str(uuid.uuid4())

        ch = _seed_state(channel, run_cell_id)
        channel = str(ch.id)
        _seed_run(channel, run_id)
        manager = _make_manager(tmpdir, channel, run_id, run_cell_id)

        # Write a file to the workspace
        os.makedirs(manager.workspace_dir, exist_ok=True)
        with open(os.path.join(manager.workspace_dir, "output.md"), "w") as f:
            f.write("# Hello\n")

        # Send the first update_cell
        payload = json.dumps({"req_id": "test-request",
            "op": "update_cell",
            "artifacts": [{
                "type": "workspace",
                "cell_path": "output.md",
                "local_path": "output.md",
            }],
            "metadata": {"name": "Test Output"},
        })
        await _send_ipc(manager, payload)

        # The first accepted file emission records a version under its genesis root.
        # The run's cell id becomes an IMMUTABLE genesis root and the emit's
        # content lands on a fresh child version.
        conn = get_db_connection(channel)
        root = conn.execute(
            "SELECT id, version_name, source_type FROM cells WHERE id=?",
            (run_cell_id,),
        ).fetchone()
        t.check("genesis root row created", root is not None)
        if root:
            t.check("root source_type is 'genesis'", root["source_type"] == "genesis")

        edge = conn.execute(
            "SELECT child_cell_id FROM cell_edges "
            "WHERE parent_cell_id=? AND parent_key='previous_version'",
            (run_cell_id,),
        ).fetchall()
        t.check("one previous_version edge from the root", len(edge) == 1,
                f"got {len(edge)} edges")

        if edge:
            v1 = conn.execute(
                "SELECT version_name, run_id FROM cells WHERE id=?",
                (edge[0]["child_cell_id"],),
            ).fetchone()
            t.check("version_name is 'Test Output'", v1["version_name"] == "Test Output",
                    f"got {v1['version_name']!r}")
            t.check("version row stamped with the run id", v1["run_id"] == run_id)

            from versions import version_file_rows
            files = version_file_rows(conn, edge[0]["child_cell_id"])
            t.check("version cell_files has output.md",
                    any(r["filepath"] == "output.md" for r in files),
                    f"got {[r['filepath'] for r in files]}")

        # The root stays untouched — no files were rewritten onto it.
        root_files = conn.execute(
            "SELECT filepath FROM cell_files WHERE cell_id=?",
            (run_cell_id,),
        ).fetchall()
        t.check("genesis root cell_files untouched", len(root_files) == 0,
                f"got {[r['filepath'] for r in root_files]}")
        conn.close()

        # Cleanup
        from state import state as app_state
        app_state.channels = [c for c in app_state.channels if c.id != ch.id]

    try:
        asyncio.run(_run())
    finally:
        os.chdir(old_cwd)
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Test 2: repeated update_cell calls mint a version chain
# ---------------------------------------------------------------------------

def test_update_cell_repeat_mints_versions(t):
    t.section("repeated update_cell calls each mint a version row")

    tmpdir = _setup_tmpdir()
    old_cwd = os.getcwd()

    async def _run():
        os.chdir(tmpdir)
        _reset_events_module()
        from database import get_db_connection
        from events import capture_loop
        capture_loop()

        channel = "test_repeat_versions"
        run_id = str(uuid.uuid4())
        run_cell_id = str(uuid.uuid4())

        ch = _seed_state(channel, run_cell_id)
        channel = str(ch.id)
        _seed_run(channel, run_id)
        manager = _make_manager(tmpdir, channel, run_id, run_cell_id)

        os.makedirs(manager.workspace_dir, exist_ok=True)
        with open(os.path.join(manager.workspace_dir, "v1.md"), "w") as f:
            f.write("version 1")

        # First emit
        await _send_ipc(manager, json.dumps({"req_id": "test-request",
            "op": "update_cell",
            "artifacts": [{"type": "workspace", "cell_path": "v1.md", "local_path": "v1.md"}],
            "metadata": {"name": "V1"},
        }))

        # Write a different file
        with open(os.path.join(manager.workspace_dir, "v2.md"), "w") as f:
            f.write("version 2")

        # Second emit — mints a second version
        await _send_ipc(manager, json.dumps({"req_id": "test-request",
            "op": "update_cell",
            "artifacts": [{"type": "workspace", "cell_path": "v2.md", "local_path": "v2.md"}],
            "metadata": {"name": "V2"},
        }))

        conn = get_db_connection(channel)
        # Each emit minted its own version row: genesis root + v1 + v2.
        count = conn.execute("SELECT COUNT(*) AS n FROM cells").fetchone()["n"]
        t.check("3 cells rows (root + one per emit)", count == 3, f"got {count}")

        from versions import find_tip
        tip = find_tip(conn, run_cell_id)
        tip_row = conn.execute(
            "SELECT version_name FROM cells WHERE id=?", (tip,)
        ).fetchone()
        t.check("tip name is V2", tip_row["version_name"] == "V2")

        # Tip carries both files: v1.md merged forward from the first
        # version, v2.md from this emit.
        from versions import version_file_rows
        files = version_file_rows(conn, tip)
        filepaths = {r["filepath"] for r in files}
        t.check("v2.md present at tip", "v2.md" in filepaths, f"got {filepaths}")
        t.check("v1.md carried forward to tip", "v1.md" in filepaths, f"got {filepaths}")

        # The genesis row is the run's cell
        run_rows = conn.execute(
            "SELECT id FROM cells WHERE run_id=?", (run_id,),
        ).fetchall()
        t.check("genesis row is the run's cell",
                sum(1 for r in run_rows if r["id"] == run_cell_id) == 1)

        # root → v1 (first emit) → v2 (second emit), each linked via cell_edges
        edges = conn.execute(
            "SELECT child_cell_id FROM cell_edges WHERE parent_cell_id=? AND parent_key='previous_version'",
            (run_cell_id,),
        ).fetchall()
        t.check("cell_edge links genesis to v1", len(edges) == 1,
                f"got {len(edges)} edges")

        if edges:
            v1_id = edges[0]["child_cell_id"]
            v1 = conn.execute(
                "SELECT version_name FROM cells WHERE id=?",
                (v1_id,),
            ).fetchone()
            t.check("v1 has the first emit's name", v1["version_name"] == "V1")
            v2_edges = conn.execute(
                "SELECT child_cell_id FROM cell_edges WHERE parent_cell_id=? AND parent_key='previous_version'",
                (v1_id,),
            ).fetchall()
            t.check("cell_edge links v1 to v2", len(v2_edges) == 1,
                    f"got {len(v2_edges)} edges")
            if v2_edges:
                t.check("v2 is the tip", v2_edges[0]["child_cell_id"] == tip)

        conn.close()

        from state import state as app_state
        app_state.channels = [c for c in app_state.channels if c.id != ch.id]

    try:
        asyncio.run(_run())
    finally:
        os.chdir(old_cwd)
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Test 5: Full chat flow — first emit + two turns
# ---------------------------------------------------------------------------

def test_full_chat_flow(t):
    t.section("Full chat flow: initial update → turn update → version chain")

    tmpdir = _setup_tmpdir()
    old_cwd = os.getcwd()

    async def _run():
        os.chdir(tmpdir)
        _reset_events_module()
        from database import get_db_connection
        from events import capture_loop
        capture_loop()

        channel = "test_full_chat"
        run_id = str(uuid.uuid4())
        run_cell_id = str(uuid.uuid4())

        ch = _seed_state(channel, run_cell_id)
        channel = str(ch.id)
        _seed_run(channel, run_id)
        manager = _make_manager(tmpdir, channel, run_id, run_cell_id, "synthetic_chat")

        os.makedirs(manager.workspace_dir, exist_ok=True)

        # Step 1: initial update (like "Starting...")
        with open(os.path.join(manager.workspace_dir, "output.md"), "w") as f:
            f.write("starting")
        await _send_ipc(manager, json.dumps({"req_id": "test-request",
            "op": "update_cell",
            "artifacts": [{"type": "workspace", "cell_path": "output.md", "local_path": "output.md"}],
            "metadata": {"name": "Chat"},
        }))

        # Step 2: first turn completes
        msg_dir = os.path.join(manager.workspace_dir, "transcripts")
        os.makedirs(msg_dir, exist_ok=True)
        with open(os.path.join(msg_dir, "main.json"), "w") as f:
            json.dump([{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}], f)
        with open(os.path.join(manager.workspace_dir, "output.md"), "w") as f:
            f.write("turn 1")

        await _send_ipc(manager, json.dumps({"req_id": "test-request",
            "op": "update_cell",
            "artifacts": [
                {"type": "workspace", "cell_path": "output.md", "local_path": "output.md"},
                {"type": "workspace", "cell_path": "transcripts/main.json",
                 "local_path": "transcripts/main.json"},
            ],
            "metadata": {"name": "Chat"},
        }))

        # Step 3: second turn
        with open(os.path.join(manager.workspace_dir, "output.md"), "w") as f:
            f.write("turn 2")

        await _send_ipc(manager, json.dumps({"req_id": "test-request",
            "op": "update_cell",
            "artifacts": [{"type": "workspace", "cell_path": "output.md", "local_path": "output.md"}],
            "metadata": {"name": "Chat"},
        }))

        # Verify chain: genesis -> v1 -> v2
        conn = get_db_connection(channel)
        all_cells = conn.execute(
            "SELECT id, version_name FROM cells WHERE run_id=? ORDER BY sequence_num",
            (run_id,),
        ).fetchall()

        t.check("4 cells rows (genesis + one version per emit)", len(all_cells) == 4,
                f"got {len(all_cells)}: {[(r['id'][:8], r['version_name']) for r in all_cells]}")

        # Walk the chain from genesis
        tip = run_cell_id
        chain = [tip]
        for _ in range(10):
            edge = conn.execute(
                "SELECT child_cell_id FROM cell_edges WHERE parent_cell_id=? AND parent_key='previous_version'",
                (tip,),
            ).fetchone()
            if not edge:
                break
            tip = edge["child_cell_id"]
            chain.append(tip)

        t.check("chain length is 4", len(chain) == 4,
                f"got {len(chain)}")

        # Verify the tip has the second turn's content
        from versions import version_file_rows
        tip_files = version_file_rows(conn, tip)
        t.check("tip has output.md", any(r["filepath"] == "output.md" for r in tip_files))

        # Verify messages file was carried forward
        t.check("tip has messages file",
                any(r["filepath"] == "transcripts/main.json" for r in tip_files),
                f"got {[r['filepath'] for r in tip_files]}")

        conn.close()

        from state import state as app_state
        app_state.channels = [c for c in app_state.channels if c.id != ch.id]

    try:
        asyncio.run(_run())
    finally:
        os.chdir(old_cwd)
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Test 6: commit IPC op is removed (should fall through to else branch)
# ---------------------------------------------------------------------------

def test_commit_op_removed(t):
    t.section("commit IPC op no longer handled (falls through to generic handler)")

    tmpdir = _setup_tmpdir()
    old_cwd = os.getcwd()

    async def _run():
        os.chdir(tmpdir)
        _reset_events_module()
        from events import capture_loop
        capture_loop()

        channel = "test_no_commit"
        run_id = str(uuid.uuid4())
        run_cell_id = str(uuid.uuid4())

        ch = _seed_state(channel, run_cell_id)
        channel = str(ch.id)
        _seed_run(channel, run_id)
        manager = _make_manager(tmpdir, channel, run_id, run_cell_id)

        # Send old-style commit — should not crash, just log to buffer
        await _send_ipc(manager, json.dumps({
            "op": "commit",
            "name": "Test",
            "artifacts": [],
            "parents": {},
            "metadata": {},
        }))

        # Should land in log_buffer as an unknown-op record: the raw frame is
        # persisted under the fixed type ``ipc_unknown_op`` (never under the
        # op name, which a generator could use to forge a rendered event),
        # alongside an ``ipc_error`` line naming the op.
        found = any(
            item.get("type") == "ipc_unknown_op"
            and json.loads(item.get("payload") or "{}").get("op") == "commit"
            for item in manager.log_buffer
        )
        t.check("commit op falls through to log_buffer", found,
                f"log_buffer has {len(manager.log_buffer)} items")
        t.check("unknown op is surfaced as ipc_error",
                any(item.get("type") == "ipc_error" and "commit" in str(item.get("payload"))
                    for item in manager.log_buffer))

        from state import state as app_state
        app_state.channels = [c for c in app_state.channels if c.id != ch.id]

    try:
        asyncio.run(_run())
    finally:
        os.chdir(old_cwd)
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Test 7: deleted_files removes files from carried-forward set
# ---------------------------------------------------------------------------

def test_deleted_files(t):
    t.section("deleted_files removes files from version tip")

    tmpdir = _setup_tmpdir()
    old_cwd = os.getcwd()

    async def _run():
        os.chdir(tmpdir)
        _reset_events_module()
        from database import get_db_connection
        from events import capture_loop
        capture_loop()

        channel = "test_delete_files"
        run_id = str(uuid.uuid4())
        run_cell_id = str(uuid.uuid4())

        ch = _seed_state(channel, run_cell_id)
        channel = str(ch.id)
        _seed_run(channel, run_id)
        manager = _make_manager(tmpdir, channel, run_id, run_cell_id)

        os.makedirs(manager.workspace_dir, exist_ok=True)
        with open(os.path.join(manager.workspace_dir, "keep.txt"), "w") as f:
            f.write("keep me")
        with open(os.path.join(manager.workspace_dir, "remove.txt"), "w") as f:
            f.write("delete me")

        # Initial: both files
        await _send_ipc(manager, json.dumps({"req_id": "test-request",
            "op": "update_cell",
            "artifacts": [
                {"type": "workspace", "cell_path": "keep.txt", "local_path": "keep.txt"},
                {"type": "workspace", "cell_path": "remove.txt", "local_path": "remove.txt"},
            ],
            "metadata": {"name": "Test"},
        }))

        # Verify both exist at the first emit's version (the tip so far —
        # the emit minted a child of the genesis root).
        conn = get_db_connection(channel)
        from versions import find_tip
        v1_id = find_tip(conn, run_cell_id)
        from versions import version_file_rows
        files = version_file_rows(conn, v1_id)
        paths = {r["filepath"] for r in files}
        t.check("first emit has keep.txt", "keep.txt" in paths)
        t.check("first emit has remove.txt", "remove.txt" in paths)
        conn.close()

        # Second update: delete remove.txt
        await _send_ipc(manager, json.dumps({"req_id": "test-request",
            "op": "update_cell",
            "artifacts": [],
            "deleted_files": ["remove.txt"],
            "metadata": {"name": "Test"},
        }))

        # Find version tip
        conn = get_db_connection(channel)
        tip_id = run_cell_id
        for _ in range(10):
            edge = conn.execute(
                "SELECT child_cell_id FROM cell_edges WHERE parent_cell_id=? AND parent_key='previous_version'",
                (tip_id,),
            ).fetchone()
            if not edge:
                break
            tip_id = edge["child_cell_id"]

        from versions import version_file_rows
        tip_files = version_file_rows(conn, tip_id)
        tip_paths = {r["filepath"] for r in tip_files}
        conn.close()

        t.check("tip still has keep.txt", "keep.txt" in tip_paths, f"got {tip_paths}")
        t.check("tip does NOT have remove.txt", "remove.txt" not in tip_paths, f"got {tip_paths}")

        from state import state as app_state
        app_state.channels = [c for c in app_state.channels if c.id != ch.id]

    try:
        asyncio.run(_run())
    finally:
        os.chdir(old_cwd)
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Test 8: run log committed into run_artifacts on finalization
# ---------------------------------------------------------------------------

def test_logs_auto_committed(t):
    t.section("Run log committed into run_artifacts on finalization")

    tmpdir = _setup_tmpdir()
    old_cwd = os.getcwd()

    async def _run():
        os.chdir(tmpdir)
        _reset_events_module()
        from database import get_db_connection
        from events import capture_loop
        capture_loop()

        channel = "test_auto_logs"
        run_id = str(uuid.uuid4())
        run_cell_id = str(uuid.uuid4())

        ch = _seed_state(channel, run_cell_id)
        channel = str(ch.id)
        _seed_run(channel, run_id)
        manager = _make_manager(tmpdir, channel, run_id, run_cell_id)

        os.makedirs(manager.workspace_dir, exist_ok=True)

        # Initial update
        with open(os.path.join(manager.workspace_dir, "output.md"), "w") as f:
            f.write("result")
        await _send_ipc(manager, json.dumps({"req_id": "test-request",
            "op": "update_cell",
            "artifacts": [{"type": "workspace", "cell_path": "output.md", "local_path": "output.md"}],
            "metadata": {"name": "Test"},
        }))

        # Simulate some log output in run_events
        conn = get_db_connection(channel)
        conn.execute(
            "INSERT INTO run_events (run_id, event_type, payload) VALUES (?, 'stdout', ?)",
            (run_id, "Running generator...\n"),
        )
        conn.execute(
            "INSERT INTO run_events (run_id, event_type, payload) VALUES (?, 'stdout', ?)",
            (run_id, "Processing files...\n"),
        )
        conn.execute(
            "INSERT INTO run_events (run_id, event_type, payload) VALUES (?, 'stderr', ?)",
            (run_id, "[WARNING] something\n"),
        )
        conn.commit()
        conn.close()

        # Simulate process exit — fake process with returncode
        class FakeProcess:
            returncode = 0
            pid = 9999
        manager.process = FakeProcess()

        # Call finalize (which calls _commit_run_log)
        await asyncio.to_thread(manager._finalize_run_status)

        # Verify: logs.txt landed in run_artifacts under the RUN's id —
        # never in any version's cell_files.
        conn = get_db_connection(channel)
        from versions import find_tip
        tip_id = find_tip(conn, run_cell_id)
        log_file = conn.execute(
            "SELECT blob_hash FROM run_artifacts "
            "WHERE run_id=? AND category='log' AND filepath='logs.txt'",
            (run_id,),
        ).fetchone()
        t.check("logs.txt in run_artifacts", log_file is not None)
        reserved = conn.execute(
            "SELECT 1 FROM cell_files WHERE filepath LIKE '.collrun/%'"
        ).fetchone()
        t.check("no reserved paths in cell_files", reserved is None)

        if log_file:
            blob_path = os.path.join(
                ".collimate", "objects",
                log_file["blob_hash"][:2],
                log_file["blob_hash"][2:],
            )
            t.check("log blob file exists", os.path.isfile(blob_path))
            if os.path.isfile(blob_path):
                with open(blob_path) as f:
                    log_content = f.read()
                t.check("log has stdout content", "Running generator" in log_content,
                        f"got: {log_content[:200]}")
                # logs.txt is now the canonical labeled format (see
                # process_manager.log_format): a "[WARNING] something" stderr
                # line is re-tagged to a WARN line with the level frame
                # stripped. Assert the content survives AND the re-tag applied.
                t.check("log has stderr content (re-tagged WARN)",
                        "WARN" in log_content and "something" in log_content,
                        f"got: {log_content[:200]}")

        # Verify: the logs commit itself minted no version row — the two rows
        # are the genesis root + the emit's version, nothing more.
        all_cells = conn.execute(
            "SELECT id FROM cells WHERE run_id=?", (run_id,)
        ).fetchall()
        t.check("2 cells rows (root + emit; none added for logs)",
                len(all_cells) == 2, f"got {len(all_cells)}")

        # Verify: output.md is still there alongside the logs
        from versions import tip_file_map
        t.check("output.md still in tip", "output.md" in tip_file_map(conn, tip_id))

        conn.close()

        from state import state as app_state
        app_state.channels = [c for c in app_state.channels if c.id != ch.id]

    try:
        asyncio.run(_run())
    finally:
        os.chdir(old_cwd)
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Test 9: category='log' artifacts route to run_artifacts, not the cell
# ---------------------------------------------------------------------------

def test_log_category_artifacts(t):
    t.section("category='log' artifacts route to run_artifacts; 'config' and 'logs.txt' are refused")

    tmpdir = _setup_tmpdir()
    old_cwd = os.getcwd()

    async def _run():
        os.chdir(tmpdir)
        _reset_events_module()
        from database import get_db_connection
        from events import capture_loop
        capture_loop()

        channel = "test_log_category"
        run_id = str(uuid.uuid4())
        run_cell_id = str(uuid.uuid4())

        ch = _seed_state(channel, run_cell_id)
        channel = str(ch.id)
        _seed_run(channel, run_id)
        manager = _make_manager(tmpdir, channel, run_id, run_cell_id)

        os.makedirs(manager.workspace_dir, exist_ok=True)
        for name, content in (
            ("out.txt", "cell content"),
            ("_agent/trace.json", '{"steps": 1}'),
            ("forged.set_yaml", "values: {}"),
            ("logs.txt", "imposter"),
        ):
            dst = os.path.join(manager.workspace_dir, name)
            os.makedirs(os.path.dirname(dst) or manager.workspace_dir, exist_ok=True)
            with open(dst, "w") as f:
                f.write(content)

        await _send_ipc(manager, json.dumps({"req_id": "test-request",
            "op": "update_cell",
            "artifacts": [
                {"type": "workspace", "cell_path": "out.txt", "local_path": "out.txt"},
                {"type": "workspace", "cell_path": "_agent/trace.json",
                 "local_path": "_agent/trace.json", "category": "log"},
                {"type": "workspace", "cell_path": "forged.set_yaml",
                 "local_path": "forged.set_yaml", "category": "config"},
                {"type": "workspace", "cell_path": "logs.txt",
                 "local_path": "logs.txt", "category": "log"},
            ],
            "metadata": {"name": "T"},
        }))

        conn = get_db_connection(channel)
        from versions import find_tip
        tip = find_tip(conn, run_cell_id)
        from versions import tip_file_map
        cell_paths = set(tip_file_map(conn, tip))
        t.check("cell has out.txt", "out.txt" in cell_paths)
        t.check("trace not in cell files", "_agent/trace.json" not in cell_paths)
        t.check("forged config not in cell files", "forged.set_yaml" not in cell_paths)

        arts = {
            (r["category"], r["filepath"]) for r in conn.execute(
                "SELECT category, filepath FROM run_artifacts WHERE run_id=?",
                (run_id,),
            ).fetchall()
        }
        t.check("trace routed to log artifacts", ("log", "_agent/trace.json") in arts,
                f"got {arts}")
        t.check("'config' over IPC dropped entirely",
                not any(c == "config" for c, _ in arts), f"got {arts}")
        t.check("generator 'logs.txt' refused (host slot)",
                ("log", "logs.txt") not in arts, f"got {arts}")

        # Per-commit upsert: a rewrite replaces the artifact content.
        with open(os.path.join(manager.workspace_dir, "_agent/trace.json"), "w") as f:
            f.write('{"steps": 2}')
        await _send_ipc(manager, json.dumps({"req_id": "test-request",
            "op": "update_cell",
            "artifacts": [{"type": "workspace", "cell_path": "_agent/trace.json",
                           "local_path": "_agent/trace.json", "category": "log"}],
            "metadata": {},
        }))
        row = conn.execute(
            "SELECT blob_hash FROM run_artifacts WHERE run_id=? AND filepath=?",
            (run_id, "_agent/trace.json"),
        ).fetchone()
        blob_path = os.path.join(
            ".collimate", "objects", row["blob_hash"][:2], row["blob_hash"][2:],
        )
        with open(blob_path) as f:
            t.check("log artifact upserted per commit", f.read() == '{"steps": 2}')
        conn.close()

        from state import state as app_state
        app_state.channels = [c for c in app_state.channels if c.id != ch.id]

    try:
        asyncio.run(_run())
    finally:
        os.chdir(old_cwd)
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    t = TestRun()

    test_update_cell_creates_genesis(t)
    test_update_cell_repeat_mints_versions(t)
    test_full_chat_flow(t)
    test_commit_op_removed(t)
    test_deleted_files(t)
    test_logs_auto_committed(t)
    test_log_category_artifacts(t)

    print(f"\n{'='*60}")
    total = t.passed + t.failed
    if t.failed == 0:
        print(green(f"All {total} checks passed."))
    else:
        print(red(f"{t.failed}/{total} checks FAILED."))
    sys.exit(1 if t.failed else 0)
