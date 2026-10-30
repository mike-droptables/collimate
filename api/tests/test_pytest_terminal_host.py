# SPDX-License-Identifier: MIT
"""A terminal on this machine: the host CLI leg of a harness whose terminal
locus is ``host`` (``gen.serve_terminal(None, …)``).

The IPC op is accepted only where the run's grant allows execution here
(docker none or full, commands not deny) and, for a harness-bound run, where
its placement record puts the CLI on this machine, in a directory of the
run's workspace; a refusal is logged with its reason. The route forks the
argv directly on the PTY as its own session with the PTY as its controlling
terminal, in the session's cwd with the session's env (and the run label)
as its whole environment; a resize is TIOCSWINSZ alone; the ring replays and
keep-alive holds as for a container session; destroying the session kills
the child's whole process group. Real PTYs and real processes throughout,
no docker.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import time
from collections import deque

import pytest
import routes.terminal as term
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from process_manager.ipc import IPCMixin
from runner_permissions import RunnerPermissions


def _registry():
    """The session registry the handler registers into, read at call time:
    another test may have reloaded ``sessions.terminal``."""
    import sessions.terminal
    return sessions.terminal


@pytest.fixture(autouse=True)
def _clean_registry():
    for registry in {_registry(), term.session_service}:
        registry._sessions.clear()
    yield
    for registry in {_registry(), term.session_service}:
        for sid in list(registry._sessions):
            registry.unregister_session(sid)


# ── the IPC op: the grant and the placement decide ────────────────────


class _Writer:
    def __init__(self):
        self.lines: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.lines.append(data)

    async def drain(self) -> None:
        return None


class _Mgr(IPCMixin):
    pass


def _manager(tmp_path, *, placement=None, **axes) -> _Mgr:
    m = _Mgr()
    m._ipc_writer = _Writer()
    m.log_buffer = deque()
    m.run_id = "run-host"
    m.cell_id = "cell-1"
    m.workspace_dir = str(tmp_path / "ws")
    os.makedirs(m.workspace_dir, exist_ok=True)
    m.permissions = RunnerPermissions.from_definition({"internet": "full", **axes})
    m.placement = placement
    return m


def _ask(m, **data) -> dict:
    frame = {"op": "create_host_terminal_session", "req_id": "r1",
             "command": ["/bin/sh"], "cwd": m.workspace_dir, "env": {"PATH": "/usr/bin:/bin"}, **data}
    asyncio.run(m._handle_ipc(json.dumps(frame)))
    return json.loads(m._ipc_writer.lines[-1].decode())


def _cli_placement(runtime="host", surface="terminal"):
    return {"harness": "claude-code", "surface": surface, "runtime": runtime, "adopted": False,
            "raised": [], "runner_grant": {}, "effective_grant": {}}


@pytest.mark.parametrize("docker", ["none", "full"])
@pytest.mark.parametrize("commands", ["ask", "allow"])
@pytest.mark.parametrize("placed", [True, False])
def test_a_host_session_is_accepted_under_none_or_full_with_ask_or_allow(tmp_path, docker, commands, placed):
    m = _manager(tmp_path, docker=docker, commands=commands,
                 placement=_cli_placement() if placed else None)
    ack = _ask(m, keep_alive=True)
    assert ack["op"] == "ack_create_host_terminal_session" and ack["ok"] is True, ack
    session = _registry().get_terminal_session(ack["terminal_id"])
    assert session.locus == "host" and session.container_id is None
    assert session.cwd == os.path.realpath(m.workspace_dir) and session.keep_alive
    assert session.env == {"PATH": "/usr/bin:/bin"} and session.run_id == "run-host"
    assert ack["ws_path"] == f"/api/terminals/{ack['terminal_id']}" and ack["token"]


@pytest.mark.parametrize("axes,placement,reason", [
    ({"docker": "limited", "commands": "ask"}, None, "docker: limited"),
    ({"docker": "none", "commands": "deny"}, None, "commands: deny"),
    ({"docker": "full", "commands": "allow"}, _cli_placement(runtime="docker"), "in its container"),
    ({"docker": "none", "commands": "ask"}, _cli_placement(surface="annotation", runtime="host"),
     "surface is 'annotation'"),
])
def test_a_host_session_is_refused_and_the_refusal_logged(tmp_path, axes, placement, reason):
    m = _manager(tmp_path, placement=placement, **axes)
    ack = _ask(m)
    assert ack["ok"] is False and ack["reason"] == "host_refused" and reason in ack["error"]
    assert _registry()._sessions == {}
    logged = [e["payload"] for e in m.log_buffer if e["type"] == "ipc_error"]
    assert any("run run-host: create_host_terminal_session refused" in line and reason in line
               for line in logged), logged


def test_a_host_session_outside_the_runs_workspace_is_refused(tmp_path):
    m = _manager(tmp_path, docker="none", commands="ask")
    ack = _ask(m, cwd=str(tmp_path))
    assert ack["ok"] is False and "not a directory of the run's workspace" in ack["error"]
    os.symlink(str(tmp_path), os.path.join(m.workspace_dir, "escape"))
    ack = _ask(m, cwd=os.path.join(m.workspace_dir, "escape"))
    assert ack["ok"] is False and "not a directory of the run's workspace" in ack["error"]


@pytest.mark.parametrize("bad", [
    {"command": []}, {"command": ["sh", 3]}, {"env": {"bad name": "x"}},
    {"env": {"COLLIMATE_HOST_TERMINAL": "forged"}}, {"env": {"X": 1}},
])
def test_a_malformed_host_session_is_an_error_not_a_session(tmp_path, bad):
    m = _manager(tmp_path, docker="none", commands="ask")
    ack = _ask(m, **bad)
    assert ack["ok"] is False and "reason" not in ack
    assert _registry()._sessions == {}


def test_the_registry_keeps_the_two_loci_apart(tmp_path):
    with pytest.raises(ValueError, match="runs in no container"):
        term.session_service.register_session("r", "c", "cid", ["sh"], cwd=str(tmp_path), locus="host")
    with pytest.raises(ValueError, match="existing absolute directory"):
        term.session_service.register_session("r", "c", None, ["sh"], cwd="relative", locus="host")
    with pytest.raises(ValueError, match="container_id is required"):
        term.session_service.register_session("r", "c", None, ["sh"])
    with pytest.raises(ValueError, match="locus"):
        term.session_service.register_session("r", "c", "cid", ["sh"], locus="elsewhere")


# ── the route: a real PTY on this machine ─────────────────────────────


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(term.router)
    return app


def _host(tmp_path, script: str, *, env=None, keep_alive=False) -> dict:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return term.session_service.register_session(
        run_id="run-host", cell_id="cell-1", container_id=None,
        command=["sh", "-c", script], cwd=str(ws),
        env={"PATH": "/usr/bin:/bin", **(env or {})}, keep_alive=keep_alive, locus="host")


def _recv_until(ws, needle: bytes, timeout: float = 8.0) -> bytes:
    got = b""
    deadline = time.monotonic() + timeout
    while needle not in got and time.monotonic() < deadline:
        try:
            got += ws.receive_bytes()
        except WebSocketDisconnect:
            break
    return got


def test_the_argv_runs_on_the_pty_in_its_cwd_with_its_env_and_a_controlling_terminal(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_SERVER_KEY", "never-inherited")
    info = _host(tmp_path, 'pwd; echo "V=$MYVAR"; echo "S=${SECRET_SERVER_KEY:-unset}"; '
                           'echo "L=$COLLIMATE_HOST_TERMINAL"; exec 3</dev/tty && echo CTTY-OK; '
                           'ps -o sid=,pgid= -p $$; echo DONE; sleep 30', env={"MYVAR": "leg-value"})
    client = TestClient(_app())
    with client.websocket_connect(f"{info['ws_path']}?token={info['token']}") as ws:
        out = _recv_until(ws, b"DONE").decode()
    assert os.path.realpath(str(tmp_path / "ws")) in out
    assert "V=leg-value" in out and "S=unset" in out and "CTTY-OK" in out
    assert re.search(r"L=[0-9a-f]{12}:\d+:run-host", out)
    sid, pgid = re.search(r"(\d+)\s+(\d+)\s*\r?\n\s*DONE", out).groups()
    assert sid == pgid  # its own session and group


def test_a_resize_is_tiocswinsz_alone_and_reaches_the_program(tmp_path, monkeypatch):
    poked = []
    real_kill = os.kill
    monkeypatch.setattr(term.os, "kill", lambda pid, sig: (poked.append(sig), real_kill(pid, sig))[1])
    info = _host(tmp_path, "trap 'echo SIZE=$(stty size)' WINCH; echo READY; "
                           "while true; do sleep 0.1; done")
    client = TestClient(_app())
    with client.websocket_connect(f"{info['ws_path']}?token={info['token']}") as ws:
        assert b"READY" in _recv_until(ws, b"READY")
        ws.send_text(json.dumps({"t": "resize", "rows": 33, "cols": 111}))
        assert b"SIZE=33 111" in _recv_until(ws, b"SIZE=33 111")
    assert signal.SIGWINCH not in poked


def test_a_keep_alive_host_session_survives_detach_and_replays_its_ring(tmp_path):
    info = _host(tmp_path, "echo first-burst; sleep 0.4; echo while-detached; sleep 30", keep_alive=True)
    client = TestClient(_app())
    url = f"{info['ws_path']}?token={info['token']}"
    with client.websocket_connect(url) as ws:
        assert b"first-burst" in _recv_until(ws, b"first-burst")
    session = term.session_service._sessions[info["terminal_id"]]
    assert session.proc is not None and session.proc.returncode is None
    time.sleep(0.8)
    with client.websocket_connect(url) as ws2:
        replay = _recv_until(ws2, b"while-detached")
    assert b"first-burst" in replay and b"while-detached" in replay
    assert session.proc.returncode is None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().split()[2] != "Z"
    except OSError:
        return False


def _wait_gone(pid: int, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


def _child_of(client, info) -> int:
    with client.websocket_connect(f"{info['ws_path']}?token={info['token']}") as ws:
        out = b""
        while not re.search(rb"CHILD=\d+\r?\n", out):
            out += ws.receive_bytes()
    return int(re.search(rb"CHILD=(\d+)", out).group(1))


def _destroy(info) -> None:
    async def destroy():
        term.session_service.unregister_session(info["terminal_id"], run_id="run-host")
        await asyncio.sleep(6)              # the SIGKILL escalation's delay
    asyncio.run(destroy())


_ORPHAN = "nohup sleep 300 >/dev/null 2>&1 & echo CHILD=$!; "


def test_destroying_a_host_session_kills_its_process_group(tmp_path):
    """The CLI's own children die with the session."""
    info = _host(tmp_path, _ORPHAN + "sleep 300", keep_alive=True)
    child = _child_of(TestClient(_app()), info)
    assert _alive(child)
    _destroy(info)
    assert _wait_gone(child), f"pid {child} survived its session"


