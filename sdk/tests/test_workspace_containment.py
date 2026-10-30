"""Host-side workspace containment against planted symlinks / FIFOs.

A containerized harness is root in the bind-mounted workspace, so it can
plant (or race in) a symlink to a host path, or a FIFO. The host-side
generator's snapshot, per-turn/exec restage, live mirror and file tools must
never read through, write through, or block on one of those.
"""

from __future__ import annotations

from collimate import _mirror as mirror_runtime

import asyncio
import errno
import os
import sys
import threading

import pytest


import _agent_shared as eng  # noqa: E402
from _agent_shared import workspace_tools  # noqa: E402
import _agent_shared.acp_run as acp_run  # noqa: E402

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="symlink / FIFO semantics are POSIX",
)


@pytest.fixture
def outside(tmp_path_factory):
    """Makes a ``secret.txt`` in a fresh directory beside the workspace (NOT
    under it), which pytest's temp retention cleans up."""
    def make(content: bytes = b"ORIGINAL\r\nline2\r\n") -> str:
        p = os.path.join(tmp_path_factory.mktemp("outside"), "secret.txt")
        with open(p, "wb") as fh:
            fh.write(content)
        return p
    return make


def _read(p: str) -> bytes:
    with open(p, "rb") as fh:
        return fh.read()


# ── snapshot ─────────────────────────────────────────────────────────


def test_snapshot_skips_symlinks(native_bridge, outside):
    import gen

    ws = gen.workspace_path()
    secret = outside()
    open(os.path.join(ws, "real.txt"), "w").write("hi")
    os.symlink(secret, os.path.join(ws, "leak"))
    os.makedirs(os.path.join(os.path.dirname(secret), "sub"))
    open(os.path.join(os.path.dirname(secret), "sub", "f.txt"), "w").write("x")
    os.symlink(os.path.join(os.path.dirname(secret), "sub"), os.path.join(ws, "linkdir"))
    snap = gen.snapshot()
    assert set(snap) == {"real.txt"}


def test_snapshot_does_not_block_on_a_fifo(native_bridge):
    import gen

    ws = gen.workspace_path()
    open(os.path.join(ws, "real.txt"), "w").write("hi")
    os.mkfifo(os.path.join(ws, "pipe"))
    box: dict = {}
    t = threading.Thread(target=lambda: box.setdefault("snap", gen.snapshot()),
                         daemon=True)
    t.start()
    t.join(2.0)
    assert not t.is_alive(), "snapshot blocked on a FIFO"
    assert set(box["snap"]) == {"real.txt"}


def test_snapshot_hash_matches_sha256_of_file_bytes(native_bridge):
    import hashlib

    import gen

    data = b"abc" * 100000
    with open(os.path.join(gen.workspace_path(), "big.bin"), "wb") as fh:
        fh.write(data)
    assert gen.snapshot()["big.bin"] == hashlib.sha256(data).hexdigest()


# ── restage (run_agent / run_bash turn epilogue) ──────────────────────


def test_restage_never_stages_or_writes_through_a_planted_symlink(native_bridge, outside):
    import gen

    ws = gen.workspace_path()
    secret = outside()
    open(os.path.join(ws, "a.txt"), "w").write("v1")
    before = workspace_tools.safe_snapshot()
    # The "turn": a harness replaces a.txt with a link and plants another.
    os.remove(os.path.join(ws, "a.txt"))
    os.symlink(secret, os.path.join(ws, "a.txt"))
    os.symlink(secret, os.path.join(ws, "leak.txt"))
    native_bridge.tracked_writes.clear()
    after = workspace_tools.safe_snapshot()
    workspace_tools.restage_exec_writes(before, after)
    assert "leak.txt" not in native_bridge.tracked_writes
    assert "a.txt" not in native_bridge.tracked_writes
    assert _read(secret) == b"ORIGINAL\r\nline2\r\n"    # not even CRLF→LF


def test_run_agent_turn_does_not_stage_a_symlink_planted_mid_turn(native_bridge, monkeypatch, outside):
    import gen

    ws = gen.workspace_path()
    secret = outside()

    async def _fake_pai(**kw):
        os.symlink(secret, os.path.join(ws, "leak.txt"))
        open(os.path.join(ws, "made.txt"), "w").write("real output")
        return ("done", [], {})

    monkeypatch.setattr(acp_run, "run_acp_turn", _fake_pai)
    native_bridge.tracked_writes.clear()
    asyncio.run(eng.run_agent(prompt="p", backend="pydantic-ai"))
    assert "made.txt" in native_bridge.tracked_writes     # restage still works
    assert "leak.txt" not in native_bridge.tracked_writes
    assert _read(secret) == b"ORIGINAL\r\nline2\r\n"


