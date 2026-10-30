"""Actual host emit work, independent of file-byte volume already in the cell."""
import hashlib
import uuid


def test_hash_cache_does_not_change_file_or_cell_equality__F0010_F0064(fresh_cwd):
    from cell_commits import file_content_hash
    from models import Cell, VirtualFile

    original = VirtualFile(name="same.txt", content="same")
    copy = original.model_copy(deep=True)
    cell = Cell(files=[original])
    copied_cell = cell.model_copy(deep=True)
    assert original == copy and cell == copied_cell
    file_content_hash(original)
    assert original == copy and cell == copied_cell
    changed = original.model_copy(update={"content": "changed"})
    assert changed != original
    changed.content = original.content
    assert changed == original
    assert "_content_hash_cache" not in original.model_dump()


def test_small_emit_keeps_unchanged_files_and_full_snapshot__F0142_F0010_F0064(fresh_cwd, monkeypatch):
    import cas
    import cell_commits
    import state
    import versions
    from database import get_db_connection
    from models import AppState, Cell, Channel, Stack, VirtualFile
    from process_manager import ProcessManager
    from runner_permissions import UNRESTRICTED

    files = [VirtualFile(name=f"file-{i}.txt", content=f"{i}:" + "x" * 10000, language="custom") for i in range(100)]
    cell = Cell(files=files)
    channel = Channel(name="scale", main_stacks=[Stack(cells=[cell])])
    from genesis import ensure_capabilities_stack
    ensure_capabilities_stack(channel)
    monkeypatch.setattr(state, "state", AppState(channels=[channel]))
    run_id = str(uuid.uuid4())
    conn = get_db_connection(str(channel.id))
    conn.execute("INSERT INTO runs(id,status) VALUES (?,'running')", (run_id,))
    from persistence import initialize_files, publish_files
    initialize_files(conn, str(cell.id), files)
    versions.synthesize_genesis_row(conn, str(cell.id), run_id, 0,
                                    files)
    conn.commit()
    conn.close()
    publish_files(channel, str(cell.id), {}, persist=False)
    files = list(cell.files)  # Identity begins at the actual accepted cache.
    state.persist_state()
    manager = ProcessManager(channel=str(channel.id), run_id=run_id, cell_id=str(cell.id),
                             app_dir=str(fresh_cwd), workspace_dir=str(fresh_cwd), entrypoint="unused.py",
                             generator_name="scale", runner_id="scale", runner_name="Scale", permissions=UNRESTRICTED)
    import builtin_packages
    builtin_packages.build()  # read once at server startup, never per emit
    changed_body = "a" * 1024
    changed_hash = hashlib.sha256(changed_body.encode()).hexdigest()
    untouched_bodies = {file.content.encode() for file in files}
    hashed, untouched_hashes, read = [], [], []
    original_hash, original_read = hashlib.sha256, cell_commits.blob_bytes
    def hash_bytes(data=b"", **kwargs):
        hashed.append(len(data))
        if data in untouched_bodies:
            untouched_hashes.append(len(data))
        return original_hash(data, **kwargs)
    monkeypatch.setattr(hashlib, "sha256", hash_bytes)

    def read_blob(digest):
        raw = original_read(digest)
        read.append((digest, len(raw or b"")))
        return raw

    monkeypatch.setattr(cell_commits, "blob_bytes", read_blob)
    monkeypatch.setattr(cas, "read_blob", read_blob)
    version, refused = manager._emit_partial(
        artifacts=[{"type": "memory", "cell_path": "delta.txt", "content": changed_body}],
        operation_id=str(uuid.uuid4()), writer_id="scale-test", edit_seq=1,
        base_revisions={"delta.txt": 0})
    assert not refused
    # Artifact admission, accepted staging, history integrity and decoded-cache
    # publication may verify changed bytes. Exact receipt/layout identity adds
    # bounded metadata work; none of these steps may rehash the original MiB.
    assert sum(hashed) <= 5 * len(changed_body) + 2048
    assert untouched_hashes == []
    assert sum(size for _, size in read) <= 2 * len(changed_body)
    assert all(digest == changed_hash for digest, _ in read)
    current = {f.name: f for f in cell.files}
    assert all(current[file.name] is file for file in files)
    assert all(current[file.name].language == "custom" for file in files)
    conn = get_db_connection(str(channel.id))
    from persistence import snapshot_files
    expected = {f['path']: f['body_hash'] for f in snapshot_files(conn, str(cell.id))}
    assert len(expected) == 101
    assert versions.tip_file_map(conn, version) == expected
    conn.close()


def test_run_event_flush_is_batched_ordered_and_empty_is_silent__F0140(monkeypatch):
    import events
    from process_manager.streams import StreamsMixin

    class Run(StreamsMixin):
        run_id = "run"
        channel = "channel"

    sent = []
    monkeypatch.setattr(events, "broadcast", lambda channel, event: sent.append((channel, event)))
    run = Run()
    run._broadcast_run_events([])
    assert not sent
    rows = [{"id": i, "event_type": "stdout", "payload": "text", "created_at": "now"} for i in range(1200)]
    run._broadcast_run_events(iter(rows))
    assert [len(event["events"]) for _, event in sent] == [500, 500, 200]
    assert [row["id"] for _, event in sent for row in event["events"]] == list(range(1200))
    assert all(event["type"] == "run_events" and event["run_id"] == "run" for _, event in sent)
