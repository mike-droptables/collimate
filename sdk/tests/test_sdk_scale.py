"""P2 proposed assertions, before product changes; credential free."""

import asyncio
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

SDK = Path(__file__).parents[1]
from collimate import native  # noqa: E402
from collimate.testing._bridge import RecordingBridge  # noqa: E402
from _agent_shared import harness_homes  # noqa: E402
from _agent_shared import acp_client as ac  # noqa: E402
import _agent_shared as agent  # noqa: E402
from _agent_shared import workspace_tools  # noqa: E402
import pytest  # noqa: E402
from tests._git_fixtures import GIT_OK  # noqa: E402


def test_unchanged_snapshots_share_digest_cache__F1324_F1620_F1222(tmp_path, monkeypatch):
    (tmp_path / "one.txt").write_text("unchanged")
    bridge = RecordingBridge.create(str(tmp_path))
    calls = []
    original = hashlib.file_digest

    def digest(fh, algorithm):
        calls.append(os.fstat(fh.fileno()).st_size)
        return original(fh, algorithm)

    monkeypatch.setattr(hashlib, "file_digest", digest)
    assert bridge.snapshot_workspace() == bridge.snapshot_workspace()
    assert calls == [9]


def test_track_file_stages_without_rewriting__F1174_F1180_F1247(tmp_path, monkeypatch):
    path = tmp_path / "one.txt"
    path.write_bytes(b"unchanged\r\n")
    bridge = RecordingBridge.create(str(tmp_path))
    monkeypatch.setattr(native, "_bridge", bridge)
    old = path.stat()
    native.track_file("one.txt")
    assert path.read_bytes() == b"unchanged\r\n"
    assert path.stat().st_mtime_ns == old.st_mtime_ns
    assert "one.txt" in bridge.tracked_writes


def test_terminal_output_limit_is_capped__F1389():
    term = ac._CommandTerminal("one", byte_limit=32 * 1024**2)
    assert term.byte_limit == 16 * 1024**2


def test_released_terminal_discards_output_keeps_kill_record__F1412_F1439():
    term = ac._CommandTerminal("one", byte_limit=None)
    term.feed(b"output")
    killed = []
    term._kill_impl = lambda: killed.append(True)
    session = SimpleNamespace(_terminals={"one": term})
    asyncio.run(ac._CollimateClient(session).release_terminal("upstream", "one"))
    assert killed == [True] and term.released
    assert session._terminals["one"] is term
    assert bytes(term.buf) == b""


def test_jsonl_sync_copies_only_new_suffix__F1432(tmp_path, monkeypatch):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    path = source / "history.jsonl"
    path.write_bytes(b"first\n")
    copied = []
    original = harness_homes.shutil.copyfileobj

    class Reader:
        def __init__(self, src):
            self.src = src

        def read(self, n=-1):
            data = self.src.read(n)
            copied.append(len(data))
            return data

    def copy(src, dst, *a, **kw):
        return original(Reader(src), dst, *a, **kw)

    monkeypatch.setattr(harness_homes.shutil, "copyfileobj", copy)
    harness_homes._copy_session_state(str(source), str(target), "codex")
    with path.open("ab") as fh:
        fh.write(b"second\n")
    harness_homes._copy_session_state(str(source), str(target), "codex")
    assert (target / "history.jsonl").read_bytes() == b"first\nsecond\n"
    assert sum(copied) <= 13


def test_large_file_first_line_avoids_full_read__F1370(tmp_path, monkeypatch):
    path = tmp_path / "large.txt"
    path.write_bytes(b"first\n" + b"x" * 9_000_000)
    bridge = RecordingBridge.create(str(tmp_path))
    monkeypatch.setattr(native, "_bridge", bridge)
    reads = []
    original = bridge.fs_read_bytes

    def read(p):
        data = original(p)
        reads.append(len(data))
        return data

    monkeypatch.setattr(bridge, "fs_read_bytes", read)
    assert asyncio.run(workspace_tools.read_file("large.txt", limit=1)) == "     1\tfirst"
    assert sum(reads) <= 8192


