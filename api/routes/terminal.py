"""Live interactive terminal bridge.

Exposes a generic WebSocket endpoint that bridges a browser-side xterm.js
instance to a PTY running a session's command: `docker exec -ti <container>
<command>` for a container session, the command itself for a host session.
A generator attaches a command in one of its own environments' containers to
the `.terminal` renderer by calling `gen.serve_terminal(container_id,
command, pointer=...)` (or the lower-level `gen.start_terminal(...)`), or one on
this machine with `container_id=None`, which posts a session into
sessions.terminal via the IPC ops handled in `process_manager/ipc.py` (which
refuses a container the run does not own, and a host session the run's grant
and placement do not allow). The `.terminal` file the generator writes
carries the `{terminal_id, ws_path, token}` pointer the renderer opens.

Design:
  - One PTY per session. Host-allocated via `pty.openpty()`, the child
    wired to the slave fd as its stdio. A container session's child is
    `docker exec -ti`, which gives us a real TTY inside the container
    without needing aiodocker or docker-py socket plumbing. A host session's
    child is the command itself, in its own session with the slave as its
    controlling terminal (setsid, then TIOCSCTTY, then the exec), in the
    session's cwd with the session's env as its whole environment plus the
    run label (`sessions.terminal.HOST_TERMINAL_LABEL`).
  - WebSocket protocol:
      * client → server: binary frames carry stdin bytes. The PTY master is
        non-blocking: stdin the program has not read yet waits in a
        per-attach queue drained by a loop writer, and the socket is not
        read while that queue is at ``_STDIN_PENDING_CAP`` or over, so a
        paste into a busy program never blocks the event loop. The queue
        holds at most the cap plus one frame. The server's WebSocket layer
        (uvicorn's sans-I/O websockets protocol, which its defaults pick,
        main.py) takes frames of up to 16 MiB, queues the frames that one
        read from the connection completes (one large frame, or many small
        ones), then stops reading the connection until the route has taken
        them all; the rest backs up to the browser.
        A full input queue pauses reads for at most 20 seconds, then
        detaches with 1013 (try again later). The renderer reconnects
        quietly; pending input belongs to the old attach and is never
        replayed. A keep_alive PTY continues running between attaches.
      * client → server: text frames are control JSON, e.g.
        {"t":"resize","cols":N,"rows":M}.
      * server → client: binary frames carry stdout+stderr bytes (the
        PTY merges them). A keep_alive attach starts with a replay of the
        session's ring (the output it kept, when there is any), also when
        the program has already ended, ahead of the error frame. When the
        program has exited by the time an attach ends, and no teardown
        took its PTY first, the socket gets the rest of its output before
        the server closes it: what was still queued for the socket, then
        what the PTY still held.
      * server → client: text frames are control JSON —
        {"t":"error","msg":...} before the server closes the socket
        (process already ended, docker missing, spawn/add_reader failure).
  - Auth: a 192-bit random token (secrets.token_urlsafe(24)) is generated
    at session creation, stored in the registry, and required on the WS
    query string. It stays valid for the session's life so a reconnect can
    re-attach, but sessions are single-attach: the `attached` flag lets only
    one socket be live at a time (4409 otherwise).
"""

from __future__ import annotations

import asyncio
import errno
import json
import os

import secrets
import signal
import struct
import fcntl
import pty
import termios
from collections import deque
from typing import Optional

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from terminal_dimensions import clamp_dim
from websocket_errors import reject_websocket
from sessions import terminal as session_service

router = APIRouter()


# How much of its PTY output a keep_alive session keeps (the newest bytes)
# for the replay on re-attach. Bounded — a chatty program can't grow host
# memory while nobody is watching; xterm re-renders the tail.

# How much stdin an attach holds for a program that is not reading it. At
# this or over, the bridge stops reading the WebSocket until the program
# catches up, so the queue never passes the cap by more than one frame.
_STDIN_PENDING_CAP = 1_048_576
# Close before the app launcher's 40-second transport pong deadline, so
# backpressure is a recoverable detach rather than a 1011 keepalive error.
_STDIN_PAUSE_SECONDS = 20.0


