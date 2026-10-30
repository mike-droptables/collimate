# SPDX-License-Identifier: MIT
"""Coverage for the new module-singleton behaviour in :mod:`collimate.native`.

The native singleton is the public ``gen.*`` runtime; under it sits a
:class:`collimate._host.bridge.HostBridge` that owns IPC + filesystem +
state. These tests exercise the singleton's contract without needing
a real IPC peer — we drive the bridge directly (workspace=tmp_path,
no socket) and assert the singleton responds the way ``gen.*``
callers see it.

No test here references a specific generator on disk — every fixture
synthesises the workspace + config + bridge state it needs.
"""
from __future__ import annotations

from collimate import _mirror as mirror_runtime

import asyncio
import os
import socket
import threading
from contextlib import contextmanager

import pytest


# ---------------------------------------------------------------------------
# Bridge fixtures
# ---------------------------------------------------------------------------


def _make_bridge(workspace: str):
    """Construct a :class:`HostBridge` rooted at ``workspace`` without
    running ``__init__`` (which would touch signals + socket env).
    Suitable for unit tests that don't exercise IPC."""
    from collimate._host.bridge import HostBridge
    bridge = HostBridge.__new__(HostBridge)
    bridge._initialize_ipc()
    bridge.workspace = str(workspace)
    bridge.run_dir = os.path.join(bridge.workspace, ".collrun")
    bridge.run_id = "test-run"
    bridge.prompt = ""
    bridge._initialize_workspace()
    bridge._initialize_persistence()
    bridge._key_attempts = {}
    bridge._force_set_ts = {}
    bridge._socket = None
    bridge._sock_rfile = None
    bridge._sock_wfile = None

    import logging
    bridge.log = logging.getLogger("test")

    # Self-pipe for cancellation polling — the ``cancelled`` property
    # reads it. We set it up read-only (never written) so cancelled
    # stays False throughout the test.
    r, w = os.pipe()
    os.set_blocking(r, True)
    os.set_blocking(w, False)
    bridge._stop_pipe_r = r
    bridge._stop_pipe_w = w
    return bridge


@pytest.fixture
def native_bridge(tmp_path, monkeypatch):
    """Yield a fresh native singleton bound to a tmp workspace.
    Auto-clears the singleton on teardown so tests don't leak state."""
    from collimate import native
    bridge = _make_bridge(tmp_path)
    native.set_bridge(bridge)
    try:
        yield bridge
    finally:
        native._clear_bridge()
        os.close(bridge._stop_pipe_r)
        os.close(bridge._stop_pipe_w)


# ---------------------------------------------------------------------------
# gen.config — always returns dict
# ---------------------------------------------------------------------------


def test_config_parses_set_yaml_with_fields_and_values(native_bridge, tmp_path):
    """``.set_yaml`` files merge ``fields:`` defaults with ``values:``
    overrides. The post-rework return type is always dict — even for
    raw text the doc says to use ``gen.read_file`` instead."""
    cfg = tmp_path / ".collrun" / "config" / "files" / "config.set_yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        "fields:\n"
        "  - { key: provider, default: anthropic }\n"
        "  - { key: model, default: sonnet }\n"
        "values:\n"
        "  model: opus\n"
    )
    from collimate import native
    out = native.config("config.set_yaml")
    assert out == {"provider": "anthropic", "model": "opus"}


def test_config_reads_an_empty_values_block_as_no_values(native_bridge, tmp_path):
    """A schema-form file whose ``values:`` block is empty holds no values —
    not a ``values`` key of its own."""
    cfg = tmp_path / ".collrun" / "config" / "files" / "config.set_yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("values: {}\n")
    from collimate import native
    assert native.config("config.set_yaml") == {}
    cfg.write_text("fields:\n  - { key: model, default: sonnet }\nvalues: {}\n")
    assert native.config("config.set_yaml") == {"model": "sonnet"}
    cfg.write_text("values:\n")
    assert native.config("config.set_yaml") == {}


def test_config_passes_a_plain_map_with_a_values_or_fields_key_through(native_bridge, tmp_path):
    """Only a schema-form shape (``fields:`` a list, ``values:`` a map or
    empty) is merged; a plain map whose ``values`` or ``fields`` is anything
    else is returned as written."""
    cfg = tmp_path / ".collrun" / "config" / "files" / "plain.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    from collimate import native
    cfg.write_text("name: x\nvalues: [1, 2]\n")
    assert native.config("plain.yaml") == {"name": "x", "values": [1, 2]}
    cfg.write_text("title: t\nfields: a,b\n")
    assert native.config("plain.yaml") == {"title": "t", "fields": "a,b"}


def test_config_parses_json(native_bridge, tmp_path):
    cfg = tmp_path / ".collrun" / "config" / "files" / "settings.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text('{"a": 1, "b": "two"}')
    from collimate import native
    assert native.config("settings.json") == {"a": 1, "b": "two"}


def test_config_returns_empty_dict_on_missing_file(native_bridge):
    from collimate import native
    assert native.config("does-not-exist.set_yaml") == {}


def test_config_returns_empty_dict_on_malformed_yaml(native_bridge, tmp_path):
    cfg = tmp_path / "broken.set_yaml"
    cfg.write_text("fields:\n  - this isn't valid: yaml: at all")
    from collimate import native
    assert native.config("broken.set_yaml") == {}


def test_config_returns_dict_for_raw_text_files(native_bridge, tmp_path):
    """Non-YAML/JSON files used to return raw string. Post-rework the
    function always returns dict — generators that want raw text use
    ``gen.read_file(name)``. ``{}`` is the expected sentinel here."""
    (tmp_path / "notes.md").write_text("# hello\n\nbody text\n")
    from collimate import native
    assert native.config("notes.md") == {}


# ---------------------------------------------------------------------------
# Output stage — update_file / read_file / has_file / list_files / delete_file
# ---------------------------------------------------------------------------


