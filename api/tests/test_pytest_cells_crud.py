"""Coverage for routes/cells.py.

The router exposes the run-node history view,
files-tree reads, /checkpoint
(group accepted revisions → version row), and /fork. All of those walk
``cell_edges`` (parent_key='previous_version' for chains; fork and
duplicate provenance is the ``cell_moves`` lineage record), so a
manually-seeded chain in the DB exercises the whole machinery without
needing a real generator run.

The fixture client boots a fresh app with a registered synthetic channel.
Synthetic cells initialize accepted SQL references before entering the canvas.
History chains use immutable CAS bodies and the real version schema.
"""
from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

import pytest

from database import get_db_connection


@pytest.fixture
def client(api_app):
    """Real API lifecycle; these route cases need no shipped definitions."""
    from fastapi.testclient import TestClient
    from genesis import ensure_capabilities_stack
    from models import Channel
    from state import state, persist_state, flush_persist_state

    with TestClient(api_app, raise_server_exceptions=False) as http:
        channel = Channel(name="Starting")
        ensure_capabilities_stack(channel)
        state.channels = [channel]
        state.active_channel_id = channel.id
        get_db_connection(str(channel.id)).close()
        persist_state()
        flush_persist_state()
        http.channel_id = str(channel.id)
        yield http


def _seed_version_chain(client, chain_len: int = 3) -> list[str]:
    """Insert `chain_len` cells wired as a previous_version chain.

    Returns the list of cell ids, oldest (head) first. Each cell gets one
    file whose content is "v<idx>" stored in the CAS. Matches the shape
    the routes in cells.py expect (sequence_num, version_name,
    source_type, cell_edges with parent_key).
    """
    conn = get_db_connection(client.channel_id)
    try:
        from models import VirtualFile
        from versions import synthesize_genesis_row
        run_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO runs (id, prompt, status) VALUES (?, 'seed', 'completed')",
            (run_id,),
        )
        ids: list[str] = []
        for i in range(chain_len):
            cid = str(uuid.uuid4())
            assert synthesize_genesis_row(conn, cid, run_id, i + 1,
                                         [VirtualFile(name="main.py", content=f"v{i}\n", language="python")])
            conn.execute(
                "UPDATE cells SET generator_name='test',version_name=?,source_type='generator' WHERE id=?",
                (f"v{i}", cid),
            )
            ids.append(cid)
        # Wire the chain: ids[i] -previous_version-> ids[i+1]
        for parent, child in zip(ids, ids[1:]):
            conn.execute(
                "INSERT INTO cell_edges "
                "(parent_cell_id, child_cell_id, parent_key) "
                "VALUES (?, ?, 'previous_version')",
                (parent, child),
            )
        from persistence import initialize_files
        initialize_files(conn, ids[0], [VirtualFile(name="main.py",content=f"v{chain_len-1}\n",language="python")])
        conn.commit()
        return ids
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# /history — the run-node view of the version chain
# ---------------------------------------------------------------------------

def test_history_collapses_system_versions_into_one_files_node(client):
    """Seed a 3-cell previous_version chain of bookkeeping (system-run)
    versions, then GET /history: they collapse into ONE files node whose
    versions run oldest→newest, keyed by a stable node_id."""
    ids = _seed_version_chain(client, 3)
    head = ids[0]

    r = client.get(f"/api/channels/{client.channel_id}/cells/{head}/history")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_versions"] == 3
    nodes = body["nodes"]
    assert len(nodes) == 1, f"expected 1 files node, got {len(nodes)}"
    node = nodes[0]
    assert node["kind"] == "files"
    assert node["run"] is None
    assert node["config"] == [] and node["logs"] == []
    assert node["node_id"] == f"files:{ids[-1]}"
    vs = node["versions"]
    assert [v["cell_id"] for v in vs] == ids  # oldest → newest within node
    for idx, entry in enumerate(vs):
        assert entry["name"] == f"v{idx}"
        assert entry["file_count"] == 1
        assert "run_id" not in entry  # internal grouping key, never shipped
    assert body["current"]["node_id"] == node["node_id"]
    assert body["current"]["version_cell_id"] == ids[-1]


