"""Conditional user writes and run-owned file boundaries.

Run UI paths are read-only. Copies exclude run folders, preserve binary
bytes and record source lineage.
"""
from __future__ import annotations

import uuid

import pytest

@pytest.fixture
def client(api_app):
    from fastapi.testclient import TestClient
    from models import Channel
    from state import state
    from database import get_db_connection
    with TestClient(api_app) as http:
        channel = Channel(name="Starting")
        state.channels = [channel]
        state.active_channel_id = channel.id
        get_db_connection(str(channel.id)).close()
        http.channel_id = str(channel.id)
        yield http


def _channel():
    from state import find_active_channel
    return str(find_active_channel().id)

LIVE = "tester-0badc0de"
DEAD = "gone-deadbeef"
CARD = f"_runs/{LIVE}/{LIVE}.run"
SURFACE = f"_runs/{LIVE}/panel/status.review_surface"


class _LiveRun:
    """What the registry reads of a live run."""


    def __init__(self, cell_id: str, label: str = LIVE):
        self.cell_id = cell_id
        self.label = label
        self.run_id = str(uuid.uuid4())
        self.log_buffer: list = []


@pytest.fixture
def live_run():
    from run_registry import active_managers
    registered: list[str] = []

    def _start(cell_id: str, label: str = LIVE) -> _LiveRun:
        run = _LiveRun(str(cell_id), label)
        active_managers[run.run_id] = run
        registered.append(run.run_id)
        return run

    yield _start
    for rid in registered:
        active_managers.pop(rid, None)


def _add_cell(files: dict[str, str], name: str = "Work"):
    from models import Cell, Stack, VirtualFile
    from state import get_active_channel
    ch = get_active_channel()
    cell = Cell(name=name, files=[VirtualFile(name=k, content=v) for k, v in files.items()])
    stack = Stack(name="S", cells=[cell])
    ch.main_stacks.append(stack)
    _admit(cell)
    return ch, stack, cell


def _admit(cell):
    from database import get_db_connection
    from persistence import initialize_files
    conn = get_db_connection(_channel())
    try:
        initialize_files(conn, str(cell.id), cell.files)
        conn.commit()
    finally:
        conn.close()


def _accepted(cell):
    from persistence import snapshot_files, read_file
    from database import get_db_connection
    conn = get_db_connection(_channel())
    try:
        files = snapshot_files(conn, str(cell.id))
    finally:
        conn.close()
    return {f['path']: read_file(_channel(), str(cell.id), f['path'])[1].decode()
            for f in files if f['encoding'] == 'utf-8'}


def _mutate(client, cell, path, text=None, *, delete=False, blob_hash=None):
    from persistence import current_file
    from database import get_db_connection
    conn = get_db_connection(_channel())
    try:
        current = current_file(conn, str(cell.id), path)
    finally:
        conn.close()
    mutation = dict(path=path, writer_id='lock-test', edit_seq=(current['revision'] + 1 if current else 1),
                    base_revision=current['revision'] if current else 0, kind='delete' if delete else 'put')
    if not delete:
        mutation.update({'blob_hash': blob_hash} if blob_hash is not None else {'content_text': text})
    return client.post(f'/api/channels/{_channel()}/cells/{cell.id}/files/mutate',
                       json=dict(operation_id=str(uuid.uuid4()), mutations=[mutation], resolves=[]))


def _tip_files(cell_id) -> dict[str, str]:
    from cell_commits import blob_bytes
    from versions import find_tip, tip_file_map
    from database import get_db_connection
    conn = get_db_connection(_channel())
    try:
        tip = tip_file_map(conn, find_tip(conn, str(cell_id)))
    finally:
        conn.close()
    return {p: (blob_bytes(h) or b"").decode("utf-8", "replace") for p, h in tip.items()}