def test_update_file_writes_to_disk_and_tracks(native_bridge, tmp_path):
    """``update_file`` is the only way to stage a write. It hits disk
    immediately (so subprocesses see it) and adds the path to
    ``tracked_writes`` for the next ``update`` flush."""
    from collimate import native
    native.update_file("output.md", "hello\n")
    assert (tmp_path / "output.md").read_text() == "hello\n"
    assert "output.md" in native_bridge.tracked_writes


def test_read_file_round_trips(native_bridge):
    from collimate import native
    native.update_file("a.txt", "first")
    assert native.read_file("a.txt") == "first"


def test_read_file_raises_when_missing(native_bridge):
    from collimate import native
    with pytest.raises(FileNotFoundError):
        native.read_file("absent.md")


def test_has_file_predicate(native_bridge):
    from collimate import native
    assert not native.has_file("nope.md")
    native.update_file("yep.md", "x")
    assert native.has_file("yep.md")


def test_list_files_excludes_collrun(native_bridge, tmp_path):
    from collimate import native
    native.update_file("a.md", "1")
    native.update_file("dir/b.md", "2")
    # Manually drop a sibling under .collrun/ — the helper must
    # filter it out so generators never see SDK-internal state.
    os.makedirs(tmp_path / ".collrun", exist_ok=True)
    (tmp_path / ".collrun" / "internal.json").write_text("{}")

    out = native.list_files()
    assert "a.md" in out
    assert "dir/b.md" in out
    assert not any(p.startswith(".collrun") for p in out), (
        f".collrun/ leaked into list_files: {out}"
    )


def test_list_files_hides_only_the_top_level_collrun(native_bridge, tmp_path):
    from collimate import native
    native.update_file("a.md", "1")
    native.update_file("sub/.collrun/kept.md", "2")
    native.update_file("trace.json", "{}")
    native_bridge.log_paths.add("trace.json")
    os.makedirs(tmp_path / ".collrun", exist_ok=True)
    (tmp_path / ".collrun" / "internal.json").write_text("{}")
    assert set(native.list_files()) == {"a.md", "sub/.collrun/kept.md"}


def test_delete_file_refuses_collrun(native_bridge, tmp_path):
    from collimate import native
    staged = tmp_path / ".collrun" / "config" / "files" / "a.set_yaml"
    staged.parent.mkdir(parents=True)
    staged.write_text("values: {}\n")
    with pytest.raises(PermissionError):
        native.delete_file(".collrun/config/files/a.set_yaml")
    assert staged.exists()
    assert native_bridge.tracked_deletions == set()


def test_read_file_reads_once_and_keeps_line_endings__F1190(native_bridge, tmp_path, monkeypatch):
    """F1190: text comes back exactly as written — CRLF and a lone CR are
    not turned into LF — so a read-then-write round trip (the live mirror's
    workspace-to-cell push, an agent's edit) never rewrites a CRLF file. A
    binary file is read once."""
    from collimate import native
    from collimate._host.bridge import HostBridge
    (tmp_path / "t.txt").write_bytes(b"a\r\nb\rc")
    (tmp_path / "b.bin").write_bytes(b"\xff\x00")
    calls: list = []
    real = HostBridge.fs_read_bytes

    def counting(self, path):
        calls.append(path)
        return real(self, path)

    monkeypatch.setattr(HostBridge, "fs_read_bytes", counting)
    assert native.read_file("t.txt") == "a\r\nb\rc"
    assert native.read_file("t.txt") == "a\r\nb\rc"
    assert {p: native.read_file(p) for p in native.list_files()}["t.txt"] == "a\r\nb\rc"
    assert native_bridge.fs_read_any("t.txt") == "a\r\nb\rc"
    calls.clear()
    assert native.read_file("b.bin") == b"\xff\x00"
    assert calls == ["b.bin"]
    # The round trip is byte-exact on disk.
    native.update_file("t.txt", native.read_file("t.txt"))
    assert (tmp_path / "t.txt").read_bytes() == b"a\r\nb\rc"


def test_the_prompt_keeps_its_line_endings__F1190(tmp_path, monkeypatch):
    """F1190: the run's prompt is text read exactly as written, like a
    workspace file."""
    from collimate._host.bridge import HostBridge
    (tmp_path / ".collrun").mkdir(exist_ok=True)
    (tmp_path / ".collrun/file_revisions.json").write_text("{}")
    monkeypatch.setenv("COLLIMATE_WORKSPACE", str(tmp_path))
    monkeypatch.delenv("COLLIMATE_IPC_PORT", raising=False)
    (tmp_path / ".collrun").mkdir(exist_ok=True)
    (tmp_path / ".collrun" / "prompt.md").write_bytes(b"one\r\ntwo\rthree\n")
    bridge = HostBridge()
    try:
        assert bridge.prompt == "one\r\ntwo\rthree\n"
    finally:
        bridge.finish(success=True)


def test_config_unreadable_yaml_or_json_is_an_empty_dict(native_bridge, tmp_path, monkeypatch):
    import builtins
    files = tmp_path / ".collrun" / "config" / "files"
    files.mkdir(parents=True)
    (files / "x.json").write_text('{"a": 1}')
    (files / "x.txt").write_text("raw")
    real_open = builtins.open

    def refusing(file, *a, **kw):
        if str(file).startswith(str(files)):
            raise PermissionError(file)
        return real_open(file, *a, **kw)

    monkeypatch.setattr(builtins, "open", refusing)
    assert native_bridge.config("x.json") == {}
    assert native_bridge.config("x.txt") == {}


def test_delete_file_removes_disk_and_tracks_deletion(native_bridge, tmp_path):
    from collimate import native
    native.update_file("doomed.md", "bye")
    assert (tmp_path / "doomed.md").exists()
    native.delete_file("doomed.md")
    assert not (tmp_path / "doomed.md").exists()
    assert "doomed.md" in native_bridge.tracked_deletions
    assert "doomed.md" not in native_bridge.tracked_writes


