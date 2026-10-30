"""Never-versioned parents are genesis; user edits retain their own kind."""

import pytest
import uuid

from database import get_db_connection


@pytest.mark.parametrize("writer", ["snapshot", "stub"])
def test_new_parent_is_genesis_and_edit_lineage_survives__F0207(fresh_cwd, writer):
    from cas import hash_and_store_bytes
    from cell_commits import blob_bytes, cell_commit_lock
    from versions import tip_file_map
    from process_manager.cells import CellsMixin
    from versions import synthesize_genesis_row, write_version_from_overlay

    conn = get_db_connection(str(uuid.uuid5(uuid.NAMESPACE_URL,"genesis-kind")))
    try:
        conn.execute("INSERT INTO runs (id, prompt, status) VALUES ('run', '', 'running')")
        binary = b"\x00\xff frozen bytes"
        binary_hash = hash_and_store_bytes(binary)
        from models import VirtualFile
        files=[VirtualFile(name='seed.txt',content='frozen text'),VirtualFile(name='asset.bin',blob_hash=binary_hash),
               VirtualFile(name='changed.txt',content='not in genesis')]
        if writer == "snapshot":
            assert synthesize_genesis_row(conn, "parent", "run", 1, files, {"changed.txt"})
        else:
            class Manager(CellsMixin):
                run_id = "run"
                generator_name = "synthetic"
            Manager()._ensure_genesis_row(conn, "parent")
        row = conn.execute("SELECT * FROM cells WHERE id='parent'").fetchone()
        assert row["source_type"] == "genesis"
        original = dict(row)
        pinned = tip_file_map(conn, "parent")
        if writer == "snapshot":
            assert set(pinned) == {"seed.txt", "asset.bin"}
            assert blob_bytes(pinned["seed.txt"]) == b"frozen text"
            assert pinned["asset.bin"] == binary_hash
            assert blob_bytes(pinned["asset.bin"]) == binary
        assert not synthesize_genesis_row(conn, "parent", "run", 9, [])
        assert dict(conn.execute("SELECT * FROM cells WHERE id='parent'").fetchone()) == original
        assert tip_file_map(conn, "parent") == pinned
        from persistence import initialize_files
        from versions import version_file_entries
        initialize_files(conn,'parent',version_file_entries(conn,'parent'))
        with cell_commit_lock("parent"):
            child = write_version_from_overlay(
                conn, "parent", "run", [("edited.txt", "set", "user edit")],
                version_name="Manual Edit", source_type="manual_edit", generator_name="manual_edit",
            )
        assert conn.execute("SELECT source_type FROM cells WHERE id=?", (child,)).fetchone()[0] == "manual_edit"
        assert conn.execute(
            "SELECT parent_key FROM cell_edges WHERE parent_cell_id='parent' AND child_cell_id=?", (child,),
        ).fetchone()[0] == "previous_version"
        assert not synthesize_genesis_row(conn, child, "run", 10, [])
        assert conn.execute("SELECT source_type FROM cells WHERE id=?", (child,)).fetchone()[0] == "manual_edit"
        assert tip_file_map(conn, "parent") == pinned
        assert tip_file_map(conn, child).items() >= pinned.items()
    finally:
        conn.close()
