# SPDX-License-Identifier: MIT
"""The detach-tolerant PTY (agent-v2 §4.3.5): a ``keep_alive`` terminal
session survives a WebSocket detach — the process keeps running, detached
output drains into a bounded ring, and the next attach replays the ring —
while a plain session keeps the v1 kill-on-detach behavior. Driven through
the real WS route with ``_spawn_pty`` patched to run a local command on a
host PTY (no docker) — synthetic throughout.
"""
from __future__ import annotations

from sessions import terminal as terminal_sessions

import asyncio
import os
import pty as pty_mod
import time

import pytest
import routes.terminal as term
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect


@pytest.fixture(autouse=True)
def _clean_registry():
    terminal_sessions._sessions.clear()
    yield
    for sid in list(terminal_sessions._sessions):
        terminal_sessions.unregister_session(sid)


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(term.router)
    return app


def _patch_local_pty(monkeypatch, argv: list):
    """Replace the docker-exec spawn with a local PTY command."""
    async def spawn(session):
        master_fd, slave_fd = pty_mod.openpty()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                start_new_session=True,
            )
        finally:
            os.close(slave_fd)
        session.master_fd = master_fd
        session.proc = proc

    monkeypatch.setattr(term, "_spawn_pty", spawn)


def _recv_until(ws, needle: bytes, timeout: float = 5.0) -> bytes:
    """What ``ws`` sends until ``needle`` arrives, the timeout passes or the
    endpoint closes the socket; the caller asserts on what arrived."""
    got = b""
    deadline = time.monotonic() + timeout
    while needle not in got and time.monotonic() < deadline:
        try:
            got += ws.receive_bytes()
        except WebSocketDisconnect:
            break
    return got


def test_keep_alive_survives_detach_and_replays_the_ring(monkeypatch):
    _patch_local_pty(monkeypatch, [
        "sh", "-c", "echo first-burst; sleep 0.4; echo while-detached; sleep 30",
    ])
    info = terminal_sessions.register_session(run_id="run-1", cell_id="cell-1", container_id="c", command=["sh"], keep_alive=True)
    client = TestClient(_app())
    url = f"{info['ws_path']}?token={info['token']}"

    with client.websocket_connect(url) as ws:
        assert b"first-burst" in _recv_until(ws, b"first-burst")
    # Detached: the process must still be alive.
    sess = terminal_sessions._sessions[info["terminal_id"]]
    assert sess.proc is not None and sess.proc.returncode is None
    time.sleep(0.8)          # the detached drain collects "while-detached"

    with client.websocket_connect(url) as ws2:
        replay = _recv_until(ws2, b"while-detached")
    assert b"first-burst" in replay      # the ring replayed the whole story
    assert b"while-detached" in replay
    assert sess.proc.returncode is None  # STILL the same live process
    terminal_sessions.unregister_session(info["terminal_id"])


def test_plain_session_still_dies_on_detach(monkeypatch):
    _patch_local_pty(monkeypatch, ["sh", "-c", "echo hi; sleep 30"])
    info = terminal_sessions.register_session(run_id="run-1", cell_id="cell-2", container_id="c", command=["sh"])
    client = TestClient(_app())
    url = f"{info['ws_path']}?token={info['token']}"
    with client.websocket_connect(url) as ws:
        assert b"hi" in _recv_until(ws, b"hi")
    sess = terminal_sessions._sessions[info["terminal_id"]]
    # v1 semantics: the detach killed the PTY.
    assert sess.proc is None and sess.master_fd is None


def test_keep_alive_reports_an_ended_process(monkeypatch):
    _patch_local_pty(monkeypatch, ["sh", "-c", "echo done-now"])
    info = terminal_sessions.register_session(run_id="run-1", cell_id="cell-3", container_id="c", command=["sh"], keep_alive=True)
    client = TestClient(_app())
    url = f"{info['ws_path']}?token={info['token']}"
    with client.websocket_connect(url) as ws:
        # The command exits and the endpoint winds down on its exit, sending
        # this socket the output it has not sent yet before it closes it.
        assert b"done-now" in _recv_until(ws, b"done-now")
        deadline = time.monotonic() + 5
        while not terminal_sessions._sessions[info["terminal_id"]].ended \
                and time.monotonic() < deadline:
            try:
                ws.receive_bytes()
            except Exception:
                break
            time.sleep(0.05)
    sess = terminal_sessions._sessions[info["terminal_id"]]
    assert sess.ended is True
    # A re-attach gets the output replayed from the ring, then is told the
    # process ended instead of a silent respawn.
    with client.websocket_connect(url) as ws2:
        replay = ws2.receive_bytes()
        frame = ws2.receive_json()
    assert b"done-now" in replay
    assert frame["t"] == "error" and "ended" in frame["msg"]


def test_kill_session_deregisters_the_detached_drain_reader():
    """Teardown of a DETACHED keep_alive session must remove the drain
    reader before closing the fd — a closed-but-registered fd leaves a stale
    selector entry, and the next PTY that recycles the fd number gets a
    reader that never fires (a permanently blank terminal)."""
    async def scenario():
        loop = asyncio.get_running_loop()
        r_fd, w_fd = os.pipe()
        sess = terminal_sessions.TerminalSession(
            terminal_id="s-kill", run_id="run-1", cell_id="c", container_id="x",
            command=["sh"], cwd=None, env={}, token="t", keep_alive=True)
        sess.master_fd = r_fd
        term._install_detached_drain(sess, loop)
        assert r_fd in loop._selector.get_map()
        terminal_sessions.kill_session(sess)
        assert r_fd not in loop._selector.get_map()
        os.close(w_fd)

    asyncio.run(scenario())