# ---------------------------------------------------------------------------
# api_key / secret — provider mapping + cancelled propagation
# ---------------------------------------------------------------------------










# ---------------------------------------------------------------------------
# main() — auto-flush on clean return
# ---------------------------------------------------------------------------


@contextmanager
def _scripted_ipc_server(server_lines):
    """Spin up a real localhost TCP server scripted with canned ack
    lines for each inbound payload. Yields ``(port, accepted)`` where
    ``port`` is what the test sets in ``COLLIMATE_IPC_PORT`` so
    ``HostBridge()``'s real ``__init__`` connects to us — we do *not*
    pre-connect from the test side, otherwise the bridge's connect
    queues behind us and the lockstep request/ack loop deadlocks.

    Used to exercise codepaths that go through the real bridge
    construction (``main()``, full lifecycle)."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    srv.settimeout(5.0)
    port = srv.getsockname()[1]

    accepted: dict = {"received": []}

    def _serve():
        try:
            client_sock, _ = srv.accept()
        except OSError:
            return  # no connection in time, or the server closed first
        accepted["sock"] = client_sock
        in_file = client_sock.makefile("r", encoding="utf-8")
        out_file = client_sock.makefile("w", encoding="utf-8")
        for line in server_lines:
            import json
            while True:
                req = in_file.readline()
                if not req:
                    return
                accepted["received"].append(req.strip())
                request = json.loads(req)
                if request.get("req_id"):
                    break
            reply = json.loads(line)
            reply["req_id"] = request["req_id"]
            if reply.get("op") == "ack_update" and reply.get("ok") is not False:
                refused = {entry['path'] for entry in reply.get('refused', [])}
                reply.update(operation_id=request["operation_id"], outcome="accepted", files=[
                    {'path':path, 'file_revision':base+1, 'writer_id':request['writer_id'],
                     'edit_seq':request['edit_seq'], 'deleted':path in request.get('deleted_files', [])}
                    for path, base in request['base_revisions'].items() if path not in refused])
            out_file.write(json.dumps(reply) + "\n")
            out_file.flush()
        # Drain remaining emits (``finish`` is fire-and-forget).
        try:
            while True:
                trailer = in_file.readline()
                if not trailer:
                    break
                accepted["received"].append(trailer.strip())
        except Exception:
            pass
        try:
            client_sock.close()
        except Exception:
            pass

    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    try:
        yield port, accepted
    finally:
        try:
            if "sock" in accepted:
                accepted["sock"].close()
        except Exception:
            pass
        srv.close()
        t.join(timeout=2.0)


def test_main_auto_flushes_when_run_returns_with_pending_writes(
    tmp_path, monkeypatch,
):
    """A generator that calls ``gen.update_file`` but never
    ``gen.commit`` should still produce a non-empty cell — ``main``
    flushes pending writes on clean return."""
    (tmp_path / ".collrun").mkdir(exist_ok=True)
    (tmp_path / ".collrun/file_revisions.json").write_text("{}")
    monkeypatch.setenv("COLLIMATE_WORKSPACE", str(tmp_path))
    monkeypatch.delenv("COLLIMATE_IPC_PORT", raising=False)

    import json as _json
    ack_update = _json.dumps({"op": "ack_update", "cell_id": "cell-x"})
    with _scripted_ipc_server([ack_update]) as (port, accepted):
        monkeypatch.setenv("COLLIMATE_IPC_PORT", str(port))
        # A host always sends the grant and names the run.
        monkeypatch.setenv("COLLIMATE_PERMISSIONS", "{}")
        monkeypatch.setenv("COLLIMATE_RUN_ID", "run-1")
        monkeypatch.setenv("COLLIMATE_CHANNEL", "chan")
        monkeypatch.setenv("COLLIMATE_RUNNER", _json.dumps({
            "runner": None, "name": None, "label": "sub-run1",
            "folder": "_runs/sub-run1"}))
        monkeypatch.setenv("COLLIMATE_UI_FILES", "[]")

        from collimate import native

        async def run():
            # No explicit ``gen.commit()`` — auto-flush should fire.
            native.update_file("auto.md", "should be flushed\n")

        native.main(run)

    update_payloads = [p for p in accepted["received"] if '"op": "update_cell"' in p]
    assert update_payloads, (
        f"main() should auto-flush update_cell on clean return; "
        f"received: {accepted['received']}"
    )


def test_main_does_not_flush_when_no_pending_writes(tmp_path, monkeypatch):
    """If the generator wrote nothing and history isn't dirty, the
    auto-flush is a no-op — no spurious ``update_cell`` IPC. Empty
    placeholder is the explicit-by-design signal."""
    import json as _json
    (tmp_path / ".collrun").mkdir(exist_ok=True)
    (tmp_path / ".collrun/file_revisions.json").write_text("{}")
    monkeypatch.setenv("COLLIMATE_WORKSPACE", str(tmp_path))
    monkeypatch.delenv("COLLIMATE_IPC_PORT", raising=False)

    with _scripted_ipc_server([]) as (port, accepted):
        monkeypatch.setenv("COLLIMATE_IPC_PORT", str(port))
        # A host always sends the grant and names the run.
        monkeypatch.setenv("COLLIMATE_PERMISSIONS", "{}")
        monkeypatch.setenv("COLLIMATE_RUN_ID", "run-1")
        monkeypatch.setenv("COLLIMATE_CHANNEL", "chan")
        monkeypatch.setenv("COLLIMATE_RUNNER", _json.dumps({
            "runner": None, "name": None, "label": "sub-run1",
            "folder": "_runs/sub-run1"}))
        monkeypatch.setenv("COLLIMATE_UI_FILES", "[]")

        from collimate import native

        async def run():
            return  # no-op

        native.main(run)

    update_payloads = [p for p in accepted["received"] if '"op": "update_cell"' in p]
    assert update_payloads == [], (
        f"main() should NOT auto-flush when nothing was written; "
        f"received: {accepted['received']}"
    )


# ---------------------------------------------------------------------------
# api_key parallel coordination — per-provider lock + debounce
# ---------------------------------------------------------------------------


def test_api_key_dynamic_attribute_resolution(native_bridge):
    """``gen.prompt`` / ``gen.cancelled`` are module-level dynamic attrs
    (PEP 562) that resolve against the bridge. Reading them outside a run raises; reading them with the
    singleton hydrated returns the bridge's value."""
    from collimate import native
    native_bridge.prompt = "the user typed this"
    assert native.prompt == "the user typed this"
    assert native.cancelled is False


