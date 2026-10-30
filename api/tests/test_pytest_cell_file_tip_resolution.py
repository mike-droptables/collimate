"""Live file reads use accepted SQL revisions; history uses explicit snapshots.

A history DAG tip, an old committed row and derived in-memory files cannot
replace the current accepted body. Pinned revisions remain immutable across
later puts/deletes, and live reads do not depend on a transient canvas layout.
The existing active-document, capability and service-worker boundaries apply
to live, pinned and immutable-history body reads alike.
"""
from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

import pytest
from database import get_db_connection

CHANNEL = str(uuid.uuid5(uuid.NAMESPACE_URL, "tests:cell-file-tip:channel"))

@pytest.fixture
def client(api_app):
    """Real API lifecycle with a registered synthetic channel, without SDK seeding."""
    from fastapi.testclient import TestClient
    from models import Channel
    from state import state, persist_state, flush_persist_state

    with TestClient(api_app, raise_server_exceptions=False) as http:
        channel = Channel(id=uuid.UUID(CHANNEL), name="Starting")
        state.channels = [channel]
        state.active_channel_id = channel.id
        get_db_connection(str(channel.id)).close()
        persist_state()
        flush_persist_state()
        http.channel_id = CHANNEL
        yield http


def _cas_write_text(text: str) -> str:
    raw = text.encode("utf-8")
    h = hashlib.sha256(raw).hexdigest()
    d = Path(".collimate") / "objects" / h[:2]
    d.mkdir(parents=True, exist_ok=True)
    (d / h[2:]).write_bytes(raw)
    return h


def _seed_cell(seq: int, files: dict[str, str]) -> str:
    """Create an immutable typed snapshot, without accepted live references."""
    from models import VirtualFile, language_for
    from versions import synthesize_genesis_row
    conn = get_db_connection(CHANNEL)
    try:
        run_id = str(uuid.uuid4())
        cid = str(uuid.uuid4())
        conn.execute("INSERT OR IGNORE INTO runs (id, prompt, status) VALUES (?, '', 'completed')", (run_id,))
        assert synthesize_genesis_row(conn, cid, run_id, seq,
                                     [VirtualFile(name=path, content=body, language=language_for(path))
                                      for path, body in files.items()])
        conn.commit()
        return cid
    finally:
        conn.close()


def _mount(cell_id: str, area: str = "main_stacks") -> None:
    """Put ``cell_id`` on the canvas: in a ``main_stacks`` stack of its own,
    or on the channel's one Capabilities stack, which sits minimized in
    ``nav_stacks``."""
    from cell_placement import capabilities_stack
    from genesis import ensure_capabilities_stack
    from models import Cell, Stack
    from state import state

    ch = next(c for c in state.channels if c.id == state.active_channel_id)
    cell = Cell(id=cell_id, name="mounted", files=[])
    if area == "main_stacks":
        ch.main_stacks.append(Stack(cells=[cell]))
        return
    ensure_capabilities_stack(ch)
    stack = capabilities_stack(ch)
    assert stack in ch.nav_stacks
    stack.cells.append(cell)


def _link_version(parent_id: str, child_id: str) -> None:
    conn = get_db_connection(CHANNEL)
    try:
        conn.execute(
            "INSERT INTO cell_edges (parent_cell_id, child_cell_id, parent_key) "
            "VALUES (?, ?, 'previous_version')",
            (parent_id, child_id),
        )
        conn.commit()
    finally:
        conn.close()


def _accepted(cell_id: str, files: dict[str, str], deleted=()) -> dict:
    from persistence import mutate_files, snapshot_files
    conn = get_db_connection(CHANNEL)
    try:
        revisions = {f["path"]: f["file_revision"] for f in snapshot_files(conn, cell_id, include_deleted=True)}
    finally:
        conn.close()
    writer = "host:test:" + str(uuid.uuid4())
    mutations = [dict(path=name, writer_id=writer, edit_seq=index + 1,
                      base_revision=revisions.get(name, 0), kind="put",
                      content_text=body, language="plaintext")
                 for index, (name, body) in enumerate(files.items())]
    mutations += [dict(path=name, writer_id=writer, edit_seq=len(mutations) + index + 1,
                       base_revision=revisions.get(name, 0), kind="delete")
                  for index, name in enumerate(deleted)]
    receipt = mutate_files(CHANNEL, cell_id, dict(operation_id=str(uuid.uuid4()),
                           mutations=mutations, resolves=[]), actor="host")
    assert receipt["outcome"] == "accepted"
    return receipt


