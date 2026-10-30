# SPDX-License-Identifier: MIT
"""Run folders on the SDK side: every run owns ``_runs/<label>/`` and every
file in a live run's folder is its locked file.

Pinned here: the path helpers (``collimate.pointers``), the write refusals
(``gen.update_file`` / ``gen.delete_file`` raise at once for every ``_runs/``
path, the run's own folder included; ``gen.commit`` raises when the host's ack
names refused paths), the UI file API (``gen.update_ui_file`` / ``gen.delete_ui_file``
/ ``gen.list_ui_files`` and the manifest's declared ``ui_files`` suffixes), the wire shapes of the ops that changed
(``update_cell`` without a pin, ``surface_register`` as the run's inbox
without a cell, ``stream_event`` without a surface, ``reveal_file``),
``gen.reveal_file``'s flush-first rule, the recording bridge's sibling runs
(``foreign_runs``), and the live mirror's rules for run folders. Synthetic
labels and paths only."""
from __future__ import annotations

from collimate import _mirror as mirror_runtime

import asyncio
import errno
import json
import os

import pytest

from collimate import native
from collimate._host.bridge import HostBridge, PartialRefusal
from collimate.pointers import is_run_path, run_card_path, run_label
from collimate.testing import make_test_bridge, run_generator_callable

SIBLING = "sibling-run-1a2b3c4d"


# ── the path helpers ───────────────────────────────────────────────────


@pytest.mark.parametrize("path, inside, label", [
    ("_runs/demo-1a2b3c4d/review/page.html", True, "demo-1a2b3c4d"),
    ("_runs/demo-1a2b3c4d/demo-1a2b3c4d.run", True, "demo-1a2b3c4d"),
    ("/_runs/demo-1a2b3c4d/x", True, "demo-1a2b3c4d"),
    ("./_runs/demo-1a2b3c4d/x", True, "demo-1a2b3c4d"),
    ("_runs/demo-1a2b3c4d", True, "demo-1a2b3c4d"),
    ("_runs/stray.txt", True, "stray.txt"),
    ("_runs", True, ""),
    ("_runsx/a/b", False, ""),
    ("notes/_runs/a/b", False, ""),
    ("_runs/../escape/a", False, ""),
    ("", False, ""),
])
def test_run_path_and_label(path, inside, label):
    assert is_run_path(path) is inside
    assert run_label(path) == label


def test_the_run_card_is_named_by_the_label():
    assert run_card_path("demo-1a2b3c4d") == "_runs/demo-1a2b3c4d/demo-1a2b3c4d.run"


# ── write refusals ─────────────────────────────────────────────────────


@pytest.mark.parametrize("path", [
    f"_runs/{SIBLING}/review/page.html",
    f"_runs/{SIBLING}/new-file.txt",
    "_runs/stray.txt",
])
def test_a_write_into_another_runs_folder_raises_at_once(native_bridge, path):
    with pytest.raises(PermissionError, match="_runs/"):
        native.update_file(path, "x")
    with pytest.raises(PermissionError):
        native.delete_file(path)
    assert not os.path.exists(os.path.join(native_bridge.workspace, path))
    assert not native_bridge.tracked_writes and not native_bridge.tracked_deletions


def test_the_refusal_names_the_owner(native_bridge):
    with pytest.raises(PermissionError, match=f"locked by {SIBLING}"):
        native.update_file(f"_runs/{SIBLING}/a.txt", "x")


def test_the_run_card_is_the_hosts(native_bridge):
    card = run_card_path(native_bridge.label)
    with pytest.raises(PermissionError, match="card"):
        native.update_file(card, "{}")
    with pytest.raises(PermissionError, match="card"):
        native.delete_file(f"./{card}")


def test_update_file_refuses_the_own_folder_too(native_bridge):
    """The run folder holds UI files only, written through gen.update_ui_file."""
    mine = native.run_path("review/page.html")
    with pytest.raises(PermissionError, match="gen.update_ui_file"):
        native.update_file(mine, "<p>ok</p>")
    with pytest.raises(PermissionError, match="gen.update_ui_file"):
        native.update_file(mine, "log", category="log")
    with pytest.raises(PermissionError, match="gen.delete_ui_file"):
        native.delete_file(mine)
    native.update_file("notes/plan.md", "plan")      # work output: anywhere else
    assert native_bridge.tracked_writes == {"notes/plan.md"}
    assert not native_bridge.tracked_deletions


# ── gen.update_ui_file ────────────────────────────────────────────────────────


