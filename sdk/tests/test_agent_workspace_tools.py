# SPDX-License-Identifier: MIT
"""Unit tests for the rich workspace + web tools of the shared agent engine
(``generators/_agent_shared``): the upgraded line-numbered ``read_file``, the
``grep`` tool (ripgrep + pure-python fallback), the run_python/run_bash
subprocess-write re-staging, the ``web_fetch`` SSRF guard + readability
fallback, ``web_search`` engine selection, the per-backend websearch gating
seams (claude disallowed list, the CLI harnesses' own config seams), and the
bridge delivery of
the file/web tool rows (``tool_entries`` + ``start_bridge`` +
bridge cache-key separation).

Synthetic only: no network, no on-disk generator names. httpx / socket / ddgs /
trafilatura / ripgrep / the MCP server thread are all stubbed.
"""
from __future__ import annotations

import asyncio
import os
import sys
import types

import pytest

from _agent_shared import web_tools, workspace_tools  # noqa: E402

HANDLER_NAMES = ["list_handlers", "handler_skills", "validate_file"]


# ══════════════════════════════════════════════════════════════════════
# read_file — cat -n line numbering, offset/limit paging, byte cap, binary
# ══════════════════════════════════════════════════════════════════════


def test_read_file_line_numbers_1_based(native_bridge):
    import gen

    gen.update_file("f.txt", "l1\nl2\nl3\nl4\nl5\n")
    out = asyncio.run(workspace_tools.read_file("f.txt"))
    lines = out.split("\n")
    assert lines[0] == "     1\tl1"        # 1-based, 6-wide right-justified
    assert lines[4] == "     5\tl5"
    assert len(lines) == 5


def test_read_file_offset_limit_slice(native_bridge):
    import gen

    gen.update_file("f.txt", "l1\nl2\nl3\nl4\nl5\n")
    # offset is 0-based; the numbers stay the true (1-based) file line numbers.
    out = asyncio.run(workspace_tools.read_file("f.txt", offset=2, limit=2))
    assert out.split("\n") == ["     3\tl3", "     4\tl4"]


def test_read_file_byte_cap_truncates(native_bridge, monkeypatch):
    import gen

    monkeypatch.setattr(workspace_tools, "READ_FILE_BYTE_CAP", 40)
    gen.update_file("big.txt", "\n".join(f"line{i}" for i in range(200)) + "\n")
    out = asyncio.run(workspace_tools.read_file("big.txt", limit=200))
    assert out.endswith("…[truncated: use offset/limit]")
    body = out[: -len("\n…[truncated: use offset/limit]")]
    assert len(body.encode("utf-8")) <= 40
    assert "line0" in out


def test_read_file_cuts_a_single_line_over_the_cap(native_bridge, monkeypatch):
    import gen

    monkeypatch.setattr(workspace_tools, "READ_FILE_BYTE_CAP", 40)
    gen.update_file("one.txt", "x" * 500 + "\n")
    out = asyncio.run(workspace_tools.read_file("one.txt"))
    assert out.startswith("     1\t")
    body, _, marker = out.partition("\n…[")
    assert body and len(body.encode("utf-8")) <= 40
    assert "line truncated" in marker


def test_read_file_binary_and_missing(native_bridge):
    import gen

    gen.update_file("b.bin", b"\xff\xfe\xfd\xff")   # not utf-8 decodable
    assert asyncio.run(workspace_tools.read_file("b.bin")) == "BINARY (4 bytes)"
    assert asyncio.run(workspace_tools.read_file("nope.txt")) == "ERROR: file not found: nope.txt"


def test_edit_file_refuses_an_empty_old(native_bridge):
    import gen

    # "" is in every string: without the guard, new was silently prepended.
    gen.update_file("f.txt", "abc")
    out = asyncio.run(workspace_tools.edit_file("f.txt", "", "X"))
    assert out.startswith("ERROR")
    assert gen.read_file("f.txt") == "abc"


# ══════════════════════════════════════════════════════════════════════
# view_image — surface pixels to the model (the MCP Image content block)
# ══════════════════════════════════════════════════════════════════════

_PNG_1x1 = __import__("base64").b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def test_view_image_returns_binary_content(native_bridge):
    import gen

    gen.update_file("images/shot.png", _PNG_1x1)
    image = asyncio.run(workspace_tools.view_image("images/shot.png"))
    # The MCP server's Image content block: the shape every bridged client renders.
    assert type(image).__name__ == "Image"
    assert image._format == "png" and image.data == _PNG_1x1