def _live(cid, name="data.txt"):
    return f"/api/channels/{CHANNEL}/cells/{cid}/files/{name}"


def _version(cid, name="data.txt"):
    return f"/api/channels/{CHANNEL}/versions/{cid}/files/{name}"


def test_sibling_reads_accepted_revision_instead_of_history_tip(client):
    root = _seed_cell(1, {"data.txt": "OLD SNAPSHOT"})
    tip = _seed_cell(2, {"data.txt": "HISTORY TIP"})
    _link_version(root, tip)
    _mount(root)
    receipt = _accepted(root, {"data.txt": "ACCEPTED CURRENT"})
    response = client.get(_live(root))
    assert response.status_code == 200, response.text
    assert response.text == "ACCEPTED CURRENT"
    assert response.headers["x-file-revision"] == str(receipt["files"][0]["file_revision"])
    assert client.get(_version(root)).text == "OLD SNAPSHOT"
    assert client.get(_version(tip)).text == "HISTORY TIP"


def test_committed_history_rows_are_not_an_implicit_live_body(client):
    cid = _seed_cell(1, {"data.txt": "SNAPSHOT ONLY"})
    assert client.get(_live(cid)).status_code == 404
    assert client.get(_version(cid)).text == "SNAPSHOT ONLY"
    _accepted(cid, {"data.txt": "CURRENT"})
    assert client.get(_live(cid)).text == "CURRENT"


def test_accepted_delete_is_404_but_pinned_and_history_bytes_survive__F0250(client):
    root = _seed_cell(1, {"data.txt": "SNAPSHOT", "extra.txt": "HISTORY EXTRA"})
    tip = _seed_cell(2, {"data.txt": "TIP", "extra.txt": "TIP STILL HAS EXTRA"})
    _link_version(root, tip)
    _mount(root)
    first = _accepted(root, {"data.txt": "CURRENT", "extra.txt": "ACCEPTED EXTRA"})
    extra = next(f for f in first["files"] if f["path"] == "extra.txt")
    _accepted(root, {}, deleted=["extra.txt"])
    assert client.get(_live(root, "extra.txt")).status_code == 404
    assert client.get(_live(root, "extra.txt") + f'?revision={extra["file_revision"]}').text == "ACCEPTED EXTRA"
    assert client.get(_version(root, "extra.txt")).text == "HISTORY EXTRA"
    assert client.get(_version(tip, "extra.txt")).text == "TIP STILL HAS EXTRA"


def test_explicit_version_address_serves_its_own_snapshot__F0250(client):
    ids = [_seed_cell(1, {"data.txt": "V0"}),
           _seed_cell(2, {"data.txt": "V1", "pic.png": "PNG-V1"}),
           _seed_cell(3, {"data.txt": "V2"})]
    for parent, child in zip(ids, ids[1:]):
        _link_version(parent, child)
    _mount(ids[0])
    _accepted(ids[0], {"data.txt": "LIVE"})
    assert client.get(_live(ids[0])).text == "LIVE"
    for index, cid in enumerate(ids):
        assert client.get(_version(cid)).text == f"V{index}"
    response = client.get(_version(ids[1], "pic.png"))
    assert response.text == "PNG-V1" and response.headers["content-type"] == "image/png"
    assert client.get(_live(ids[1], "pic.png")).status_code == 404
    assert client.get(_version(ids[2], "pic.png")).status_code == 404


@pytest.mark.parametrize("mount", ["main_stacks", "nav_stacks", "restore"])
def test_canvas_placement_does_not_choose_a_history_body__F0250(client, mount):
    root = _seed_cell(1, {"data.txt": "ROOT"})
    mounted = _seed_cell(2, {"data.txt": "MOUNTED SNAPSHOT", "gone.txt": "OLD"})
    _link_version(root, mounted)
    _mount(mounted, "main_stacks" if mount == "restore" else mount)
    _accepted(mounted, {"data.txt": "MOUNTED CURRENT", "gone.txt": "CURRENT OLD"})
    if mount == "restore":
        for step in ("remove", "restore"):
            response = client.post(f"/api/channels/{CHANNEL}/cells/{mounted}/{step}")
            assert response.status_code == 200, response.text
    newer = _seed_cell(3, {"data.txt": "NEW HISTORY TIP"})
    _link_version(mounted, newer)
    _accepted(mounted, {"data.txt": "NEW CURRENT"}, deleted=["gone.txt"])
    assert client.get(_live(mounted)).text == "NEW CURRENT"
    assert client.get(_live(mounted, "gone.txt")).status_code == 404
    assert client.get(_version(mounted)).text == "MOUNTED SNAPSHOT"