def test_history_single_entry_for_unchained_cell(client):
    """A cell with no outgoing previous_version edge is one files node
    holding itself."""
    ids = _seed_version_chain(client, 1)
    r = client.get(f"/api/channels/{client.channel_id}/cells/{ids[0]}/history")
    assert r.status_code == 200
    body = r.json()
    assert body["total_versions"] == 1
    assert len(body["nodes"]) == 1
    assert [v["cell_id"] for v in body["nodes"][0]["versions"]] == ids


# ---------------------------------------------------------------------------
# /files-tree — files at a version
# ---------------------------------------------------------------------------

def test_files_tree_returns_immutable_body_reference_with_language(client):
    """files-tree returns the explicit immutable type and body reference."""
    ids = _seed_version_chain(client, 1)
    r = client.get(f"/api/channels/{client.channel_id}/cells/{ids[0]}/files-tree")
    assert r.status_code == 200
    body = r.json()
    assert body["cell_id"] == ids[0]
    assert len(body["files"]) == 1
    f = body["files"][0]
    assert f["name"] == "main.py"
    assert f["language"] == "python"
    assert "content" not in f
    assert f["body_hash"] == hashlib.sha256(b"v0\n").hexdigest()
    assert f["encoding"] == "utf-8"
    pinned = client.get(f"/api/channels/{client.channel_id}/versions/{ids[0]}/files/main.py")
    assert pinned.status_code == 200 and pinned.text == "v0\n"


# ---------------------------------------------------------------------------
# remove and restore — a versioned cell comes back at its tip
# ---------------------------------------------------------------------------

def _mount(client, cell_id: str, files: dict[str, str] | None = None):
    """Put the DB cell ``cell_id`` on the canvas in its own stack."""
    from models import Cell, Stack, VirtualFile, language_for
    from state import state
    ch = next(c for c in state.channels if c.id == state.active_channel_id)
    cell = Cell(id=cell_id, name="src",
                files=[VirtualFile(name=k, content=v, language=language_for(k))
                       for k, v in (files or {}).items()])
    ch.main_stacks.append(Stack(cells=[cell]))
    _initialize_cell(client, cell)
    return ch


def test_a_removed_versioned_cell_restores_at_its_tip_O02(client):
    """Remove by id, then restore: the cell comes back under its own id in a
    new stack at the right end, carrying its version tip's files."""
    ids = _seed_version_chain(client, 2)
    head = ids[0]
    ch = _mount(client, head, {"main.py": "v1\n"})

    r = client.post(f"/api/channels/{client.channel_id}/cells/{head}/remove")
    assert r.status_code == 200, r.text
    assert all(str(c.id) != head for s in ch.main_stacks for c in s.cells)

    r = client.post(f"/api/channels/{client.channel_id}/cells/{head}/restore")
    assert r.status_code == 200, r.text
    assert r.json()["cell_id"] == head
    [placed] = ch.main_stacks[-1].cells
    assert str(placed.id) == head
    assert {f.name: f.content for f in placed.files} == {"main.py": "v1\n"}


def test_restore_refuses_a_cell_in_a_minimized_stack_and_an_unknown_id_O02(client):
    """A cell in a minimized (nav) stack is in the layout: 409, and the main
    canvas gains no second copy. An id with no removed record is 404."""
    from cell_placement import capabilities_stack
    from models import Cell
    from state import state
    head = _seed_version_chain(client, 1)[0]
    ch = next(c for c in state.channels if c.id == state.active_channel_id)
    capabilities_stack(ch).cells.append(Cell(id=head, name="src"))
    before = len(ch.main_stacks)
    r = client.post(f"/api/channels/{client.channel_id}/cells/{head}/restore")
    assert r.status_code == 409, r.text
    assert len(ch.main_stacks) == before
    r = client.post(f"/api/channels/{client.channel_id}/cells/{uuid.uuid4()}/restore")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# /checkpoint — group accepted revisions without changing their ownership
# ---------------------------------------------------------------------------


def _initialize_cell(client, cell):
    from persistence import initialize_files, publish_files
    conn = get_db_connection(client.channel_id)
    try:
        initialize_files(conn, str(cell.id), cell.files)
        conn.commit()
    finally:
        conn.close()
    publish_files(client.channel_id, str(cell.id), {}, persist=False)