def test_fallback_grep_skips_oversized_file__F1362_F1323(tmp_path, monkeypatch):
    (tmp_path / "large.txt").write_bytes(b"needle\n" + b"x" * 20_000_001)
    monkeypatch.setattr(agent.shutil, "which", lambda _: None)
    out = workspace_tools.grep_impl(tmp_path, "needle", max_results=1)
    assert "large.txt:1:needle" not in out
    assert "skip" in out.lower()


def test_listing_caps_output_and_hides_vendor_folders__F1374_F1333_F1371(monkeypatch):
    values = [f"folder/file-{i:05d}-{'x' * 50}.txt" for i in range(3000)]
    monkeypatch.setattr(agent.gen, "list_files", lambda: ["node_modules/private.js", *values])
    out = asyncio.run(workspace_tools.list_files())
    assert "node_modules" not in out
    assert len(out.encode()) < 66_000
    assert "truncated" in out


def test_acp_read_runs_off_loop__F1430(tmp_path, monkeypatch):
    import threading

    main_thread = threading.get_ident()
    seen = []
    monkeypatch.setattr(ac.gen, "has_file", lambda _: True)
    monkeypatch.setattr(
        ac.gen, "read_file", lambda _: seen.append(threading.get_ident()) or "hello"
    )
    client = ac._CollimateClient(SimpleNamespace())
    monkeypatch.setattr(client, "_rel", lambda path: path)
    response = asyncio.run(client.read_text_file("upstream", "file.txt"))
    assert response.content == "hello"
    assert seen and seen[0] != main_thread


def test_account_helpers_run_off_loop__F1527_F1558(monkeypatch):
    import threading
    from _agent_shared import accounts

    main_thread = threading.get_ident()
    seen = []
    snapshot = {
        "kind": "model",
        "service": "openai",
        "protocol": "openai-compatible",
        "model": "test",
        "secrets": {"api_key": "inert"},
    }
    monkeypatch.setattr(
        accounts.gen, "account", lambda role: seen.append(threading.get_ident()) or snapshot
    )
    assert asyncio.run(accounts.account_api_key("openai")) == "inert"
    assert asyncio.run(accounts.account_secret("model.api_key")) == "inert"
    assert seen and all(value != main_thread for value in seen)


def test_terminal_status_probe_runs_off_loop__F1176_F1168(monkeypatch, native_bridge):
    import threading

    main_thread = threading.get_ident()
    seen = []
    monkeypatch.setattr(
        agent.gen,
        "terminal_status",
        lambda _: seen.append(threading.get_ident()) or {"ended": True, "exit_code": 0},
    )
    monkeypatch.setattr(agent.gen, "log", lambda _: None)
    assert asyncio.run(agent.serve_tty_until(terminal_id="owned")) == "exited"
    assert seen[0] != main_thread


def test_read_image_tools_already_use_workers__F1618(monkeypatch, tmp_path):
    import threading

    (tmp_path / "image.png").write_bytes(b"\x89PNG")
    bridge = RecordingBridge.create(str(tmp_path))
    monkeypatch.setattr(native, "_bridge", bridge)
    original = bridge.fs_read_any
    seen = []
    monkeypatch.setattr(
        bridge, "fs_read_any", lambda path: seen.append(threading.get_ident()) or original(path)
    )
    main_thread = threading.get_ident()
    assert asyncio.run(workspace_tools.load_view_image("image.png"))[0] is None
    assert seen[0] != main_thread


def test_codex_host_tail_reads_only_appended_bytes__F1564_P0F15(tmp_path, monkeypatch):
    import json
    import time
    from _agent_shared import terminal_tail
    from _agent_shared.harnesses.codex import backend as codex_backend

    (tmp_path / ".collimate-codex-start").write_text("")
    time.sleep(0.002)
    path = tmp_path / "sessions" / "today" / "rollout.jsonl"
    path.parent.mkdir(parents=True)
    line = (
        json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "first"}],
                },
            }
        )
        + "\n"
    )
    path.write_text(line)
    reader = terminal_tail._CodexTail(str(tmp_path))
    calls = []
    original = os.read

    def read(fd, size):
        value = original(fd, size)
        calls.append(len(value))
        return value

    monkeypatch.setattr(os, "read", read)
    assert reader.read()
    first_bytes = sum(calls)
    assert first_bytes == len(line.encode())
    calls.clear()
    assert reader.read()
    assert sum(calls) == 0
    second = line.replace("first", "second")
    with path.open("a") as fh:
        fh.write(second)
    calls.clear()
    assert len(reader.read()) == 2
    assert sum(calls) == len(second.encode())
    assert codex_backend._newest_codex_session(str(tmp_path)) == line + second