def test_off_canvas_history_id_does_not_follow_its_tip__F0250(client):
    root = _seed_cell(1, {"data.txt": "V0"})
    tip = _seed_cell(2, {"data.txt": "V1", "new.txt": "TIP ONLY"})
    _link_version(root, tip)
    assert client.get(_live(root)).status_code == 404
    assert client.get(_version(root)).text == "V0"
    assert client.get(_version(root, "new.txt")).status_code == 404
    assert client.get(_live(uuid.uuid4())).status_code == 404


@pytest.mark.parametrize("move", ["move_cell_to", "restore_minimized"])
def test_live_read_during_layout_move_keeps_accepted_identity__F0250(client, move):
    import threading
    from models import Stack
    from state import state, state_lock
    cid = _seed_cell(1, {"data.txt": "SNAPSHOT"})
    _mount(cid, "main_stacks" if move == "move_cell_to" else "nav_stacks")
    _accepted(cid, {"data.txt": "CURRENT"})
    ch = next(c for c in state.channels if c.id == state.active_channel_id)
    stack = next(s for s in ch.main_stacks + ch.nav_stacks if any(str(c.id) == cid for c in s.cells))
    mid_move, release, restored = threading.Event(), threading.Event(), threading.Event()
    def mover():
        with state_lock:
            area = ch.main_stacks if move == "move_cell_to" else ch.nav_stacks
            area.remove(stack)
            mid_move.set()
            release.wait(5)
            ch.main_stacks.append(stack if move == "restore_minimized" else Stack(cells=stack.cells))
        restored.set()
    writer = threading.Thread(target=mover)
    writer.start()
    try:
        assert mid_move.wait(5)
        response = client.get(_live(cid))
        answered_while_moving = not restored.is_set()
    finally:
        release.set()
        writer.join(5)
    assert response.status_code == 200 and response.text == "CURRENT"
    assert answered_while_moving, "Accepted SQL body read waited for an unrelated canvas move"


def test_pinned_live_body_read_does_not_queue_behind_state_lock__F0250(client):
    import threading
    from state import state_lock
    cid = _seed_cell(1, {})
    _mount(cid)
    first = _accepted(cid, {"data.txt": "PINNED"})
    _accepted(cid, {"data.txt": "CURRENT"})
    revision = first["files"][0]["file_revision"]
    held, release, released = threading.Event(), threading.Event(), threading.Event()
    def holder():
        with state_lock:
            held.set()
            release.wait(5)
        released.set()
    writer = threading.Thread(target=holder)
    writer.start()
    try:
        assert held.wait(5)
        response = client.get(_live(cid) + f"?revision={revision}")
        answered_while_held = not released.is_set()
    finally:
        release.set()
        writer.join(5)
    assert response.text == "PINNED" and answered_while_held


@pytest.mark.parametrize("length", [70, 10_052])
def test_history_chain_length_cannot_select_live_bytes__F0002(client, length):
    root = _seed_cell(1, {"data.txt": "ROOT"})
    tip = _seed_cell(2, {"data.txt": "TIP"})
    middle = [str(uuid.uuid4()) for _ in range(length - 2)]
    conn = get_db_connection(CHANNEL)
    try:
        run_id = str(uuid.uuid4())
        conn.execute("INSERT INTO runs (id, prompt, status) VALUES (?, '', 'completed')", (run_id,))
        conn.executemany("INSERT INTO cells (id, run_id, generator_name, sequence_num) VALUES (?, ?, 'x', ?)",
                         [(cid, run_id, n) for n, cid in enumerate(middle, 3)])
        chain = [root, *middle, tip]
        conn.executemany("INSERT INTO cell_edges (parent_cell_id, child_cell_id, parent_key) VALUES (?, ?, 'previous_version')", zip(chain, chain[1:]))
        conn.commit()
    finally:
        conn.close()
    _mount(root)
    _accepted(root, {"data.txt": "CURRENT"})
    assert client.get(_live(root)).text == "CURRENT"
    assert client.get(_version(root)).text == "ROOT"
    assert client.get(_version(tip)).text == "TIP"