def test_view_image_rejects_non_image_missing_and_oversize(native_bridge, monkeypatch):
    import gen

    gen.update_file("notes.txt", "hello")
    assert asyncio.run(workspace_tools.view_image("notes.txt")).startswith(
        "ERROR: notes.txt is not a viewable image"
    )
    assert asyncio.run(workspace_tools.view_image("gone.png")) == "ERROR: file not found: gone.png"
    monkeypatch.setattr(workspace_tools, "VIEW_IMAGE_BYTE_CAP", 3)
    gen.update_file("big.png", _PNG_1x1)
    assert asyncio.run(workspace_tools.view_image("big.png")).startswith("ERROR: big.png is too large")


def test_view_image_refuses_oversize_without_reading_it(native_bridge, monkeypatch):
    import gen

    gen.update_file("big.png", _PNG_1x1)
    monkeypatch.setattr(workspace_tools, "VIEW_IMAGE_BYTE_CAP", 3)

    def _no_read(path):
        raise AssertionError("an oversize image must be refused before it is read")

    monkeypatch.setattr(gen, "read_file", _no_read)
    assert asyncio.run(workspace_tools.view_image("big.png")).startswith("ERROR: big.png is too large")


# ══════════════════════════════════════════════════════════════════════
# grep — pure-python fallback + rg path
# ══════════════════════════════════════════════════════════════════════


def _no_rg(monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: None)


def test_grep_impl_match_format_and_glob(tmp_path, monkeypatch):
    _no_rg(monkeypatch)
    (tmp_path / "a.py").write_text("import os\nx = 1\n")
    (tmp_path / "b.txt").write_text("import sys\n")
    out = workspace_tools.grep_impl(str(tmp_path), "import")
    assert "a.py:1:import os" in out
    assert "b.txt:1:import sys" in out
    # glob filter restricts to matching relpaths
    only_py = workspace_tools.grep_impl(str(tmp_path), "import", path_glob="*.py")
    assert "a.py:1:import os" in only_py and "b.txt" not in only_py


def test_grep_impl_context_lines(tmp_path, monkeypatch):
    _no_rg(monkeypatch)
    (tmp_path / "c.txt").write_text("a\nMATCH\nb\n")
    out = workspace_tools.grep_impl(str(tmp_path), "MATCH", context=1)
    assert out.split("\n") == ["c.txt-1-a", "c.txt:2:MATCH", "c.txt-3-b"]


def test_grep_impl_no_match_and_bad_regex(tmp_path, monkeypatch):
    _no_rg(monkeypatch)
    (tmp_path / "a.txt").write_text("nothing here\n")
    assert workspace_tools.grep_impl(str(tmp_path), "zzzz") == "(no matches)"
    assert workspace_tools.grep_impl(str(tmp_path), "((").startswith("ERROR: invalid regex")


def test_grep_impl_rejects_escaping_glob(tmp_path, monkeypatch):
    _no_rg(monkeypatch)
    assert workspace_tools.grep_impl(str(tmp_path), "x", path_glob="/etc/*").startswith("ERROR:")
    assert workspace_tools.grep_impl(str(tmp_path), "x", path_glob="../secrets").startswith("ERROR:")


def test_grep_impl_max_results_cap(tmp_path, monkeypatch):
    _no_rg(monkeypatch)
    (tmp_path / "m.txt").write_text("\n".join("hit" for _ in range(50)) + "\n")
    out = workspace_tools.grep_impl(str(tmp_path), "hit", max_results=5)
    matches = [ln for ln in out.split("\n") if ln.startswith("m.txt:")]
    assert len(matches) == 5
    assert "truncated" in out


def test_grep_impl_byte_cap(tmp_path, monkeypatch):
    _no_rg(monkeypatch)
    monkeypatch.setattr(workspace_tools, "GREP_BYTE_CAP", 60)
    (tmp_path / "m.txt").write_text("\n".join(f"needle {i}" for i in range(50)) + "\n")
    out = workspace_tools.grep_impl(str(tmp_path), "needle", max_results=200)
    assert "truncated" in out
    assert len(out.encode("utf-8")) < 200