def _put_file(client, cell_id, path, text, base=0):
    request = {"operation_id":str(uuid.uuid4()),"mutations":[{
        "path":path,"base_revision":base,"writer_id":"fixture-user","edit_seq":1,
        "kind":"put","content_text":text,"language":"plaintext"}],"resolves":[]}
    response = client.post(f"/api/channels/{client.channel_id}/cells/{cell_id}/files/mutate", json=request)
    assert response.status_code == 200, response.text
    assert response.json()["outcome"] == "accepted", response.text
    return response.json()


def test_checkpoint_without_accepted_changes_is_noop(client):
    head = _seed_version_chain(client, 1)[0]
    _mount(client, head, {"main.py":"v0\n"})
    response = client.post(f"/api/channels/{client.channel_id}/cells/{head}/checkpoint")
    assert response.status_code == 200
    assert response.json() == {"created":False,"version_id":None}


def test_checkpoint_with_new_accepted_file_creates_version(client):
    head = _seed_version_chain(client, 1)[0]
    _mount(client, head, {"main.py":"v0\n"})
    receipt = _put_file(client, head, 'new_file.md', 'accepted text')
    response = client.post(f"/api/channels/{client.channel_id}/cells/{head}/checkpoint")
    assert response.status_code == 200 and response.json()["created"] is True
    version = response.json()["version_id"]
    history = client.get(f"/api/channels/{client.channel_id}/cells/{head}/history").json()
    assert history["total_versions"] == 2
    newest = history["nodes"][0]["versions"][-1]
    assert newest["cell_id"] == version and newest["source_type"] == "manual_edit"
    current = client.get(f"/api/channels/{client.channel_id}/cells/{head}/files").json()["files"]
    assert next(file for file in current if file["path"] == 'new_file.md') == receipt["files"][0]
    assert client.get(f"/api/channels/{client.channel_id}/versions/{version}/files/new_file.md").text == 'accepted text'


def test_checkpoint_preserves_large_accepted_file_revision_and_body_cache(client):
    cell = _canvas_cell(client)
    assert client.post(f"/api/channels/{client.channel_id}/cells/{cell.id}/checkpoint").status_code == 200
    _put_file(client, cell.id, 'large.md', 'x'*60_000)
    before = next(file for file in cell.files if file.name == 'large.md')
    metadata = (before.file_revision, before.body_hash, before.writer_id, before.edit_seq)
    response = client.post(f"/api/channels/{client.channel_id}/cells/{cell.id}/checkpoint")
    assert response.status_code == 200 and response.json()["created"] is True
    after = next(file for file in cell.files if file.name == 'large.md')
    assert after is before and after.content == 'x'*60_000
    assert (after.file_revision, after.body_hash, after.writer_id, after.edit_seq) == metadata
    again = client.post(f"/api/channels/{client.channel_id}/cells/{cell.id}/checkpoint")
    assert again.json() == {"created":False,"version_id":None}


# ---------------------------------------------------------------------------
# /fork — branch a version
# ---------------------------------------------------------------------------