def _seed_version(cell_id: str, files: dict[str, str]) -> None:
    """A committed version row for ``cell_id`` holding ``files`` (a history
    cell a run once wrote, run folders and all)."""
    from cell_commits import cell_commit_lock
    from database import get_db_connection
    from versions import synthesize_genesis_row
    from models import VirtualFile
    conn = get_db_connection(_channel())
    try:
        run_id = str(uuid.uuid4())
        conn.execute("INSERT INTO runs (id, prompt, status) VALUES (?, 'seed', 'completed')",
                     (run_id,))
        with cell_commit_lock(cell_id):
            synthesize_genesis_row(conn, cell_id, run_id, 1, [
                VirtualFile(name=p, content=c) for p, c in files.items()
            ])
        conn.commit()
    finally:
        conn.close()


def _mem_files(cell) -> dict[str, str]:
    return {f.name: f.content for f in cell.files}


def test_delete_of_run_ui_is_refused_and_normal_file_keeps_tombstone(client, live_run):
    from database import get_db_connection
    from persistence import current_file
    _ch, _stack, cell = _add_cell({'notes.md': 'n', SURFACE: '{}'})
    live_run(cell.id)
    response = _mutate(client, cell, SURFACE, delete=True)
    assert response.status_code == 409 and response.json()['detail'] == 'Run UI files are read-only'
    assert _mutate(client, cell, 'notes.md', delete=True).json()['outcome'] == 'accepted'
    assert _accepted(cell) == {SURFACE: '{}'}
    conn = get_db_connection(_channel())
    try:
        tombstone = current_file(conn, str(cell.id), 'notes.md')
        assert tombstone['deleted'] and tombstone['revision'] == 2
    finally:
        conn.close()


def test_reserved_path_is_refused_without_mutation(client):
    _ch, _stack, cell = _add_cell({'notes.md': 'n'})
    assert _mutate(client, cell, '.collrun/config/x', delete=True).status_code == 422
    assert _accepted(cell) == {'notes.md': 'n'}


@pytest.mark.parametrize('path', [SURFACE, CARD, f'_runs/{LIVE}/new.md', f'_runs/{DEAD}/x.chat', '_runs/x.md'])
def test_run_ui_put_is_refused_even_for_existing_or_inactive_paths(client, live_run, path):
    _ch, _stack, cell = _add_cell({SURFACE: 'old', CARD: '{}', f'_runs/{DEAD}/x.chat': '{}'})
    live_run(cell.id)
    before = _accepted(cell)
    response = _mutate(client, cell, path, 'edited')
    assert response.status_code == 409 and response.json()['detail'] == 'Run UI files are read-only'
    assert _accepted(cell) == before


def test_metadata_patch_does_not_change_run_owned_files(client, live_run):
    _ch, _stack, cell = _add_cell({'notes.md': 'n', SURFACE: 'stored', CARD: '{}'})
    live_run(cell.id)
    before = _accepted(cell)
    response = client.patch(f'/api/channels/{_channel()}/cells/{cell.id}/meta', json={'name': 'Renamed'})
    assert response.status_code == 200, response.text
    assert cell.name == 'Renamed' and _accepted(cell) == before


def test_checkpoint_keeps_current_revisions_while_run_is_live(client, live_run):
    from persistence import snapshot_files
    from database import get_db_connection
    _ch, _stack, cell = _add_cell({'notes.md': 'old'})
    assert _mutate(client, cell, 'notes.md', 'new').json()['outcome'] == 'accepted'
    live_run(cell.id)
    response = client.post(f'/api/channels/{_channel()}/cells/{cell.id}/checkpoint')
    assert response.json() == {'created': False, 'version_id': None}
    assert _accepted(cell) == {'notes.md': 'new'}
    conn = get_db_connection(_channel())
    try:
        assert snapshot_files(conn, str(cell.id))[0]['file_revision'] == 2
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Cross-cell copies never carry _runs/
# ---------------------------------------------------------------------------

RUN_FILES = {SURFACE: "s", CARD: "{}", f"_runs/{DEAD}/old.chat": "dead"}