def test_grep_impl_skips_binary_and_agent_dir(tmp_path, monkeypatch):
    _no_rg(monkeypatch)
    (tmp_path / "bin.dat").write_bytes(b"needle\x00needle")
    (tmp_path / "t.txt").write_text("needle here\n")
    os.makedirs(tmp_path / "_agent")
    (tmp_path / "_agent" / "hidden.txt").write_text("needle secret\n")
    os.makedirs(tmp_path / "src" / "_agent")
    (tmp_path / "src" / "_agent" / "x.txt").write_text("needle\n")
    out = workspace_tools.grep_impl(str(tmp_path), "needle")
    assert "t.txt:1:needle here" in out
    assert "bin.dat" not in out             # null-byte binary skipped
    assert "_agent/hidden.txt" not in out   # the root _agent dir skipped
    # only the workspace-root _agent is bookkeeping (as list_files hides it)
    assert "src/_agent/x.txt:1:needle" in out


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_grep_impl_fallback_never_reads_through_symlinks(tmp_path, monkeypatch, tmp_path_factory):
    """grep runs in the HOST process; a container harness (root in the bind
    mount) can plant `notes.txt -> ~/.ssh/id_rsa`. The fallback must not
    follow a symlinked file or dir out of the workspace, nor block on a FIFO."""
    import threading

    _no_rg(monkeypatch)
    outside = str(tmp_path_factory.mktemp("outside"))
    with open(os.path.join(outside, "id_rsa"), "w") as fh:
        fh.write("header\nTOPSECRET\n")
    ws = tmp_path / "ws"
    ws.mkdir()
    os.symlink(os.path.join(outside, "id_rsa"), ws / "notes.txt")
    os.symlink(outside, ws / "linkdir")
    os.mkfifo(ws / "pipe")
    (ws / "real.txt").write_text("TOPSECRET is fine here\n")
    box: dict = {}
    t = threading.Thread(
        target=lambda: box.setdefault("out", workspace_tools.grep_impl(str(ws), "TOPSECRET")),
        daemon=True)
    t.start()
    t.join(5.0)
    assert not t.is_alive(), "grep blocked on a FIFO"
    assert box["out"] == "real.txt:1:TOPSECRET is fine here"


def test_grep_impl_rg_path_argv_and_exit_codes(tmp_path, monkeypatch):
    import shutil
    import subprocess

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/rg" if name == "rg" else None)

    import json

    captured = {}
    original_popen = subprocess.Popen
    result = {
        "rc": 0,
        "record": {
            "type": "match",
            "data": {"path": {"text": "a.py"}, "line_number": 1, "lines": {"text": "import os\n"}},
        },
    }

    def fake_popen(cmd, **kw):
        captured.update(cmd=cmd, cwd=kw.get("cwd"), stdin=kw.get("stdin"))
        script = (
            "import sys; print(sys.argv[1]) if sys.argv[1] else None; sys.exit(int(sys.argv[2]))"
        )
        body = json.dumps(result["record"]) if result["record"] else ""
        return original_popen([sys.executable, "-c", script, body, str(result["rc"])], **kw)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    out = workspace_tools.grep_impl(str(tmp_path), "import", path_glob="*.py", context=2, max_results=10)
    assert out == "a.py:1:import os"
    cmd = captured["cmd"]
    assert cmd[0] == "rg"
    assert "-C" in cmd and cmd[cmd.index("-C") + 1] == "2"
    assert "-g" in cmd and cmd[cmd.index("-g") + 1] == "*.py"
    assert "-e" in cmd and cmd[cmd.index("-e") + 1] == "import"
    # the pattern rides -e and comes last (no glob/pattern confusion)
    assert cmd[-2:] == ["-e", "import"]
    # the run's root _agent/ bookkeeping is excluded, after the user glob
    assert "!/_agent/" in cmd and cmd.index("!/_agent/") > cmd.index("*.py")
    assert captured["cwd"] == str(tmp_path)
    # Never follow symlinks out of the workspace.
    assert "-L" not in cmd and "--follow" not in cmd
    # With no path argument rg searches a readable stdin instead of cwd — and
    # the host spawns generators with an open stdin pipe, so rg would hang.
    assert captured["stdin"] is subprocess.DEVNULL

    # ripgrep exit 1 == no matches; exit 2+ == a real error
    result.update(rc=1, record=None)
    assert workspace_tools.grep_impl(str(tmp_path), "x") == "(no matches)"
    result.update(rc=2, record=None)
    assert workspace_tools.grep_impl(str(tmp_path), "x").startswith("ERROR:")


# ══════════════════════════════════════════════════════════════════════
# restage_exec_writes — subprocess-write re-staging
# ══════════════════════════════════════════════════════════════════════


class _RestageGen:
    """Minimal ``gen`` stub recording update_file / delete_file over a real
    workspace dir. The restage reads each file itself, once and byte-exact,
    so its ``read_file`` (recorded) is never called."""

    def __init__(self, ws):
        self._ws = ws
        self.updated: list = []
        self.deleted: list = []
        self.reads: list = []

    def workspace_path(self):
        return self._ws

    def read_file(self, path):
        self.reads.append(path)
        with open(os.path.join(self._ws, path), "rb") as fh:
            return fh.read()

    def update_file(self, path, content):
        self.updated.append((path, content))

    def track_file(self, path, *, max_bytes=None):
        from collimate import fs

        fd = fs.open_file(self._ws, path, write=False)
        with os.fdopen(fd, "rb") as stream:
            if max_bytes is not None and os.fstat(stream.fileno()).st_size > max_bytes:
                raise ValueError("file exceeds tracking cap")
            self.updated.append((path, stream.read()))

    def delete_file(self, path):
        self.deleted.append(path)