def test_ui_file_writes_into_the_run_folder_and_is_flushed(tmp_path):
    bridge = make_test_bridge(tmp_path, ui_files=[".website", ".html"])
    native.set_bridge(bridge)
    try:
        path = native.update_ui_file("preview/site.website", "{}")
        assert path == f"{bridge.folder}/preview/site.website"
        native.update_ui_file("preview/page.html", "<p>hi</p>")
        assert (tmp_path / path).read_text() == "{}"
        assert native.list_ui_files() == ["preview/page.html", "preview/site.website"]
        asyncio.run(native.commit())
        [rec] = bridge.updates
        assert set(rec["files"]) == {path, f"{bridge.folder}/preview/page.html"}
        native.delete_ui_file("preview/site.website")
        assert not (tmp_path / path).exists()
        assert native.list_ui_files() == ["preview/page.html"]
        asyncio.run(native.commit())
        assert bridge.tracked_deletions == set()
    finally:
        native._clear_bridge()
        bridge.close()


def test_ui_file_refuses_an_undeclared_suffix_at_the_call(tmp_path):
    bridge = make_test_bridge(tmp_path, ui_files=[".chat"])
    native.set_bridge(bridge)
    try:
        with pytest.raises(ValueError, match="ui_files"):
            native.update_ui_file("notes.md", "x")
        with pytest.raises(ValueError, match="ui_files"):
            native.delete_ui_file("notes.md")
        assert not (tmp_path / bridge.folder / "notes.md").exists()
        assert not bridge.tracked_writes and not bridge.tracked_deletions
    finally:
        native._clear_bridge()
        bridge.close()


@pytest.mark.parametrize("name", [
    "", "/abs.chat", "../escape.chat", "a/../../b.chat", "a/../b.chat",
    "_runs/other-1a2b3c4d/x.chat",
])
def test_ui_file_names_stay_inside_the_folder(native_bridge, name):
    with pytest.raises(ValueError):
        native.update_ui_file(name, "{}")


def test_ui_file_never_writes_the_run_card(tmp_path):
    bridge = make_test_bridge(tmp_path, ui_files=[".run"])
    native.set_bridge(bridge)
    try:
        with pytest.raises(ValueError, match="card"):
            native.update_ui_file(f"{bridge.label}.run", "{}")
    finally:
        native._clear_bridge()
        bridge.close()


def test_delete_ui_file_can_keep_the_last_content_as_a_log(native_bridge):
    path = native.update_ui_file("chat/main.json", "[1]")
    asyncio.run(native.commit())
    native.delete_ui_file("chat/main.json", log=True)
    assert native.list_ui_files() == [], "a logged UI file is no longer cell content"
    asyncio.run(native.commit())
    assert native_bridge.log_files == {path: "[1]"}
    assert path not in native_bridge.updates[-1]["files"]


def test_fs_list_under_walks_only_that_folder(native_bridge, tmp_path):
    native.update_file("top.md", "t")
    native.update_ui_file("chat/main.json", "[]")
    folder = native_bridge.folder
    scoped = native_bridge.fs_list(folder)
    assert scoped == [p for p in native_bridge.fs_list() if p.startswith(folder + "/")]
    assert scoped == [f"{folder}/chat/main.json"]
    assert native_bridge.fs_list("no/such/folder") == []


def test_the_host_refuses_an_undeclared_own_folder_file(tmp_path):
    """A path that slipped past the SDK (staged by hand) is refused by the
    host's partition with reason ui_type; the rest is committed."""
    bridge = make_test_bridge(tmp_path, ui_files=[".chat"])
    native.set_bridge(bridge)
    try:
        sneaky = f"{bridge.folder}/notes.md"
        (tmp_path / bridge.folder).mkdir(parents=True)
        (tmp_path / sneaky).write_text("x")
        native.update_file("keep.md", "k")
        bridge.tracked_writes.add(sneaky)
        with pytest.raises(PermissionError, match="not a UI file type"):
            asyncio.run(native.commit())
        [rec] = bridge.updates
        assert list(rec["files"]) == ["keep.md"]
    finally:
        native._clear_bridge()
        bridge.close()


def test_update_raises_for_what_the_host_refused_and_commits_the_rest(native_bridge):
    # A path that slipped past the SDK (staged by hand) is the host's to refuse.
    native.update_file("keep.md", "k")
    native_bridge.tracked_writes.add(f"_runs/{SIBLING}/sneaky.md")
    with pytest.raises(PermissionError, match=f"locked by {SIBLING}"):
        asyncio.run(native.commit())
    [rec] = native_bridge.updates
    assert list(rec["files"]) == ["keep.md"]