@pytest.mark.skipif(not GIT_OK, reason="needs git 2.31 or later")
def test_repository_existing_security_scan_runs_off_loop__F1622_F1621_F1584(
    tmp_path, native_bridge, fake_remote, monkeypatch
):
    import threading
    from _env_shared import repositories

    fake_remote.create("https://github.com/owner/repo.git", {"source.txt": "source\n"})
    target = repositories.Target("https://github.com/owner/repo.git", "repo")
    asyncio.run(repositories.sync(target, account=None, log=lambda _line: None))
    (tmp_path / "repo" / "local.txt").write_text("local edit\n")
    fake_remote.commit("https://github.com/owner/repo.git", {"source.txt": "moved\n"})
    seen = {}
    main_thread = threading.get_ident()
    for name in ("_folder_state", "_cell_files"):
        original = getattr(repositories, name)

        def scan(*args, _name=name, _original=original):
            seen.setdefault(_name, []).append(threading.get_ident())
            return _original(*args)

        monkeypatch.setattr(repositories, name, scan)
    asyncio.run(repositories.sync(target, account=None, log=lambda _line: None))
    assert set(seen) == {"_folder_state", "_cell_files"}
    assert main_thread not in {thread for threads in seen.values() for thread in threads}
    assert (tmp_path / "repo" / "source.txt").read_text() == "moved\n"


def test_idle_action_polls_back_off_without_changing_filters__F1177(monkeypatch):
    class Bridge:
        cancelled = False

        def __init__(self):
            self.calls = []

        def poll_renderer_action_event(self, **kwargs):
            self.calls.append(kwargs)
            return {"action_id": "go"} if len(self.calls) == 5 else None

    bridge = Bridge()
    monkeypatch.setattr(native, "_bridge", bridge)
    delays = []

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(native.asyncio, "sleep", sleep)
    event = asyncio.run(native.wait_for_action_event(source="card", action_id="go"))
    assert event == {"action_id": "go"}
    assert delays[1] > delays[0] and max(delays) <= 1.0
    assert bridge.calls == [{"source": "card", "action_id": "go"}] * 5


def test_streamed_rg_counts_matches_not_context__F1361_F1369_F1323(tmp_path):
    (tmp_path / "many.txt").write_text("before\nhit\nafter\nlaterhit\n")
    out = workspace_tools.grep_impl(tmp_path, "hit", max_results=1, context=1)
    assert "many.txt:2:hit" in out
    assert "many.txt-1-before" in out and "many.txt-3-after" in out
    assert "many.txt:4:laterhit" not in out
    assert "truncated" in out


def test_mcp_sync_web_handler_runs_off_server_loop__F1617(monkeypatch, native_bridge):
    import inspect
    import threading
    from mcp.server.mcpserver import MCPServer
    from _agent_shared import mcp_bridge

    captured = {}
    original = MCPServer.tool

    def tool(self, *args, **kwargs):
        decorate = original(self, *args, **kwargs)

        def register(fn):
            captured[kwargs.get("name")] = fn
            return decorate(fn)

        return register

    monkeypatch.setattr(MCPServer, "tool", tool)
    seen = []

    def web_fetch(url: str) -> str:
        seen.append(threading.get_ident())
        return url

    loop = asyncio.new_event_loop()
    bridge = mcp_bridge.start_bridge(
        loop=loop,
        bind_host="127.0.0.1",
        extra_entries=[("web_fetch", "web", False, "controlled fetch", web_fetch)],
    )
    try:
        assert bridge is not None
        main_thread = threading.get_ident()

        async def invoke():
            value = captured["web_fetch"]("inert")
            return await value if inspect.isawaitable(value) else value

        assert asyncio.run(invoke()) == "inert"
        assert seen[0] != main_thread
    finally:
        if bridge is not None:
            bridge.stop()
        loop.close()