# ---------------------------------------------------------------------------
# PTY helpers
# ---------------------------------------------------------------------------

def _set_winsize(fd: int, rows: int, cols: int) -> None:
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except (OSError, struct.error):
        pass


def _exec_argv_and_env(session: session_service.TerminalSession) -> tuple[list[str], dict]:
    """The `docker exec` argv and the environment to run the client in.

    The session env carries provider API keys, and an argv is world-readable
    (/proc/<pid>/cmdline, ps) for as long as the terminal lives. So values go
    in the client's own environment (/proc/<pid>/environ is owner-only) and
    the argv names them with a bare `-e NAME`, which docker copies from
    there (``docker_runner.env_argv``, shared with the docker:limited launch).
    """
    from process_manager.docker_runner import env_argv
    argv = ["docker", "exec", "-i", "-t"]
    if session.cwd:
        argv += ["-w", session.cwd]
    flags, child_env = env_argv(session.env)
    argv += [*flags, "--", session.container_id, *session.command]
    return argv, child_env


def host_terminal_label(run_id: str) -> str:
    """The run label a host session's child carries: this server instance,
    this server process and the run (``startup_cleanup`` reads it back)."""
    from instance import instance_id
    return f"{instance_id()}:{os.getpid()}:{run_id}"


def _host_argv_and_env(session: session_service.TerminalSession) -> tuple[list[str], dict]:
    """A host session's argv and its whole environment: the session's own
    (nothing of this server's), plus the run label."""
    env = dict(session.env)
    env[session_service.HOST_TERMINAL_LABEL] = host_terminal_label(session.run_id)
    return list(session.command), env


def _take_controlling_terminal() -> None:
    """In the forked child, after setsid: make the PTY slave (its stdin) the
    controlling terminal, so the kernel delivers SIGWINCH on a resize and
    the program can open /dev/tty."""
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


async def _spawn_pty(session: session_service.TerminalSession) -> None:
    """Fork off the session's child wired to a fresh host PTY: `docker exec
    -ti <container> <command>` for a container session; for a host session
    the command itself, its own session leader with the PTY as its
    controlling terminal, in its cwd and env. Stores master_fd + proc on the
    session for the pump loops.
    """
    master_fd, slave_fd = pty.openpty()
    _set_winsize(master_fd, 24, 80)

    host = session.locus == "host"
    argv, child_env = _host_argv_and_env(session) if host else _exec_argv_and_env(session)

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            env=child_env,
            cwd=session.cwd if host else None,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            start_new_session=True,
            preexec_fn=_take_controlling_terminal if host else None,
        )
    finally:
        try:
            os.close(slave_fd)
        except OSError:
            pass

    session.master_fd = master_fd
    session.proc = proc
    session.spawned = True


def _install_detached_drain(session: session_service.TerminalSession, loop) -> None:
    """While a keep_alive session sits detached, keep reading the PTY into
    the bounded ring — a full kernel buffer would otherwise BLOCK the
    program mid-write. Removed by the next attach (which installs its own
    reader) or by teardown."""
    fd = session.master_fd
    if fd is None:
        return

    def _drain() -> None:
        try:
            chunk = os.read(fd, 65536)
        except OSError as exc:
            if exc.errno in (errno.EIO, errno.EBADF):
                try:
                    loop.remove_reader(fd)
                except (ValueError, OSError):
                    pass
                session.ended = True
            return
        if not chunk:
            try:
                loop.remove_reader(fd)
            except (ValueError, OSError):
                pass
            session.ended = True
            return
        session.feed_ring(chunk)

    try:
        loop.add_reader(fd, _drain)
    except Exception:
        pass


def _drain_ended_pty(session: session_service.TerminalSession) -> bytes:
    """Read and return what an ended program left unread in its PTY: its
    exit can win the attach's wait before the reader has taken its last
    output. A keep_alive session's ring gets it too, so a re-attach replays
    it. Reads to the end of what is buffered (EIO once nothing holds the
    PTY's other end, EAGAIN while something still does: the master fd is
    non-blocking by then), and stops after about one ring's worth when
    whatever still holds the PTY keeps writing."""
    fd = session.master_fd
    if fd is None:
        return b""
    tail = bytearray()
    while len(tail) < session_service.KEEP_ALIVE_RING_CAP:
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            break
        if not chunk:
            break
        tail += chunk
    out = bytes(tail)
    if session.keep_alive:
        session.feed_ring(out)
    return out