def _wire(bridge, ack):
    """Capture what a HostBridge method puts on the wire, answering ``ack``."""
    sent: list = []

    def emit_and_wait(payload, wait_op, **kw):
        sent.append(payload)
        refused = {entry['path'] for entry in ack.get('refused', [])}
        expected = payload.get('base_revisions', {})
        return dict(ack, op=wait_op, operation_id=payload.get("operation_id"),
                    outcome="accepted", files=[
                        {'path':path, 'file_revision':base+1, 'writer_id':payload['writer_id'],
                         'edit_seq':payload['edit_seq'], 'deleted':path in payload.get('deleted_files', [])}
                        for path, base in expected.items() if path not in refused])

    bridge._commit_update = lambda **kw: HostBridge._commit_update(bridge, **kw)
    bridge.emit_and_wait = emit_and_wait
    bridge.emit = sent.append
    bridge._sock_wfile = object()   # hosted, as far as the op is concerned
    return sent


def test_update_cell_carries_files_and_a_name_never_a_pin(tmp_path):
    bridge = make_test_bridge(tmp_path)
    sent = _wire(bridge, {"cell_id": "cell-1"})
    bridge.fs_write_any("a.txt", "a")
    bridge.fs_unlink("b.txt")
    assert HostBridge.update_cell(
        bridge, name="Named", writes=["a.txt"]) == "cell-1"
    [payload] = sent
    assert payload["op"] == "update_cell"
    assert payload["metadata"] == {"name": "Named"}
    assert payload["deleted_files"] == ["b.txt"]
    assert payload["base_revisions"] == {"a.txt": 0, "b.txt": 0}
    assert payload["edit_seq"] == 1
    assert payload["writer_id"].startswith(f"run:{bridge.run_id}:")
    assert payload["artifacts"] == [{"type": "workspace", "cell_path": "a.txt",
        "local_path": f'.collrun/persistence/{payload["operation_id"]}/0', "encoding": "utf-8"}]
    bridge.close()


def test_update_cell_maps_the_refused_entries(tmp_path):
    bridge = make_test_bridge(tmp_path)
    _wire(bridge, {"cell_id": "cell-1", "refused": [
        {"path": f"_runs/{SIBLING}/x.md", "reason": "locked", "owner": SIBLING},
        {"path": run_card_path(bridge.label), "reason": "host_owned",
         "owner": bridge.label},
        {"path": f"{bridge.folder}/notes.md", "reason": "ui_type",
         "owner": bridge.label}]})
    with pytest.raises(PartialRefusal) as err:
        HostBridge.update_cell(bridge, writes=[])
    assert isinstance(err.value, PermissionError)
    assert f"_runs/{SIBLING}/x.md is locked by {SIBLING}" in str(err.value)
    assert "is the run card" in str(err.value)
    assert f"{bridge.folder}/notes.md is not a UI file type" in str(err.value)
    bridge.close()


# ── the session ops' wire shapes ───────────────────────────────────────


def test_surface_register_names_the_path_and_the_inbox(tmp_path):
    bridge = make_test_bridge(tmp_path)
    sent = _wire(bridge, {"ok": True, "chat_id": "s", "ws_path": "/ws/s", "token": "t"})
    path = f"{bridge.folder}/chat/conversation.chat"
    assert HostBridge.surface_register(bridge, path, label="Chat", transport_only=False,
                                       broadcast=False) == {
        "chat_id": "s", "ws_path": "/ws/s", "token": "t"}
    assert sent == [{"op": "surface_register", "path": path, "label": "Chat",
                     "transport_only": False, "broadcast": False}]
    with pytest.raises(ValueError, match="broadcast"):
        HostBridge.surface_register(bridge, path, label="Chat", transport_only=False,
                                    broadcast=True)
    assert len(sent) == 1
    bridge.close()


@pytest.mark.parametrize("error, exc", [
    ("surface_taken", RuntimeError),
    ("inbox_exists", RuntimeError),
])
def test_surface_register_refusals(tmp_path, error, exc):
    bridge = make_test_bridge(tmp_path)
    sent = _wire(bridge, {"ok": False, "error": error})
    with pytest.raises(exc, match=error):
        HostBridge.surface_register(bridge, f"{bridge.folder}/chat/c.chat", label="Chat",
                                    transport_only=True, broadcast=False)
    # The inbox lives in the run folder: anywhere else is refused unsent.
    with pytest.raises(ValueError):
        HostBridge.surface_register(bridge, "elsewhere.chat", label="Chat",
                                    transport_only=True, broadcast=False)
    assert len(sent) == 1
    bridge.close()