def _binary_cell():
    from cas import hash_and_store_bytes
    from models import VirtualFile
    ch, stack, cell = _add_cell({"notes.md": "n", **RUN_FILES})
    digest = hash_and_store_bytes(b"\x00\xffbinary")
    cell.files.append(VirtualFile(name="img.bin", content="", language="binary",
                                  blob_hash=digest))
    _admit(cell)
    return ch, stack, cell, digest


def _copy_ok(cell, digest) -> None:
    files = {f.name: f for f in cell.files}
    assert set(files) == {"notes.md", "img.bin"}
    assert files["img.bin"].blob_hash == digest


def test_duplicate_cell_in_stack_strips_run_folders_and_keeps_binaries_F0286(client, live_run):
    ch, stack, cell, digest = _binary_cell()
    live_run(cell.id)
    r = client.post(f"/api/stacks/{client.channel_id}/{stack.id}/duplicate_cell_in_stack/0")
    assert r.status_code == 200, r.text
    assert len(stack.cells) == 2
    _copy_ok(stack.cells[1], digest)


def test_duplicate_stack_strips_run_folders_and_keeps_binaries(client, live_run):
    ch, stack, cell, digest = _binary_cell()
    live_run(cell.id)
    r = client.post(f"/api/stacks/{client.channel_id}/{stack.id}/duplicate_stack")
    assert r.status_code == 200, r.text
    idx = next(i for i, s in enumerate(ch.main_stacks) if s.id == stack.id)
    _copy_ok(ch.main_stacks[idx + 1].cells[0], digest)


def test_duplicate_stack_copies_accepted_current_files(client):
    ch, stack, cell = _add_cell({"notes.md": "old"})
    assert _mutate(client, cell, "notes.md", "new").json()["outcome"] == "accepted"
    r = client.post(f"/api/stacks/{client.channel_id}/{stack.id}/duplicate_stack")
    assert r.status_code == 200, r.text
    idx = next(i for i, s in enumerate(ch.main_stacks) if s.id == stack.id)
    assert _mem_files(ch.main_stacks[idx + 1].cells[0]) == {"notes.md": "new"}


def _moves() -> list[tuple]:
    from database import get_db_connection
    conn = get_db_connection(_channel())
    try:
        return [tuple(r) for r in conn.execute(
            "SELECT op, op_id, from_cell, from_version, to_cell, to_version, ord FROM cell_moves ORDER BY id")]
    finally:
        conn.close()


def test_duplicate_cell_in_stack_records_the_move_from_the_accepted_checkpoint(client):
    from database import get_db_connection
    from versions import find_tip
    ch, stack, cell = _add_cell({"notes.md": "old"})
    assert _mutate(client, cell, "notes.md", "new").json()["outcome"] == "accepted"
    r = client.post(f"/api/stacks/{client.channel_id}/{stack.id}/duplicate_cell_in_stack/0")
    assert r.status_code == 200, r.text
    copy = stack.cells[1]
    conn = get_db_connection(_channel())
    try:
        tip = find_tip(conn, str(cell.id))
        tip_run = conn.execute("SELECT run_id FROM cells WHERE id=?", (tip,)).fetchone()[0]
    finally:
        conn.close()
    assert _tip_files(cell.id) == {"notes.md": "new"}
    [move] = _moves()
    op_id = move[1]
    assert move == ("duplicate", op_id, str(cell.id), tip, str(copy.id), None, 0)
    conn = get_db_connection(_channel())
    try:
        assert {row[0] for row in conn.execute(
            "SELECT prompt FROM runs WHERE id IN (?,?)", (tip_run, op_id))} == {"duplicate"}
    finally:
        conn.close()


