"""Host half of hash-only live_files and selected-body reads."""
import asyncio
import hashlib
import uuid

import pytest


@pytest.fixture
def host(fresh_cwd):
    from database import get_db_connection
    from versions import synthesize_genesis_row
    from process_manager import ProcessManager
    from runner_permissions import UNRESTRICTED

    run_id, cell_id = str(uuid.uuid4()), str(uuid.uuid4())
    from models import Cell, Channel, Stack
    from state import state
    cell = Cell(id=cell_id)
    channel = Channel(name="hashes",main_stacks=[Stack(cells=[cell])])
    state.channels = [channel]
    state.active_channel_id = channel.id
    conn = get_db_connection(str(channel.id))
    conn.execute("INSERT INTO runs(id,status) VALUES (?,'running')", (run_id,))
    from persistence import initialize_files, restore_snapshot
    from models import VirtualFile
    original_files = [VirtualFile(name="a.txt", content="old"),
                      VirtualFile(name="b.txt", content="unchanged"),
                      VirtualFile(name="gone.txt", content="gone")]
    initialize_files(conn, cell_id, original_files)
    synthesize_genesis_row(conn, cell_id, run_id, 0, original_files)
    restore_snapshot(conn,cell_id,[VirtualFile(name="a.txt",content="saved"),
                                   VirtualFile(name="b.txt",content="unchanged")],writer_id="host:test")
    conn.commit()
    conn.close()
    manager = ProcessManager(channel=str(channel.id), run_id=run_id, cell_id=cell_id,
                             app_dir=str(fresh_cwd), workspace_dir=str(fresh_cwd), entrypoint="unused.py",
                             generator_name="test", runner_id="test", runner_name="Test", permissions=UNRESTRICTED)
    replies = []
    async def ack(reply, request):
        replies.append(reply)
    manager._send_ack = ack
    return manager, replies


def test_hash_request_returns_accepted_revisions_without_body_reads__F0098_F0099_F0121(host, monkeypatch):
    manager, replies = host
    import builtins
    original = builtins.open
    def guarded(path, *args, **kwargs):
        assert ".collimate/objects" not in str(path), "hash manifest read CAS body"
        return original(path, *args, **kwargs)
    monkeypatch.setattr(builtins, "open", guarded)
    asyncio.run(manager._op_read_cell_hashes({"req_id": "hashes"}))
    assert replies[0]['ok']
    files=replies[0]['hashes']
    assert set(files)=={'a.txt','b.txt','gone.txt'}
    assert files['gone.txt']['deleted'] is True and files['gone.txt']['file_revision']==2
    assert files['gone.txt']['body_hash'] is None
    assert files['a.txt']==dict(body_hash=hashlib.sha256(b'saved').hexdigest(),file_revision=2,encoding='utf-8',deleted=False)
    assert files['b.txt']['body_hash']==hashlib.sha256(b'unchanged').hexdigest()



@pytest.mark.parametrize("paths,expected", [([], {}), (["a.txt", "missing.txt"], {"a.txt": "saved"}), (["b.txt"], {"b.txt": "unchanged"})])
def test_selected_body_read_is_exact__F0099(host, paths, expected):
    manager, replies = host
    import base64
    asyncio.run(manager._op_read_cell_files({"req_id": "files", "paths": paths}))
    assert "error" not in replies[0]
    assert {path:base64.b64decode(file["content"]).decode("utf-8")
            for path,file in replies[0]["files"].items()} == expected
    assert all(file["file_revision"]>=1 for file in replies[0]["files"].values())


@pytest.mark.parametrize("paths", [["../outside"], ["/absolute"], ["a\\b"], "a.txt", [12]])
def test_hash_path_filter_refuses_invalid_paths__F0099(host, paths):
    manager, replies = host
    asyncio.run(manager._op_read_cell_hashes({"req_id": "hashes", "paths": paths}))
    assert replies[0].get("error")


def test_chat_matching_body_keeps_workspace_mtime__F0106(host, fresh_cwd):
    import os
    manager, _ = host
    path = fresh_cwd / "same.txt"
    path.write_bytes(b"same")
    os.utime(path, (1000, 1000))
    assert manager._place_in_workspace(str(fresh_cwd), "same.txt", data=b"same")
    assert path.stat().st_mtime == 1000