def test_restage_new_changed_deleted_and_agent_skip(tmp_path, monkeypatch):
    ws = str(tmp_path)
    (tmp_path / "new.txt").write_text("brand new")
    (tmp_path / "changed.txt").write_text("v2")
    os.makedirs(tmp_path / "_agent")
    (tmp_path / "_agent" / "internal.txt").write_text("skip me")
    os.makedirs(tmp_path / "_runs" / "r-0badc0de")
    (tmp_path / "_runs" / "r-0badc0de" / "x.html").write_text("a run's file")
    fake = _RestageGen(ws)
    monkeypatch.setattr(workspace_tools, "gen", fake)

    before = {"changed.txt": "sha_old", "gone.txt": "sha_gone",
              "_runs/r-0badc0de/gone.json": "sha_t"}
    after = {"new.txt": "sha_new", "changed.txt": "sha_new2", "_agent/internal.txt": "sha_a",
             "_runs/r-0badc0de/x.html": "sha_r"}
    notes = workspace_tools.restage_exec_writes(before, after)

    staged = dict(fake.updated)
    assert staged["new.txt"] == b"brand new"         # new file staged
    assert staged["changed.txt"] == b"v2"            # changed file staged
    assert "_agent/internal.txt" not in staged       # _agent/ ignored
    assert not any(p.startswith("_runs/") for p in staged)   # runs' files too
    assert fake.deleted == ["gone.txt"]              # vanished → delete_file
    assert notes == []


def test_restage_keeps_a_file_the_snapshot_could_not_read(tmp_path, monkeypatch):
    # The after-snapshot skips an unreadable file; one still on disk is kept.
    (tmp_path / "kept.txt").write_text("still here")
    fake = _RestageGen(str(tmp_path))
    monkeypatch.setattr(workspace_tools, "gen", fake)
    notes = workspace_tools.restage_exec_writes({"kept.txt": "sha"}, {})
    assert fake.deleted == []
    assert any("kept.txt" in n for n in notes)


def test_restage_oversize_left_with_note(tmp_path, monkeypatch):
    ws = str(tmp_path)
    (tmp_path / "big.bin").write_text("0123456789")   # 10 bytes
    fake = _RestageGen(ws)
    monkeypatch.setattr(workspace_tools, "gen", fake)
    monkeypatch.setattr(workspace_tools, "RESTAGE_FILE_CAP", 5)
    notes = workspace_tools.restage_exec_writes({}, {"big.bin": "sha"})
    assert fake.updated == []                          # too large → not staged
    assert any("big.bin" in n and "unstaged" in n for n in notes)


def test_restage_stages_the_files_own_bytes__F1364(tmp_path, monkeypatch):
    """F1364: a changed file is staged byte-exact — CRLF and a lone CR stay
    as written (a text-mode read-back turned both into LF, and the cell
    committed the LF copy), and a binary is staged as its bytes."""
    files = {
        "win.csv": b"a,b\r\nc,d\r\n",
        "mac.txt": b"one\rtwo\r",
        "blob.bin": bytes(range(256)) * 4,
    }
    for name, data in files.items():
        (tmp_path / name).write_bytes(data)
    fake = _RestageGen(str(tmp_path))
    monkeypatch.setattr(workspace_tools, "gen", fake)
    notes = workspace_tools.restage_exec_writes({}, {name: "sha" for name in files})
    assert notes == []
    assert dict(fake.updated) == files
    assert fake.reads == []          # one read per file: the restage's own


def test_the_restage_tracking_refuses_a_symlink_and_a_fifo__F1364(tmp_path, native_bridge):
    """Tracking safe-opens regular files without copying or following links."""
    (tmp_path / "sub").mkdir()
    path = tmp_path / "sub/f.txt"
    path.write_bytes(b"a\r\nb")
    outside = tmp_path.parent / "secret.txt"
    outside.write_bytes(b"host secret")
    os.symlink(str(outside), str(tmp_path / "link.txt"))
    os.symlink(str(tmp_path / "sub"), str(tmp_path / "dirlink"))
    os.mkfifo(str(tmp_path / "pipe"))
    initial = path.stat()
    workspace_tools.track_restage_file(str(tmp_path), "sub/f.txt")
    assert path.read_bytes() == b"a\r\nb"
    assert path.stat().st_mtime_ns == initial.st_mtime_ns
    for name in ("link.txt", "dirlink/f.txt", "pipe"):
        with pytest.raises(OSError):
            workspace_tools.track_restage_file(str(tmp_path), name)
    assert outside.read_bytes() == b"host secret"