def test_feed_ring_keeps_only_the_bounded_tail():
    """The detached ring is the reason the drain is safe to leave running —
    it must stay bounded, keeping the NEWEST bytes."""
    sess = terminal_sessions.TerminalSession(
        terminal_id="s-ring", run_id="run-1", cell_id="c", container_id="x",
        command=["sh"], cwd=None, env={}, token="t", keep_alive=True)
    sess.feed_ring(b"old-" * 10)
    sess.feed_ring(b"x" * terminal_sessions.KEEP_ALIVE_RING_CAP)
    assert len(sess.ring) == terminal_sessions.KEEP_ALIVE_RING_CAP
    assert bytes(sess.ring[-4:]) == b"xxxx"
    assert b"old-" not in bytes(sess.ring[:64])


def test_keep_alive_reattach_loses_no_output_from_a_busy_program(monkeypatch):
    """Re-attach against a CONTINUOUSLY writing program: the tick sequence
    seen across detach + ring replay + live frames must be gapless — output
    produced while the ring replay is in flight must reach the client (the
    drain reader is taken off the fd before the snapshot, so those bytes
    wait in the kernel buffer for the live reader)."""
    _patch_local_pty(monkeypatch, [
        "sh", "-c", "i=0; while [ $i -lt 200 ]; do echo tick-$i; "
                    "i=$((i+1)); sleep 0.02; done; sleep 30",
    ])
    info = terminal_sessions.register_session(run_id="run-1", cell_id="cell-gap", container_id="c", command=["sh"], keep_alive=True)
    url = f"{info['ws_path']}?token={info['token']}"

    got = b""
    from starlette.websockets import WebSocketDisconnect
    # ONE client context = one portal event loop across both attaches — the
    # PTY subprocess and its watchers stay on the loop that created them
    # (separate connects would each spin their own loop; the real app runs
    # everything on the single uvicorn loop).
    with TestClient(_app()) as client:
        with client.websocket_connect(url) as ws:
            got += _recv_until(ws, b"tick-5")
        time.sleep(0.8)          # detached: ticks accumulate in the ring
        with client.websocket_connect(url) as ws2:
            deadline = time.monotonic() + 8
            while b"tick-60" not in got and time.monotonic() < deadline:
                try:
                    got += ws2.receive_bytes()
                except WebSocketDisconnect:
                    break

    import re as _re
    ticks = sorted({int(m) for m in _re.findall(rb"tick-(\d+)", got)})
    assert ticks and ticks[0] == 0 and ticks[-1] >= 60
    missing = set(range(ticks[0], ticks[-1] + 1)) - set(ticks)
    assert not missing, f"lost ticks across re-attach: {sorted(missing)[:10]}"
    terminal_sessions.unregister_session(info["terminal_id"])


def test_session_status_reports_a_dead_program(monkeypatch):
    """``gen.terminal_status`` (the ``terminal_status`` IPC op): a registered
    session with a live process is not ended; once the program exits it is,
    with its exit code; an unknown id reads as gone."""
    _patch_local_pty(monkeypatch, ["sh", "-c", "exit 3"])
    reg = terminal_sessions.register_session(run_id="run-1", cell_id="cell-x", container_id="c1", command=["sh"],
                                cwd=None, env={}, keep_alive=True)
    sid = reg["terminal_id"]
    assert terminal_sessions.session_status("nope", run_id="run-1") == {"exists": False, "ended": True, "exit_code": None}
    before = terminal_sessions.session_status(sid, run_id="run-1")
    assert before["exists"] is True and before["ended"] is False
    session = terminal_sessions._sessions[sid]

    async def _spawn_and_wait():
        await term._spawn_pty(session)
        await session.proc.wait()

    asyncio.run(_spawn_and_wait())
    after = terminal_sessions.session_status(sid, run_id="run-1")
    assert after["ended"] is True and after["exit_code"] == 3
    terminal_sessions.unregister_session(sid)
    assert terminal_sessions.session_status(sid, run_id="run-1")["exists"] is False


def test_a_terminal_session_belongs_to_its_run__F0363():
    """Several runs may serve terminals on one cell: status and destroy by
    another run see nothing, and a run's teardown drops only its own PTYs."""
    mine = terminal_sessions.register_session(run_id="run-1", cell_id="cell-t", container_id="c1",
                                 command=["sh"])["terminal_id"]
    theirs = terminal_sessions.register_session(run_id="run-2", cell_id="cell-t", container_id="c1",
                                   command=["sh"])["terminal_id"]
    assert terminal_sessions.session_status(theirs, run_id="run-1")["exists"] is False
    terminal_sessions.unregister_session(theirs, run_id="run-1")
    assert theirs in terminal_sessions._sessions
    terminal_sessions.unregister_sessions_for_run("run-1")
    assert mine not in terminal_sessions._sessions and theirs in terminal_sessions._sessions
    terminal_sessions.unregister_session(theirs)


