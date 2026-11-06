# SPDX-License-Identifier: MIT
"""The run's identity and its host-placed environments through public ``gen``
calls: ``gen.run_info()`` (read from the bridge: the host's
``COLLIMATE_RUNNER`` label and folder, or a dev run's minted identity),
``gen.run_path()``, ``gen.proxy_url()`` (the one spelling of the host's proxy
route, which ``website_pointer`` uses too), and the environment-plane calls
``_env_shared.DockerEnvironment`` wraps."""
from __future__ import annotations

import json
import re
import socket

import pytest

from collimate import native
from collimate._host.bridge import HostBridge
from collimate.pointers import website_pointer
from collimate.testing import make_test_bridge


@pytest.fixture
def hosted_env(monkeypatch, tmp_path):
    """The environment a host hands a run. Its IPC port is a listening
    socket nobody answers: a hosted bridge must connect (it stops when it
    cannot), and these tests send nothing that awaits an ack."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    monkeypatch.setenv("COLLIMATE_IPC_PORT", str(srv.getsockname()[1]))
    (tmp_path / ".collrun").mkdir(exist_ok=True)
    (tmp_path / ".collrun/file_revisions.json").write_text("{}")
    monkeypatch.setenv("COLLIMATE_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("COLLIMATE_PERMISSIONS", "{}")
    monkeypatch.setenv("COLLIMATE_RUN_ID", "3f9a2c1d5e6f")
    monkeypatch.setenv("COLLIMATE_CHANNEL", "chan")
    monkeypatch.setenv("COLLIMATE_CELL_ID", "cell-3")
    monkeypatch.setenv("COLLIMATE_RUNNER", json.dumps({
        "runner": "demo-runner", "name": "Demo runner",
        "label": "demo-runner-3f9a2c1d", "folder": "_runs/demo-runner-3f9a2c1d"}))
    monkeypatch.setenv("COLLIMATE_UI_FILES", json.dumps([".chat", ".json"]))
    try:
        yield monkeypatch
    finally:
        srv.close()


def _installed(bridge):
    native.set_bridge(bridge)
    return bridge


def test_run_info_reads_the_identity_the_host_set(hosted_env):
    bridge = _installed(HostBridge())
    try:
        assert native.run_info() == native.RunInfo(
            run_id="3f9a2c1d5e6f", channel="chan", cell_id="cell-3",
            runner_id="demo-runner", runner_name="Demo runner",
            label="demo-runner-3f9a2c1d", folder="_runs/demo-runner-3f9a2c1d")
        assert native.run_path("chat/main.json") == "_runs/demo-runner-3f9a2c1d/chat/main.json"
    finally:
        native._clear_bridge()
        bridge.finish(success=True)




@pytest.mark.parametrize("runner", [
    None,
    {"runner": "demo-runner", "name": "Demo runner"},
    {"runner": "demo-runner", "label": "demo-runner-3f9a2c1d"},
    {"runner": "demo-runner", "folder": "_runs/demo-runner-3f9a2c1d"},
])
def test_a_host_run_without_its_label_and_folder_stops_the_bridge(hosted_env, runner):
    if runner is None:
        hosted_env.delenv("COLLIMATE_RUNNER")
    else:
        hosted_env.setenv("COLLIMATE_RUNNER", json.dumps(runner))
    with pytest.raises(RuntimeError, match="label and folder"):
        HostBridge()


def test_a_run_folder_is_named_by_its_label(hosted_env):
    hosted_env.setenv("COLLIMATE_RUNNER", json.dumps({
        "runner": "demo-runner", "label": "demo-runner-3f9a2c1d",
        "folder": "elsewhere/demo-runner-3f9a2c1d"}))
    with pytest.raises(RuntimeError, match="named by its label"):
        HostBridge()


def test_a_host_run_reads_its_ui_file_types(hosted_env):
    bridge = _installed(HostBridge())
    try:
        assert bridge.ui_suffixes == (".chat", ".json")
        assert native.update_ui_file("chat/conversation.chat", "{}") == \
            "_runs/demo-runner-3f9a2c1d/chat/conversation.chat"
        with pytest.raises(ValueError, match="ui_files"):
            native.update_ui_file("preview/site.website", "{}")
    finally:
        native._clear_bridge()
        bridge.finish(success=True)


def test_a_host_run_declaring_nothing_writes_no_ui_file(hosted_env):
    hosted_env.setenv("COLLIMATE_UI_FILES", "[]")
    bridge = _installed(HostBridge())
    try:
        assert bridge.ui_suffixes == ()
        with pytest.raises(ValueError, match="declared: none"):
            native.update_ui_file("chat/conversation.chat", "{}")
    finally:
        native._clear_bridge()
        bridge.finish(success=True)


@pytest.mark.parametrize("value, match", [
    (None, "COLLIMATE_UI_FILES is missing"),
    ("not json", "not JSON"),
    ('{"a": 1}', "list of filename suffixes"),
    ('["chat"]', "list of filename suffixes"),
    ('[""]', "list of filename suffixes"),
])
def test_a_host_run_without_a_valid_ui_file_list_stops_the_bridge(hosted_env, value, match):
    if value is None:
        hosted_env.delenv("COLLIMATE_UI_FILES")
    else:
        hosted_env.setenv("COLLIMATE_UI_FILES", value)
    with pytest.raises(RuntimeError, match=match):
        HostBridge()


def test_a_dev_run_mints_its_identity_once(monkeypatch, tmp_path):
    for name in ("COLLIMATE_IPC_PORT", "COLLIMATE_RUN_ID", "COLLIMATE_CHANNEL",
                 "COLLIMATE_CELL_ID", "COLLIMATE_RUNNER", "COLLIMATE_UI_FILES"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / ".collrun").mkdir(exist_ok=True)
    (tmp_path / ".collrun/file_revisions.json").write_text("{}")
    monkeypatch.setenv("COLLIMATE_WORKSPACE", str(tmp_path))
    bridge = _installed(HostBridge())
    try:
        info = native.run_info()
        assert re.fullmatch(r"[0-9a-f]{32}", info.run_id)
        assert info.label == f"dev-{info.run_id[:8]}"
        assert info.folder == f"_runs/{info.label}"
        assert (info.channel, info.cell_id, info.runner_id) == ("", "", "")
        assert native.run_info() == info
        assert bridge.ui_suffixes is None, "a host-free run accepts every UI type"
    finally:
        native._clear_bridge()
        bridge.finish(success=True)


def test_the_recording_bridge_models_the_identity(tmp_path):
    dev = make_test_bridge(tmp_path)
    assert dev.label == f"dev-{dev.run_id[:8]}" and dev.folder == f"_runs/{dev.label}"
    named = make_test_bridge(tmp_path, runner={"runner": "demo", "name": "Demo"},
                             run_id="0123456789abcdef")
    assert (named.runner_id, named.runner_name) == ("demo", "Demo")
    assert named.label == "demo-01234567" and named.folder == "_runs/demo-01234567"
    placed = make_test_bridge(tmp_path, label="x-deadbeef", channel="chan", cell_id="cell-9")
    assert placed.folder == "_runs/x-deadbeef"
    assert (placed.channel, placed.cell_id) == ("chan", "cell-9")
    for b in (dev, named, placed):
        b.close()


@pytest.mark.parametrize("rel", ["", ".", "..", "../x", "a/../../x"])
def test_run_path_stays_inside_the_folder(native_bridge, rel):
    with pytest.raises(ValueError):
        native.run_path(rel)


def test_run_path_normalizes(native_bridge):
    assert native.run_path("/tabs/./tab-1.webview") == f"{native_bridge.folder}/tabs/tab-1.webview"


def test_proxy_url_is_the_hosts_route_and_the_website_pointer_uses_it(native_bridge):
    native_bridge.run_id, native_bridge.channel = "run-7", "chan"
    assert native.proxy_url(8080) == "/proxy/chan/run-7/8080/"
    assert website_pointer(8080)["url"] == native.proxy_url(8080)
    assert website_pointer(8080)["directUrl"] == "http://127.0.0.1:8080/"


def test_place_environment_returns_ids_and_raises_the_hosts_reason(native_bridge):
    assert native.place_environment({"image": "x"}) == ("env-1", "recorded-container")
    native_bridge.stub_env_place(lambda spec: {"ok": False, "error": "no daemon"})
    with pytest.raises(RuntimeError, match="no daemon"):
        native.place_environment({"image": "x"})


def test_environment_exec_detach_and_failure(native_bridge):
    native_bridge.stub_env_exec(lambda env_id, cmd, kw: (3, "out"))
    assert native.exec_environment("env-1", ["ls"], cwd="/w") == (3, "out")
    assert native.exec_environment("env-1", ["sleep", "9"], detach=True) == (0, "")
    call = native_bridge.env_ops[-1]
    assert call["detach"] is True and call["command"] == ["sleep", "9"]
    native_bridge.stub_env_exec(lambda env_id, cmd, kw: {"ok": False, "error": "gone"})
    assert native.exec_environment("env-1", ["ls"]) == (-1, "gone")


def test_expose_environment_port_and_close(native_bridge):
    assert native.expose_environment_port("env-1", 8080) == 40080
    native.close_environment("env-1")
    assert [o["op"] for o in native_bridge.env_ops] == ["env_expose_port", "env_close"]


# ---------------------------------------------------------------------------
# A hosted run that cannot reach its host retries briefly, then stops (F1192)
# ---------------------------------------------------------------------------


def _closed_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_a_host_run_that_cannot_connect_retries_then_stops__F1192(hosted_env, monkeypatch):
    """F1192: a failed connect used to leave the bridge in dev mode, so a
    hosted run did all its work (a paid agent session) with every host op a
    no-op and nothing committed. It now retries after each configured pause
    and then raises, before any work starts."""
    from collimate._host import bridge as _host
    monkeypatch.setattr(_host, "_IPC_CONNECT_RETRY_DELAYS", (0.0, 0.0))
    hosted_env.setenv("COLLIMATE_IPC_PORT", str(_closed_port()))
    attempts: list = []
    real = socket.create_connection

    def counting(addr, *a, **kw):
        attempts.append(addr)
        return real(addr, *a, **kw)

    monkeypatch.setattr(_host.socket, "create_connection", counting)
    with pytest.raises(RuntimeError, match="cannot reach the host's IPC port .* after 3 attempts"):
        HostBridge()
    assert len(attempts) == 3


def test_a_host_run_whose_first_connect_fails_connects_on_retry__F1192(hosted_env, monkeypatch):
    """A host still binding (or a slow docker DNS answer) is survived: the
    retry connects and the token frame goes out on the live connection."""
    from collimate._host import bridge as _host
    monkeypatch.setattr(_host, "_IPC_CONNECT_RETRY_DELAYS", (0.0, 0.0))
    hosted_env.setenv("COLLIMATE_IPC_TOKEN", "tok-1")
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    hosted_env.setenv("COLLIMATE_IPC_PORT", str(srv.getsockname()[1]))
    real = socket.create_connection
    calls: list = []

    def flaky(addr, *a, **kw):
        calls.append(addr)
        if len(calls) == 1:
            raise ConnectionRefusedError(111, "Connection refused")
        return real(addr, *a, **kw)

    monkeypatch.setattr(_host.socket, "create_connection", flaky)
    try:
        bridge = HostBridge()
        conn, _ = srv.accept()
        try:
            assert bridge.has_host
            assert bridge._socket.gettimeout() is None, "acks are awaited in blocking mode"
            with conn.makefile("r", encoding="utf-8") as rfile:
                first = rfile.readline()
            assert json.loads(first) == {"op": "__auth__", "token": "tok-1"}
        finally:
            bridge.finish(success=True)
            conn.close()
    finally:
        srv.close()
    assert len(calls) == 2


def test_a_failed_attempt_leaves_no_half_open_socket__F1192(hosted_env, monkeypatch):
    """A connect whose token frame cannot be written closes and clears the
    socket and both file objects before the retry (a stale reader left set
    would make ``emit_and_wait`` block on a dead socket)."""
    from collimate._host import bridge as _host
    monkeypatch.setattr(_host, "_IPC_CONNECT_RETRY_DELAYS", (0.0,))
    hosted_env.setenv("COLLIMATE_IPC_TOKEN", "tok-1")
    closed: list = []

    class _Broken:
        def __init__(self, what):
            self.what = what

        def write(self, _text):
            raise BrokenPipeError(32, "Broken pipe")

        def flush(self):
            pass

        def close(self):
            closed.append(self.what)

    class _Sock(_Broken):
        def settimeout(self, _t):
            pass

        def makefile(self, mode, **_kw):
            return _Broken(f"file-{mode}")

    monkeypatch.setattr(_host.socket, "create_connection", lambda *a, **kw: _Sock("socket"))
    with pytest.raises(RuntimeError, match="after 2 attempts"):
        HostBridge()
    assert sorted(closed) == sorted(["file-w", "file-r", "socket"] * 2)


def test_a_malformed_ipc_port_stops_the_bridge__F1192(hosted_env):
    hosted_env.setenv("COLLIMATE_IPC_PORT", "not-a-port")
    with pytest.raises(RuntimeError, match="not a port number"):
        HostBridge()
