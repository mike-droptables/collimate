"""Public core ownership retains actual source/version/staging behavior."""

import pytest
import uuid


def test_source_commit_transaction_and_rollback__F0202(fresh_cwd, monkeypatch):
    import cell_commits
    import versions
    import state
    from models import Cell, Channel, Stack, VirtualFile
    from database import get_db_connection
    cell = Cell(name="Source", files=[VirtualFile(name="pkg/code.py", content="old")])
    ch = Channel(name="Core source", nav_stacks=[Stack(stack_type="capabilities", cells=[cell])])
    monkeypatch.setattr(state.state, "channels", [ch])
    from persistence import initialize_files
    conn=get_db_connection(str(ch.id))
    initialize_files(conn,str(cell.id),cell.files)
    conn.commit()
    conn.close()
    new = [VirtualFile(name="pkg/code.py", content="new", language="python")]
    cell_commits.commit_source_cell(ch, cell, new, label="Source edit", generator_name="fixture", persist=False)
    assert [(f.name,f.content,f.language) for f in cell.files] == [(f.name,f.content,f.language) for f in new]
    conn = get_db_connection(str(ch.id))
    try:
        assert conn.execute("SELECT COUNT(*) FROM cells").fetchone()[0] == 2
        assert conn.execute("SELECT source_type FROM cells WHERE id=?", (str(cell.id),)).fetchone()[0] == "genesis"
        before = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        def fail(*args, **kwargs):
            raise RuntimeError("injected source failure")
        monkeypatch.setattr(versions, "insert_version", fail)
        with pytest.raises(RuntimeError, match="injected"):
            cell_commits.commit_source_cell(ch, cell, [VirtualFile(name="pkg/code.py", content="later")], persist=False)
        assert [(f.name,f.content,f.language) for f in cell.files] == [(f.name,f.content,f.language) for f in new]
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == before
    finally:
        conn.close()


def test_versions_own_tip_genesis_overlay_and_binary_snapshot__F0050_F0113_F0191_F0348_F0440(fresh_cwd, monkeypatch):
    import versions
    from cas import hash_and_store_bytes
    from cell_commits import cell_commit_lock
    from database import get_db_connection
    conn = get_db_connection(str(uuid.uuid5(uuid.NAMESPACE_URL,"core_versions")))
    try:
        conn.execute("INSERT INTO runs(id,prompt,status) VALUES('fixture','','completed')")
        binary = hash_and_store_bytes(b"\x00\xff")
        from models import VirtualFile
        from persistence import initialize_files
        entries=[VirtualFile(name='image.bin',blob_hash=binary),VirtualFile(name='note.md',content='old')]
        initialize_files(conn,'cell',entries)
        versions.synthesize_genesis_row(conn, 'cell', 'fixture', 1, entries)
        writes = []
        actual_insert = versions.insert_version
        def capture_insert(*args, **kwargs):
            writes.append(kwargs)
            return actual_insert(*args, **kwargs)
        monkeypatch.setattr(versions, "insert_version", capture_insert)
        with cell_commit_lock("cell"):
            tip = versions.write_version_from_overlay(conn, "cell", "fixture", [("note.md", "set", "new")],
                version_name="Changed", source_type="manual_edit", generator_name="fixture")
        assert len(writes) == 1
        assert versions.find_tip(conn, "cell") == tip
        files = versions.build_virtual_files(versions.version_file_entries(conn, tip))
        assert {f.name: f.content for f in files}["note.md"] == "new"
        assert next(f for f in files if f.name == "image.bin").blob_hash == binary
        conn.rollback()
        assert conn.execute("SELECT COUNT(*) FROM cells").fetchone()[0] == 0
    finally:
        conn.close()


def test_run_staging_owns_frozen_hydration_and_path_guard__F0191_F0348(fresh_cwd, tmp_path):
    import run_staging
    from cas import hash_and_store_bytes
    from database import get_db_connection
    conn = get_db_connection(str(uuid.uuid5(uuid.NAMESPACE_URL,"core_stage")))
    try:
        conn.execute("INSERT INTO runs(id,prompt,status) VALUES('fixture','','completed')")
        conn.execute("INSERT INTO cells(id,run_id,sequence_num) VALUES('frozen','fixture',1)")
        digest = hash_and_store_bytes(b"frozen bytes")
        conn.execute("INSERT INTO blobs(hash,size) VALUES(?,?)", (digest, 12))
        conn.execute("INSERT INTO cell_files(cell_id,filepath,blob_hash,encoding,language) VALUES('frozen','data.txt',?,'utf-8','text')", (digest,))
        from versions import initialize_file_snapshot
        initialize_file_snapshot(conn,'frozen')
        conn.commit()
    finally:
        conn.close()
    run_staging.hydrate_workspace(str(uuid.uuid5(uuid.NAMESPACE_URL,"core_stage")), "logical", str(tmp_path), tip_id="frozen")
    assert (tmp_path / "data.txt").read_bytes() == b"frozen bytes"
    assert run_staging.safe_dest_under(str(tmp_path), "../escape") is None


def test_cell_placement_owns_explicit_channel_lookup_and_capabilities_scope__F0070():
    import pytest
    import cell_placement
    from models import Cell, Channel, Stack
    main_cell, card, source_cell = Cell(name="Main"), Cell(name="Card", builtin=True), Cell(name="Source")
    main = Stack(cells=[main_cell])
    source = Stack(stack_type=cell_placement.CAPABILITIES_STACK_TYPE, cells=[card, source_cell])
    ch = Channel(name="Explicit", main_stacks=[main], nav_stacks=[source])
    assert list(cell_placement.iter_stacks(ch)) == [main, source]
    assert cell_placement.find_cell(ch, str(source_cell.id)) == (source, source_cell)
    assert cell_placement.find_cell(None, str(source_cell.id)) == (None, None)
    assert cell_placement.find_cell(ch, "missing") == (None, None)
    assert cell_placement.is_capabilities_stack(source)
    assert not cell_placement.is_capabilities_stack(main)
    assert cell_placement.capabilities_stack(ch) is source
    assert cell_placement.capabilities_stack_cells(ch) == [source_cell]
    assert cell_placement.builtin_head(source) == 1
    # Open on the board, the stack is found the same way.
    ch.nav_stacks, ch.main_stacks = [], [main, source]
    assert cell_placement.capabilities_stack(ch) is source
    with pytest.raises(LookupError):
        cell_placement.capabilities_stack(Channel(name="None", main_stacks=[main]))