def test_two_racing_upgrades_spawn_one_pty(monkeypatch):
    """Two upgrades for one session arriving together (two tabs, a remount):
    the attach guard must claim the session before the accept await, so only
    one spawns a PTY and the other is refused with 4409 — otherwise the
    second spawn overwrites the first, whose client and fd then leak."""
    spawns = []

    async def spawn(session):
        spawns.append(session.terminal_id)
        await asyncio.sleep(0)
        raise RuntimeError("stub spawn")

    monkeypatch.setattr(term, "_spawn_pty", spawn)
    info = terminal_sessions.register_session(run_id="run-1", cell_id="cell-race", container_id="c", command=["sh"])

    class FakeWS:
        def __init__(self, gate):
            self.gate = gate
            self.closed_code = "open"

        async def accept(self):
            await self.gate.wait()

        async def send_text(self, _text):
            pass

        async def close(self, code=1000, reason=None):
            self.closed_code = code

    async def scenario():
        gate = asyncio.Event()
        a, b = FakeWS(gate), FakeWS(gate)
        tasks = [
            asyncio.create_task(term.terminal_ws(a, info["terminal_id"], token=info["token"])),
            asyncio.create_task(term.terminal_ws(b, info["terminal_id"], token=info["token"])),
        ]
        await asyncio.sleep(0)
        gate.set()
        await asyncio.gather(*tasks)
        return a, b

    a, b = asyncio.run(scenario())
    assert len(spawns) == 1
    assert b.closed_code == 4409
    # The failed spawn released the claim, so a later attach can retry.
    assert terminal_sessions._sessions[info["terminal_id"]].attached is False


def test_malformed_resize_frames_do_not_kill_the_program(monkeypatch):
    """A control frame that is not a dict, or a resize with non-numeric or
    out-of-range dimensions, is ignored or clamped — it must not end the
    attach (which, for a plain session, kills the user's program)."""
    _patch_local_pty(monkeypatch, ["cat"])
    info = terminal_sessions.register_session(run_id="run-1", cell_id="cell-rsz", container_id="c", command=["sh"])
    url = f"{info['ws_path']}?token={info['token']}"
    with TestClient(_app()) as client:
        with client.websocket_connect(url) as ws:
            ws.send_text("[]")
            ws.send_text('{"t":"resize","rows":"x","cols":80}')
            ws.send_text('{"t":"resize","rows":70000,"cols":-1}')
            ws.send_text('{"t":"resize","rows":Infinity,"cols":NaN}')
            ws.send_bytes(b"ping\n")
            assert b"ping" in _recv_until(ws, b"ping")
            sess = terminal_sessions._sessions[info["terminal_id"]]
            assert sess.proc is not None and sess.proc.returncode is None
    terminal_sessions.unregister_session(info["terminal_id"])


class _StdinWS:
    """A WebSocket stand-in the route reads stdin frames from, counting how
    many it has taken off the socket."""

    def __init__(self):
        from starlette.websockets import WebSocketState
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.taken = 0
        self.client_state = WebSocketState.CONNECTED

    async def accept(self):
        pass

    async def receive(self):
        msg = await self.inbox.get()
        self.taken += 1
        return msg

    async def send_bytes(self, _data):
        pass

    async def send_text(self, _text):
        pass

    async def close(self, code=1000, reason=None):
        from starlette.websockets import WebSocketState
        self.client_state = WebSocketState.DISCONNECTED


async def test_stdin_a_program_is_not_reading_never_blocks_the_loop__F0320_F0387(
        monkeypatch, tmp_path):
    """A paste bigger than the PTY buffer into a program that is not reading
    (busy, or a TUI mid-redraw) must not park the event loop in os.write —
    that stalls every route, stream and terminal of the server. The bytes
    wait in the attach's bounded queue, the socket is left unread while the
    queue is over the cap, and once the program reads it gets every byte in
    order. The program reads nothing until the test opens a gate, so both
    are measured over a window in which it is certainly not reading; a
    blocking write would park the loop until a fallback timer opens the
    gate from another thread."""
    import threading
    import tty

    cap = 65_536
    monkeypatch.setattr(term, "_STDIN_PENDING_CAP", cap)
    payload = bytes(range(256)) * 4096          # 1 MiB, 16x the cap
    frame = 32_768
    out = tmp_path / "stdin.bin"
    gate_path = tmp_path / "gate"
    os.mkfifo(gate_path)
    gate = os.open(gate_path, os.O_RDWR)        # never blocks; nothing to read yet

    async def spawn(session):
        master_fd, slave_fd = pty_mod.openpty()
        tty.setraw(slave_fd)                    # bytes pass through as-is
        try:
            proc = await asyncio.create_subprocess_exec(
                "sh", "-c",
                f"read _ < '{gate_path}'; exec head -c {len(payload)} > '{out}'",
                stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                start_new_session=True,
            )
        finally:
            os.close(slave_fd)
        session.master_fd = master_fd
        session.proc = proc

    monkeypatch.setattr(term, "_spawn_pty", spawn)
    info = terminal_sessions.register_session(run_id="run-1", cell_id="cell-stdin",
                                 container_id="c", command=["sh"])
    loop = asyncio.get_running_loop()
    ws = _StdinWS()
    for i in range(0, len(payload), frame):
        ws.inbox.put_nowait({"type": "websocket.receive", "bytes": payload[i:i + frame]})
    frames = ws.inbox.qsize()

    worst_gap = 0.0
    stop_ticking = asyncio.Event()

    async def ticker():
        nonlocal worst_gap
        while not stop_ticking.is_set():
            before = loop.time()
            await asyncio.sleep(0.02)
            worst_gap = max(worst_gap, loop.time() - before - 0.02)

    fallback = threading.Timer(5.0, os.write, (gate, b"go\n"))
    fallback.start()
    ticking = asyncio.create_task(ticker())
    route = asyncio.create_task(term.terminal_ws(ws, info["terminal_id"], token=info["token"]))
    try:
        await asyncio.sleep(0.5)                # the program is not reading
        gated_gap = worst_gap
        assert ws.taken < frames, "the socket must be left unread over the cap"
        os.write(gate, b"go\n")
        await asyncio.wait_for(route, timeout=15)   # head exits once it has it all
    finally:
        fallback.cancel()
        stop_ticking.set()
        await ticking
        terminal_sessions.unregister_session(info["terminal_id"])
        os.close(gate)

    # A blocking write would park the loop until the fallback opens the
    # gate, 5 s in; half a second leaves room for a loaded host.
    assert gated_gap < 0.5, f"the event loop stalled for {gated_gap:.2f}s"
    assert out.read_bytes() == payload