def test_the_restage_tracking_refuses_growth_before_classification__F1364(
    tmp_path, native_bridge, monkeypatch
):
    from types import SimpleNamespace

    cap = 16
    (tmp_path / "grow.log").write_bytes(b"x" * (cap * 4096))
    monkeypatch.setattr(workspace_tools, "RESTAGE_FILE_CAP", cap)
    real_fstat = os.fstat
    calls = []

    def fstat(fd):
        actual = real_fstat(fd)
        calls.append(actual.st_size)
        if len(calls) <= 2:
            return SimpleNamespace(st_mode=actual.st_mode, st_size=8)
        return actual

    monkeypatch.setattr(os, "fstat", fstat)
    with pytest.raises(ValueError):
        workspace_tools.track_restage_file(str(tmp_path), "grow.log")
    assert len(calls) >= 2
    assert native_bridge.tracked_writes == set()
    assert (tmp_path / "grow.log").stat().st_size == cap * 4096


def test_safe_snapshot_tolerates_missing(monkeypatch):
    class _Boom:
        def snapshot(self):
            raise RuntimeError("no snapshot in this stub")

    monkeypatch.setattr(workspace_tools, "gen", _Boom())
    assert workspace_tools.safe_snapshot() is None


# ══════════════════════════════════════════════════════════════════════
# web_fetch — SSRF guard + readability fallback
# ══════════════════════════════════════════════════════════════════════


def _addrinfo(ip):
    import socket

    def _fn(host, port, *a, **k):
        fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
        sockaddr = (ip, port, 0, 0) if fam == socket.AF_INET6 else (ip, port)
        return [(fam, socket.SOCK_STREAM, 6, "", sockaddr)]

    return _fn


def test_vet_url_private_and_public(monkeypatch):
    import socket

    for ip in ("127.0.0.1", "10.0.0.5", "169.254.169.254", "fc00::"):
        monkeypatch.setattr(socket, "getaddrinfo", _addrinfo(ip))
        assert web_tools.vet_url("http://host.test/")[0] is not None, ip
    # a public address resolves clean → allowed
    monkeypatch.setattr(socket, "getaddrinfo", _addrinfo("93.184.216.34"))
    assert web_tools.vet_url("http://example.test/")[0] is None


def test_vet_url_localhost_without_resolution(monkeypatch):
    import socket

    def _boom(*a, **k):
        raise AssertionError("localhost must be blocked before DNS")

    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    assert web_tools.vet_url("http://localhost/x")[0] is not None


def test_web_fetch_rejects_non_http_scheme(monkeypatch):
    # A bare stub httpx keeps the import happy; the scheme guard fires first.
    monkeypatch.setitem(sys.modules, "httpx", types.ModuleType("httpx"))
    assert "scheme" in web_tools.fetch("file:///etc/passwd")
    assert "scheme" in web_tools.fetch("ftp://host/x")


_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
               "http_proxy", "https_proxy", "all_proxy", "no_proxy")


@pytest.fixture
def no_proxy_env(monkeypatch):
    for var in _PROXY_VARS:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def _local_http(routes):
    """A 127.0.0.1 server answering ``routes[path] -> (status, headers, body)``
    (404 otherwise); returns ``(server, hits)`` where ``hits`` lists every
    request line it received."""
    import http.server
    import threading

    hits: list = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.requestline)
            status, headers, body = routes.get(self.path, (404, {}, b""))
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, hits


def test_vet_url_carrier_grade_nat(no_proxy_env):
    """100.64.0.0/10 is neither private nor reserved to ``ipaddress``, but it
    is no public address either — tailnet peers and a cloud metadata service
    live there."""
    for host in ("100.64.0.1", "100.100.100.200", "100.127.255.254", "[::ffff:100.64.0.1]"):
        assert web_tools.vet_url(f"http://{host}/")[0] is not None, host


def test_web_fetch_connects_to_the_address_it_vetted(no_proxy_env, monkeypatch):
    """DNS rebinding: the name answers a public address to the guard and
    127.0.0.1 afterwards. The fetch must connect to the vetted address, never
    re-resolve the name (which would reach a loopback-only service)."""
    import socket

    srv, hits = _local_http({"/": (200, {"Content-Type": "text/plain"}, b"LOOPBACK-SECRET")})
    answers = iter(["93.184.216.34"])

    def rebinding(host, port, *a, **k):
        if host == "rebind.test":
            return _addrinfo(next(answers, "127.0.0.1"))(host, port)
        return real_gai(host, port, *a, **k)

    connects: list = []

    def fake_connect(address, *a, **k):
        connects.append(address[0])
        raise ConnectionRefusedError("no network in unit tests")

    real_gai = socket.getaddrinfo
    monkeypatch.setattr(socket, "getaddrinfo", rebinding)
    monkeypatch.setattr(socket, "create_connection", fake_connect)
    try:
        out = web_tools.fetch(f"http://rebind.test:{srv.server_address[1]}/")
    finally:
        srv.shutdown()
    assert "LOOPBACK-SECRET" not in out and out.startswith("ERROR")
    assert connects == ["93.184.216.34"] and hits == []