# ---------------------------------------------------------------------------
# Bytes roundtrip — update_file / read_file carry str | bytes
# ---------------------------------------------------------------------------


def test_update_file_bytes_roundtrip(native_bridge, tmp_path):
    """Write bytes via update_file, read back via read_file — bytes in,
    bytes out. utf-8 round-trips as str."""
    from collimate import native
    native.update_file("plot.png", b"\x89PNG\r\n\x1a\n_BINARY_")
    native.update_file("notes.md", "# Hello\n")
    assert native.read_file("plot.png") == b"\x89PNG\r\n\x1a\n_BINARY_"
    assert native.read_file("notes.md") == "# Hello\n"
    # Both writes tracked for the next update() flush.
    assert "plot.png" in native_bridge.tracked_writes
    assert "notes.md" in native_bridge.tracked_writes


def test_read_file_returns_bytes_for_non_utf8(native_bridge, tmp_path):
    """``read_file`` auto-detects: utf-8 decodes to str, otherwise bytes."""
    (tmp_path / "blob.bin").write_bytes(b"\xff\xfe\x00\x01nope")
    (tmp_path / "hello.txt").write_text("hi")
    from collimate import native
    assert isinstance(native.read_file("blob.bin"), bytes)
    assert native.read_file("hello.txt") == "hi"


def test_file_listing_mixed_text_and_bytes(native_bridge, tmp_path):
    (tmp_path / "a.txt").write_text("text")
    (tmp_path / "b.bin").write_bytes(b"\x80\x81\x82")
    from collimate import native
    files = {p: native.read_file(p) for p in native.list_files()}
    assert files["a.txt"] == "text"
    assert files["b.bin"] == b"\x80\x81\x82"


def test_update_file_rejects_non_str_bytes(native_bridge):
    """``fs_write_any`` raises TypeError for anything that's not str or
    bytes. update_file forwards that error so the caller hears about
    the bad type at the call site, not later."""
    from collimate import native
    with pytest.raises(TypeError):
        native.update_file("x", 42)  # type: ignore[arg-type]


def testset_bridge_drops_the_previous_runs_chat_state__P6F01(native_bridge):
    """A new run starts with no queued turn, config change or approval
    answer left over from a previous run in the same process."""
    from collimate import native
    native._turn_queue.append({"content": "x", "thread_id": "main"})
    native._deliver_config_request({"config_id": "a", "value": 1})
    native.deliver_permission_answer({"id": "card"})
    native.set_bridge(native_bridge)
    assert native.chat_busy() is False
    assert native.pop_config_request() is None
    assert native._pop_permission_answer("card") is None


def test_read_file_and_has_file_normalize_like_update_file(native_bridge, tmp_path):
    """A path written with update_file reads back under the same spelling,
    and has_file never reports a file outside the workspace."""
    from collimate import native
    native.update_file("/notes.md", "x")
    assert native.read_file("/notes.md") == "x"
    assert native.has_file("./notes.md")
    (tmp_path.parent / "outside.txt").write_text("secret")
    assert native.has_file("../outside.txt") is False
    assert native.has_file("/" + str(tmp_path.parent / "outside.txt")) is False


def test_sync_callable_returning_a_coroutine_is_awaited():
    """A sync callable (a lambda around an async run) that returns a
    coroutine has it awaited, not dropped with a silent success."""
    from collimate import native
    from collimate.testing import run_generator_callable

    async def run():
        await asyncio.sleep(0)
        native.update_file("ran.txt", "yes")

    res = run_generator_callable(lambda: run())
    assert res.ok, res.error
    assert res.files.get("ran.txt") == "yes"


# ---------------------------------------------------------------------------
# Side cells stage their files outside the run's own workspace (F2027, F1191)
# ---------------------------------------------------------------------------


def test_has_file_follows_no_symlink_and_agrees_with_read_file__F1211(native_bridge, tmp_path):
    """F1211: ``fs_exists`` walks the path the way ``fs_read_bytes`` opens it —
    no symlink at any component, regular files and directories only — so
    ``has_file`` never answers True for a path ``read_file`` then refuses."""
    from collimate import native
    ws = tmp_path / "ws"
    ws.mkdir()
    native_bridge.workspace = str(ws)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (ws / "real.txt").write_text("real")
    (ws / "sub").mkdir()
    (ws / "sub" / "f.txt").write_text("f")
    os.symlink(outside, ws / "leak")
    os.symlink(ws / "real.txt", ws / "inside-link")      # even one pointing inside
    os.symlink(ws / "sub", ws / "linkdir")
    os.mkfifo(ws / "pipe")
    assert native.has_file("real.txt") and native.has_file("sub/f.txt")
    assert native.has_file("sub"), "a real directory is there"
    for absent in ("leak", "inside-link", "linkdir/f.txt", "linkdir", "pipe",
                   "missing.txt", "real.txt/x", "../outside.txt", ""):
        assert native.has_file(absent) is False, absent
        if absent:
            with pytest.raises(OSError):
                native.read_file(absent)
    assert native_bridge.fs_exists(str(outside)) is False
    assert native_bridge.fs_exists(".collrun/prompt.md") is False


# ---------------------------------------------------------------------------
# A failed flush stages its files again (F1130); a failed final flush fails
# the run (F1135); the live mirror rolls its echo map back (F1130)
# ---------------------------------------------------------------------------