def test_fork_records_a_duplicate_move_from_the_forked_version_O03(client):
    """POST /fork on a version creates a new cell in a new stack and records
    the lineage: op 'duplicate' from the canvas cell, ``from_version`` the
    forked version, the branch's own root as ``to_version``; no version
    edge. The source must be on the canvas (fork places the branch beside
    its stack)."""
    from models import Cell, Stack
    from state import state
    ids = _seed_version_chain(client, 2)
    head = ids[0]
    ch = next(c for c in state.channels if c.id == state.active_channel_id)
    ch.main_stacks.append(Stack(cells=[Cell(id=head, name="src")]))

    r = client.post(
        f"/api/channels/{client.channel_id}/cells/{head}/fork",
        json={"target_cell_id": ids[1]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    new_id = body["new_cell_id"]
    assert body["forked_from"] == ids[1]

    conn = get_db_connection(client.channel_id)
    try:
        move = conn.execute(
            "SELECT op, op_id, from_cell, from_version, to_cell, to_version FROM cell_moves",
        ).fetchall()
        branch_run = conn.execute("SELECT run_id FROM cells WHERE id=?", (new_id,)).fetchone()[0]
        edges = conn.execute("SELECT COUNT(*) FROM cell_edges WHERE child_cell_id=?", (new_id,)).fetchone()[0]
    finally:
        conn.close()
    assert [tuple(m) for m in move] == [("duplicate", branch_run, head, ids[1], new_id, new_id)]
    assert edges == 0


def test_fork_rejects_target_outside_chain(client):
    """target_cell_id must be part of the visible cell's version chain.
    Unrelated cell ids get a 404."""
    ids = _seed_version_chain(client, 2)
    head = ids[0]
    _mount(client, head, {"main.py": "v1\n"})

    other = _seed_version_chain(client, 1)[0]
    r = client.post(
        f"/api/channels/{client.channel_id}/cells/{head}/fork",
        json={"target_cell_id": other},
    )
    assert r.status_code == 404


def _branch_count(client) -> int:
    conn = get_db_connection(client.channel_id)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM cells WHERE source_type='branch'",
        ).fetchone()[0]
    finally:
        conn.close()


def test_fork_without_source_on_canvas_leaves_no_branch(client):
    """A fork whose source is not on the main canvas is rejected before any
    DB write: no orphan branch cell."""
    ids = _seed_version_chain(client, 2)
    before = _branch_count(client)
    r = client.post(
        f"/api/channels/{client.channel_id}/cells/{ids[0]}/fork",
        json={"target_cell_id": ids[1]},
    )
    assert r.status_code == 404, r.text
    assert _branch_count(client) == before


def test_fork_of_nav_stack_cell_is_404_without_orphan(client):
    """A source in a minimized (nav) stack is not on the main canvas: 404
    (not a 500 from the placement helper) and no committed branch."""
    from cell_placement import capabilities_stack
    from models import Cell
    from state import state
    ids = _seed_version_chain(client, 2)
    ch = next(c for c in state.channels if c.id == state.active_channel_id)
    capabilities_stack(ch).cells.append(Cell(id=ids[0], name="src"))
    before = _branch_count(client)
    r = client.post(
        f"/api/channels/{client.channel_id}/cells/{ids[0]}/fork",
        json={"target_cell_id": ids[1]},
    )
    assert r.status_code == 404, r.text
    assert _branch_count(client) == before


# ---------------------------------------------------------------------------
# Unknown channels — resolve-or-404 before any db is opened
# ---------------------------------------------------------------------------

# Fixed ids keep the parametrize node ids stable (``--lf``, ``-k``).
_CELL = "00000000-0000-4000-8000-000000000001"
_RUN = "00000000-0000-4000-8000-000000000002"
_CHANNEL_ROUTES = [
    ("GET", f"cells/{_CELL}/history", None),
    ("GET", f"cells/{_CELL}/files-tree", None),
    ("POST", f"cells/{_CELL}/checkpoint", None),
    ("POST", f"cells/{_CELL}/fork", {"target_cell_id": _CELL}),
    ("GET", f"cells/{_CELL}/files/a.txt", None),
    ("GET", "runs", None),
    ("GET", "runs/active", None),
    ("GET", f"runs/{_RUN}", None),
    ("GET", f"runs/{_RUN}/events_since", None),
    ("GET", f"runs/{_RUN}/artifacts", None),
    ("GET", f"cells/{_CELL}/files", None),
    ("POST", f"cells/{_CELL}/duplicate", {}),
    ("POST", f"cells/{_CELL}/restore", None),
    ("POST", f"cells/{_CELL}/remove", None),
    ("POST", "import", {"stacks": [{"name": "s", "cells": [{"name": "c", "files": [], "dirs": []}]}]}),
    ("GET", "outline", None),
    ("GET", "export/manifest?scope=all", None),
    ("GET", "export.zip?scope=all", None),
    ("GET", "export.zip?scope=tag&tag=x", None),
    ("POST", "merge", {"stack_id": _CELL}),
    ("POST", "split", {"cell_id": _CELL}),
    ("POST", f"cells/{_CELL}/files/mutate", {"operation_id":_RUN,"mutations":[{"path":"a.txt","base_revision":0,"writer_id":"fixture","edit_seq":1,"kind":"delete"}],"resolves":[]}),
]


def _channel_db_files() -> set[str]:
    """Every ``*.db`` in the channels folder, dotfiles included: a ``...``
    channel param used to become ``...db``, which the startup reconcile's
    ``glob.glob`` never lists."""
    return {p.name for p in Path(".collimate/channels").iterdir() if p.name.endswith(".db")}


@pytest.mark.parametrize("method,path,body", _CHANNEL_ROUTES)
def test_channel_routes_404_an_unknown_channel_without_a_stray_db__F0258_F0266_F0030(
    client, method, path, body,
):
    """A stale tab or a typo'd channel must not mint a channel db file (a
    symbol-only name used to become ``!!!.db`` or the dotfile ``...db``):
    every channel-scoped read route, the cell, canvas and merge writes
    and the draft autosave writes resolve the channel first
    (``state.require_channel``) and 404."""
    before = _channel_db_files()
    for ghost in ("no-such-chan", "%21%21%21", "..."):
        r = client.request(method, f"/api/channels/{ghost}/{path}", json=body)
        assert r.status_code == 404, (ghost, path, r.status_code, r.text)
        assert _channel_db_files() == before, ghost


def test_conditional_writes_keep_the_channel_uuid_after_rename(client):
    from state import state
    channel = next(c for c in state.channels if c.id == state.active_channel_id)
    cell = _canvas_cell(client)
    before = _channel_db_files()
    response = client.put(f"/api/channels/{channel.id}/rename", params={"name":"Renamed"})
    assert response.status_code == 200
    assert _channel_db_files() == before == {f"{channel.id}.db"}
    request = {"operation_id":str(uuid.uuid4()),"mutations":[{"path":"notes.md","base_revision":1,"writer_id":"user","edit_seq":1,"kind":"put","content_text":"late edit"}],"resolves":[]}
    stale = client.post(f"/api/channels/starting/cells/{cell.id}/files/mutate", json=request)
    assert stale.status_code == 404 and _channel_db_files() == before
    accepted = client.post(f"/api/channels/{channel.id}/cells/{cell.id}/files/mutate", json=request)
    assert accepted.status_code == 200 and accepted.json()["outcome"] == 'accepted'
    assert _channel_db_files() == before
    assert client.get(f"/api/channels/{channel.id}/cells/{cell.id}/files/notes.md?revision=2").text == 'late edit'


# ---------------------------------------------------------------------------
# PATCH /cells/{id}/meta — name and icon only
# ---------------------------------------------------------------------------

def _canvas_cell(client, *, on_capabilities: bool = False, **cell_fields):
    """A cell with one file in a new board stack, or on the channel's
    Capabilities stack."""
    from cell_placement import capabilities_stack
    from models import Cell, Stack, VirtualFile
    from state import state

    cell = Cell(files=[VirtualFile(name="notes.md", content="ORIGINAL")], **cell_fields)
    ch = next(c for c in state.channels if c.id == state.active_channel_id)
    if on_capabilities:
        capabilities_stack(ch).cells.append(cell)
    else:
        ch.main_stacks.append(Stack(cells=[cell]))
    _initialize_cell(client, cell)
    return cell


def test_patch_meta_renames_without_touching_files__F0546(client):
    """A rename carries no files, so a file another writer changed after the
    client cached the cell survives it (a whole-cell update_top echo of the
    stale cache would revert it)."""
    from models import VirtualFile

    cell = _canvas_cell(client, name="Old")
    before = cell.updated_at
    # Another writer lands after the client last saw the cell.
    cell.files = [VirtualFile(name="notes.md", content="CHANGED"),
                  VirtualFile(name="added.md", content="NEW")]

    r = client.patch(f"/api/channels/{client.channel_id}/cells/{cell.id}/meta", json={"name": "Renamed"})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Renamed"
    assert cell.name == "Renamed"
    assert {f.name: f.content for f in cell.files} == {"notes.md": "CHANGED", "added.md": "NEW"}
    assert cell.updated_at > before
    state_cell = next(
        c for s in client.get("/api/state").json()["channels"][0]["main_stacks"]
        for c in s["cells"] if c["id"] == str(cell.id)
    )
    assert state_cell["name"] == "Renamed"


def test_patch_meta_sets_and_clears_a_capabilities_cell_icon__F0546(client):
    cell = _canvas_cell(client, on_capabilities=True, name="gens", icon="box")
    r = client.patch(f"/api/channels/{client.channel_id}/cells/{cell.id}/meta", json={"icon": "star"})
    assert r.status_code == 200, r.text
    assert (cell.icon, cell.name) == ("star", "gens")
    r = client.patch(f"/api/channels/{client.channel_id}/cells/{cell.id}/meta", json={"icon": None})
    assert r.status_code == 200, r.text
    assert cell.icon is None and r.json()["icon"] is None


def test_patch_meta_rejects_empty_or_null_name_and_unknown_cell__F0546(client):
    """An empty body, a null name, an unbounded name or icon (the name rides
    every state broadcast) and an unknown cell are all refused."""
    cell = _canvas_cell(client, name="Keep")
    assert client.patch(f"/api/channels/{client.channel_id}/cells/{cell.id}/meta", json={}).status_code == 400
    assert client.patch(f"/api/channels/{client.channel_id}/cells/{cell.id}/meta", json={"name": None}).status_code == 400
    assert client.patch(f"/api/channels/{client.channel_id}/cells/{cell.id}/meta", json={"name": "n" * 513}).status_code == 422
    assert client.patch(f"/api/channels/{client.channel_id}/cells/{cell.id}/meta", json={"icon": "i" * 65}).status_code == 422
    assert (cell.name, cell.icon) == ("Keep", None)
    r = client.patch(f"/api/channels/{client.channel_id}/cells/{uuid.uuid4()}/meta", json={"name": "x"})
    assert r.status_code == 404
    assert client.patch(f"/api/channels/{client.channel_id}/cells/{cell.id}/meta", json={"name": "n" * 512}).status_code == 200


# ---------------------------------------------------------------------------
# POST /cells/{cell_id}/duplicate — copy a live cell into a new stack
# ---------------------------------------------------------------------------

def _stack_index(stack_id) -> int:
    from state import state
    ch = next(c for c in state.channels if c.id == state.active_channel_id)
    return next(i for i, s in enumerate(ch.main_stacks) if str(s.id) == str(stack_id))


def _duplicate(client, cell_id, **body):
    return client.post(f"/api/channels/{client.channel_id}/cells/{cell_id}/duplicate", json=body)


def _moves(client) -> list[tuple]:
    conn = get_db_connection(client.channel_id)
    try:
        return [tuple(r) for r in conn.execute(
            "SELECT op, op_id, from_cell, from_version, to_cell, to_version, ord FROM cell_moves ORDER BY id")]
    finally:
        conn.close()


def test_duplicate_creates_a_copy_right_of_its_source_O04(client):
    cell = _canvas_cell(client, name="Alpha")
    source_stack = next(s for s in client.get("/api/state").json()["channels"][0]["main_stacks"]
                        if any(c["id"] == str(cell.id) for c in s["cells"]))
    r = _duplicate(client, cell.id)
    assert r.status_code == 200, r.text
    body = r.json()
    ch = body["channel"]
    assert _stack_index(body["new_stack_id"]) == _stack_index(source_stack["id"]) + 1
    [copy] = next(s for s in ch["main_stacks"] if s["id"] == body["new_stack_id"])["cells"]
    assert copy["name"] == "Alpha (Copy)" and copy["id"] == body["new_cell_id"] != str(cell.id)
    assert ch["main_selection_id"] == body["new_stack_id"]


def test_duplicate_placement_prefers_the_anchor_O04(client):
    cell = _canvas_cell(client, name="Src")
    anchor = _canvas_cell(client, name="Anchor")
    anchor_stack = next(s for s in client.get("/api/state").json()["channels"][0]["main_stacks"]
                        if any(c["id"] == str(anchor.id) for c in s["cells"]))
    r = _duplicate(client, cell.id, anchor_stack_id=anchor_stack["id"])
    assert r.status_code == 200, r.text
    assert _stack_index(r.json()["new_stack_id"]) == _stack_index(anchor_stack["id"]) + 1


@pytest.mark.parametrize("route", ["cell", "in_stack", "stack"])
def test_a_duplicate_whose_lineage_write_fails_leaves_no_copy_on_the_canvas_O03(client, monkeypatch, route):
    """The copy and its 'duplicate' move land together: when the move cannot
    be written the in-memory copy is taken back and the selection restored."""
    import cell_files
    from fastapi.testclient import TestClient
    from state import state

    def refuse(*_a, **_k):
        raise RuntimeError("lineage write refused")
    monkeypatch.setattr(cell_files, "record_duplicates", refuse)
    cell = _canvas_cell(client, name="Kept")
    ch = next(c for c in state.channels if c.id == state.active_channel_id)
    stack = next(s for s in ch.main_stacks if s.cells == [cell])
    ch.main_selection_id = selection = ch.main_stacks[0].id
    before = [(s.id, [c.id for c in s.cells]) for s in ch.main_stacks]
    url = {"cell": f"/api/channels/{client.channel_id}/cells/{cell.id}/duplicate",
           "in_stack": f"/api/stacks/{client.channel_id}/{stack.id}/duplicate_cell_in_stack/0",
           "stack": f"/api/stacks/{client.channel_id}/{stack.id}/duplicate_stack"}[route]
    quiet = TestClient(client.app, raise_server_exceptions=False)
    assert quiet.post(url).status_code == 500
    assert [(s.id, [c.id for c in s.cells]) for s in ch.main_stacks] == before
    assert ch.main_selection_id == selection
    assert _moves(client) == []


def test_duplicate_of_a_cell_off_the_canvas_is_404_O04(client):
    assert _duplicate(client, uuid.uuid4()).status_code == 404
    assert _moves(client) == []


def test_duplicate_of_a_never_versioned_cell_pins_its_genesis_O03(client):
    """A fresh 'duplicate' system run is the op, and the source's
    genesis row is written so ``from_version`` is a real version."""
    cell = _canvas_cell(client, name="Fresh")
    r = _duplicate(client, cell.id)
    assert r.status_code == 200, r.text
    [move] = _moves(client)
    assert move[0] == "duplicate" and move[2:] == (str(cell.id), str(cell.id), r.json()["new_cell_id"], None, 0)
    conn = get_db_connection(client.channel_id)
    try:
        assert tuple(conn.execute("SELECT prompt, status FROM runs WHERE id=?", (move[1],)).fetchone()) == ("duplicate", "completed")
        assert conn.execute("SELECT source_type FROM cells WHERE id=?", (str(cell.id),)).fetchone()[0] == "genesis"
    finally:
        conn.close()


def test_duplicate_records_the_tip_of_accepted_changes_O03(client):
    """The duplicate names the checkpoint containing accepted SQL changes."""
    from versions import find_tip
    cell = _canvas_cell(client, name="Drafted")
    checkpoint = client.post(f"/api/channels/{client.channel_id}/cells/{cell.id}/checkpoint")
    assert checkpoint.status_code == 200
    _put_file(client, cell.id, 'notes.md', 'ACCEPTED', base=1)
    r = _duplicate(client, cell.id)
    assert r.status_code == 200, r.text
    conn = get_db_connection(client.channel_id)
    try:
        tip = find_tip(conn, str(cell.id))
        tip_run = conn.execute("SELECT run_id FROM cells WHERE id=?", (tip,)).fetchone()[0]
    finally:
        conn.close()
    assert tip != str(cell.id)
    assert _moves(client) == [("duplicate", tip_run, str(cell.id), tip, r.json()["new_cell_id"], None, 0)]


# ---------------------------------------------------------------------------
# lineage_changed: every route that changes the lineage announces it
# ---------------------------------------------------------------------------

@pytest.fixture
def lineage_events(monkeypatch):
    """The ``cell_ids`` of every ``lineage_changed`` the channel's SSE sends."""
    import events
    sent = []
    original = events.broadcast

    def record(channel, event):
        if event.get("type") == "lineage_changed":
            sent.append(event["cell_ids"])
        return original(channel, event)

    monkeypatch.setattr(events, "broadcast", record)
    return sent


def _stack_of(cell):
    from state import state
    ch = next(c for c in state.channels if c.id == state.active_channel_id)
    return next(s for s in ch.main_stacks if any(c.id == cell.id for c in s.cells))


def test_each_duplicate_route_announces_the_source_and_its_copy_O04(client, lineage_events):
    cell = _canvas_cell(client, name="Alpha")
    stack = _stack_of(cell)

    r = _duplicate(client, cell.id)
    assert r.status_code == 200, r.text
    assert lineage_events == [[str(cell.id), r.json()["new_cell_id"]]]

    r = client.post(f"/api/stacks/{client.channel_id}/{stack.id}/duplicate_cell_in_stack/0")
    assert r.status_code == 200, r.text
    [copy] = r.json()["duplicated_cell_ids"]
    assert lineage_events[1:] == [[str(cell.id), copy]]

    del lineage_events[:]
    lone = _canvas_cell(client, name="Lone")
    r = client.post(f"/api/stacks/{client.channel_id}/{_stack_of(lone).id}/duplicate_stack")
    assert r.status_code == 200, r.text
    [copy] = [m[4] for m in _moves(client) if m[2] == str(lone.id)]
    assert lineage_events == [[str(lone.id), copy]]


def test_removing_a_lower_cell_by_id_announces_it_and_keeps_its_stack_O02(client, lineage_events):
    from models import Cell
    cell = _canvas_cell(client, name="First")
    stack = _stack_of(cell)
    second = Cell(name="Second")
    stack.cells.append(second)
    r = client.post(f"/api/channels/{client.channel_id}/cells/{second.id}/remove")
    assert r.status_code == 200, r.text
    assert lineage_events == [[str(second.id)]]
    assert [c.id for c in stack.cells] == [cell.id]


# ---------------------------------------------------------------------------
# GET /outline — capability cells and board stacks
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("shown", [False, True])
def test_outline_lists_capability_cells_and_the_board(client, shown):
    """``capabilities_cells`` are the Capabilities stack's cells after the
    cards, shown or hidden; ``main_stacks`` are the board's other stacks."""
    from cell_placement import capabilities_stack
    from models import Cell, Stack
    from state import state

    ch = next(c for c in state.channels if c.id == state.active_channel_id)
    stack = capabilities_stack(ch)
    seeded, own = Cell(name="Seeded"), Cell(name="Own")
    stack.cells.extend([seeded, own])
    board = Stack(name="Board", cells=[Cell(name="Work")])
    ch.main_stacks.append(board)
    if shown:
        ch.nav_stacks.remove(stack)
        ch.main_stacks.insert(0, stack)
    r = client.get(f"/api/channels/{client.channel_id}/outline")
    assert r.status_code == 200, r.text
    assert r.json() == {
        "capabilities_cells": [
            {"id": str(seeded.id), "name": "Seeded"},
            {"id": str(own.id), "name": "Own"},
        ],
        "main_stacks": [{"id": str(board.id), "name": "Board",
                         "cells": [{"id": str(board.cells[0].id), "name": "Work"}]}],
    }


@pytest.mark.parametrize("shown", [False, True])
def test_a_capability_cell_duplicates_into_a_new_standard_stack_O04(client, shown):
    """The door the in-stack refusal points to: a non-card Capabilities cell
    duplicated through the cell route lands in a new standard stack, at the
    end of the board while Capabilities is hidden and right after it while
    shown, selected; Capabilities keeps exactly its cells, so the copy's
    generator is not catalogued twice."""
    from cell_placement import capabilities_stack, capabilities_stack_cells
    from conftest import install_test_generator
    from runners import generator_index
    from state import state

    board = _canvas_cell(client, name="Board")
    gid = install_test_generator(name="synth_gen", draft_files={"settings.set_yaml": "fields: []\n"},
                                 manifest_extra="main_config: settings.set_yaml")
    ch = next(c for c in state.channels if c.id == state.active_channel_id)
    caps = capabilities_stack(ch)
    (original,) = [c for c in capabilities_stack_cells(ch)]
    if shown:
        ch.nav_stacks.remove(caps)
        ch.main_stacks.insert(0, caps)
    before = [s.id for s in ch.main_stacks]
    caps_cells = [c.id for c in caps.cells]

    r = _duplicate(client, original.id)
    assert r.status_code == 200, r.text
    body = r.json()
    new_stack = next(s for s in ch.main_stacks if str(s.id) == body["new_stack_id"])
    assert new_stack.stack_type == "standard" and [str(c.id) for c in new_stack.cells] == [body["new_cell_id"]]
    assert any(f.name == f"{gid}/choice.generator" for f in new_stack.cells[0].files)
    assert ch.main_selection_id == new_stack.id
    assert [c.id for c in caps.cells] == caps_cells
    if shown:
        assert [s.id for s in ch.main_stacks] == [caps.id, new_stack.id, *before[1:]]
    else:
        assert [s.id for s in ch.main_stacks] == [*before, new_stack.id] and caps in ch.nav_stacks
    assert any(c.id == board.id for s in ch.main_stacks for c in s.cells)
    assert generator_index(capabilities_stack_cells(ch))[gid]["cell"] is original