def test_restage_stages_a_crlf_file_as_its_own_bytes_with_no_text_read__F1364(
        native_bridge, monkeypatch):
    """F1364: re-staging a CRLF file a subprocess wrote stages it unchanged —
    the file is tracked for the flush and the workspace copy the flush
    commits is its own bytes, never an LF rewrite of them. The restage reads
    the file once, as bytes: ``gen.read_file`` (the text read-back the LF
    rewrite came from) is never called."""
    import gen

    def _no_read_back(*a, **k):
        raise AssertionError("the restage read the file back through gen.read_file")

    ws = gen.workspace_path()
    data = b"id,name\r\n1,alpha\r\n2,beta\r\n"
    before = workspace_tools.safe_snapshot()
    with open(os.path.join(ws, "table.csv"), "wb") as fh:
        fh.write(data)
    native_bridge.tracked_writes.clear()
    monkeypatch.setattr(gen, "read_file", _no_read_back)
    notes = workspace_tools.restage_exec_writes(before, workspace_tools.safe_snapshot())
    assert notes == []
    assert "table.csv" in native_bridge.tracked_writes
    assert _read(os.path.join(ws, "table.csv")) == data


def test_restage_read_raced_to_symlink_is_refused(native_bridge, outside):
    """The snapshot saw a regular file; a link is swapped in before the
    read-back. The host open must refuse it (a note), not follow it."""
    import gen

    ws = gen.workspace_path()
    secret = outside()
    os.symlink(secret, os.path.join(ws, "x.txt"))
    notes = workspace_tools.restage_exec_writes({}, {"x.txt": "sha-of-a-regular-file"})
    assert "x.txt" not in native_bridge.tracked_writes
    assert any("x.txt" in n for n in notes)
    assert _read(secret) == b"ORIGINAL\r\nline2\r\n"


# ── host fs ops ───────────────────────────────────────────────────────


def test_host_fs_ops_refuse_symlinks_at_any_component(native_bridge, outside):
    import gen

    ws = gen.workspace_path()
    secret = outside()
    os.symlink(secret, os.path.join(ws, "leak"))
    os.symlink(os.path.dirname(secret), os.path.join(ws, "dir"))
    for bad in ("leak", "dir/secret.txt"):
        with pytest.raises(OSError):
            native_bridge.fs_write_any(bad, "PWNED")
        with pytest.raises(OSError):
            native_bridge.fs_read_any(bad)
        with pytest.raises(OSError):
            native_bridge.fs_read_bytes(bad)
    for bad in ("../x", "/etc/hostname"):
        with pytest.raises(OSError):
            native_bridge.fs_write_any(bad, "PWNED")
    # Deleting through a symlinked dir must not reach the target.
    native_bridge.fs_unlink("dir/secret.txt")
    assert _read(secret) == b"ORIGINAL\r\nline2\r\n"
    # Plain workspace writes (incl. new nested dirs) keep working.
    gen.update_file("n/e/w.txt", "ok")
    assert gen.read_file("n/e/w.txt") == "ok"
    gen.update_file("b.bin", b"\xff\xfe")
    assert gen.read_file("b.bin") == b"\xff\xfe"


def test_host_read_refuses_a_fifo_without_blocking(native_bridge):
    import gen

    os.mkfifo(os.path.join(gen.workspace_path(), "pipe"))
    box: dict = {}

    def _go():
        try:
            native_bridge.fs_read_any("pipe")
        except OSError as e:
            box["err"] = e

    t = threading.Thread(target=_go, daemon=True)
    t.start()
    t.join(2.0)
    assert not t.is_alive() and "err" in box


# ── live mirror (workspace endpoint) ──────────────────────────────────


def test_local_workspace_write_and_delete_refuse_escaping_symlink(native_bridge, outside):
    import gen

    ws = gen.workspace_path()
    secret = outside()
    os.symlink(secret, os.path.join(ws, "leak"))
    os.symlink(os.path.dirname(secret), os.path.join(ws, "dir"))
    lw = mirror_runtime.LocalWorkspace(set())
    with pytest.raises(OSError):
        asyncio.run(lw.write("leak", "CHOSEN"))
    with pytest.raises(OSError):
        asyncio.run(lw.write("dir/secret.txt", "CHOSEN"))
    asyncio.run(lw.delete("dir/secret.txt"))
    assert _read(secret) == b"ORIGINAL\r\nline2\r\n"