def test_update_restages_what_a_failed_flush_carried__F1130(native_bridge, tmp_path):
    """F1130: ``gen.commit`` takes the staged set before the round trip; when
    the host times out, NAKs or disconnects, the set is staged again so the
    next flush sends it — a path touched meanwhile keeps its newer state."""
    from collimate import native
    from collimate._host.bridge import IPCDisconnectedError, IPCTimeoutError
    native.update_file("a.txt", "a")
    native.update_file("b.txt", "b")
    (tmp_path / "gone.txt").write_text("g")
    native.delete_file("gone.txt")
    native.delete_file("old.txt")
    sent: list = []

    def failing(*, name=None, writes=None, deleted_files=None):
        sent.append((sorted(writes), deleted_files))
        # Loop code touched two of the paths while the flush was in flight.
        native_bridge.tracked_writes.discard("a.txt")
        native_bridge.tracked_deletions.add("a.txt")
        native_bridge.tracked_deletions.discard("gone.txt")
        native_bridge.tracked_writes.add("gone.txt")
        raise IPCTimeoutError("ack_update", 60.0)

    native_bridge._commit_update = failing
    with pytest.raises(IPCTimeoutError):
        asyncio.run(native.commit())
    assert sent == [(["a.txt", "b.txt"], ["gone.txt", "old.txt"])]
    assert native_bridge.tracked_writes == {"b.txt", "gone.txt"}
    assert native_bridge.tracked_deletions == {"a.txt", "old.txt"}

    # The synchronous flush restages the same way.
    def disconnected(*, name=None, writes=None, deleted_files=None):
        raise IPCDisconnectedError("host closed IPC channel")

    native_bridge._commit_update = disconnected
    with pytest.raises(IPCDisconnectedError):
        native._flush_staged(native_bridge)
    assert native_bridge.tracked_writes == {"b.txt", "gone.txt"}
    assert native_bridge.tracked_deletions == {"a.txt", "old.txt"}

    # The next flush that reaches the host sends them.
    def ok(*, name=None, writes=None, deleted_files=None):
        sent.append((sorted(writes), deleted_files))
        return "cell-1"

    native_bridge._commit_update = ok
    asyncio.run(native.commit())
    assert sent[-1] == (["b.txt", "gone.txt"], ["a.txt", "old.txt"])
    assert not native_bridge.tracked_writes and not native_bridge.tracked_deletions


def test_update_restages_a_log_reclassification_whole__F1130(native_bridge, tmp_path):
    """A file→log reclassification stages a write (the log artifact) and a
    delete (the old cell copy) of one path; a failed flush restores both."""
    from collimate import native
    from collimate._host.bridge import IPCNakError
    native.update_file("trace.json", "t", category="log")
    assert native_bridge.tracked_writes == native_bridge.tracked_deletions == {"trace.json"}

    def nak(**_kw):
        raise IPCNakError("oversize", "update_cell")

    native_bridge._commit_update = nak
    with pytest.raises(IPCNakError):
        asyncio.run(native.commit())
    assert native_bridge.tracked_writes == native_bridge.tracked_deletions == {"trace.json"}


def test_a_partial_refusal_is_not_restaged__F1130(native_bridge):
    """A :class:`PartialRefusal` (a ``PermissionError``) is the host's
    partial refusal: it committed the rest, so nothing is staged again."""
    from collimate import native
    from collimate._host.bridge import PartialRefusal
    native.update_file("keep.md", "k")

    def refused(**_kw):
        raise PartialRefusal("_runs/x/y.md is locked by x (the rest of the update was committed)")

    native_bridge._commit_update = refused
    with pytest.raises(PermissionError):
        asyncio.run(native.commit())
    assert native_bridge.tracked_writes == set()


def test_a_naks_permission_error_is_restaged_and_fails_thefinal_flush__F1130_F1135(native_bridge):
    """A NAK's ``PermissionError`` (``permission_denied``)
    committed nothing, unlike a partial refusal: the flush's changes are
    staged again, and a final flush that meets it fails the run."""
    from collimate import native
    native.update_file("keep.md", "k")

    def denied(**_kw):
        raise PermissionError("permission denied for 'update_cell'")

    native_bridge._commit_update = denied
    with pytest.raises(PermissionError):
        asyncio.run(native.commit())
    assert native_bridge.tracked_writes == {"keep.md"}
    with pytest.raises(RuntimeError, match="final flush failed"):
        native.final_flush(native_bridge)
    assert native_bridge.tracked_writes == {"keep.md"}


def test_a_flush_waits_for_the_one_in_flight__F1130(native_bridge, tmp_path):
    """Flushes run one at a time. A second ``update()`` started while the
    first is in flight neither overtakes it on the wire nor takes the newer
    write of a path the first was deleting — which the first, failing,
    would then stage again as a delete, and the next flush would send
    after the write had committed."""
    import threading
    from collimate import native
    from collimate._host.bridge import IPCTimeoutError
    (tmp_path / "p.md").write_text("old")
    native.delete_file("p.md")
    sent: list = []
    first_started, fail_first = threading.Event(), threading.Event()

    def update_cell(*, name=None, writes, deleted_files=None):
        sent.append((sorted(writes), list(deleted_files or ())))
        if len(sent) == 1:
            first_started.set()
            fail_first.wait(5)
            raise IPCTimeoutError("ack_update", 60.0)
        return "cell-1"

    native_bridge._commit_update = update_cell

    async def go():
        first = asyncio.create_task(native.commit())
        while not first_started.is_set():
            await asyncio.sleep(0.01)
        native.update_file("p.md", "new")          # the run writes it again
        second = asyncio.create_task(native.commit())
        await asyncio.sleep(0.05)                   # the second is waiting
        assert len(sent) == 1, "a flush overtook the one in flight"
        fail_first.set()
        with pytest.raises(IPCTimeoutError):
            await first
        await second
        await native.commit()

    asyncio.run(go())
    # The first flush's delete is not staged again over the newer write, so
    # no later flush deletes p.md after its write committed.
    assert sent == [([], ["p.md"]), (["p.md"], []), ([], [])]
    assert (tmp_path / "p.md").read_text() == "new"
    assert not native_bridge.tracked_writes and not native_bridge.tracked_deletions