def test_actual_trace_export_is_compact_json__F1577(monkeypatch):
    import json
    from _agent_shared.harnesses.claude_code import backend as claude_code_backend

    captured = {}
    monkeypatch.setattr(
        claude_code_backend.gen,
        "update_file",
        lambda path, body, **kwargs: captured.__setitem__(path, body),
    )
    raw = json.dumps({"type": "user", "message": {"role": "user", "content": "hello"}}) + "\n"
    claude_code_backend._export_trace(raw)
    body = captured[claude_code_backend.TRACE_FILE]
    assert json.loads(body)
    assert "\n" not in body


def test_filtered_cell_hashes_and_bodies_use_actual_bridge_contract__F1188(tmp_path, monkeypatch):
    bridge = RecordingBridge.create(str(tmp_path))
    calls = []

    def reply(payload, **kwargs):
        calls.append((payload, kwargs["wait_op"]))
        if payload["op"] == "read_cell_hashes":
            return {"hashes": {"one.txt": {"body_hash": "digest", "file_revision": 1, "encoding": "utf-8"}}}
        return {"files": {"one.txt": {"content": "Ym9keQ==", "body_hash": __import__("hashlib").sha256(b"body").hexdigest(), "file_revision": 1, "encoding": "utf-8"}}}

    from collimate._host.bridge import HostBridge

    monkeypatch.setattr(bridge, "emit_and_wait", reply)
    monkeypatch.setattr(bridge, "read_cell_descriptors", lambda **kw: HostBridge.read_cell_descriptors(bridge, **kw))
    assert HostBridge.read_cell_hashes(bridge, paths=["one.txt"]) == {"one.txt": "digest"}
    assert HostBridge.read_cell_files(bridge, paths=["one.txt"]) == {"one.txt": "body"}
    assert calls == [
        ({"op": "read_cell_hashes", "paths": ["one.txt"]}, "ack_read_cell_hashes"),
        ({"op": "read_cell_files", "paths": ["one.txt"]}, "ack_read_cell_files"),
    ]


def test_tail_reader_worker_and_small_merge_stay_ordered__F1499(monkeypatch):
    import threading
    from _agent_shared import terminal_tail

    seen = []

    class Reader:
        def read(self):
            seen.append(("read", threading.get_ident()))
            return []

    tail = terminal_tail.TranscriptTail(Reader(), backend="codex")
    monkeypatch.setattr(
        tail, "merge", lambda value: seen.append(("merge", threading.get_ident())) or 0
    )
    main = threading.get_ident()
    assert asyncio.run(tail.poll()) == 0
    assert seen[0][1] != main
    assert seen[1] == ("merge", main)


def test_codex_tail_resets_replacement_growth_and_partial_records__F1564_P0F15(tmp_path):
    import json
    from _agent_shared import terminal_tail
    from _agent_shared.harnesses.codex import backend as codex_backend

    marker = tmp_path / codex_backend._START_MARKER
    marker.write_text("")
    os.utime(marker, ns=(1, 1))
    path = tmp_path / "sessions/rollout.jsonl"
    path.parent.mkdir()

    def line(text):
        return (
            json.dumps(
                {
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": text}],
                    }
                }
            )
            + "\n"
        )

    path.write_text(line("first"))
    tail = terminal_tail._CodexTail(str(tmp_path))
    assert tail.read()[0].content == "first"
    path.write_text(line("replacement grows"))
    assert [m.content for m in tail.read()] == ["replacement grows"]
    path.write_text(line("short"))
    assert [m.content for m in tail.read()] == ["short"]
    with path.open("a") as stream:
        stream.write(line("next")[:-1])
    assert len(tail.read()) == 1
    with path.open("a") as stream:
        stream.write("\n")
    assert [m.content for m in tail.read()] == ["short", "next"]
    marker.unlink()
    assert tail.read() == []