def test_web_fetch_rechecks_every_redirect_hop(no_proxy_env, monkeypatch):
    """A public page that 302s to a loopback URL: the second hop is refused
    and the loopback target is never requested."""
    import socket

    srv, hits = _local_http({})
    port = srv.server_address[1]
    secret = f"http://127.0.0.1:{port}/secret"

    real_gai, real_connect = socket.getaddrinfo, socket.create_connection

    def gai(host, p, *a, **k):
        if host == "public.test":
            return _addrinfo("93.184.216.34")(host, p)
        return real_gai(host, p, *a, **k)

    def connect(address, *a, **k):
        # The "public" address is served by the local server (no network).
        if address[0] == "93.184.216.34":
            return real_connect(("127.0.0.1", port), *a, **k)
        return real_connect(address, *a, **k)


    def do_GET(self):
        hits.append(self.path)
        self.send_response(302 if self.path == "/start" else 200)
        self.send_header("Location", secret)
        self.send_header("Content-Length", "6")
        self.end_headers()
        self.wfile.write(b"secret")
    monkeypatch.setattr(srv.RequestHandlerClass, "do_GET", do_GET)
    monkeypatch.setattr(socket, "getaddrinfo", gai)
    monkeypatch.setattr(socket, "create_connection", connect)
    try:
        out = web_tools.fetch(f"http://public.test:{port}/start")
    finally:
        srv.shutdown()
    assert out == "ERROR: refusing to fetch a private/loopback address (127.0.0.1)"
    assert hits == ["/start"]


def test_vet_url_defers_an_unresolvable_name_to_the_proxy(no_proxy_env, monkeypatch):
    """A proxy/none-internet container has a dead resolver and reaches the
    web only through the egress proxy, which resolves names itself: a local
    lookup failure must not refuse a proxied fetch. Without a proxy it still
    does, and a literal private address is refused either way."""
    import socket

    def dead(*a, **k):
        raise socket.gaierror(-3, "Temporary failure in name resolution")
    monkeypatch.setattr(socket, "getaddrinfo", dead)
    assert "could not resolve" in web_tools.vet_url("https://example.com/")[0]
    monkeypatch.setenv("HTTPS_PROXY", "http://host.docker.internal:3128")
    monkeypatch.setenv("NO_PROXY", "localhost,host.docker.internal")
    assert web_tools.vet_url("https://example.com/")[0] is None
    assert web_tools.vet_url("https://10.0.0.5/")[0] is not None
    assert web_tools.vet_url("https://100.64.0.1/")[0] is not None


def test_web_fetch_goes_through_the_proxy_when_the_resolver_is_dead(
        no_proxy_env, monkeypatch):
    """End to end: with only the proxy able to resolve, the fetch reaches the
    proxy (absolute-form request for the original host), not an ERROR."""
    import socket

    proxy, hits = _local_http({})

    def proxy_get(self):
        hits.append(self.requestline)
        body = b"via proxy"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    monkeypatch.setattr(proxy.RequestHandlerClass, "do_GET", proxy_get)
    real_gai = socket.getaddrinfo

    def gai(host, *a, **k):
        if host == "allowed.test":
            raise socket.gaierror(-3, "Temporary failure in name resolution")
        return real_gai(host, *a, **k)
    monkeypatch.setattr(socket, "getaddrinfo", gai)
    monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{proxy.server_address[1]}")
    try:
        out = web_tools.fetch("http://allowed.test/page")
    finally:
        proxy.shutdown()
    assert out == "via proxy"
    assert hits and "http://allowed.test/page" in hits[0]


def test_strip_html_drops_script_style():
    html = "<div><style>a{color:red}</style>Keep <script>evil()</script>this</div>"
    assert web_tools.strip_html(html) == "Keep this"


def test_extract_readable_falls_back_without_trafilatura(monkeypatch):
    # Force `import trafilatura` to fail → the stdlib stripper takes over.
    monkeypatch.setitem(sys.modules, "trafilatura", None)
    html = "<html><script>bad</script><p>Hello World</p></html>"
    assert web_tools.extract_readable(html) == "Hello World"