def test_a_log_rewritten_mid_flight_keeps_its_reclassification_delete__F1130(native_bridge):
    """A log path written again while the flush that carried its
    reclassification is in flight stages only the write; the failed flush
    stages its delete of the old cell copy again beside it."""
    from collimate import native
    from collimate._host.bridge import IPCTimeoutError
    native.update_file("trace.json", "t", category="log")

    def failing(**_kw):
        native.update_file("trace.json", "t2", category="log")
        raise IPCTimeoutError("ack_update", 60.0)

    native_bridge._commit_update = failing
    with pytest.raises(IPCTimeoutError):
        asyncio.run(native.commit())
    assert native_bridge.tracked_writes == native_bridge.tracked_deletions == {"trace.json"}


def _stalled_update_cell(native_bridge, *, fail_first):
    """An ``update_cell`` stub whose first call waits for ``release`` and
    then fails (``fail_first``) or commits; later calls commit at once."""
    from collimate._host.bridge import IPCTimeoutError
    sent: list = []
    started, release = threading.Event(), threading.Event()

    def update_cell(*, name=None, writes, deleted_files=None):
        sent.append((sorted(writes), list(deleted_files or ())))
        if len(sent) == 1:
            started.set()
            release.wait(5)
            if fail_first:
                raise IPCTimeoutError("ack_update", 60.0)
        return "cell-1"

    native_bridge._commit_update = update_cell
    return sent, started, release


@pytest.mark.parametrize("cancels", [1, 2])
@pytest.mark.parametrize("fails", [True, False])
def test_a_cancelled_flush_waits_for_its_round_trip_and_restages_a_failure__F1130(
        native_bridge, tmp_path, fails, cancels):
    """F1130 (round-2 review): a cancel cannot stop the worker thread
    carrying a flush. The cancelled ``update()`` waits for it, stages its
    files again when it failed (a log artifact and a delete included:
    nothing the live mirror would push again), and only then raises the
    cancel. ``_Mirror.__aexit__`` cancels a tick inside ``update()`` on
    every ``live_files`` exit; a second cancel while it waits (the task
    running ``__aexit__`` cancelled in turn, or ``asyncio.run``'s
    cancel-all) changes nothing."""
    from collimate import native
    native.update_file("trace.json", "t", category="log")
    native.update_file("a.md", "a")
    (tmp_path / "gone.md").write_text("g")
    native.delete_file("gone.md")
    sent, started, release = _stalled_update_cell(native_bridge, fail_first=fails)

    async def go():
        flush = asyncio.create_task(native.commit())
        while not started.is_set():
            await asyncio.sleep(0.01)
        for _ in range(cancels):
            flush.cancel()
            await asyncio.sleep(0.05)
            assert not flush.done(), "the cancel did not wait for the round trip"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await flush

    asyncio.run(go())
    assert sent == [(["a.md", "trace.json"], ["gone.md", "trace.json"])]
    if fails:
        assert native_bridge.tracked_writes == {"a.md", "trace.json"}
        assert native_bridge.tracked_deletions == {"gone.md", "trace.json"}
    else:
        assert not native_bridge.tracked_writes and not native_bridge.tracked_deletions


def test_a_flush_after_a_cancelled_one_waits_for_its_round_trip__F1130(native_bridge, tmp_path):
    """F1130 (round-2 review): a flush started after another was cancelled
    mid-flight does not reach ``update_cell`` before the cancelled one's
    round trip returns, so an older snapshot (a delete) never lands after a
    newer write; and it carries what the cancelled one, failing, staged
    again."""
    from collimate import native
    (tmp_path / "p.md").write_text("old")
    native.delete_file("p.md")
    native.update_file("a.md", "a")
    sent, started, release = _stalled_update_cell(native_bridge, fail_first=True)

    async def go():
        first = asyncio.create_task(native.commit())
        while not started.is_set():
            await asyncio.sleep(0.01)
        first.cancel()
        await asyncio.sleep(0)
        native.update_file("p.md", "new")          # the run writes it again
        second = asyncio.create_task(native.commit())
        await asyncio.sleep(0.05)
        assert len(sent) == 1, "a flush overtook the cancelled one in flight"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        await second

    asyncio.run(go())
    assert sent == [(["a.md"], ["p.md"]), (["a.md", "p.md"], [])]
    assert (tmp_path / "p.md").read_text() == "new"
    assert not native_bridge.tracked_writes and not native_bridge.tracked_deletions