def test_kill_session_deregisters_the_stdin_writer__F0320():
    """Teardown removes a live attach's stdin writer before closing the fd,
    so a PTY that recycles the fd number never inherits it."""
    async def scenario():
        loop = asyncio.get_running_loop()
        r_fd, w_fd = os.pipe()
        sess = terminal_sessions.TerminalSession(
            terminal_id="s-kill-w", run_id="run-1", cell_id="c", container_id="x",
            command=["sh"], cwd=None, env={}, token="t")
        sess.master_fd = w_fd
        loop.add_writer(w_fd, lambda: None)
        assert w_fd in loop._selector.get_map()
        terminal_sessions.kill_session(sess)
        assert w_fd not in loop._selector.get_map()
        os.close(r_fd)

    asyncio.run(scenario())


def _patch_silent_pty(monkeypatch) -> list:
    """A local PTY program that never reads stdin and ignores SIGTERM (so a
    teardown mid-test cannot end the attach before the test is done with
    it). Returns the list its processes are appended to, for cleanup."""
    import tty

    procs: list = []

    async def spawn(session):
        master_fd, slave_fd = pty_mod.openpty()
        tty.setraw(slave_fd)
        try:
            proc = await asyncio.create_subprocess_exec(
                "sh", "-c", "trap '' TERM; exec sleep 30",
                stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                start_new_session=True,
            )
        finally:
            os.close(slave_fd)
        procs.append(proc)
        session.master_fd = master_fd
        session.proc = proc

    monkeypatch.setattr(term, "_spawn_pty", spawn)
    return procs