def test_a_group_that_outlives_its_leader_dies_with_the_session(tmp_path):
    """A detached CLI that exits leaves a child that ignores the hangup; the
    session's destroy kills that group by its number, the leader gone."""
    info = _host(tmp_path, _ORPHAN + "sleep 1; exit 0", keep_alive=True)
    child = _child_of(TestClient(_app()), info)
    session = term.session_service._sessions[info["terminal_id"]]
    assert _wait_gone(session.proc.pid) and _alive(child)
    _destroy(info)
    assert _wait_gone(child), f"pid {child} survived its session"


def test_a_program_that_exits_while_attached_takes_its_group(tmp_path):
    """The attach that sees its program exit ends the session's group with
    it: nothing the CLI started outlives the leg."""
    info = _host(tmp_path, _ORPHAN + "sleep 1; exit 0", keep_alive=True)
    client = TestClient(_app())
    with client.websocket_connect(f"{info['ws_path']}?token={info['token']}") as ws:
        out = b""
        while not re.search(rb"CHILD=\d+\r?\n", out):
            out += ws.receive_bytes()
        child = int(re.search(rb"CHILD=(\d+)", out).group(1))
        assert _alive(child)
        _recv_until(ws, b"never-printed", timeout=6)     # until the program exits
    assert term.session_service._sessions[info["terminal_id"]].ended
    assert _wait_gone(child), f"pid {child} survived its program"


def test_a_missing_program_says_so_on_this_machine(tmp_path):
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    info = term.session_service.register_session(
        run_id="run-host", cell_id="cell-1", container_id=None, command=["no-such-cli-xyz"],
        cwd=str(ws_dir), env={"PATH": "/usr/bin:/bin"}, locus="host")
    client = TestClient(_app())
    with client.websocket_connect(f"{info['ws_path']}?token={info['token']}") as ws:
        msg = json.loads(ws.receive_text())
    assert msg == {"t": "error", "msg": "`no-such-cli-xyz` not found on this machine"}