def test_duplicate_stack_records_one_move_per_cell_under_one_op_O03(client):
    from database import get_db_connection
    from models import Cell, VirtualFile
    ch, stack, first = _add_cell({"a.md": "a"}, name="First")
    second = Cell(name="Second", files=[VirtualFile(name="b.md", content="b")])
    stack.cells.append(second)
    _admit(second)
    r = client.post(f"/api/stacks/{client.channel_id}/{stack.id}/duplicate_stack")
    assert r.status_code == 200, r.text
    idx = next(i for i, s in enumerate(ch.main_stacks) if s.id == stack.id)
    copies = [str(c.id) for c in ch.main_stacks[idx + 1].cells]
    moves = _moves()
    op_id = moves[0][1]
    # The copy operation pins both accepted source checkpoints.
    assert moves == [("duplicate", op_id, str(src.id), str(src.id), copy, None, i)
                     for i, (src, copy) in enumerate(zip([first, second], copies))]
    conn = get_db_connection(_channel())
    try:
        assert conn.execute("SELECT prompt FROM runs WHERE id=?", (op_id,)).fetchone()[0] == "duplicate"
        checkpoint_runs = {r[0] for r in conn.execute("SELECT run_id FROM cells WHERE id IN (?, ?)",
                                                      (str(first.id), str(second.id)))}
        assert len(checkpoint_runs) == 1
        assert conn.execute("SELECT prompt FROM runs WHERE id=?", (next(iter(checkpoint_runs)),)).fetchone()[0] == "duplicate"
    finally:
        conn.close()


def test_duplicate_cell_strips_run_folders(client, live_run):
    ch, stack, cell, digest = _binary_cell()
    live_run(cell.id)
    r = client.post(f"/api/channels/{_channel()}/cells/{cell.id}/duplicate", json={})
    assert r.status_code == 200, r.text
    new = next(c for s in ch.main_stacks for c in s.cells if str(c.id) == r.json()["new_cell_id"])
    _copy_ok(new, digest)
    # The source keeps its run folder.
    assert SURFACE in _mem_files(cell)


def test_fork_strips_run_folders_F0286(client):
    ch, _stack, cell = _add_cell({"notes.md": "n"})
    _seed_version(str(cell.id), {"notes.md": "n", **RUN_FILES})
    r = client.post(f"/api/channels/{_channel()}/cells/{cell.id}/fork",
                    json={"target_cell_id": str(cell.id)})
    assert r.status_code == 200, r.text
    new_id = r.json()["new_cell_id"]
    new = next(c for s in ch.main_stacks for c in s.cells if str(c.id) == new_id)
    assert _mem_files(new) == {"notes.md": "n"}
    assert _tip_files(new_id) == {"notes.md": "n"}


def test_merge_strips_dead_run_folders(client):
    from models import Cell, VirtualFile
    ch, stack, cell = _add_cell({"notes.md": "n", f"_runs/{DEAD}/old.chat": "dead"}, name="A")
    second = Cell(name="B", files=[VirtualFile(name="b.md", content="b")])
    stack.cells.append(second)
    _admit(second)
    r = client.post(f"/api/channels/{_channel()}/merge", json={"stack_id": str(stack.id)})
    assert r.status_code == 200, r.text
    names = {f.name for f in stack.cells[0].files}
    assert names == {"A/notes.md", "B/b.md", ".options_stack"}


def test_export_strips_run_folders(client):
    _ch, _stack, cell = _add_cell({"notes.md": "n", f"_runs/{DEAD}/old.chat": "dead"})
    response = client.get(f"/api/channels/{_channel()}/export/manifest",
                          params={"scope": "cell", "cell_id": str(cell.id)})
    assert response.status_code == 200, response.text
    names = [entry["path"] for entry in response.json()["entries"]]
    assert names == ["Work/notes.md"]
    assert not any("_runs" in name for name in names + response.json()["dirs"])


# ---------------------------------------------------------------------------
# Restoring a removed cell sweeps its dead run folders
# ---------------------------------------------------------------------------

def test_restore_cell_sweeps_dead_run_folders(client):
    from state import get_active_channel
    ch, stack, cell = _add_cell({"notes.md": "n", f"_runs/{DEAD}/old.chat": "dead"})
    assert client.post(f"/api/stacks/{client.channel_id}/{stack.id}/remove").status_code == 200
    # The removal version already dropped the dead folder from the tip.
    assert _tip_files(cell.id) == {"notes.md": "n"}
    # A late user write to a removed cell cannot recreate its files.
    assert _mutate(client, cell, f"_runs/{DEAD}/late.chat", "late").status_code == 404
    r = client.post(f"/api/channels/{_channel()}/cells/{cell.id}/restore")
    assert r.status_code == 200, r.text
    restored = next(c for s in get_active_channel().main_stacks for c in s.cells
                    if c.id == cell.id)
    assert _mem_files(restored) == {"notes.md": "n"}