def _hosted_env(monkeypatch, tmp_path, port):
    import json as _json
    (tmp_path / ".collrun").mkdir(exist_ok=True)
    (tmp_path / ".collrun/file_revisions.json").write_text("{}")
    monkeypatch.setenv("COLLIMATE_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("COLLIMATE_IPC_PORT", str(port))
    monkeypatch.setenv("COLLIMATE_PERMISSIONS", "{}")
    monkeypatch.setenv("COLLIMATE_RUN_ID", "run-1")
    monkeypatch.setenv("COLLIMATE_CHANNEL", "chan")
    monkeypatch.setenv("COLLIMATE_RUNNER", _json.dumps({
        "runner": None, "name": None, "label": "sub-run1", "folder": "_runs/sub-run1"}))
    monkeypatch.setenv("COLLIMATE_UI_FILES", "[]")


def _finish_frame(accepted) -> dict:
    import json as _json
    [finish] = [_json.loads(p) for p in accepted["received"] if '"op": "finish"' in p]
    return finish


def test_main_fails_the_run_when_thefinal_flush_fails__F1135(tmp_path, monkeypatch):
    """F1135: the auto-flush after ``run()`` returns is the run's output; when
    the host NAKs it nothing was committed, so the run reports failure
    instead of finishing green."""
    import json as _json
    from collimate import native
    monkeypatch.delenv("COLLIMATE_IPC_PORT", raising=False)
    nak = _json.dumps({"op": "nak", "for_op": "update_cell", "reason": "db_locked"})
    with _scripted_ipc_server([nak]) as (port, accepted):
        _hosted_env(monkeypatch, tmp_path, port)

        async def run():
            native.update_file("out.md", "the output\n")

        with pytest.raises(SystemExit) as exit_:
            native.main(run)

    assert exit_.value.code == 1
    finish = _finish_frame(accepted)
    assert finish["success"] is False
    assert "final flush failed" in finish["error_message"]
    assert "db_locked" in finish["error_message"]


@contextmanager
def _closing_ipc_server():
    """A host that accepts the run's IPC connection and closes it at once:
    every later frame of the run, its finish included, goes nowhere."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    srv.settimeout(5.0)

    def _serve():
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        conn.close()

    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    try:
        yield srv.getsockname()[1]
    finally:
        srv.close()
        t.join(timeout=2.0)


def test_main_exits_nonzero_when_the_host_closed_the_channel__F1135(tmp_path, monkeypatch):
    """F1135: with the channel gone neither the final flush nor the finish
    frame reaches the host, which then reads the run's outcome from its
    exit status (0 reads as completed). A run whose output did not land
    exits 1."""
    from collimate import native
    monkeypatch.delenv("COLLIMATE_IPC_PORT", raising=False)
    with _closing_ipc_server() as port:
        _hosted_env(monkeypatch, tmp_path, port)

        async def run():
            await asyncio.sleep(0.2)               # the host has hung up by now
            native.update_file("out.md", "the output\n")

        with pytest.raises(SystemExit) as exit_:
            native.main(run)
    assert exit_.value.code == 1


def test_main_exits_nonzero_when_the_generator_fails__F1135(tmp_path, monkeypatch):
    """The exit status says what the finish frame says, for a generator
    failure too."""
    from collimate import native
    monkeypatch.delenv("COLLIMATE_IPC_PORT", raising=False)
    with _scripted_ipc_server([]) as (port, accepted):
        _hosted_env(monkeypatch, tmp_path, port)

        async def run():
            raise RuntimeError("boom")

        with pytest.raises(SystemExit) as exit_:
            native.main(run)
    assert exit_.value.code == 1
    finish = _finish_frame(accepted)
    assert finish["success"] is False and finish["error_message"] == "boom"


def test_main_succeeds_when_thefinal_flush_is_partly_refused__F1135(tmp_path, monkeypatch):
    """A partial refusal committed the rest: the run stands."""
    import json as _json
    from collimate import native
    monkeypatch.delenv("COLLIMATE_IPC_PORT", raising=False)
    ack = _json.dumps({"op": "ack_update", "cell_id": "cell-1", "refused": [
        {"path": "_runs/other-1/x.md", "reason": "locked", "owner": "other-1"}]})
    with _scripted_ipc_server([ack]) as (port, accepted):
        _hosted_env(monkeypatch, tmp_path, port)

        async def run():
            native.update_file("out.md", "the output\n")

        native.main(run)

    assert _finish_frame(accepted)["success"] is True


class _DictSide(mirror_runtime.FileEndpoint):
    """A mirror endpoint over a dict; ``commit`` fails ``fail`` times."""

    def __init__(self, label, files, *, fail=0):
        self.label = label
        self.files = dict(files)
        self.fail = fail
        self.commits = 0

    async def hashes(self):
        import hashlib
        return {p: hashlib.sha256(c.encode("utf-8") if isinstance(c, str) else c).hexdigest() for p, c in self.files.items()}

    async def read(self, path):
        return self.files[path]

    async def write(self, path, content):
        self.files[path] = content

    async def delete(self, path):
        self.files.pop(path, None)

    async def commit(self):
        self.commits += 1
        if self.fail:
            self.fail -= 1
            from collimate._host.bridge import IPCTimeoutError
            raise IPCTimeoutError("ack_update", 60.0)


def test_mirror_rolls_back_its_echo_map_when_a_commit_fails__F1130(native_bridge):
    """F1130 (live files): a push whose commit fails reached nothing, so the
    mirror forgets it recorded the path as synced. The next tick pushes the
    workspace edit again instead of taking the cell's stale copy for the
    newer one and pulling it back over the edit."""
    local = _DictSide("workspace", {"a.md": "v1"})
    remote = _DictSide("cell", {"a.md": "v1"}, fail=1)
    mirror = mirror_runtime.Mirror(local, remote, own_folder="_runs/test")
    asyncio.run(mirror._tick())                 # seeds the echo map
    local.files["a.md"] = "v2"                  # the run edits its copy
    with pytest.raises(Exception):
        asyncio.run(mirror._tick())             # push staged, commit fails
    # The commit failed, so the cell never took v2 (a dict side keeps the
    # staged write; the cell's real read would still answer v1).
    remote.files["a.md"] = "v1"
    asyncio.run(mirror._tick())
    assert local.files["a.md"] == "v2", "the edit was reverted from the cell"
    assert remote.files["a.md"] == "v2"
    assert remote.commits == 2


def test_mirror_rolls_back_a_pull_whose_commit_fails__F1130(native_bridge):
    """The same rollback on the other side: a pull whose commit fails
    reached nothing, so the next tick pulls the cell's edit again instead
    of taking the local side's stale copy for the newer one and pushing it
    back over the edit."""
    local = _DictSide("workspace", {"a.md": "v1"}, fail=1)
    remote = _DictSide("cell", {"a.md": "v1"})
    mirror = mirror_runtime.Mirror(local, remote, own_folder="_runs/test")
    asyncio.run(mirror._tick())                 # seeds the echo map
    remote.files["a.md"] = "v2"                 # the user edits the cell
    with pytest.raises(Exception):
        asyncio.run(mirror._tick())             # pull staged, commit fails
    local.files["a.md"] = "v1"                  # the pull never landed
    asyncio.run(mirror._tick())
    assert remote.files["a.md"] == "v2", "the cell edit was pushed over"
    assert local.files["a.md"] == "v2"
    assert local.commits == 2


def test_mirror_keeps_its_echo_map_on_a_partial_refusal__F1130(native_bridge):
    """A partial refusal committed the rest: those pushes landed, so their
    echo entries stand. Rolled back, a committed path the user then edits
    in the cell would read as a workspace change and be pushed back over
    the user's edit."""
    from collimate._host.bridge import PartialRefusal

    class _Refusing(_DictSide):
        async def commit(self):
            self.commits += 1
            raise PartialRefusal("x.md is locked by x (the rest of the update was committed)")

    local = _DictSide("workspace", {"a.md": "v1"})
    remote = _Refusing("cell", {"a.md": "v1"})
    mirror = mirror_runtime.Mirror(local, remote, own_folder="_runs/test")
    asyncio.run(mirror._tick())                 # seeds the echo map
    local.files["a.md"] = "v2"
    with pytest.raises(PartialRefusal):
        asyncio.run(mirror._tick())             # v2 committed, the refusal named another path
    remote.files["a.md"] = "user edit"          # the user edits the committed file
    asyncio.run(mirror._tick())
    assert local.files["a.md"] == "user edit", "the workspace copy was pushed over the edit"
    assert remote.files["a.md"] == "user edit"


@pytest.mark.parametrize("fails", [False, True])
def test_mirror_keeps_the_echo_map_of_a_cancelled_commit_that_landed__F1130(native_bridge, fails):
    """F1130 (round-2 review): a tick cancelled inside its commit
    (``_Mirror.__aexit__`` does that on every ``live_files`` exit) waits for
    the flush on its way. When that flush landed, the push is in the cell
    and its echo entry stands: rolled back, the final reconcile would push
    the workspace copy over a cell edit made since. When it failed, the
    entry is rolled back and the push goes again."""
    from collimate import native

    class _Cell(_DictSide):
        async def commit(self):
            self.commits += 1
            await native.commit()

    local = _DictSide("workspace", {"a.md": "v1"})
    remote = _Cell("cell", {"a.md": "v1"})
    mirror = mirror_runtime.Mirror(local, remote, own_folder="_runs/test")
    asyncio.run(mirror._tick())                 # seeds the echo map
    local.files["a.md"] = "v2"                  # the run edits its copy
    _sent, started, release = _stalled_update_cell(native_bridge, fail_first=fails)

    async def go():
        tick = asyncio.create_task(mirror._tick())
        while not started.is_set():
            await asyncio.sleep(0.01)
        tick.cancel()                           # the push's flush is on its way
        await asyncio.sleep(0.05)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await tick

    asyncio.run(go())
    if fails:
        remote.files["a.md"] = "v1"             # the push never landed
        asyncio.run(mirror._tick())
        assert remote.files["a.md"] == "v2" and local.files["a.md"] == "v2"
    else:
        remote.files["a.md"] = "user edit"      # the user edits the committed file
        asyncio.run(mirror._tick())
        assert local.files["a.md"] == "user edit", "the workspace copy was pushed over the edit"
        assert remote.files["a.md"] == "user edit"


def test_mirror_pushes_a_crlf_file_byte_exact__F1190(native_bridge, tmp_path):
    """F1190: the live mirror reads a workspace file to push it to the cell;
    that read keeps CRLF, so a CRLF file reaches the cell unchanged instead
    of being rewritten as LF in the cell and then in the workspace."""
    (tmp_path / "run.bat").write_bytes(b"@echo off\r\necho hi\r\n")
    remote = _DictSide("cell", {})
    mirror = mirror_runtime.Mirror(mirror_runtime.LocalWorkspace(set()), remote, own_folder="_runs/test")
    asyncio.run(mirror._tick())
    assert remote.files == {"run.bat": "@echo off\r\necho hi\r\n"}
    assert remote.commits == 1
    asyncio.run(mirror._tick())                 # nothing echoes back
    assert (tmp_path / "run.bat").read_bytes() == b"@echo off\r\necho hi\r\n"
    assert remote.commits == 1


def test_failed_host_commit_restages_the_whole_update__F0097(native_bridge, monkeypatch):
    from collimate import native
    from collimate._host.bridge import IPCError
    native_bridge.fs_write_any('report.md', 'candidate')
    native_bridge.tracked_writes = {'report.md'}
    native_bridge.tracked_deletions = {'obsolete.md'}
    monkeypatch.setattr(native_bridge, 'emit_and_wait', lambda *_a, **_k: {
        'op': 'ack_update', 'ok': False, 'error': 'commit failed: disk full'})
    with pytest.raises(IPCError, match='disk full'):
        asyncio.run(native.commit())
    assert native_bridge.tracked_writes == {'report.md'}
    assert native_bridge.tracked_deletions == {'obsolete.md'}


def test_set_bridge_resets_the_annotation_namespace(tmp_path):
    """A new run carries no annotation subscription, notice, undelivered batch
    or card-showing inbox over from the previous one."""
    from collimate import native
    from collimate._results import AnnotationNotice
    from collimate.testing import make_test_bridge

    bridge = make_test_bridge(tmp_path)
    try:
        native.annotations._undelivered = ["stale"]
        native.annotations._notice = AnnotationNotice(5, 2, 1)
        native.annotations._subscriptions = [object()]
        native.annotations._stopped = True
        native._inbox_shows_cards[0] = True
        native.set_bridge(bridge)
        assert native.annotations._undelivered == []
        assert native.annotations._subscriptions == []
        assert native.annotations._stopped is False
        assert native.annotations.notice == AnnotationNotice(0, 0, 0)
        assert native._inbox_shows_cards[0] is False
    finally:
        native._clear_bridge()
        bridge.close()