def test_stream_event_and_start_terminal_carry_no_cell(tmp_path):
    bridge = make_test_bridge(tmp_path)
    sent = _wire(bridge, {"ok": True, "terminal_id": "s"})
    HostBridge.stream_event(bridge, "t1", {"type": "text_delta", "text": "x"})
    HostBridge.start_terminal(bridge, container_id="c", command=["bash"])
    assert sent[0] == {"op": "stream_event", "turn_id": "t1",
                       "event": {"type": "text_delta", "text": "x"}}
    assert all("cell_id" not in p for p in sent)
    assert sent[1]["op"] == "create_terminal_session"
    bridge.close()


def test_serve_port_has_no_snapshot_source(tmp_path):
    import inspect

    assert "snapshot_source" not in inspect.signature(native.serve_port).parameters
    bridge = make_test_bridge(tmp_path)
    sent = _wire(bridge, {})
    HostBridge.serve_port(bridge, 8080, name="app")
    assert sent == [{"op": "port_bound", "port": 8080, "name": "app"}]
    bridge.close()


def test_the_runner_accessor_is_gone():
    """``gen.run_info()`` carries the runner; there is no second accessor."""
    assert "runner" not in native.__all__ and not hasattr(native, "runner")


# ── gen.reveal_file ────────────────────────────────────────────────────


def test_reveal_file_flushes_a_staged_path_first():
    async def run():
        import gen

        pointer = gen.update_ui_file("tab-1.webview", "{}")
        await gen.reveal_file(pointer)          # flushes, then reveals
        await gen.reveal_file("./" + pointer)   # already committed: no flush

    res = run_generator_callable(run)
    assert res.ok, res.error
    reveals = [c for c in res.tier_b_calls if c["op"] == "reveal_file"]
    assert len(reveals) == 2 and all(r["committed"] for r in reveals)
    assert reveals[0]["path"] == reveals[1]["path"]
    assert res.files


def test_reveal_file_sends_the_op(tmp_path):
    bridge = make_test_bridge(tmp_path)
    sent = _wire(bridge, {})
    HostBridge.reveal_file(bridge, "notes.md")
    assert sent == [{"op": "reveal_file", "path": "notes.md"}]
    bridge.close()


# ── the recording bridge's sibling runs ────────────────────────────────


def _sibling_run(tmp_path):
    bridge = make_test_bridge(tmp_path, foreign_runs={SIBLING: {
        "review/page.body": "Plan", "review/page.notes": "Notes"}})
    native.set_bridge(bridge)
    return bridge


def test_foreign_runs_are_in_the_workspace_and_read_only(tmp_path):
    bridge = _sibling_run(tmp_path)
    try:
        doc = f"_runs/{SIBLING}/review/page.body"
        assert native.read_file(doc) == "Plan"
        assert doc in native.list_files()
        with pytest.raises(PermissionError, match=f"locked by {SIBLING}"):
            native.update_file(doc, "mine now")
        with pytest.raises(PermissionError):
            native.delete_file(doc)
        assert bridge.foreign_runs[SIBLING] == [doc, f"_runs/{SIBLING}/review/page.notes"]
    finally:
        native._clear_bridge()
        bridge.close()


def test_foreign_runs_through_the_harness():
    """``run_generator(foreign_runs=…)`` seeds the siblings a generator under
    test reads; this run keeps its own label."""
    async def run():
        import gen

        doc = f"_runs/{SIBLING}/review/page.body"
        gen.update_file("seen.txt", gen.read_file(doc))
        gen.update_file("label.txt", gen.run_info().label)

    res = run_generator_callable(run, foreign_runs={SIBLING: {
        "review/page.body": "plan", "review/page.notes": "notes"}},
                                 runner={"runner": "demo", "name": "Demo"},
                                 run_id="0123456789abcdef")
    assert res.ok, res.error
    assert res.files["seen.txt"] == "plan"
    assert res.files["label.txt"] == "demo-01234567"


# ── the live mirror and run folders ────────────────────────────────────