def test_restore_cell_sweeps_a_tip_written_mid_run(client):
    from state import get_active_channel
    ch, stack, cell = _add_cell({"notes.md": "n"})
    assert client.post(f"/api/stacks/{client.channel_id}/{stack.id}/remove").status_code == 200
    # A later version of the chain (a crash left a run folder in the tip).
    from cell_commits import cell_commit_lock
    from versions import find_tip, insert_version, tip_file_map, version_file_entries
    from cas import hash_and_store_bytes
    from database import get_db_connection
    conn = get_db_connection(_channel())
    try:
        # The crashed run the dead label names (its short id is the label's).
        run_id = "deadbeef" + str(uuid.uuid4())[8:]
        conn.execute("INSERT INTO runs (id, prompt, status) VALUES (?, 'crash', 'failed')",
                     (run_id,))
        with cell_commit_lock(str(cell.id)):
            tip = find_tip(conn, str(cell.id))
            parent = tip_file_map(conn, tip)
            files = {**parent, f"_runs/{DEAD}/x.chat": hash_and_store_bytes(b"{}") or ""}
            entries = [dict(entry) for entry in version_file_entries(conn, tip)]
            entries.append({"path": f"_runs/{DEAD}/x.chat", "body_hash": files[f"_runs/{DEAD}/x.chat"],
                            "encoding": "utf-8", "language": "text"})
            insert_version(conn, tip_id=tip, parent_files=parent, run_id=run_id,
                           files=files, file_entries=entries, version_name="mid-run", source_type="generator",
                           generator_name="g")
            from models import VirtualFile
            from persistence import restore_snapshot
            restore_snapshot(conn, str(cell.id), [VirtualFile(name="notes.md", content="n"),
                                                  VirtualFile(name=f"_runs/{DEAD}/x.chat", content="{}")])
        conn.commit()
    finally:
        conn.close()
    assert f"_runs/{DEAD}/x.chat" in _tip_files(cell.id)
    r = client.post(f"/api/channels/{_channel()}/cells/{cell.id}/restore")
    assert r.status_code == 200, r.text
    restored = next(c for s in get_active_channel().main_stacks for c in s.cells
                    if c.id == cell.id)
    assert _mem_files(restored) == {"notes.md": "n"}
    assert _tip_files(cell.id) == {"notes.md": "n"}
    # What the sweep removed is kept in the crashed run's Logs.
    conn = get_db_connection(_channel())
    try:
        logged = {r["filepath"] for r in conn.execute(
            "SELECT filepath FROM run_artifacts WHERE run_id=? AND category='log'", (run_id,))}
    finally:
        conn.close()
    assert logged == {f"_runs/{DEAD}/x.chat"}


# ---------------------------------------------------------------------------
# Structural gates keep asking "does the cell have ANY live run"
# ---------------------------------------------------------------------------

def test_remove_and_merge_refuse_a_cell_with_a_live_run(client, live_run):
    from models import Cell
    _ch, stack, cell = _add_cell({"notes.md": "n"})
    below = Cell(name="below")
    stack.cells.append(below)
    _admit(below)
    live_run(cell.id)
    assert client.post(f"/api/stacks/{client.channel_id}/{stack.id}/remove").status_code == 409
    assert client.post(f"/api/channels/{_channel()}/cells/{cell.id}/remove").status_code == 409
    assert client.post(f"/api/channels/{_channel()}/merge",
                       json={"stack_id": str(stack.id)}).status_code == 409
    # The other cell of the stack has no run: it may go.
    assert client.post(f"/api/channels/{_channel()}/cells/{below.id}/remove").status_code == 200