# ---------------------------------------------------------------------------
# WebSocket route
# ---------------------------------------------------------------------------

@router.websocket("/api/terminals/{terminal_id}")
async def terminal_ws(
    websocket: WebSocket,
    terminal_id: str,
    token: str = Query(default=""),
):
    session = session_service.get_terminal_session(terminal_id)
    if session is None:
        await reject_websocket(websocket, 4404, "no such session")
        return
    if not token or not secrets.compare_digest(token, session.token):
        await reject_websocket(websocket, 4401, "bad token")
        return
    if session.attached:
        await reject_websocket(websocket, 4409, "session already attached")
        return
    # Claim the session before the first await: a second upgrade racing
    # this one must see it taken, or both would spawn a PTY.
    session.attached = True

    try:
        await websocket.accept()
    except BaseException:
        session.attached = False
        raise

    # keep_alive: the program may already have ENDED (while detached, or
    # during an earlier attach). Replay what the ring kept of its output,
    # its last output included, then tell the renderer it ended instead of
    # respawning a fresh process under the same id.
    if session.keep_alive and session.ended:
        session.attached = False
        try:
            if session.ring:
                await websocket.send_bytes(bytes(session.ring))
            await websocket.send_text(json.dumps({
                "t": "error", "msg": "the terminal's process has ended",
            }))
            await websocket.close()
        except Exception:
            pass
        return

    # Spawn once: a keep_alive re-attach reuses the LIVE PTY (that is the
    # whole point); a plain session always spawns fresh (its detach killed
    # the previous process).
    if session.proc is None or session.master_fd is None:
        try:
            await _spawn_pty(session)
        except FileNotFoundError:
            session.attached = False
            missing = (f"`{session.command[0]}` not found on this machine" if session.locus == "host"
                       else "`docker` not found on PATH on the host")
            try:
                await websocket.send_text(json.dumps({
                    "t": "error",
                    "msg": missing,
                }))
                await websocket.close()
            except Exception:
                pass
            return
        except Exception as exc:
            session.attached = False
            try:
                await websocket.send_text(json.dumps({"t": "error", "msg": str(exc)}))
                await websocket.close()
            except Exception:
                pass
            return
        # A fresh program: a plain session's earlier one may have ended.
        session.ended = False

    assert session.master_fd is not None
    assert session.proc is not None
    master_fd = session.master_fd
    proc = session.proc
    # Non-blocking, so a stdin write the program is not reading can never
    # park the event loop (see the stdin queue below).
    os.set_blocking(master_fd, False)

    loop = asyncio.get_running_loop()

    # Bounded queue bridges the sync fd reader to the async WS sender.
    # When the client stalls we pause the reader rather than letting the
    # queue grow without limit — otherwise a slow browser could OOM the
    # host on a chatty program.
    ws_send_queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=256)
    reader_paused = False

    # keep_alive re-attach: replay the ring before live frames so the
    # renderer's xterm shows the program's output so far, what happened
    # while away included. The detached-drain reader is removed FIRST —
    # output produced across the send await then stays in the kernel PTY
    # buffer for the live reader below, instead of draining into the ring
    # AFTER the snapshot (which this attach would never see).
    if session.keep_alive:
        try:
            loop.remove_reader(master_fd)
        except (ValueError, OSError):
            pass
    if session.keep_alive and session.ring:
        try:
            await websocket.send_bytes(bytes(session.ring))
        except Exception:
            session.attached = False
            _install_detached_drain(session, loop)
            try:
                await websocket.close()
            except Exception:
                pass
            return

    def _stop_reader() -> None:
        try:
            loop.remove_reader(master_fd)
        except (ValueError, OSError):
            pass

    def _on_readable() -> None:
        nonlocal reader_paused
        if ws_send_queue.qsize() >= ws_send_queue.maxsize - 1:
            _stop_reader()
            reader_paused = True
            return
        try:
            chunk = os.read(master_fd, 65536)
        except OSError as exc:
            if exc.errno in (errno.EIO, errno.EBADF):
                _stop_reader()
                session.ended = True
                ws_send_queue.put_nowait(None)
            return
        if not chunk:
            _stop_reader()
            session.ended = True
            ws_send_queue.put_nowait(None)
            return
        if session.keep_alive:
            session.feed_ring(chunk)
        ws_send_queue.put_nowait(chunk)

    try:
        loop.add_reader(master_fd, _on_readable)
    except Exception as exc:
        await websocket.send_text(json.dumps({"t": "error", "msg": f"add_reader failed: {exc}"}))
        await websocket.close()
        session_service.kill_session(session)
        return

    def _owns_pty() -> bool:
        # False once a teardown (session_service.kill_session) closed this attach's PTY:
        # the fd number may since belong to a newer session's PTY, so
        # nothing may read, write, resize or re-register it any more.
        return session.proc is proc and session.master_fd == master_fd

    async def _pump_to_ws() -> None:
        nonlocal reader_paused
        while True:
            item = await ws_send_queue.get()
            if item is None:
                return
            try:
                await websocket.send_bytes(item)
            except Exception:
                return
            if reader_paused and ws_send_queue.qsize() < ws_send_queue.maxsize // 2:
                if not _owns_pty():
                    return
                reader_paused = False
                try:
                    loop.add_reader(master_fd, _on_readable)
                except Exception:
                    return

    # A host session's program has the PTY as its controlling terminal:
    # TIOCSWINSZ on the master is the whole resize, the kernel signals its
    # foreground group. The docker CLI runs in its own session with no
    # controlling terminal, so the kernel never delivers SIGWINCH to it when
    # we resize the master pty. We poke it by hand on every resize: docker re-reads its stdout winsize on
    # SIGWINCH and resizes the container pty to match, so TUIs (lazygit, the
    # Claude CLI) actually reflow instead of staying frozen at the 24×80 the
    # CLI read once at startup. The CLI installs that handler a beat after it
    # starts, so the very first resize frame (the renderer's onopen fit) can
    # arrive before the handler exists and be dropped — and the renderer dedups
    # identical dimensions, so it never re-sends, leaving the program stuck
    # small. Re-poke once ~150ms after the first resize to guarantee delivery.
    winch_primed = False
    reprime_task: Optional[asyncio.Task] = None

    poke = session.locus == "container"

    def _poke_winch() -> None:
        if not poke or not _owns_pty():
            return  # torn down: the pid may be reaped and reused
        try:
            os.kill(proc.pid, signal.SIGWINCH)
        except (ProcessLookupError, OSError):
            pass

    async def _reprime_winch() -> None:
        await asyncio.sleep(0.15)
        _poke_winch()

    # --- stdin: WS → PTY, never blocking the loop ---
    # Bytes the PTY has not taken yet, in order; a loop writer drains them
    # as the program reads. A write error (the PTY is gone) ends the pump.
    stdin_queue: deque[memoryview] = deque()
    stdin_pending = 0
    stdin_failed = False
    stdin_paused_out = False
    writer_on = False
    stdin_room = asyncio.Event()

    def _flush_stdin() -> None:
        nonlocal stdin_pending, stdin_failed, writer_on
        while stdin_queue:
            head = stdin_queue[0]
            try:
                n = os.write(master_fd, head)
            except BlockingIOError:
                break
            except OSError:
                stdin_failed = True
                stdin_queue.clear()
                stdin_pending = 0
                break
            stdin_pending -= n
            if n == len(head):
                stdin_queue.popleft()
            else:
                stdin_queue[0] = head[n:]
        want = bool(stdin_queue)
        if want != writer_on:
            try:
                if want:
                    loop.add_writer(master_fd, _flush_stdin)
                else:
                    loop.remove_writer(master_fd)
                writer_on = want
            except (ValueError, OSError):
                stdin_failed = True
        if stdin_failed or stdin_pending < _STDIN_PENDING_CAP:
            stdin_room.set()

    # --- writer: WS → PTY ---
    async def _pump_from_ws() -> None:
        nonlocal winch_primed, reprime_task, stdin_pending, stdin_paused_out
        while True:
            # Backpressure: while the program is behind by the cap, leave
            # the socket unread; the browser's send buffer takes the rest.
            while stdin_pending >= _STDIN_PENDING_CAP and not stdin_failed:
                stdin_room.clear()
                try:
                    await asyncio.wait_for(stdin_room.wait(), _STDIN_PAUSE_SECONDS)
                except asyncio.TimeoutError:
                    stdin_paused_out = True
                    return
            if stdin_failed:
                return
            try:
                msg = await websocket.receive()
            except WebSocketDisconnect:
                return
            except Exception:
                return
            if msg.get("type") == "websocket.disconnect":
                return
            text = msg.get("text")
            binary = msg.get("bytes")
            if text is not None:
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                if payload.get("t") == "resize":
                    if not _owns_pty():
                        return
                    rows = clamp_dim(payload.get("rows"), 24)
                    cols = clamp_dim(payload.get("cols"), 80)
                    _set_winsize(master_fd, rows, cols)
                    _poke_winch()
                    if poke and not winch_primed:
                        winch_primed = True
                        reprime_task = asyncio.create_task(_reprime_winch())
            elif binary:
                if not _owns_pty():
                    return
                stdin_queue.append(memoryview(binary))
                stdin_pending += len(binary)
                _flush_stdin()

    to_ws_task = asyncio.create_task(_pump_to_ws())
    from_ws_task = asyncio.create_task(_pump_from_ws())
    exit_task = asyncio.create_task(proc.wait())

    try:
        await asyncio.wait(
            {to_ws_task, from_ws_task, exit_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        from_ws_task.cancel()
        exit_task.cancel()
        if reprime_task is not None:
            reprime_task.cancel()
        # What the PTY still held of the program's output once it exited;
        # None unless this attach saw the exit with its PTY still its own.
        tail: Optional[bytes] = None
        # Unhook this attach's reader and stdin writer, unless a teardown
        # (session_service.kill_session) already did and closed the fd, whose number a
        # newer PTY may now hold. Stdin the program never read goes with
        # the attach.
        if _owns_pty():
            try:
                loop.remove_reader(master_fd)
                loop.remove_writer(master_fd)
            except (ValueError, OSError):
                pass
            if proc.returncode is not None:
                # The program exited, and its exit can win the wait above
                # before the reader sees the PTY close: record the end here
                # too, or a keep_alive re-attach would spawn it again. What
                # it wrote last may still be unread in the PTY: read it now,
                # while the fd is open, for the socket below.
                tail = _drain_ended_pty(session)
                session.ended = True
        # The rest of an exited program's output goes to the socket (below),
        # so the output pump is left running for it; any other end stops
        # the pump here.
        flush = tail is not None \
            and websocket.client_state != WebSocketState.DISCONNECTED
        if not flush:
            to_ws_task.cancel()
        if session.keep_alive and not session.ended \
                and proc.returncode is None:
            # Detach-tolerant: the PTY lives on. A detached-drain reader
            # keeps emptying the master fd into the bounded ring so the
            # program never blocks on a full kernel buffer; the next attach
            # removes it and replays the ring.
            session.attached = False
            _install_detached_drain(session, loop)
        else:
            session_service.kill_session(session)
        if flush:
            # In order: the pump sends everything it still holds, the chunk
            # it is sending included (a send cancelled while the server
            # waits for room in its write buffer is never written), and
            # stops at the marker; then the PTY's tail. The PTY is closed
            # now, so the pump must not stop to re-add a paused reader. The
            # marker always fits: the reader pauses with a slot still free.
            reader_paused = False
            ws_send_queue.put_nowait(None)
            await to_ws_task
            if tail:
                try:
                    await websocket.send_bytes(tail)
                except Exception:
                    pass
        if websocket.client_state != WebSocketState.DISCONNECTED:
            try:
                await websocket.close(code=1013 if stdin_paused_out else 1000,
                                      reason="terminal input paused" if stdin_paused_out else "")
            except Exception:
                pass