class _Store(mirror_runtime.FileEndpoint):
    """An in-memory endpoint (either side of a mirror)."""

    def __init__(self, label, files=None):
        self.label = label
        self.files: dict = dict(files or {})

    async def hashes(self):
        return {p: __import__("hashlib").sha256(c.encode("utf-8") if isinstance(c, str) else c).hexdigest() for p, c in self.files.items()}

    async def read(self, p):
        return self.files[p]

    async def write(self, p, c):
        self.files[p] = c

    async def delete(self, p):
        self.files.pop(p, None)


def _mirror(bridge, ws, cell, **kw):
    return mirror_runtime.Mirror(ws, cell, own_folder=kw.pop("own_folder", bridge.folder), **kw)


def _tick(m, n=1):
    async def go():
        for _ in range(n):
            await m._tick()
    asyncio.run(go())


def test_the_own_folder_mirrors_both_ways(native_bridge):
    mine = f"{native_bridge.folder}/review/page.html"
    ws, cell = _Store("workspace", {"x": "1"}), _Store("cell", {"x": "1"})
    m = _mirror(native_bridge, ws, cell)
    ws.files[mine] = "v1"
    _tick(m)
    assert cell.files[mine] == "v1"
    cell.files[f"{mine}.draft"] = "draft"     # the user's draft beside it
    _tick(m)
    assert ws.files[f"{mine}.draft"] == "draft"
    del ws.files[mine]
    _tick(m)
    assert mine not in cell.files


def test_the_run_card_is_never_pushed_and_is_restored(native_bridge):
    card = run_card_path(native_bridge.label)
    ws, cell = _Store("workspace", {card: "{}", "x": "1"}), _Store("cell", {card: "{}", "x": "1"})
    m = _mirror(native_bridge, ws, cell)
    _tick(m)
    ws.files[card] = "edited"
    _tick(m)
    assert cell.files[card] == "{}" and ws.files[card] == "{}"
    del ws.files[card]                         # e.g. rm -r of the run folder
    _tick(m)
    assert cell.files[card] == "{}", "the mirror never pushes a delete of the card"
    assert ws.files[card] == "{}"
    assert any(line == f"[live] restored {card} (read-only, owned by {native_bridge.label})"
               for line in native_bridge._rec_logs)


def test_another_runs_folder_is_pulled_and_its_local_edits_restored(native_bridge):
    doc = f"_runs/{SIBLING}/review/page.html"
    ws, cell = _Store("workspace", {doc: "v1", "x": "1"}), _Store("cell", {doc: "v1", "x": "1"})
    m = _mirror(native_bridge, ws, cell)
    _tick(m)
    cell.files[doc] = "v2"                   # the owner updated its document
    _tick(m)
    assert ws.files[doc] == "v2"
    ws.files[doc] = "hijacked"                 # a harness edited it locally
    _tick(m)
    assert cell.files[doc] == "v2" and ws.files[doc] == "v2"
    assert f"[live] restored {doc} (read-only, owned by {SIBLING})" in native_bridge._rec_logs
    ws.files[f"_runs/{SIBLING}/planted.txt"] = "x"   # a file the cell never had
    _tick(m)
    assert f"_runs/{SIBLING}/planted.txt" not in ws.files
    assert f"_runs/{SIBLING}/planted.txt" not in cell.files
    del cell.files[doc]                        # the owner stopped: the host swept it
    _tick(m)
    assert doc not in ws.files


def test_a_one_way_mirror_never_pushes_another_runs_folder(native_bridge):
    doc = f"_runs/{SIBLING}/review/page.html"
    ws, cell = _Store("workspace", {"x": "1"}), _Store("cell", {"x": "1"})
    m = _mirror(native_bridge, ws, cell, from_remote=False)
    ws.files[doc] = "mine"
    ws.files[run_card_path(native_bridge.label)] = "{}"
    _tick(m, 2)
    assert set(cell.files) == {"x"}




def test_declared_private_globs_are_never_mirrored(native_bridge):
    ws, cell = _Store("workspace", {"x": "1"}), _Store("cell", {"x": "1"})
    m = _mirror(native_bridge, ws, cell)
    m._skip_globs = ("_agent/**",)
    ws.files["_agent/trace.md"] = "log"
    cell.files["_agent/threads/main.json"] = "[]"
    _tick(m, 2)
    assert "_agent/trace.md" not in cell.files and "_agent/threads/main.json" not in ws.files