_ACTIVE = ["page.html", "page.htm", "doc.xhtml", "art.svg", "feed.xml"]
_INERT = ["notes.txt", "data.json", "photo.png", "script.js"]


@pytest.mark.parametrize("name", _ACTIVE + _INERT)
def test_live_pinned_and_history_body_security_headers__F1986(client, name):
    cid = _seed_cell(1, {name: "<svg/>"})
    _mount(cid)
    receipt = _accepted(cid, {name: "<svg/>"})
    revision = receipt["files"][0]["file_revision"]
    for url in (_live(cid, name), _live(cid, name) + f"?revision={revision}", _version(cid, name)):
        response = client.get(url)
        assert response.status_code == 200, response.text
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["cache-control"] == "no-store"
        csp = response.headers.get("content-security-policy")
        assert csp == ("sandbox allow-scripts allow-popups allow-forms" if name in _ACTIVE else None)


@pytest.mark.parametrize("media_type", [
    "text/xsl", "text/xsl; charset=utf-8", "TEXT/HTML", "application/xslt+xml",
    "application/xhtml+xml", "image/svg+xml",
])
def test_every_document_type_is_sandboxed_whatever_the_host_mime_map__F1986(media_type):
    """The served type comes from ``mimetypes``, which reads the host's
    mime.types files, so the sandbox rule is pinned on the media type itself:
    every XML document type a browser runs scripts in (``text/xsl`` among
    them, whatever extension a host maps to it) gets the CSP sandbox."""
    from routes.blobs import _cell_file_headers

    headers = _cell_file_headers(media_type)
    assert headers["Content-Security-Policy"] == "sandbox allow-scripts allow-popups allow-forms"
    assert headers["X-Content-Type-Options"] == "nosniff"


def test_no_cell_file_or_blob_can_be_a_service_worker_script__F1935_F1986(client):
    cid = _seed_cell(1, {"sw.js": "self.onfetch = () => {};"})
    _mount(cid)
    _accepted(cid, {"sw.js": "self.onfetch = () => {};"})
    cap = client.get(f"/api/channels/{CHANNEL}/cells/{cid}/file_cap").json()["cap"]
    blob = _cas_write_text("self.onfetch = () => {};")
    urls = [_live(cid, "sw.js"), _version(cid, "sw.js"),
            f"/api/cellcap/{cap}/channels/{CHANNEL}/cells/{cid}/files/sw.js",
            f"/api/blob/{blob}"]
    for url in urls:
        assert client.get(url).status_code == 200
        assert client.get(url, headers={"Service-Worker": "script"}).status_code == 403


def test_unknown_channel_is_404_without_creating_a_db_F0286(client):
    before = set(Path(".collimate").rglob("*.db"))
    r = client.get("/api/channels/no-such-chan/cells/x/files/a.txt")
    assert r.status_code == 404, r.text
    assert set(Path(".collimate").rglob("*.db")) == before


def test_history_root_has_an_explicit_snapshot_address__P0F02(client):
    from urllib.parse import quote
    root = _seed_cell(1, {"nested/a #%.txt": "ORIGINAL", "removed.txt": "OLD"})
    tip = _seed_cell(2, {"nested/a #%.txt": "HISTORY TIP"})
    _link_version(root, tip)
    _mount(root)
    first = _accepted(root, {"nested/a #%.txt": "ACCEPTED FIRST"})
    _accepted(root, {"nested/a #%.txt": "ACCEPTED CURRENT"})
    path = quote("nested/a #%.txt", safe="/")
    assert client.get(_live(root, path)).text == "ACCEPTED CURRENT"
    assert client.get(_live(root, path) + f'?revision={first["files"][0]["file_revision"]}').text == "ACCEPTED FIRST"
    assert client.get(_version(root, path)).text == "ORIGINAL"
    assert client.get(_version(root, "removed.txt")).text == "OLD"
    assert client.get(_version(tip, "removed.txt")).status_code == 404
    cap = client.get(f"/api/channels/{CHANNEL}/cells/{root}/file_cap").json()["cap"]
    capped = f"/api/cellcap/{cap}/channels/{CHANNEL}/versions/{root}/files/"
    response = client.get(capped + path, headers={"Origin": "null"})
    assert response.status_code == 200 and response.text == "ORIGINAL"
    wrong = f"/api/cellcap/{cap}/channels/{CHANNEL}/versions/{tip}/files/" + path
    assert client.get(wrong, headers={"Origin": "null"}).status_code == 403