def test_digest_cache_invalidates_ctime_inode_and_deleted_paths__F1324_F1620(tmp_path, monkeypatch):
    path = tmp_path / "file.txt"
    path.write_text("old")
    bridge = RecordingBridge.create(str(tmp_path))
    original = hashlib.file_digest
    reads = []

    def digest(stream, algorithm):
        reads.append(True)
        return original(stream, algorithm)

    monkeypatch.setattr(hashlib, "file_digest", digest)
    first = bridge.snapshot_workspace()
    stamp = path.stat().st_mtime_ns
    path.write_text("new")
    os.utime(path, ns=(stamp, stamp))
    second = bridge.snapshot_workspace()
    assert first != second and len(reads) == 2
    replacement = tmp_path / "replacement"
    replacement.write_text("end")
    os.utime(replacement, ns=(stamp, stamp))
    replacement.replace(path)
    assert bridge.snapshot_workspace() != second
    assert len(reads) == 3
    path.unlink()
    assert bridge.snapshot_workspace() == {}
    assert bridge._workspace_digests == {}


def test_jsonl_sync_falls_back_on_overwrite_and_truncation__F1432(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    target.mkdir()
    path = source / "history.jsonl"
    path.write_bytes(b"original\n")
    harness_homes._copy_session_state(str(source), str(target), "codex")
    for body in [b"replacement longer\n", b"tiny\n", b"partial"]:
        path.write_bytes(body)
        harness_homes._copy_session_state(str(source), str(target), "codex")
        assert (target / "history.jsonl").read_bytes() == body
    with path.open("ab") as stream:
        stream.write(b" completed\n")
    harness_homes._copy_session_state(str(source), str(target), "codex")
    assert (target / "history.jsonl").read_bytes() == b"partial completed\n"


@pytest.mark.skipif(not GIT_OK, reason="needs git 2.31 or later")
def test_repository_placement_tracks_without_rewriting__F1622_F1621_F1584(
    tmp_path, native_bridge, fake_remote, monkeypatch
):
    from _env_shared import repositories

    fake_remote.create("https://github.com/owner/repo.git", {"source.txt": b"source\r\n"})
    real_update = repositories.gen.update_file

    def update(path, content, **kwargs):
        if path != repositories.marker_path("repo"):
            raise AssertionError("rewrite")
        return real_update(path, content, **kwargs)

    stamps = {}
    real_move = repositories.shutil.move

    def move(source, destination):
        stamps[destination] = os.stat(source).st_mtime_ns
        return real_move(source, destination)

    monkeypatch.setattr(repositories.gen, "update_file", update)
    monkeypatch.setattr(repositories.shutil, "move", move)
    commits = []
    monkeypatch.setattr(repositories, "_commit",
                        lambda: (commits.append(set(native_bridge.tracked_writes)), repositories.gen.commit())[1])
    asyncio.run(repositories.sync(repositories.Target("https://github.com/owner/repo.git", "repo"), account=None,
                                  log=lambda _line: None))
    destination = tmp_path / "repo" / "source.txt"
    assert destination.read_bytes() == b"source\r\n"
    assert destination.stat().st_mtime_ns == stamps[str(destination)]
    assert commits == [{"repo/source.txt"}, {"repo.git-checkout.json"}]


@pytest.mark.skipif(not GIT_OK, reason="needs git 2.31 or later")
def test_repository_cancellation_joins_placement_before_temporary_cleanup__F1622_F1621_F1584(
    tmp_path, native_bridge, fake_remote, monkeypatch
):
    import threading
    import pytest
    from _env_shared import repositories

    fake_remote.create("https://github.com/owner/repo.git", {"source.txt": b"source\r\n"})
    entered, release = threading.Event(), threading.Event()
    observed = {}
    original = repositories._apply

    def place(workspace, changes):
        observed["temporary"] = changes[0].checkout
        entered.set()
        release.wait(5)
        observed["exists_on_resume"] = changes[0].checkout.exists()
        if changes[0].checkout.exists():
            original(workspace, changes)

    monkeypatch.setattr(repositories, "_apply", place)

    async def flow():
        task = asyncio.create_task(repositories.sync(
            repositories.Target("https://github.com/owner/repo.git", "repo"), account=None, log=lambda _line: None))
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        try:
            await asyncio.sleep(0.02)
            assert not task.done(), "cancellation must join the publication worker"
            assert observed["temporary"].exists()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert observed["exists_on_resume"]
        assert not observed["temporary"].exists()

    asyncio.run(flow())
    assert (tmp_path / "repo/source.txt").read_bytes() == b"source\r\n"
    assert "repo/source.txt" in native_bridge.tracked_writes