def test_a_restore_never_writes_through_a_swapped_symlink(native_bridge, tmp_path):
    """A harness swaps another run's file for a link to a host file: the
    cell-wins restore refuses to follow it (the workspace endpoint writes
    symlink-safe)."""
    doc = f"_runs/{SIBLING}/review/page.html"
    ws_root = native_bridge.workspace
    os.makedirs(os.path.join(ws_root, os.path.dirname(doc)))
    secret = tmp_path / "secret.txt"
    secret.write_text("ORIGINAL")
    os.symlink(secret, os.path.join(ws_root, doc))
    cell = _Store("cell", {doc: "the owner's copy", "x": "1"})
    open(os.path.join(ws_root, "x"), "w").write("1")
    m = mirror_runtime.Mirror(mirror_runtime.LocalWorkspace(set()), cell, own_folder=native_bridge.folder)
    with pytest.raises(OSError) as refused:
        _tick(m, 2)
    assert refused.value.errno == errno.ELOOP
    assert secret.read_bytes() == b"ORIGINAL"
    assert os.path.islink(os.path.join(ws_root, doc))


def test_an_undeclared_own_folder_file_is_never_pushed_and_is_removed(tmp_path):
    bridge = make_test_bridge(tmp_path, ui_files=[".website"])
    native.set_bridge(bridge)
    try:
        pointer = f"{bridge.folder}/preview/site.website"
        stray = f"{bridge.folder}/scratch/notes.md"
        ws, cell = _Store("workspace", {"x": "1"}), _Store("cell", {"x": "1"})
        m = _mirror(bridge, ws, cell)
        ws.files[pointer] = "{}"                  # a declared type: pushed
        ws.files[stray] = "work"                  # the harness dropped work here
        _tick(m)
        assert cell.files[pointer] == "{}"
        assert stray not in cell.files and stray not in ws.files
        assert f"[live] restored {stray} (undeclared ui type)" in bridge._rec_logs
        cell.files[pointer] = '{"v": 2}'          # the cell side still pulls
        _tick(m)
        assert ws.files[pointer] == '{"v": 2}'
    finally:
        native._clear_bridge()
        bridge.close()


def test_the_cell_store_writes_own_folder_files_through_the_ui_path(tmp_path):
    bridge = make_test_bridge(tmp_path, ui_files=[".website"])
    native.set_bridge(bridge)
    try:
        store = mirror_runtime.CellStore()
        pointer = f"{bridge.folder}/preview/site.website"
        # The local endpoint already holds the bytes; the cell endpoint
        # tracks them without another write.
        native._fs.write_file(bridge.workspace, pointer, "{}")
        native._fs.write_file(bridge.workspace, "notes.md", "n")
        asyncio.run(store.write(pointer, "{}"))
        asyncio.run(store.write("notes.md", "n"))
        assert bridge.tracked_writes == {pointer, "notes.md"}
        asyncio.run(store.delete(pointer))
        assert bridge.tracked_deletions == {pointer}
    finally:
        native._clear_bridge()
        bridge.close()


def test_live_files_mirrors_with_the_runs_folder(native_bridge):
    assert native.live_files()._own_folder == native_bridge.folder


def test_run_card_json_is_not_a_surface(native_bridge):
    """The run card is the host's: it is never a served file nor the inbox."""
    from collimate.surfaces import surface_declaration

    card = run_card_path(native_bridge.label)
    with pytest.raises(ValueError):
        surface_declaration(card, folder=native_bridge.folder, label="Chat")

    async def serve():
        async def on_turn(turn):  # pragma: no cover — never served
            pass
        await native.serve_chat(on_turn, files=False, pointer_file=card,
                                threads_dir=native.run_path("chat"))

    with pytest.raises(ValueError):
        asyncio.run(serve())
    assert json.dumps(native.run_info().folder).startswith('"_runs/')


def test_update_snapshots_the_stage_on_the_loop_thread(native_bridge):
    """gen.commit takes the staged writes before handing the flush to a
    worker thread: a write staged while the flush runs is neither lost nor
    raced, it stays staged for the next flush."""
    import threading

    native.update_file("early.txt", "e")
    started, release = threading.Event(), threading.Event()
    seen: list = []
    orig = native_bridge.update_cell

    def blocking(**kw):
        seen.append(sorted(kw.get("writes") or ()))
        started.set()
        release.wait(5)
        return orig(**kw)

    native_bridge.update_cell = blocking

    async def go():
        flush = asyncio.create_task(native.commit())
        while not started.is_set():
            await asyncio.sleep(0.01)
        native.update_file("late.txt", "l")
        release.set()
        await flush

    asyncio.run(go())
    assert seen == [["early.txt"]]
    assert "late.txt" in native_bridge.tracked_writes
    assert "early.txt" not in native_bridge.tracked_writes