def test_extract_readable_keeps_the_page_title_and_heading(monkeypatch):
    """trafilatura returns the body without the title or <h1>; the page's
    name must still reach the model, once."""
    fake = types.ModuleType("trafilatura")
    fake.extract = lambda html, **k: "This domain is for use in documentation examples."
    monkeypatch.setitem(sys.modules, "trafilatura", fake)
    html = ("<html><head><title>Example Domain</title></head><body>"
            "<h1>Example Domain</h1><p>This domain is for use in documentation"
            " examples.</p></body></html>")
    assert web_tools.extract_readable(html) == (
        "# Example Domain\nThis domain is for use in documentation examples.")
    # An extraction that already opens with the heading is left alone.
    fake.extract = lambda html, **k: "Example Domain\nbody"
    assert web_tools.extract_readable(html) == "Example Domain\nbody"


# ══════════════════════════════════════════════════════════════════════
# web_search — engine selection
# ══════════════════════════════════════════════════════════════════════


def test_web_search_tavily_uses_selected_account(monkeypatch):
    fake = types.ModuleType("httpx")

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"results": [
                {"title": "T1", "url": "http://a", "content": "snip1"},
                {"title": "T2", "url": "http://b", "content": "snip2"},
            ]}

    def _post(url, json=None, timeout=None):
        assert url == "https://custom.tavily.test/search"
        assert json["api_key"] == "secret-key"
        return _Resp()

    fake.post = _post
    monkeypatch.setitem(sys.modules, "httpx", fake)
    monkeypatch.setenv("TAVILY_API_KEY", "ambient-must-not-be-used")
    monkeypatch.setattr(web_tools, "web_search_account", lambda: {"auth_type": "api_key", "protocol": "tavily", "endpoint": "https://custom.tavily.test", "secrets": {"api_key": "secret-key"}})

    out = web_tools.search("q", max_results=5)
    assert "1. T1 — http://a" in out
    assert "snip1" in out
    assert "2. T2 — http://b" in out


def test_web_search_errors_without_engine(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "ambient-must-not-be-used")
    monkeypatch.setattr(web_tools, "web_search_account", lambda: None)
    monkeypatch.setitem(sys.modules, "ddgs", None)            # the DDGS import fails
    out = web_tools.search("q")
    assert out.startswith("ERROR:") and "Key Manager" in out


# ══════════════════════════════════════════════════════════════════════
# per-backend websearch gating seams
# ══════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════
# tool builders + bridge delivery
# ══════════════════════════════════════════════════════════════════════


def test_workspace_tool_entries_gating_matrix():
    def names(**kw):
        return [r[0] for r in workspace_tools.tool_entries(**kw)]

    # Every ACP harness gets host-mediated file operations even when native
    # tools depend on the selected model or command policy. The annotation rows follow the Annotation
    # surface alone, file tools or not.
    file_tools = ["list_files", "read_file", "grep", "view_image", "write_file", "edit_file"]
    annotation_tools = ["annotate", "list_annotations"]
    assert names(enable_file_tools=True, websearch=True) == \
        [*HANDLER_NAMES, *file_tools, "web_fetch", "web_search"]
    assert names(enable_file_tools=True, websearch=True, annotation_tools=True) == \
        [*HANDLER_NAMES, *file_tools, *annotation_tools, "web_fetch", "web_search"]
    assert names(enable_file_tools=False, websearch=True) == [*HANDLER_NAMES, "web_fetch", "web_search"]
    assert names(enable_file_tools=True, websearch=False) == [*HANDLER_NAMES, *file_tools]
    assert names(enable_file_tools=False, websearch=False) == HANDLER_NAMES
    assert names(enable_file_tools=False, websearch=False, annotation_tools=True) == \
        [*HANDLER_NAMES, *annotation_tools]
    assert [r[4] for r in workspace_tools.tool_entries(
        enable_file_tools=True, websearch=False) if r[0] == "view_image"] == [workspace_tools.view_image]
    # 5-tuple shape, informational gate label; main_loop only for the rows
    # that await a host op (annotate / list_annotations → gen.annotations).
    for row in workspace_tools.tool_entries(enable_file_tools=True, websearch=True,
                                            annotation_tools=True):
        assert len(row) == 5
        assert row[1] in ("handler", "file", "web", "annotation")
        assert row[2] is (row[0] in annotation_tools)


def test_start_bridge_registers_extra_entries(monkeypatch):
    pytest.importorskip("mcp")
    import _agent_shared.mcp_bridge as bridge_mod

    # Register the tools, but don't actually run the server thread (the schema
    # registration + tool_names population happen synchronously beforehand).
    class _NoThread:
        def __init__(self, *a, **k):
            pass

        def start(self):
            pass

    monkeypatch.setattr(bridge_mod.threading, "Thread", _NoThread)
    monkeypatch.setattr(bridge_mod, "_secret_log", lambda *a, **k: None, raising=False)

    async def _go():
        loop = asyncio.get_running_loop()
        entries = workspace_tools.tool_entries(enable_file_tools=True, websearch=True)
        bridge = bridge_mod.start_bridge(
            loop=loop, bind_host="127.0.0.1",
            extra_entries=entries,
        )
        assert bridge is not None
        for name in ("list_files", "read_file", "grep", "view_image",
                     "write_file", "edit_file", "web_fetch", "web_search"):
            assert f"mcp__collimate__{name}" in bridge.tool_names

    asyncio.run(_go())


