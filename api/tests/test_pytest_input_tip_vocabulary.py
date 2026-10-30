"""Scalar input provenance and hydration retain the frozen version."""

import uuid

import pytest

from database import get_db_connection
from process_manager import ProcessManager
from runner_permissions import UNRESTRICTED


@pytest.fixture
def channel_id(fresh_cwd):
    from models import Channel
    from state import state
    channel = Channel(name="Scalar input")
    state.channels = [channel]
    state.active_channel_id = channel.id
    return str(channel.id)


def test_manager_constructs_with_scalar_input_tip__F0216(channel_id, tmp_path):
    manager = ProcessManager(
        channel=channel_id, run_id=str(uuid.uuid4()), app_dir=str(tmp_path),
        workspace_dir=str(tmp_path), entrypoint="main.py", permissions=UNRESTRICTED,
        input_tip_id="input-version",
    )
    assert manager.input_tip_id == "input-version"


@pytest.mark.parametrize("operation", ["edge", "hydration"])
def test_scalar_input_tip_drives_frozen_version__F0216(channel_id, operation):
    manager = ProcessManager.__new__(ProcessManager)
    manager.input_tip_id = "input-version"
    conn = get_db_connection(channel_id)
    try:
        conn.execute("INSERT INTO runs (id, prompt, status) VALUES ('run', '', 'running')")
        for seq, cid in enumerate(("input-version", "output-version")):
            conn.execute(
                "INSERT INTO cells (id, run_id, generator_name, sequence_num, source_type) "
                "VALUES (?, 'run', 'synthetic', ?, 'genesis')", (cid, seq),
            )
        if operation == "edge":
            manager._stamp_input_edges(conn, "output-version")
            edges = conn.execute("SELECT parent_cell_id, parent_key FROM cell_edges").fetchall()
            assert [tuple(row) for row in edges] == [("input-version", "input_0")]
            assert manager._provenance_for_write(conn) == (None, "input-version")
        else:
            from cas import hash_and_store_bytes
            blob = hash_and_store_bytes(b"frozen content")
            conn.execute("INSERT INTO blobs (hash, size) VALUES (?, ?)",
                         (blob, len(b"frozen content")))
            conn.execute(
                "INSERT INTO cell_files (cell_id, filepath, blob_hash, encoding, language) "
                "VALUES (?, ?, ?, 'utf-8', 'text')",
                ("input-version", "seed.txt", blob),
            )
            from versions import initialize_file_snapshot
            initialize_file_snapshot(conn, "input-version")
            assert manager._workspace_cell_base(conn) == {"seed.txt": blob}
    finally:
        conn.close()