def _kill_all(procs) -> None:
    import signal as _signal
    for proc in procs:
        try:
            os.killpg(proc.pid, _signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


async def _wait_for(cond, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not cond():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.01)


async def test_a_keep_alive_detach_with_stdin_pending_drops_the_writer__F0320(monkeypatch):
    """A keep_alive attach that detaches while the program has not read its
    stdin leaves the PTY with only the detached drain's reader: its stdin
    writer goes with it (and so does the stdin), so the loop never keeps
    writing into a PTY nobody is attached to."""
    import selectors

    procs = _patch_silent_pty(monkeypatch)
    info = terminal_sessions.register_session(run_id="run-1", cell_id="cell-ka-stdin",
                                 container_id="c", command=["sh"], keep_alive=True)
    loop = asyncio.get_running_loop()
    ws = _StdinWS()
    route = asyncio.create_task(term.terminal_ws(ws, info["terminal_id"], token=info["token"]))
    try:
        sess = terminal_sessions._sessions[info["terminal_id"]]
        await _wait_for(lambda: sess.master_fd is not None and sess.attached)
        fd = sess.master_fd
        # More than the PTY takes, less than the cap: the writer is on.
        ws.inbox.put_nowait({"type": "websocket.receive", "bytes": b"x" * 262_144})
        await _wait_for(lambda: fd in loop._selector.get_map()
                        and loop._selector.get_map()[fd].events & selectors.EVENT_WRITE)
        ws.inbox.put_nowait({"type": "websocket.disconnect"})
        await asyncio.wait_for(route, timeout=5)
        assert not sess.attached and sess.proc is not None and sess.proc.returncode is None
        assert loop._selector.get_map()[fd].events == selectors.EVENT_READ
    finally:
        terminal_sessions.unregister_session(info["terminal_id"])
        _kill_all(procs)


async def test_stdin_after_a_teardown_never_reaches_a_recycled_fd__F0320(monkeypatch):
    """A teardown (a force stop, a run end) closes the attach's PTY while the
    attach is still open, and the next PTY may get the same fd number. A
    stdin frame that arrives afterwards must not be written there, and the
    attach's own exit must not unhook what the new owner registered on it."""
    procs = _patch_silent_pty(monkeypatch)
    info = terminal_sessions.register_session(run_id="run-1", cell_id="cell-recycle",
                                 container_id="c", command=["sh"])
    loop = asyncio.get_running_loop()
    ws = _StdinWS()
    route = asyncio.create_task(term.terminal_ws(ws, info["terminal_id"], token=info["token"]))
    r_fd = None
    fd = None
    try:
        sess = terminal_sessions._sessions[info["terminal_id"]]
        await _wait_for(lambda: sess.master_fd is not None and sess.attached)
        fd = sess.master_fd
        r_fd, w_fd = os.pipe()
        terminal_sessions.kill_session(sess)                 # the program ignores the TERM
        # The fd number now belongs to someone else, with its own writer.
        os.dup2(w_fd, fd)
        os.close(w_fd)
        os.set_blocking(r_fd, False)
        loop.add_writer(fd, lambda: None)
        ws.inbox.put_nowait({"type": "websocket.receive", "bytes": b"secret\n"})
        await asyncio.wait_for(route, timeout=5)
        with pytest.raises(BlockingIOError):
            os.read(r_fd, 64)
        assert fd in loop._selector.get_map()
    finally:
        if fd is not None:
            try:
                loop.remove_writer(fd)
            except (ValueError, OSError):
                pass
        if r_fd is not None:
            os.close(r_fd)
            os.close(fd)
        terminal_sessions.unregister_session(info["terminal_id"])
        _kill_all(procs)


async def test_a_resize_after_a_teardown_never_reaches_a_recycled_fd__F0320(monkeypatch):
    """A resize frame that arrives after a teardown closed the attach's PTY
    must not resize (or SIGWINCH) whatever now holds the fd number: another
    session's PTY."""
    import fcntl
    import struct
    import termios

    procs = _patch_silent_pty(monkeypatch)
    info = terminal_sessions.register_session(run_id="run-1", cell_id="cell-resize-recycle",
                                 container_id="c", command=["sh"])
    ws = _StdinWS()
    route = asyncio.create_task(term.terminal_ws(ws, info["terminal_id"], token=info["token"]))
    fd = other_master = other_slave = None
    try:
        sess = terminal_sessions._sessions[info["terminal_id"]]
        await _wait_for(lambda: sess.master_fd is not None and sess.attached)
        fd = sess.master_fd
        terminal_sessions.kill_session(sess)                 # the program ignores the TERM
        other_master, other_slave = pty_mod.openpty()
        os.dup2(other_master, fd)                # the fd number is another PTY's now
        ws.inbox.put_nowait({"type": "websocket.receive",
                             "text": '{"t":"resize","rows":50,"cols":132}'})
        ws.inbox.put_nowait({"type": "websocket.disconnect"})
        await asyncio.wait_for(route, timeout=5)
        rows, cols, _x, _y = struct.unpack(
            "HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8))
        assert (rows, cols) != (50, 132)
    finally:
        # The new PTY usually gets the very fd number back, so close each
        # descriptor once.
        if other_master is not None:
            for f in {other_master, other_slave, fd}:
                os.close(f)
        terminal_sessions.unregister_session(info["terminal_id"])
        _kill_all(procs)


async def test_the_resize_reprime_never_signals_after_a_teardown__F0320(monkeypatch):
    """The first resize re-pokes the program with SIGWINCH 150 ms later. A
    teardown in that window (a force stop, a run end) leaves the re-poke
    nothing to signal: once its PTY is gone the program's pid may be reaped
    and reused."""
    import signal as _signal

    procs = _patch_silent_pty(monkeypatch)
    info = terminal_sessions.register_session(run_id="run-1", cell_id="cell-reprime",
                                 container_id="c", command=["sh"])
    ws = _StdinWS()
    route = asyncio.create_task(term.terminal_ws(ws, info["terminal_id"], token=info["token"]))
    winches: list = []
    real_kill = os.kill
    try:
        sess = terminal_sessions._sessions[info["terminal_id"]]
        await _wait_for(lambda: sess.master_fd is not None and sess.attached)
        pid = sess.proc.pid

        def kill(target, sig):
            if target == pid and sig == _signal.SIGWINCH:
                winches.append(sig)
                return
            real_kill(target, sig)
        monkeypatch.setattr(os, "kill", kill)
        ws.inbox.put_nowait({"type": "websocket.receive",
                             "text": '{"t":"resize","rows":50,"cols":132}'})
        await _wait_for(lambda: winches)         # the resize's own poke; the re-poke is armed
        terminal_sessions.kill_session(sess)                 # the program ignores the TERM
        reprime = [t for t in asyncio.all_tasks()
                   if getattr(t.get_coro(), "__name__", "") == "_reprime_winch"]
        assert len(reprime) == 1
        await asyncio.wait_for(reprime[0], timeout=5)
        assert winches == [_signal.SIGWINCH]
        ws.inbox.put_nowait({"type": "websocket.disconnect"})
        await asyncio.wait_for(route, timeout=5)
    finally:
        terminal_sessions.unregister_session(info["terminal_id"])
        _kill_all(procs)


class _RecordingWS(_StdinWS):
    """A socket that keeps every frame the route sends it, in order: a
    binary frame as ("bytes", data), a text frame as ("text", text), and
    its close as ("close", code). Like Starlette's socket, once it is
    closed it refuses any further send, a close included, with Starlette's
    RuntimeError, so nothing the route sends after closing it is kept."""

    def __init__(self):
        super().__init__()
        self.frames: list = []

    def _refuse_once_closed(self) -> None:
        from starlette.websockets import WebSocketState
        if self.client_state == WebSocketState.DISCONNECTED:
            raise RuntimeError('Cannot call "send" once a close message has been sent.')

    async def send_bytes(self, data):
        self._refuse_once_closed()
        self.frames.append(("bytes", bytes(data)))

    async def send_text(self, text):
        self._refuse_once_closed()
        self.frames.append(("text", text))

    async def close(self, code=1000, reason=None):
        self._refuse_once_closed()
        self.frames.append(("close", code))
        await super().close(code, reason)


class _GatedSendWS(_RecordingWS):
    """Controlled socket fake: binary sends record only after an explicit
    gate and scheduling yield. A cancelled held send records nothing;
    closing rejects a later send. This models a pending send for teardown
    tests, not the real transport's buffer/write timing or delivery ACKs.
    ``sends`` counts calls begun, including a held in-flight chunk.
    """

    def __init__(self):
        super().__init__()
        self.gate = asyncio.Event()
        self.sends = 0

    async def send_bytes(self, data):
        self.sends += 1
        await self.gate.wait()
        await asyncio.sleep(0)
        await super().send_bytes(data)


async def test_a_paused_reader_is_never_re_added_on_a_recycled_fd__F0320(monkeypatch):
    """An attach whose PTY reader paused on a full output queue must not
    re-add it once the queue drains if a teardown closed the PTY meanwhile:
    add_reader on the recycled fd number would replace the new owner's
    reader and carry another session's output into this attach."""
    import tty

    procs: list = []

    async def spawn(session):
        master_fd, slave_fd = pty_mod.openpty()
        tty.setraw(slave_fd)
        try:
            # A burst of output (more than the attach's queue holds), then a
            # program that outlives the teardown's SIGTERM.
            proc = await asyncio.create_subprocess_exec(
                "sh", "-c", "trap '' TERM; head -c 20000000 /dev/zero; exec sleep 30",
                stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                start_new_session=True,
            )
        finally:
            os.close(slave_fd)
        procs.append(proc)
        session.master_fd = master_fd
        session.proc = proc

    monkeypatch.setattr(term, "_spawn_pty", spawn)
    info = terminal_sessions.register_session(run_id="run-1", cell_id="cell-pump-recycle",
                                 container_id="c", command=["sh"])
    loop = asyncio.get_running_loop()
    ws = _GatedSendWS()
    route = asyncio.create_task(term.terminal_ws(ws, info["terminal_id"], token=info["token"]))
    fd = r_fd = w_fd = None
    try:
        sess = terminal_sessions._sessions[info["terminal_id"]]
        await _wait_for(lambda: sess.master_fd is not None and sess.attached)
        fd = sess.master_fd
        # Output reached the socket and the reader has paused.
        await _wait_for(lambda: ws.sends >= 1 and fd not in loop._selector.get_map())
        terminal_sessions.kill_session(sess)
        r_fd, w_fd = os.pipe()
        os.dup2(r_fd, fd)                        # the fd number is someone else's now

        def owner():
            pass
        loop.add_reader(fd, owner)
        ws.gate.set()                            # the queue drains
        await asyncio.wait_for(route, timeout=5)
        assert loop._selector.get_map()[fd].data[0]._callback is owner
    finally:
        if fd is not None and r_fd is not None:
            try:
                loop.remove_reader(fd)
            except (ValueError, OSError):
                pass
            for f in {r_fd, w_fd, fd}:
                os.close(f)
        terminal_sessions.unregister_session(info["terminal_id"])
        _kill_all(procs)


def _patch_counted_pty(monkeypatch, *scripts: str) -> list:
    """Spawn the ``sh -c`` scripts in order, one per spawn, on a local PTY,
    recording on the session what ``_spawn_pty`` records. Returns the list
    the spawned processes are appended to, so a test can count the spawns
    and clean them up."""
    procs: list = []

    async def spawn(session):
        master_fd, slave_fd = pty_mod.openpty()
        try:
            proc = await asyncio.create_subprocess_exec(
                "sh", "-c", scripts[len(procs)],
                stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                start_new_session=True,
            )
        finally:
            os.close(slave_fd)
        procs.append(proc)
        session.master_fd = master_fd
        session.proc = proc
        session.spawned = True

    monkeypatch.setattr(term, "_spawn_pty", spawn)
    return procs


def _error_frames(ws: _RecordingWS) -> list:
    import json
    return [frame["msg"] for kind, text in ws.frames if kind == "text"
            for frame in [json.loads(text)] if frame.get("t") == "error"]


def _output(ws: _RecordingWS) -> bytes:
    """The program output the route sent ``ws``: its binary frames, joined."""
    return b"".join(data for kind, data in ws.frames if kind == "bytes")


def _kinds(ws: _RecordingWS) -> list:
    return [kind for kind, _ in ws.frames]


def _assert_output_then_close(ws: _RecordingWS, output: bytes) -> None:
    """``ws`` got exactly ``output``, then the route closed it: the close
    is its last frame and every frame before it is output."""
    assert _output(ws) == output
    assert _kinds(ws) == ["bytes"] * (len(ws.frames) - 1) + ["close"]


async def test_a_keep_alive_program_whose_exit_wins_the_wait_is_recorded_ended__F0320(
        monkeypatch):
    """Raised in the review of Row 15's attach rework (F0320). The attach
    waits on the socket, the output pump and the program's exit at once, and
    the exit can win before the PTY reader has seen the PTY close: here a
    background child of the program keeps the PTY open, so the reader never
    does. The attach still records the program ended, and its socket gets
    the program's output before it closes. A re-attach is not a respawn: it
    gets that output replayed from the ring, then the error that the
    program ended, then the close."""
    procs = _patch_counted_pty(monkeypatch, "sleep 30 & echo last-words; sleep 0.2")
    info = terminal_sessions.register_session(run_id="run-1", cell_id="cell-exit-wins",
                                 container_id="c", command=["sh"], keep_alive=True)
    sid = info["terminal_id"]
    try:
        first = _RecordingWS()
        await asyncio.wait_for(term.terminal_ws(first, sid, token=info["token"]), timeout=5)
        sess = terminal_sessions._sessions[sid]
        assert sess.ended is True
        _assert_output_then_close(first, b"last-words\r\n")
        again = _RecordingWS()
        await asyncio.wait_for(term.terminal_ws(again, sid, token=info["token"]), timeout=5)
        assert len(procs) == 1, "the ended program was spawned again"
        assert _kinds(again) == ["bytes", "text", "close"]
        assert _output(again) == b"last-words\r\n"
        assert _error_frames(again) == ["the terminal's process has ended"]
    finally:
        terminal_sessions.unregister_session(sid)
        _kill_all(procs)


async def test_an_ended_programs_unread_output_reaches_the_socket_and_the_replay__F0320(
        monkeypatch):
    """What the program wrote last can still sit in the PTY, unread, when its
    exit wins the attach's wait. Here the attach's reader never runs at all
    (its registration is a no-op), so everything the program wrote is
    unread, and PTY reads are cut to 16 bytes, so taking it all needs many
    reads. The attach reads all of it and sends it on the socket before it
    closes it, and the session's ring keeps it, so the re-attach's replay
    carries it ahead of the error that the program ended and the close."""
    procs = _patch_counted_pty(
        monkeypatch, "head -c 300 /dev/zero | tr '\\0' x; echo; echo last-words; sleep 0.2")
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_reader", lambda fd, callback, *args: None)
    info = terminal_sessions.register_session(run_id="run-1", cell_id="cell-unread",
                                 container_id="c", command=["sh"], keep_alive=True)
    sid = info["terminal_id"]
    sess = terminal_sessions._sessions[sid]
    real_read = os.read
    monkeypatch.setattr(os, "read", lambda fd, n: real_read(
        fd, min(n, 16) if fd == sess.master_fd else n))
    written = b"x" * 300 + b"\r\nlast-words\r\n"
    try:
        first = _RecordingWS()
        await asyncio.wait_for(term.terminal_ws(first, sid, token=info["token"]), timeout=5)
        assert sess.ended is True
        _assert_output_then_close(first, written)
        again = _RecordingWS()
        await asyncio.wait_for(term.terminal_ws(again, sid, token=info["token"]), timeout=5)
        assert len(procs) == 1
        assert _kinds(again) == ["bytes", "text", "close"]
        assert _output(again) == written
        assert _error_frames(again) == ["the terminal's process has ended"]
    finally:
        terminal_sessions.unregister_session(sid)
        _kill_all(procs)


async def test_an_exited_programs_output_reaches_a_slow_socket_whole__F0320(
        monkeypatch, tmp_path):
    """The program exits while its socket is slow: a chunk of its output is
    being sent, the output queue is full, the reader has paused on it and
    the rest is still in the PTY. The socket still gets every byte, in
    order, before it closes: the output pump is let finish rather than
    cancelled (a send cancelled while the server waits for room in its write
    buffer is never written), the closed PTY does not stop it early, and the
    PTY's tail follows. The slow socket does not hold up the teardown: by
    the time the attach waits on the pump it has closed the PTY and
    released the session. PTY reads are cut to 16 bytes, so output small
    enough for the PTY to hold fills the queue; a background child keeps
    the PTY open, so the program's exit wins the attach's wait and only the
    attach's end records it."""
    gate_path = tmp_path / "exit-gate"
    os.mkfifo(gate_path)
    exit_gate = os.open(gate_path, os.O_RDWR)   # never blocks; nothing to read yet
    procs = _patch_counted_pty(monkeypatch, f"sleep 30 & seq 1 1000; read _ < '{gate_path}'")
    info = terminal_sessions.register_session(run_id="run-1", cell_id="cell-slow-exit",
                                 container_id="c", command=["sh"])
    sid = info["terminal_id"]
    sess = terminal_sessions._sessions[sid]
    real_read = os.read
    monkeypatch.setattr(os, "read", lambda fd, n: real_read(
        fd, min(n, 16) if fd == sess.master_fd else n))
    loop = asyncio.get_running_loop()
    ws = _GatedSendWS()
    route = asyncio.create_task(term.terminal_ws(ws, sid, token=info["token"]))
    try:
        # A send is held and the reader has paused on the full queue.
        await _wait_for(lambda: ws.sends >= 1
                        and sess.master_fd not in loop._selector.get_map())
        os.write(exit_gate, b"go\n")             # the program exits
        await _wait_for(lambda: sess.ended)
        # The send is still held, and the PTY is already torn down.
        assert sess.proc is None and sess.master_fd is None and not sess.attached
        ws.gate.set()
        await asyncio.wait_for(route, timeout=5)
        _assert_output_then_close(ws, b"".join(b"%d\r\n" % i for i in range(1, 1001)))
    finally:
        if not route.done():
            route.cancel()
        terminal_sessions.unregister_session(sid)
        _kill_all(procs)
        os.close(exit_gate)


@pytest.mark.parametrize("keep_alive", [False, True], ids=["plain", "keep_alive"])
@pytest.mark.parametrize("read_size", [4, 65536], ids=["queued", "lone-in-flight"])
async def test_an_exited_programs_output_in_flight_reaches_the_socket_when_its_pty_is_empty__F0320_P0F27(
        monkeypatch, tmp_path, keep_alive, read_size):
    """The common end of a session: the program writes its last output and
    exits, nothing else holds its PTY, and the reader has already taken all
    of that output, so the PTY has nothing left when the exit wins the
    attach's wait. The socket is slow: a chunk of the output is being sent
    and the rest is queued. The attach still lets the output pump send all
    of it before it closes the socket, though the PTY gives it no tail. A
    keep_alive session's re-attach replays the output ahead of the error
    that the program ended and the close. Four-byte reads leave queued
    chunks; a whole-output read leaves only one in-flight send, no queue."""
    gate_path = tmp_path / "exit-gate"
    os.mkfifo(gate_path)
    exit_gate = os.open(gate_path, os.O_RDWR)   # never blocks; nothing to read yet
    procs = _patch_counted_pty(monkeypatch, f"echo last-words; read _ < '{gate_path}'")
    info = terminal_sessions.register_session(run_id="run-1", cell_id="cell-in-flight",
                                 container_id="c", command=["sh"], keep_alive=keep_alive)
    sid = info["terminal_id"]
    sess = terminal_sessions._sessions[sid]
    written = b"last-words\r\n"
    taken: list = []
    real_read = os.read

    def read(fd, n):
        if fd != sess.master_fd:
            return real_read(fd, n)
        chunk = real_read(fd, min(n, read_size))
        taken.append(chunk)
        return chunk

    monkeypatch.setattr(os, "read", read)
    ws = _GatedSendWS()
    route = asyncio.create_task(term.terminal_ws(ws, sid, token=info["token"]))
    try:
        # The first chunk is being sent and the reader has taken the rest:
        # the PTY is empty.
        await _wait_for(lambda: ws.sends >= 1 and b"".join(taken) == written)
        if read_size >= len(written):
            assert taken == [written] and ws.sends == 1
        # Choose the exit-before-EOF-reader ordering explicitly. Otherwise
        # an EOF marker itself can make the queue nonempty and conceal the
        # missing lone-in-flight condition we need to exercise.
        asyncio.get_running_loop().remove_reader(sess.master_fd)
        os.write(exit_gate, b"go\n")             # the program exits
        # With the send held and the socket silent, only the exit can end
        # the attach's wait: the attach records the end and tears down the
        # PTY with the output still in flight.
        await _wait_for(lambda: sess.ended and sess.master_fd is None)
        ws.gate.set()
        await asyncio.wait_for(route, timeout=5)
        _assert_output_then_close(ws, written)
        if keep_alive:
            again = _RecordingWS()
            await asyncio.wait_for(term.terminal_ws(again, sid, token=info["token"]), timeout=5)
            assert len(procs) == 1, "the ended program was spawned again"
            assert _kinds(again) == ["bytes", "text", "close"]
            assert _output(again) == written
            assert _error_frames(again) == ["the terminal's process has ended"]
    finally:
        if not route.done():
            route.cancel()
        terminal_sessions.unregister_session(sid)
        _kill_all(procs)
        os.close(exit_gate)


async def test_a_plain_sessions_respawned_program_is_not_ended__F0320(monkeypatch):
    """A plain session whose program exited reads as ended, also when the
    exit wins the attach's wait: a background child keeps the PTY open
    here, so the reader never sees it close and only the attach's own end
    records it; the attach's socket gets the program's output. Its
    re-attach spawns the program afresh (a plain session always does). The
    new program is live: the session no longer reads as ended, and it
    shields its run from the watchdog again."""
    procs = _patch_counted_pty(monkeypatch, "sleep 30 & echo first; sleep 0.2", "exec sleep 30")
    info = terminal_sessions.register_session(run_id="run-plain", cell_id="cell-respawn",
                                 container_id="c", command=["sh"])
    sid = info["terminal_id"]
    route = None
    ws = _RecordingWS()
    try:
        first = _RecordingWS()
        await asyncio.wait_for(term.terminal_ws(first, sid, token=info["token"]), timeout=5)
        sess = terminal_sessions._sessions[sid]
        assert sess.ended is True
        _assert_output_then_close(first, b"first\r\n")
        assert terminal_sessions.session_status(sid, run_id="run-plain")["ended"] is True
        assert terminal_sessions.has_live_session("run-plain") is False
        route = asyncio.create_task(term.terminal_ws(ws, sid, token=info["token"]))
        await _wait_for(lambda: len(procs) == 2 and sess.proc is procs[1] and sess.attached)
        assert sess.ended is False
        assert terminal_sessions.session_status(sid, run_id="run-plain")["ended"] is False
        assert terminal_sessions.has_live_session("run-plain") is True
        ws.inbox.put_nowait({"type": "websocket.disconnect"})
        await asyncio.wait_for(route, timeout=5)
    finally:
        if route is not None and not route.done():
            route.cancel()
        terminal_sessions.unregister_session(sid)
        _kill_all(procs)


@pytest.mark.parametrize("keep_alive", [False, True])
def test_ended_pty_drain_stops_while_a_descendant_keeps_writing__P0F27_F0320(monkeypatch, keep_alive):
    """An always-readable source cannot keep teardown in an endless loop."""
    info = terminal_sessions.register_session(run_id="run-bound", cell_id="cell-bound",
                                 container_id="c", command=["sh"], keep_alive=keep_alive)
    session = terminal_sessions._sessions[info["terminal_id"]]
    session.master_fd = 123456  # read seam only; never passed to the OS
    calls = 0
    chunk_size = 65536
    bound = (terminal_sessions.KEEP_ALIVE_RING_CAP + chunk_size - 1) // chunk_size

    def always_readable(fd, count):
        nonlocal calls
        assert fd == session.master_fd and count == chunk_size
        calls += 1
        assert calls <= bound, "drain exceeded one ring while the PTY stayed readable"
        return bytes([calls % 256]) * count

    monkeypatch.setattr(term.os, "read", always_readable)
    try:
        tail = term._drain_ended_pty(session)
        assert calls == bound
        assert terminal_sessions.KEEP_ALIVE_RING_CAP <= len(tail) < terminal_sessions.KEEP_ALIVE_RING_CAP + chunk_size
        assert session.ring == (tail[-terminal_sessions.KEEP_ALIVE_RING_CAP:] if keep_alive else b"")
    finally:
        session.master_fd = None