# ── the shared container base context ────────────────────────────────


def test_container_base_context_stages_shared_source_and_sdk_D23(native_bridge):
    # The build-context helper points at the _env_shared/base sibling — shared
    # by every Docker-family cell and materialised alongside the agent cell at
    # runtime — so a missing base image can be built on first use rather than
    # degrading to a RuntimeError.
    import _env_shared as shared

    ctx = shared.base_context()
    assert ctx is not None
    assert os.path.isfile(os.path.join(ctx, "Dockerfile"))
    assert ctx == os.path.join(native_bridge.workspace, ".collrun", "container-base")
    assert os.path.isfile(os.path.join(ctx, "sdk", "collimate", "testing", "forward.py"))


# ── tool_entries: no exec rows ─────────────────────────────


def test_workspace_tool_entries_carry_no_exec_rows():
    """The registry serves handler, file and web tools: every harness runs
    commands with its own shell."""
    names = [r[0] for r in workspace_tools.tool_entries(
        enable_file_tools=True, websearch=True)]
    assert names == [*HANDLER_NAMES, "list_files", "read_file", "grep", "view_image",
                     "write_file", "edit_file",
                     "web_fetch", "web_search"]
    assert [r[0] for r in workspace_tools.tool_entries(
        enable_file_tools=False, websearch=False)] == HANDLER_NAMES
    rows = workspace_tools.tool_entries(enable_file_tools=True, websearch=True, commands="allow")
    assert not {"run_python", "run_bash"} & {r[0] for r in rows}
    assert not hasattr(workspace_tools, "exec_tools")


# ── the run folder as the file tools see it ────────────────────────────

from _agent_shared import run_files as rf  # noqa: E402

_PEER = "peer-1234abcd"
_PEER_DOC = f"_runs/{_PEER}/chat/main.json"
_PEER_JSON = '{"messages": [{"role": "user", "content": "peer"}]}'


@pytest.fixture
def run_with_peer(tmp_path):
    """A host-free run of this cell (runner ``tester``) beside a sibling run
    that has a chat transcript."""
    from collimate import native
    from collimate.testing import make_test_bridge

    bridge = make_test_bridge(
        tmp_path, runner={"runner": "tester", "name": "Tester"},
        run_id="0badc0de" + "0" * 24,
        ui_files=[".chat", ".json", ".terminal"],
        foreign_runs={_PEER: {"chat/main.json": _PEER_JSON}})
    native.set_bridge(bridge)
    try:
        yield bridge
    finally:
        native._clear_bridge()
        bridge.close()


def _own(bridge, rel):
    return f"{bridge.folder}/{rel}"


def test_list_files_shows_runs_and_hides_agent_bookkeeping(run_with_peer):
    import gen
    gen.update_file("notes.md", "n")
    gen.update_file("_agent/trace.json", "[]")
    out = asyncio.run(workspace_tools.list_files()).splitlines()
    assert "notes.md" in out and _PEER_DOC in out
    assert not any(p.startswith("_agent/") for p in out)


def test_write_file_refuses_another_runs_folder(run_with_peer):
    out = asyncio.run(workspace_tools.write_file(_PEER_DOC, "{}"))
    assert out.startswith("ERROR:") and _PEER in out and "read-only" in out
    assert open(os.path.join(run_with_peer.workspace, _PEER_DOC)).read() == _PEER_JSON
    out = asyncio.run(workspace_tools.edit_file(_PEER_DOC, "peer", "mine"))
    assert out.startswith("ERROR:") and "read-only" in out


def test_run_file_tools_refuse_every_own_folder_write(run_with_peer):
    """The run's own folder holds only its interface files, which the
    session writes: the model's write tools refuse every path there (its
    card included), and task output goes outside _runs/."""
    b = run_with_peer
    for path in [_own(b, "chat/main.json"), _own(b, "notes.md"), _own(b, "other.html")]:
        out = asyncio.run(workspace_tools.write_file(path, "work"))
        assert out.startswith("ERROR:") and "interface folder" in out, out
        with pytest.raises(PermissionError):
            rf.write_file(path, "work")
    assert asyncio.run(workspace_tools.write_file(f"{b.folder}/{b.label}.run", "{}")).startswith("ERROR:")
    assert asyncio.run(workspace_tools.write_file("out/report.md", "r")).startswith("wrote")