def test_mirror_never_pulls_cell_content_through_a_swapped_symlink__D07(native_bridge, outside):
    """A synced workspace file is swapped for a link to a host file while the
    cell still holds (or later changes) that path: no tick may write the
    cell's content onto the link's target."""
    import gen

    class _Cell(mirror_runtime.FileEndpoint):
        label = "cell"

        def __init__(self):
            self.files = {"a.txt": "v1"}

        async def hashes(self):
            return {p: __import__("hashlib").sha256(c.encode("utf-8") if isinstance(c, str) else c).hexdigest() for p, c in self.files.items()}

        async def read(self, path):
            return self.files[path]

        async def write(self, path, content):
            self.files[path] = content

        async def delete(self, path):
            # The host carries a skipped path forward from the tip — model
            # the worst case: the cell keeps it.
            pass

    ws = gen.workspace_path()
    secret = outside()
    open(os.path.join(ws, "a.txt"), "w").write("v1")
    open(os.path.join(ws, "keep.txt"), "w").write("k")
    cell = _Cell()
    cell.files["keep.txt"] = "k"
    m = mirror_runtime.Mirror(mirror_runtime.LocalWorkspace(set()), cell,
                       own_folder=native_bridge.folder, interval=0.1)

    async def _go():
        await m.__aenter__()
        m._stopped = True                       # drive ticks by hand
        if m._task is not None:
            m._task.cancel()
        os.remove(os.path.join(ws, "a.txt"))
        os.symlink(secret, os.path.join(ws, "a.txt"))
        # The first tick requests deletion of the omitted symlink. This fake
        # cell deliberately retains the path; subsequent pulls must fail loudly.
        await m._tick()
        for _ in range(2):
            with pytest.raises(OSError) as denied:
                await m._tick()
            assert denied.value.errno == errno.ELOOP
            assert _read(secret) == b"ORIGINAL\r\nline2\r\n"
        cell.files["a.txt"] = "CHOSEN-BY-WORKSPACE-WRITER"   # a cell-side change
        for _ in range(3):
            with pytest.raises(OSError) as denied:
                await m._tick()
            assert denied.value.errno == errno.ELOOP
            assert _read(secret) == b"ORIGINAL\r\nline2\r\n"
            assert _read(os.path.join(ws, "keep.txt")) == b"k"
            assert cell.files["keep.txt"] == "k"

    asyncio.run(_go())
    assert _read(secret) == b"ORIGINAL\r\nline2\r\n"


# ── file tools: the contain_ws_path → open race (F04) ─────────────────


def _race_after_check(monkeypatch, swap):
    """Make the race deterministic: contain_ws_path passes on a regular file,
    then ``swap`` runs before the tool's open — as a harness winning it."""
    real = workspace_tools.contain_ws_path

    def _checked_then_swapped(p):
        out = real(p)
        swap()
        return out

    monkeypatch.setattr(workspace_tools, "contain_ws_path", _checked_then_swapped)


def _swap_to_link(path, target):
    def _do():
        os.remove(path)
        os.symlink(target, path)
    return _do


def test_edit_file_race_to_symlink_cannot_write_outside(native_bridge, monkeypatch, outside):
    import gen

    ws = gen.workspace_path()
    secret = outside(b"original")
    x = os.path.join(ws, "x")
    open(x, "w").write("original")
    _race_after_check(monkeypatch, _swap_to_link(x, secret))
    out = asyncio.run(workspace_tools.edit_file("x", "original", "PWNED"))
    assert out.startswith("ERROR")
    assert _read(secret) == b"original"


def test_write_file_race_to_symlink_cannot_write_outside(native_bridge, monkeypatch, outside):
    import gen

    ws = gen.workspace_path()
    secret = outside(b"original")
    x = os.path.join(ws, "x")
    open(x, "w").write("mine")
    _race_after_check(monkeypatch, _swap_to_link(x, secret))
    out = asyncio.run(workspace_tools.write_file("x", "PWNED"))
    assert out.startswith("ERROR")
    assert _read(secret) == b"original"


def test_write_file_race_on_parent_dir_cannot_write_outside(native_bridge, monkeypatch, outside):
    import gen

    ws = gen.workspace_path()
    secret = outside(b"original")
    d = os.path.join(ws, "d")
    os.makedirs(d)
    open(os.path.join(d, "secret.txt"), "w").write("mine")

    def _swap_dir():
        os.remove(os.path.join(d, "secret.txt"))
        os.rmdir(d)
        os.symlink(os.path.dirname(secret), d)

    _race_after_check(monkeypatch, _swap_dir)
    out = asyncio.run(workspace_tools.write_file("d/secret.txt", "PWNED"))
    assert out.startswith("ERROR")
    assert _read(secret) == b"original"


def test_read_and_view_race_to_symlink_cannot_read_outside(native_bridge, monkeypatch, outside):
    import gen

    ws = gen.workspace_path()
    secret = outside(b"TOPSECRET")
    x = os.path.join(ws, "x.png")
    open(x, "w").write("harmless")
    _race_after_check(monkeypatch, _swap_to_link(x, secret))
    out = asyncio.run(workspace_tools.read_file("x.png"))
    assert out.startswith("ERROR") and "TOPSECRET" not in out
    os.remove(x)
    open(x, "w").write("harmless")
    out = asyncio.run(workspace_tools.view_image("x.png"))
    assert isinstance(out, str) and out.startswith("ERROR")


def test_contain_ws_path_turns_a_resolution_oserror_into_an_escape(native_bridge, monkeypatch):
    from _agent_shared.workspace_tools import PathEscape, contain_ws_path

    real = os.path.realpath

    def _flaky(p, *a, **kw):
        if str(p).endswith("racy"):
            raise OSError(22, "Invalid argument")
        return real(p, *a, **kw)

    monkeypatch.setattr(os.path, "realpath", _flaky)
    with pytest.raises(PathEscape):
        contain_ws_path("racy")
