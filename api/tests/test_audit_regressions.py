"""Regression coverage for the generator-run audit fixes.

Each test here anchors one concrete fix against the behaviour that used
to be broken. They are unit-level where possible — the goal is to fail
loud if a future refactor reintroduces the specific bug the fix closed,
not to re-test the whole route/lifecycle surface (which lives in
test_pytest_runs_lifecycle.py).

Scope notes:
  * Tests that would need to globally monkeypatch asyncio primitives
    (e.g. the settings-update timeout path) are deliberately omitted —
    those hooks collide with pytest-asyncio's own fixture plumbing and
    can deadlock the runner.
  * Tests that would need an orchestrated subprocess (the actual
    timeout watchdog, force-stop SIGTERM→SIGKILL ladder) belong in a
    manual/e2e pass; they're called out in the plan file.
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
import uuid
from collections import deque

import pytest

API_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)

from database import get_db_connection  # noqa: E402


# ---------------------------------------------------------------------------
# Local helpers (small on purpose — duplicating tiny shapes keeps this
# file independent of test_pytest_runs_lifecycle's seeding helpers)
# ---------------------------------------------------------------------------

def _seed_run(channel_id: str, status: str = "running") -> str:
    rid = str(uuid.uuid4())
    conn = get_db_connection(channel_id)
    try:
        conn.execute(
            "INSERT INTO runs (id, prompt, status) VALUES (?, 'seed', ?)",
            (rid, status),
        )
        conn.commit()
        return rid
    finally:
        conn.close()


def _seed_cell_row(channel_id: str, cell_id: str) -> None:
    conn = get_db_connection(channel_id)
    try:
        run_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO runs (id, prompt, status) VALUES (?, 'seed', 'completed')",
            (run_id,),
        )
        conn.execute(
            "INSERT INTO cells (id, run_id, generator_name, sequence_num, "
            "version_name, source_type) "
            "VALUES (?, ?, 'seed', 1, 'v1', 'genesis')",
            (cell_id, run_id),
        )
        conn.commit()
    finally:
        conn.close()


def _link_version(channel_id: str, parent: str, child: str) -> None:
    conn = get_db_connection(channel_id)
    try:
        conn.execute(
            "INSERT INTO cell_edges (parent_cell_id, child_cell_id, parent_key) "
            "VALUES (?, ?, 'previous_version')",
            (parent, child),
        )
        conn.commit()
    finally:
        conn.close()


def _finalizing_manager(channel_id: str, returncode: int):
    """A ProcessManager shell whose process exited with ``returncode`` —
    enough state for ``_finalize_run_status``."""
    from process_manager import ProcessManager

    def _make(rid: str):
        m = ProcessManager.__new__(ProcessManager)
        m.channel = channel_id
        m.run_id = rid
        m.process = type("P", (), {"returncode": returncode})()
        m.settings_history = []
        m.workspace_dir = os.path.join(os.getcwd(), "missing-workspace")
        m.timeout_reason = None
        m.current_phase = None
        m.current_phase_message = None
        m.log_buffer = deque()
        m._log_flush_lock = threading.Lock()
        m._broadcast_run_status = lambda *_a, **_k: None
        return m

    return _make


# ===========================================================================
# Fix A2 — finalization race: conditional UPDATE leaves non-running rows alone
# ===========================================================================

def test_finalize_still_marks_running_as_failed_on_nonzero(client):
    """Symmetric check — the conditional UPDATE must still fire when
    the row is still 'running'."""
    _Dummy = _finalizing_manager(client.channel_id, 1)

    rid = _seed_run(client.channel_id, "running")
    _Dummy(rid)._finalize_run_status()

    conn = get_db_connection(client.channel_id)
    try:
        row = conn.execute("SELECT status FROM runs WHERE id=?", (rid,)).fetchone()
    finally:
        conn.close()
    assert row["status"] == "failed"


def test_finalize_marks_completed_on_zero_exit(client):
    _Dummy = _finalizing_manager(client.channel_id, 0)

    rid = _seed_run(client.channel_id, "running")
    _Dummy(rid)._finalize_run_status()

    conn = get_db_connection(client.channel_id)
    try:
        row = conn.execute("SELECT status FROM runs WHERE id=?", (rid,)).fetchone()
    finally:
        conn.close()
    assert row["status"] == "completed"


# ===========================================================================
# Fix D33 — version-walk cycle detection
# ===========================================================================

def test_find_version_tip_breaks_on_cycle(client):
    """A corrupt A→B→A cycle in cell_edges must not hang the tip walk: the
    walker is bounded and returns a node of the cycle."""
    from versions import find_tip

    a = str(uuid.uuid4())
    b = str(uuid.uuid4())
    _seed_cell_row(client.channel_id, a)
    _seed_cell_row(client.channel_id, b)
    _link_version(client.channel_id, a, b)
    _link_version(client.channel_id, b, a)  # cycle: A → B → A

    conn = get_db_connection(client.channel_id)
    try:
        assert find_tip(conn, a) in {a, b}
    finally:
        conn.close()


def test_find_version_tip_linear_chain_returns_tail(client):
    """Sanity check — the bound must not break the normal case."""
    from versions import find_tip

    a, b, c = (str(uuid.uuid4()) for _ in range(3))
    _seed_cell_row(client.channel_id, a)
    _seed_cell_row(client.channel_id, b)
    _seed_cell_row(client.channel_id, c)
    _link_version(client.channel_id, a, b)
    _link_version(client.channel_id, b, c)

    conn = get_db_connection(client.channel_id)
    try:
        assert find_tip(conn, a) == c
    finally:
        conn.close()


def test_find_version_tip_fork_takes_newest_edge(client):
    """Two previous_version edges leave one node: the newer edge's branch
    wins, and a non-version edge off the tip is ignored."""
    from versions import find_tip

    root, old, new, new_tail, other = (str(uuid.uuid4()) for _ in range(5))
    for cid in (root, old, new, new_tail, other):
        _seed_cell_row(client.channel_id, cid)
    _link_version(client.channel_id, root, old)
    _link_version(client.channel_id, root, new)  # newer rowid
    _link_version(client.channel_id, new, new_tail)
    conn = get_db_connection(client.channel_id)
    try:
        conn.execute(
            "INSERT INTO cell_edges (parent_cell_id, child_cell_id, parent_key) "
            "VALUES (?, ?, 'input_0')",
            (new_tail, other),
        )
        conn.commit()
        assert find_tip(conn, root) == new_tail
    finally:
        conn.close()


def test_find_version_tip_walks_past_ten_thousand(client):
    """No silent cap: a chain past 10,000 versions resolves to its tail, not
    the node at depth 10,000; only a chain longer than MAX_VERSION_CHAIN
    raises."""
    from versions import find_tip

    ids = [str(uuid.uuid4()) for _ in range(10_051)]
    run_id = str(uuid.uuid4())
    conn = get_db_connection(client.channel_id)
    try:
        conn.execute(
            "INSERT INTO runs (id, prompt, status) VALUES (?, 'seed', 'completed')",
            (run_id,),
        )
        conn.executemany(
            "INSERT INTO cells (id, run_id, generator_name, sequence_num, "
            "version_name, source_type) "
            "VALUES (?, ?, 'seed', ?, 'v1', 'genesis')",
            [(cid, run_id, n) for n, cid in enumerate(ids, 1)],
        )
        conn.executemany(
            "INSERT INTO cell_edges (parent_cell_id, child_cell_id, parent_key) "
            "VALUES (?, ?, 'previous_version')",
            list(zip(ids, ids[1:])),
        )
        conn.commit()
        assert find_tip(conn, ids[0]) == ids[-1]
    finally:
        conn.close()


# ===========================================================================
# Fix A8 — line-buffered drains keep long lines intact + preserve indent
# ===========================================================================

@pytest.mark.asyncio
async def test_drain_stream_preserves_long_line(client):
    """The old read(4096) loop split long lines across event rows. The
    line-buffered drain should deliver a 10 KB single line as one log
    entry."""
    from process_manager.streams import StreamsMixin

    long_line = ("x" * 10_000) + "\n"

    class _Fake(StreamsMixin):
        def __init__(self):
            self.log_buffer: list = []
            self.current_phase = "running"

    reader = asyncio.StreamReader(limit=1_048_576)
    reader.feed_data(long_line.encode())
    reader.feed_eof()

    fake = _Fake()
    await fake._drain_stream(reader, "stdout")
    assert len(fake.log_buffer) == 1
    assert fake.log_buffer[0]["payload"].count("x") == 10_000


@pytest.mark.asyncio
async def test_drain_stream_preserves_indentation(client):
    """The old code did line.trim() which stripped leading whitespace.
    Regression for Phase-A8 / Phase-B19."""
    from process_manager.streams import StreamsMixin

    class _Fake(StreamsMixin):
        def __init__(self):
            self.log_buffer: list = []
            self.current_phase = "running"

    reader = asyncio.StreamReader()
    reader.feed_data(b"    def foo():\n")
    reader.feed_data(b"        return 1\n")
    reader.feed_eof()

    fake = _Fake()
    await fake._drain_stream(reader, "stdout")

    lines = [row["payload"] for row in fake.log_buffer]
    assert all(line.startswith("    ") for line in lines), lines


@pytest.mark.asyncio
async def test_drain_stream_drops_oversize_line_whole(client):
    """A line over the reader limit is replaced by one marker row; the
    lines around it stay intact (the old readline + read(1 MiB) path logged
    the following lines glued into one mislabeled row)."""
    from process_manager.streams import StreamsMixin

    class _Fake(StreamsMixin):
        def __init__(self):
            self.log_buffer: list = []
            self.current_phase = "running"

    reader = asyncio.StreamReader(limit=1024)
    reader.feed_data(b"a\n" + b"x" * 5000 + b"\nb\nc\n")
    reader.feed_eof()

    fake = _Fake()
    await fake._drain_stream(reader, "stdout")
    payloads = [row["payload"] for row in fake.log_buffer]
    assert payloads == ["a\n", "[log line over 1 MiB dropped]\n", "b\n", "c\n"]


@pytest.mark.asyncio
async def test_drain_stream_drops_oversize_line_arriving_in_pieces(client):
    """Same, when the limit trips before the long line's newline has
    arrived: no fragment of the long line is logged."""
    from process_manager.streams import StreamsMixin

    class _Fake(StreamsMixin):
        def __init__(self):
            self.log_buffer: list = []
            self.current_phase = "running"

    reader = asyncio.StreamReader(limit=1024)

    async def _feed():
        reader.feed_data(b"a\n")
        for _ in range(6):
            reader.feed_data(b"x" * 1000)
            await asyncio.sleep(0)
        reader.feed_data(b"x\nb\n")
        reader.feed_eof()

    fake = _Fake()
    await asyncio.gather(_feed(), fake._drain_stream(reader, "stdout"))
    payloads = [row["payload"] for row in fake.log_buffer]
    assert payloads == ["a\n", "[log line over 1 MiB dropped]\n", "b\n"]


def test_apply_phase_message_only_skips_db_write(client, monkeypatch):
    """The same phase with a new message logs and broadcasts but does not
    rewrite runs.phase; a real phase change still does."""
    from collections import deque

    import database
    import events
    from process_manager import ProcessManager

    calls = []

    class _Conn:
        def execute(self, *_a):
            calls.append(_a)

        def commit(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(database, "get_db_connection", lambda _ch: _Conn())
    monkeypatch.setattr(events, "broadcast", lambda *_a, **_k: None)

    m = ProcessManager.__new__(ProcessManager)
    m.channel = client.channel_id
    m.run_id = str(uuid.uuid4())
    m.current_phase = "running"
    m.current_phase_message = "a"
    m.log_buffer = deque()

    m._apply_phase("running", "b")
    assert calls == []
    assert len(m.log_buffer) == 1 and m.log_buffer[0]["type"] == "phase"
    assert m.current_phase_message == "b"

    m._apply_phase("awaiting_input", "b")
    assert len(calls) == 1


# ===========================================================================
# Fix D35 — unknown IPC opcode gets tagged with ipc_error alongside the row
# ===========================================================================

@pytest.mark.asyncio
async def test_unknown_ipc_op_logs_ipc_error(client):
    """New SDK versions may emit ops the backend doesn't have a branch
    for. The raw frame is kept under the fixed type ``ipc_unknown_op``
    (never the op name, which could forge a rendered or modal event), and
    an ipc_error is logged so operators can find it."""
    import json

    from process_manager.ipc import IPCMixin

    class _Fake(IPCMixin):
        def __init__(self):
            self.channel = client.channel_id
            self.run_id = "r"
            self.log_buffer: list = []
            self._ipc_writer = None

    fake = _Fake()
    await fake._handle_ipc('{"op":"mystery_op","blah":1}')
    types = [e.get("type") for e in fake.log_buffer]
    assert "ipc_error" in types
    assert "mystery_op" not in types
    kept = [e for e in fake.log_buffer if e.get("type") == "ipc_unknown_op"]
    assert [json.loads(e["payload"])["op"] for e in kept] == ["mystery_op"]

    await fake._handle_ipc('{"op":"unrecognized_event","settings_content":"x"}')
    assert "unrecognized_event" not in [e.get("type") for e in fake.log_buffer]


@pytest.mark.asyncio
async def test_bad_json_logs_ipc_error_row(client):
    """Malformed IPC line becomes a single ipc_error event row rather
    than crashing the handler (existing behaviour; belt-and-braces
    regression test)."""
    from process_manager.ipc import IPCMixin

    class _Fake(IPCMixin):
        def __init__(self):
            self.channel = client.channel_id
            self.run_id = "r"
            self.log_buffer: list = []
            self._ipc_writer = None

    fake = _Fake()
    await fake._handle_ipc("not-json-at-all")
    assert any(e.get("type") == "ipc_error" for e in fake.log_buffer)


if __name__ == "__main__":
    # Run as a script (the standalone loop): hand this file to pytest, which
    # collects an explicitly named file whatever its name pattern.
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
