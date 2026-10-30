# SPDX-License-Identifier: MIT
"""ACP client — the one wire every external harness speaks (shared/docs/agent-runtime.md, harness ownership).

One :class:`AcpSession` per conversation (cached per ``(backend,
session_key)`` by ``acp_run.run_acp_turn``) replaces the per-backend dispatch
arms and their ~700 lines of bespoke event parsers: the harness's ACP
adapter runs as a child process (a **host subprocess** for claude-code, an
**attached docker exec** for the container harnesses), speaks
newline-delimited JSON-RPC over the pinned ``agent-client-protocol`` SDK's
:class:`ClientSideConnection`, and every ``session/update`` notification is
normalized into the engine's ONE event vocabulary by :func:`normalize_update`
(pure, wire-shape dicts — the JSON schema, not SDK class names, is the
contract).

Load-bearing wire facts (verified against primary sources, 2026-08-25 — see
shared/docs/agent-runtime.md in this SDK):

  * stdio MCP entries carry NO ``type`` key (claude's adapter silently drops a
    server sent as ``{"type": "stdio"}``); ``env``/``headers`` are arrays of
    ``{name, value}`` objects. Both are guaranteed here by constructing the
    SDK's own models (:func:`build_mcp_servers`).
  * ``session/set_model`` does not exist — model selection goes through the
    per-harness spawn profile's env / config seam where one exists
    (``SpawnProfile.model_applied``); ``session/set_config_option`` against a
    ``category: "model"`` option runs only where no seam carried the model.
  * ``docker exec`` must NEVER allocate a TTY (``tty=False``): a PTY turns
    ``\\n`` into ``\\r\\n`` and echoes input — both corrupt the frame stream.
    The exec socket multiplexes stdout/stderr in 8-byte-header frames;
    :class:`_DemuxParser` splits them (stderr goes to the adapter log, never
    the JSON-RPC reader).
  * ``session/cancel`` is a notification; the turn ends when ``session/prompt``
    returns ``stopReason: "cancelled"``. The client marks unfinished tool calls
    cancelled itself (v1 has no ``cancelled`` status) and answers pending
    permission requests ``{outcome: "cancelled"}``.
  * ``session/load`` replays the whole history through ``session/update``
    BEFORE the response returns — the handler is attached first, and replayed
    updates are absorbed silently (no live deltas, no duplicate transcript:
    the transcript and chat records they would duplicate were written earlier
    in this run — session.json is a run log, so a load only reopens a session
    this run started).

Auth is provisioned BEFORE ``session/new`` (env keys on the child process /
subscription mounts + per-cell config homes) — never a mid-session modal.
"""
from __future__ import annotations
from _agent_shared.logging import safe_log as _secret_log
from _agent_shared.secret_hygiene import write_log as _write_log

import asyncio
import contextlib
import functools
import json
import os
import re
import shlex
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import gen
from . import harness_homes, acp_policy
from collimate import fs

# The pinned python SDK is the Agent generators' PEP 723 dependency
# (agent-client-protocol==…, mirrored in pyproject's dev extra). It is
# generated from ACP schema v1.19 (protocol version 1) while the stable
# schema is v1.26, and from the unstable schema at that — wrap it, never
# inherit its experimental contrib helpers.

# Where adapter stderr lands (category="log" — the live mirror skips it).
ACP_LOG_FILE = "_agent/acp-{backend}.log"
# Session continuity record, written on session/new (§2.5).
SESSION_FILE = "_agent/session.json"

_PROTOCOL_VERSION = 1

# Bounded caps for normalized event payloads. Tool input/result reuse the
# engine's own caps; structured diff/terminal content is bounded here so a
# whole-file diff can't blow the event socket.
_DIFF_TEXT_CAP = 16_000

# How long a cancel gets to come back as ``stopReason: cancelled`` before the
# hard watchdog path takes over.
_CANCEL_GRACE_S = 8.0

# The approval card's wait bound lives with the shared card helper
# (``approval.ASK_TIMEOUT_S``); the card itself is ``gen.ask``'s.




# ACP command terminals (§2.1 Phase 2): the client-implemented five
# (create/output/wait_for_exit/kill/release) over PIPES, never PTYs. Bounds:
# our own retained-output ceiling when the agent names no outputByteLimit,
# and truncation at a character boundary (spec MUST).
_TERM_DEFAULT_LIMIT = 1_048_576          # 1 MiB retained per terminal
_TERM_KILL_GRACE_S = 3.0


class _CommandTerminal:
    """One agent-requested command over pipes (terminal/create … release).

    A docker exec in the harness's own container (the same pidfile wrapper
    the adapter spawn uses, so kill reaches the process group). The
    merged stdout+stderr stream appends to a bounded buffer — truncated from
    the FRONT (the tail is what the agent polls for), flagged, and cut at a
    UTF-8 character boundary on decode. Release frees the output; its process/exit record stays available for cleanup."""

    def __init__(self, terminal_id: str, *, byte_limit: "int | None") -> None:
        self.terminal_id = terminal_id
        self.byte_limit = min(16 * 1024 * 1024, max(1, int(byte_limit))) if byte_limit else _TERM_DEFAULT_LIMIT
        self.buf = bytearray()
        self.truncated = False
        self.exit_code: "int | None" = None
        self.signal: "str | None" = None
        self.exited = asyncio.Event()
        self.released = False
        self._kill_impl: "Callable[[], None] | None" = None

    # ── output plumbing ───────────────────────────────────────────
    def feed(self, data: bytes) -> None:
        if self.released:
            return
        self.buf += data
        if len(self.buf) > self.byte_limit:
            del self.buf[: len(self.buf) - self.byte_limit]
            self.truncated = True

    def output_text(self) -> str:
        raw = bytes(self.buf)
        if self.truncated:
            # Cut at a character boundary: skip UTF-8 continuation bytes at
            # the (truncated) front so the decode never starts mid-character.
            i = 0
            while i < len(raw) and (raw[i] & 0xC0) == 0x80:
                i += 1
            raw = raw[i:]
        return raw.decode("utf-8", errors="replace")

    def finish(self, exit_code: "int | None", sig: "str | None") -> None:
        self.exit_code = exit_code
        self.signal = sig
        self.exited.set()

    def kill(self) -> None:
        if self._kill_impl is not None and not self.exited.is_set():
            try:
                self._kill_impl()
            except Exception:  # noqa: BLE001 — best-effort; EOF finishes it
                pass


def _attached_exec(container_id: str, argv: list, pidfile: str, *,
                   stdin: bool, environment, workdir):
    """Start ``argv`` as an attached, non-TTY docker exec whose shell records
    its pid in ``pidfile`` first; returns ``(client, exec_id, sock)`` with the
    socket's timeout cleared for a blocking reader thread. Synchronous: run
    it via ``asyncio.to_thread``."""
    import docker

    quoted = " ".join(shlex.quote(str(a)) for a in argv)
    wrapper = ["sh", "-c", f"echo $$ > {pidfile}; exec {quoted}"]
    client = docker.from_env()
    sock = None
    try:
        exec_id = client.api.exec_create(
            container_id, wrapper,
            stdin=stdin, stdout=True, stderr=True, tty=False,
            environment=environment or None,
            workdir=workdir,
        )["Id"]
        sock = client.api.exec_start(exec_id, socket=True, demux=False)
        # The blocking-read thread owns this socket; no global timeout.
        with contextlib.suppress(Exception):
            raw = getattr(sock, "_sock", sock)
            raw.settimeout(None)
        return client, exec_id, sock
    except BaseException:
        if sock is not None:
            _close_exec_socket(sock)
        with contextlib.suppress(Exception):
            client.close()
        raise



async def _spawn_container_terminal(session, term: _CommandTerminal,
                                    argv: list, env_pairs: dict,
                                    cwd: "str | None") -> None:
    """A container-locus command terminal: a docker exec in the harness's own
    container (its existing exec boundary), pidfile-wrapped so kill reaches
    the process group; a reader thread demuxes the exec socket into the
    bounded buffer and resolves the exit code from exec_inspect at EOF."""
    env_obj = session.profile.env_obj
    if env_obj is None:
        raise RuntimeError("no container environment for this session")
    loop = asyncio.get_running_loop()
    pidfile = f"/tmp/{term.terminal_id}.pid"
    client, exec_id, sock = await asyncio.to_thread(
        _attached_exec, env_obj.container_id, argv, pidfile,
        stdin=False, environment=env_pairs, workdir=cwd or session.agent_cwd)

    def _kill() -> None:
        script = (
            f'p=$(cat {pidfile} 2>/dev/null); '
            f'[ -n "$p" ] && {{ kill -TERM -"$p" 2>/dev/null || '
            f'kill -TERM "$p" 2>/dev/null; sleep {_TERM_KILL_GRACE_S:.0f}; '
            f'kill -KILL -"$p" 2>/dev/null || kill -KILL "$p" 2>/dev/null; }}; '
            f'rm -f {pidfile}; true'
        )
        with contextlib.suppress(Exception):
            env_obj.exec(["sh", "-c", f"({script}) </dev/null >/dev/null 2>&1 &"])

    term._kill_impl = _kill

    def _post(fn, *args) -> bool:
        try:
            loop.call_soon_threadsafe(fn, *args)
            return True
        except RuntimeError:               # loop gone: nobody is listening
            return False

    def _read_loop() -> None:
        demux = _DemuxParser()
        raw = getattr(sock, "_sock", sock)
        try:
            while True:
                try:
                    data = raw.recv(65536)
                except Exception:  # noqa: BLE001 — socket torn down
                    break
                if not data:
                    break
                for _stream_id, payload in demux.feed(data):
                    # Merged output: stdout AND stderr both land in the
                    # buffer (pipes, no PTY — the agent sees one stream).
                    if not _post(term.feed, payload):
                        return
        finally:
            try:
                exit_code = None
                with contextlib.suppress(Exception):
                    exit_code = client.api.exec_inspect(exec_id).get("ExitCode")
                _post(term.finish, exit_code, None)
            finally:
                try:
                    _close_exec_socket(sock)
                finally:
                    with contextlib.suppress(Exception):
                        client.close()

    threading.Thread(
        target=_read_loop, name=f"acp-term-{term.terminal_id}", daemon=True,
    ).start()


# ──────────────────────────────────────────────────────────────────────
# The attached-stdio exec primitive (docker) + the host subprocess, both
# behind the SDK's 3-method Transport protocol.
# ──────────────────────────────────────────────────────────────────────


class _DemuxParser:
    """The 8-byte-header stream demultiplexer for a ``tty=False`` docker
    attach/exec socket. Pure — feed bytes, get ``(stream_id, payload)`` frames
    (1 = stdout, 2 = stderr)."""

    def __init__(self) -> None:
        self.buf = bytearray()

    def feed(self, data: bytes) -> list:
        self.buf += data
        buf = self.buf
        pos, n = 0, len(buf)
        frames: list = []
        # Walk the frames by offset and drop the consumed prefix once: no
        # per-frame copy of the remainder.
        while n - pos >= 8:
            size = int.from_bytes(buf[pos + 4:pos + 8], "big")
            if n - pos < 8 + size:
                break
            frames.append((buf[pos], bytes(buf[pos + 8:pos + 8 + size])))
            pos += 8 + size
        if pos:
            del buf[:pos]
        return frames


class _LineAssembler:
    """Accumulate stream bytes into COMPLETE lines only — a half-written
    newline-delimited JSON-RPC frame must never reach the parser.

    The pending tail never holds a newline, so each feed searches only the
    new bytes and appends in place: a multi-MB line arriving in many frames
    costs linear, not quadratic, copying."""

    def __init__(self) -> None:
        self.buf = bytearray()

    def feed(self, data: bytes) -> list:
        nl = data.rfind(b"\n")
        if nl < 0:
            self.buf += data
            return []
        self.buf += data[:nl]
        head = bytes(self.buf)
        self.buf = bytearray(data[nl + 1:])
        return [ln for ln in head.split(b"\n") if ln.strip()]


class DockerExecTransport:
    """The SDK ``Transport`` over a docker-py exec socket with stderr demux.

    A reader THREAD does the blocking socket reads (docker-py's exec socket is
    a plain blocking ``SocketIO``), demuxes frames, assembles complete stdout
    lines, parses each as JSON, and hands the dicts to the event loop over an
    ``asyncio.Queue``; stderr frames append to the adapter log via the
    ``on_stderr`` callback. Writes go through ``asyncio.to_thread`` under a
    lock. ``receive`` returning ``None`` is EOF."""

    def __init__(self, sock, *, loop, on_stderr: Callable[[bytes], None]) -> None:
        self._sock = sock
        self._loop = loop
        self._queue: asyncio.Queue = asyncio.Queue()
        self._write_lock = threading.Lock()
        self._closed = False
        self._on_stderr = on_stderr
        self._thread = threading.Thread(
            target=self._read_loop, name="acp-docker-read", daemon=True,
        )
        self._thread.start()

    def _raw(self):
        return getattr(self._sock, "_sock", self._sock)

    def _read_loop(self) -> None:
        demux = _DemuxParser()
        lines = _LineAssembler()
        try:
            while not self._closed:
                try:
                    data = self._sock.read(65536)
                except Exception:  # noqa: BLE001 — socket torn down
                    break
                if not data:
                    break
                for stream_id, payload in demux.feed(data):
                    if stream_id == 2:
                        try:
                            self._on_stderr(payload)
                        except Exception:  # noqa: BLE001 — log is best-effort
                            pass
                        continue
                    for line in lines.feed(payload):
                        try:
                            msg = json.loads(line)
                        except Exception:  # noqa: BLE001 — non-JSON noise
                            continue
                        if not self._post(msg):
                            return
        finally:
            with contextlib.suppress(Exception):
                getattr(self._on_stderr, "close", lambda: None)()
            self._post(None)

    def _post(self, item) -> bool:
        """Hand ``item`` to the loop; False when the loop is gone (teardown
        raced the reader — stop reading, nobody is listening)."""
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, item)
            return True
        except RuntimeError:
            return False

    async def send(self, message: dict) -> None:
        if self._closed:
            raise ConnectionError("transport closed")
        data = (json.dumps(message, separators=(",", ":")) + "\n").encode()

        def _write():
            with self._write_lock:
                self._raw().sendall(data)

        await asyncio.to_thread(_write)

    async def receive(self) -> "dict | None":
        return await self._queue.get()

    def _dispose(self) -> None:
        """Release the owned attached response without requiring its event loop."""
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            import socket as _socket
            self._raw().shutdown(_socket.SHUT_WR)
        _close_exec_socket(self._sock)

    async def close(self) -> None:
        self._dispose()


def _close_exec_socket(sock) -> None:
    """Close a docker-py attached exec socket in the order its layers need.

    ``exec_start(socket=True)`` returns the connection's raw socket file with
    the HTTP response pinned on it as ``_response`` (docker-py does that so
    the response outlives the call). Closing the socket first and letting the
    response be garbage-collected later made urllib3's ``HTTPResponse.close``
    flush an already-closed file — an "Exception ignored … ValueError: I/O
    operation on closed file" traceback in the run log at every teardown.
    Close the response (and its file) FIRST, then the socket."""
    resp = getattr(sock, "_response", None)
    raw = getattr(resp, "raw", None)
    with contextlib.suppress(Exception):
        if raw is not None:
            raw.close()
    with contextlib.suppress(Exception):
        if resp is not None:
            resp.close()
    with contextlib.suppress(Exception):
        getattr(sock, "_sock", sock).close()
    with contextlib.suppress(Exception):
        sock.close()


# The largest JSON-RPC line a host adapter may send: the stdout reader's
# ``readline`` limit. One frame carries a whole ``fs/write_text_file`` body or
# a whole-file diff, so the cap sits far above any real frame; a line past it
# fails the session loudly (logged, then EOF) rather than being skipped, since
# a dropped request or response would leave its turn waiting forever. The
# StreamReader pauses the pipe only past twice its limit, so an adapter can
# buffer up to 512 MiB ahead of the reader, and a frame at the cap briefly
# costs a few copies of itself (the line, its decoded text, the parsed
# message).
_HOST_FRAME_LIMIT = 256 * 1024 * 1024


class SubprocessTransport:
    """The SDK ``Transport`` over host subprocess pipes (the host locus:
    claude-code and the ``docker: none`` twins). stderr is drained through the selected-secret scrubber; ``log`` appends to that same log (a line over
    :data:`_HOST_FRAME_LIMIT` is reported there before the session fails)."""

    def __init__(self, proc: "asyncio.subprocess.Process", *,
                 log: Callable[[bytes], None]) -> None:
        self._proc = proc
        self._closed = False
        self._log = log
        self._stderr_task = (asyncio.create_task(self._drain_stderr())
                             if getattr(proc, 'stderr', None) is not None else None)

    async def _drain_stderr(self):
        try:
            while block := await self._proc.stderr.read(65536):
                await asyncio.to_thread(self._log, block)
        except (ConnectionError, OSError):
            pass
        finally:
            await asyncio.to_thread(getattr(self._log, 'close', lambda: None))

    async def send(self, message: dict) -> None:
        if self._closed or self._proc.stdin is None:
            raise ConnectionError("transport closed")
        self._proc.stdin.write(
            (json.dumps(message, separators=(",", ":")) + "\n").encode()
        )
        await self._proc.stdin.drain()

    async def receive(self) -> "dict | None":
        if self._proc.stdout is None:
            return None
        while True:
            try:
                line = await self._proc.stdout.readline()
            except ConnectionError:              # torn pipe: the adapter is gone
                return None
            except ValueError as exc:            # a line over _HOST_FRAME_LIMIT
                self._report_overrun(exc)
                return None
            if not line:
                return None
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except Exception:  # noqa: BLE001 — non-JSON noise on stdout
                continue

    def _report_overrun(self, exc: ValueError) -> None:
        """Say why the session is about to end: the adapter sent a line past
        :data:`_HOST_FRAME_LIMIT`. Without this the respawn that follows reads
        as a bare "Connection closed"."""
        msg = (f"collimate: the adapter sent a JSON-RPC line over "
               f"{_HOST_FRAME_LIMIT // (1024 * 1024)} MiB ({exc}); ending the "
               f"session")
        with contextlib.suppress(Exception):
            self._log(f"\n{msg}\n".encode())
        _log(msg)

    async def close(self) -> None:
        self._closed = True
        with contextlib.suppress(Exception):
            if self._proc.stdin is not None:
                self._proc.stdin.close()
        if self._stderr_task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._stderr_task), timeout=1)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._stderr_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._stderr_task


@dataclass
class AdapterProcess:
    """One running ACP adapter child + its transport and teardown story."""
    transport: Any
    kill: Callable[[], None]        # kill the whole process group, both loci
    proc: Any = None                # asyncio subprocess (host locus)
    # Synchronous resources owned by this adapter, independent of transport
    # wrappers. Host subprocess pipes remain owned by their event loop.
    dispose: Callable[[], None] | None = None


def register_adapter_log(backend: str) -> str:
    """Register ``_agent/acp-<backend>.log`` as a run LOG artifact (empties it
    for this run) and return its absolute path. MUST run on the generator's
    main loop (it calls ``gen.update_file(category="log")``): marking the path
    in ``log_paths`` is sticky and excludes it from the live-files mirror,
    ``list_files``, and workspace snapshots — so the later raw appends from the
    socket reader THREAD (which cannot make a loop-affine ``gen.*`` call) stay
    out of the cell's Files and out of run output. Best-effort."""
    rel = ACP_LOG_FILE.format(backend=backend)
    try:
        _write_log(rel, "", category="log")
    except Exception:  # noqa: BLE001 — bridge-free context
        pass
    try:
        path = os.path.join(gen.workspace_path(), rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return path
    except Exception:  # noqa: BLE001
        return ""


def _adapter_log_writer(log_path: str) -> Callable[[bytes], None]:
    """A stderr sink appending raw bytes to ``log_path`` — plain file I/O ONLY:
    it runs on the docker-socket reader THREAD, where a loop-affine ``gen.*``
    call is forbidden. The path was registered as a log artifact on the main
    loop by :func:`register_adapter_log`."""
    if not log_path:
        return lambda data: None
    from .secret_hygiene import ByteScrubber, refresh
    ws, rel = _adapter_log_target(log_path)
    scrubber = ByteScrubber()
    lock = threading.Lock()
    closed = False

    def write(data: bytes) -> None:
        with lock:
            if closed:
                return
            try:
                refresh()
                cleaned = scrubber.feed(data)
                if cleaned:
                    fs.append_bytes(ws, rel, cleaned)
            except Exception:
                pass

    def close():
        nonlocal closed
        with lock:
            if closed:
                return
            closed = True
            try:
                tail = scrubber.finish()
                if tail:
                    fs.append_bytes(ws, rel, tail)
            except Exception:
                pass
    write.close = close
    return write


def _adapter_log_target(log_path: str) -> "tuple[str, str]":
    """``(workspace, relpath)`` for :func:`fs.append_bytes` — the log's path
    relative to the workspace, so every parent dir is walked by fd too (a
    swapped ``_agent`` dir can't redirect it). Outside a workspace, its own
    dir is the root (the leaf is still never followed)."""
    try:
        ws = gen.workspace_path()
        rel = os.path.relpath(log_path, ws)
        if ws and rel != ".." and not rel.startswith(".." + os.sep) and not os.path.isabs(rel):
            return ws, rel
    except Exception:  # noqa: BLE001 — bridge-free context
        pass
    return os.path.dirname(log_path) or ".", os.path.basename(log_path)


async def spawn_host_adapter(
    backend: str, argv: list, *, env: dict, cwd: str, pass_fds: tuple = (),
) -> AdapterProcess:
    """Spawn a host-locus adapter as a plain subprocess (its own session/group,
    stderr to the adapter log file). The env REPLACES the child's environment —
    every credential must already be in it, or on one of ``pass_fds``, the
    descriptors the child alone inherits (nothing else is inherited
    implicitly beyond what the caller put there). The Git account's harness
    environment is added here (accounts.git_harness_environment)."""
    from .accounts import git_harness_environment
    log_path = register_adapter_log(backend)
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, env={**env, **git_harness_environment(backend, "host")}, cwd=cwd,
        limit=_HOST_FRAME_LIMIT, start_new_session=True, pass_fds=tuple(pass_fds))

    def _kill() -> None:
        import signal
        with contextlib.suppress(Exception):
            os.killpg(proc.pid, signal.SIGTERM)
        for _ in range(20):
            if proc.returncode is not None:
                return
            time.sleep(0.1)
        with contextlib.suppress(Exception):
            os.killpg(proc.pid, signal.SIGKILL)

    return AdapterProcess(
        transport=SubprocessTransport(proc, log=_adapter_log_writer(log_path)),
        kill=_kill, proc=proc,
    )


def _adapter_kill_script(pidfile: str) -> str:
    """The in-container teardown for an adapter exec: TERM the adapter + its
    direct children (the harness core it spawned), wait up to 1 s for them
    all to exit, then KILL survivors. Deeper descendants die with the
    container (close_all) — the reaper backstop."""
    return (
        f'p=$(cat {pidfile} 2>/dev/null); '
        f'[ -n "$p" ] && {{ '
        f'kids=$(pgrep -P "$p" 2>/dev/null); '
        f'kill -TERM $p $kids 2>/dev/null; '
        f'i=0; while [ $i -lt 10 ]; do a=; '
        f'for q in $p $kids; do kill -0 $q 2>/dev/null && a=1; done; '
        f'[ -z "$a" ] && break; sleep 0.1; i=$((i+1)); done; '
        f'kill -KILL $p $kids 2>/dev/null; }}; '
        f'rm -f {pidfile}; true'
    )


async def spawn_container_adapter(
    backend: str, env_obj, argv: list, *, env_vars: dict, cwd: str = "/workspace",
) -> AdapterProcess:
    """Spawn a container-locus adapter as an ATTACHED docker exec:
    ``exec_create(stdin=True, tty=False)`` + ``exec_start(socket=True)`` with
    8-byte-header stderr demux — never a TTY (it would corrupt the JSON-RPC
    framing), never the ``docker`` CLI (docker-py is the declared dep).

    The adapter's pid is recorded in-container so teardown can signal it and
    its children explicitly (SIGTERM to the exec socket reaches nothing —
    orphaned harnesses are the documented risk; container removal and the
    run-label reaper stay the backstops). Deliberately NO ``setsid``: docker
    exec makes the command a process-group leader, so the setsid utility
    would FORK — the exec's primary process then exits and docker closes the
    attach socket under the adapter mid-handshake."""
    import uuid
    from .accounts import git_harness_environment

    loop = asyncio.get_running_loop()
    pidfile = f"/tmp/acp-{uuid.uuid4().hex}.pid"
    log_path = register_adapter_log(backend)
    client, _exec_id, sock = await asyncio.to_thread(
        _attached_exec, env_obj.container_id, argv, pidfile, stdin=True,
        environment={k: str(v) for k, v in {**(env_vars or {}), **git_harness_environment(backend, "container")}.items()},
        workdir=cwd)
    try:
        transport = DockerExecTransport(
            sock, loop=loop, on_stderr=_adapter_log_writer(log_path),
        )
    except BaseException:
        _close_exec_socket(sock)
        with contextlib.suppress(Exception):
            client.close()
        raise
    disposed = False
    disposal_lock = threading.Lock()

    def _dispose() -> None:
        nonlocal disposed
        with disposal_lock:
            if disposed:
                return
            disposed = True
            try:
                transport._dispose()
            finally:
                with contextlib.suppress(Exception):
                    client.close()

    def _kill() -> None:
        with contextlib.suppress(Exception):
            env_obj.exec(["sh", "-c", _adapter_kill_script(pidfile)])

    return AdapterProcess(transport=transport, kill=_kill, dispose=_dispose)


# ──────────────────────────────────────────────────────────────────────
# The claude host-locus adapter: pin + install + preflight (§1 packaging).
# ──────────────────────────────────────────────────────────────────────




# ──────────────────────────────────────────────────────────────────────
# The normalizer — 11 session/update variants → the engine vocabulary.
# Pure functions over WIRE-shape dicts (camelCase, `sessionUpdate`
# discriminator): the JSON schema is the contract, not SDK class names.
# ──────────────────────────────────────────────────────────────────────

# The terminal output a tool call's updates may stream in their _meta
# (codex-acp's ``terminal_output_delta``), kept per call as its result's
# stand-in when the finishing update carries none: the tail, bounded.
TERMINAL_OUTPUT_CAP = 64 * 1024

# ACP ToolCallStatus → the engine's tool-event status.
_STATUS_MAP = {
    "pending": "running", "in_progress": "running",
    "completed": "done", "failed": "error",
}


def _content_text(content: "dict | None") -> str:
    """The text of a ContentBlock (``{type: "text", text}``); other block
    types (image/audio/resource) have no text — ``""``."""
    if isinstance(content, dict) and content.get("type") == "text":
        return str(content.get("text") or "")
    return ""


def _tool_content_bits(blocks) -> tuple:
    """``(result_text, extras)`` from a ToolCall's ``content`` list: plain
    ``content`` blocks join into the bounded result text; ``diff`` /
    ``terminal`` blocks ride as STRUCTURED extras (bounded), for the renderer's
    kind-typed display."""
    texts: list = []
    extras: list = []
    for b in blocks or []:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "content":
            s = _content_text(b.get("content"))
            if s:
                texts.append(s)
        elif t == "diff":
            extras.append({
                "type": "diff",
                "path": str(b.get("path") or ""),
                "oldText": (str(b.get("oldText"))[:_DIFF_TEXT_CAP]
                            if b.get("oldText") is not None else None),
                "newText": str(b.get("newText") or "")[:_DIFF_TEXT_CAP],
            })
        elif t == "terminal":
            extras.append({
                "type": "terminal",
                "terminalId": str(b.get("terminalId") or ""),
            })
    return "\n".join(texts), extras


def normalize_update(u: dict) -> dict:
    """One ``session/update`` params dict → ``{"events": [...], "trace": [...]}``
    in the engine vocabulary (§2.1's table). Unknown variants normalize to
    nothing (forward compatibility, never a crash)."""
    from .agent_events import TOOL_INPUT_CAP, TOOL_RESULT_CAP, tool_summary, trace_event

    upd = u.get("update") if isinstance(u.get("update"), dict) else u
    kind = upd.get("sessionUpdate") or ""
    events: list = []
    trace: list = []

    if kind == "agent_message_chunk":
        text = _content_text(upd.get("content"))
        if text:
            events.append({"type": "text_delta", "text": text})
            trace.append(trace_event("text", text=text))
    elif kind == "agent_thought_chunk":
        text = _content_text(upd.get("content"))
        if text:
            events.append({"type": "thinking_delta", "text": text})
            trace.append(trace_event("thinking", text=text))
    elif kind == "user_message_chunk":
        text = _content_text(upd.get("content"))
        if text:
            trace.append(trace_event("user", text=text))
    elif kind in ("tool_call", "tool_call_update"):
        tid = str(upd.get("toolCallId") or "")
        title = str(upd.get("title") or "")
        status = _STATUS_MAP.get(str(upd.get("status") or ""), "")
        if kind == "tool_call" and not status:
            status = "running"          # ToolCall status defaults to pending
        result_text, extras = _tool_content_bits(upd.get("content"))
        ev: dict = {"type": "tool", "id": tid}
        if title:
            ev["name"] = title
        if status:
            ev["status"] = status
        raw_input = upd.get("rawInput")
        if raw_input is not None:
            ev["input"] = tool_summary(raw_input, TOOL_INPUT_CAP)
        if result_text:
            ev["result"] = tool_summary(result_text, TOOL_RESULT_CAP)
        elif status in ("done", "error") and upd.get("rawOutput") is not None:
            ev["result"] = tool_summary(upd.get("rawOutput"), TOOL_RESULT_CAP)
        if upd.get("kind"):
            ev["kind"] = str(upd["kind"])
        locs = upd.get("locations")
        if isinstance(locs, list) and locs:
            ev["locations"] = [
                {"path": str(loc.get("path") or ""),
                 **({"line": loc["line"]} if isinstance(loc.get("line"), int) else {})}
                for loc in locs if isinstance(loc, dict)
            ][:20]
        if extras:
            ev["content"] = extras
        events.append(ev)
        if kind == "tool_call":
            trace.append(trace_event(
                "tool_use", tool=title or tid, input=upd.get("rawInput"),
            ))
        if status in ("done", "error"):
            out = result_text or upd.get("rawOutput")
            if out is not None and out != "":
                trace.append(trace_event(
                    "tool_result", tool=title, output=out,
                ))
    elif kind == "plan":
        entries = [
            {"content": str(e.get("content") or ""),
             "priority": str(e.get("priority") or ""),
             "status": str(e.get("status") or "")}
            for e in (upd.get("entries") or []) if isinstance(e, dict)
        ]
        events.append({"type": "plan", "entries": entries})
        trace.append({"type": "plan", "entries": entries})
    elif kind == "usage_update":
        # Context-window occupancy + CUMULATIVE session cost — a context
        # meter, NOT per-turn token usage (which has no ACP source).
        ev = {"type": "context"}
        for k in ("used", "size"):
            if isinstance(upd.get(k), (int, float)):
                ev[k] = upd[k]
        cost = upd.get("cost")
        if isinstance(cost, dict) and isinstance(cost.get("amount"), (int, float)):
            ev["cost"] = {"amount": cost["amount"],
                          "currency": str(cost.get("currency") or "")}
        events.append(ev)
    elif kind == "session_info_update":
        events.append({"type": "session_info",
                       **{k: upd[k] for k in ("title", "updatedAt") if upd.get(k)}})
    elif kind == "available_commands_update":
        events.append({"type": "available_commands",
                       "commands": upd.get("availableCommands") or []})
    elif kind == "current_mode_update":
        events.append({"type": "current_mode",
                       "modeId": str(upd.get("currentModeId") or "")})
    elif kind == "config_option_update":
        events.append({"type": "config_option", "update": {
            k: v for k, v in upd.items() if k != "sessionUpdate"}})
    return {"events": events, "trace": trace}


# ──────────────────────────────────────────────────────────────────────
# MCP declaration assembly — one list for session/new|load.mcpServers.
# ──────────────────────────────────────────────────────────────────────


def build_mcp_servers(
    *, bridge, mcp_decls, locus: str, http_ok: bool, stdio_proxy_argv=None,
) -> tuple:
    """``(servers, warnings)`` — the SDK model list for ``session/new|load``.

    One tool set, two carriers (§2.3): the per-run bridge rides as an HTTP
    entry when the agent advertised ``mcpCapabilities.http``, else as the
    in-container stdio↔HTTP proxy (``stdio_proxy_argv`` — installed in every
    adapter image; stdio is the protocol's only MANDATORY transport). The
    cell's ``_mcp`` declarations join the same list: http entries with their
    (pre-resolved) headers, loopback rewritten to ``host.docker.internal`` on
    container loci; stdio entries ONLY on container loci — handing a host-locus
    adapter a command server would be a new host exec surface.

    Model instances, not dicts, so the wire shape is right by construction:
    stdio entries carry NO ``type`` key and ``env``/``headers`` are
    ``{name, value}`` arrays."""
    from acp.schema import EnvVariable, HttpHeader, HttpMcpServer, McpServerStdio

    from .cell_attach import rewrite_for_container
    from .mcp_bridge import SERVER_NAME

    servers: list = []
    warnings: list = []
    if bridge is not None:
        url = bridge.url_container if locus == "container" else bridge.url_host
        if http_ok:
            servers.append(HttpMcpServer(
                type="http", name=SERVER_NAME, url=url, headers=[],
            ))
        elif locus == "container" and stdio_proxy_argv:
            # mcp-remote (0.2.4) refuses a non-HTTPS URL that is not
            # localhost — "Error: Non-HTTPS URLs are only allowed for
            # localhost or when --allow-http flag is provided" — and the
            # bridge a container reaches is http://host.docker.internal:<port>
            # by construction, so without --allow-http this carrier could
            # never start at all (the proxy exits before the agent's first
            # tools/list, and the session just has no collimate tools).
            argv = [str(a) for a in stdio_proxy_argv] + [
                bridge.url_container, "--allow-http", "--transport", "http-only",
            ]
            servers.append(McpServerStdio(
                name=SERVER_NAME, command=argv[0], args=argv[1:], env=[],
            ))
        else:
            warnings.append(
                "the agent advertises no HTTP MCP capability and no stdio "
                "proxy is available — the collimate tool server is not attached"
            )
    for d in mcp_decls or []:
        # ACP's mcpServers has NO per-server tool filter on the wire, so a
        # decl's allowed_tools (which v1 enforced per-harness — claude's
        # allowed_tools list, pydantic-ai's .filtered()) can't be honored on
        # the ACP path. Surface it rather than silently exposing the whole
        # server's toolset — a narrowing the operator asked for is now void.
        if getattr(d, "allowed_tools", None):
            warnings.append(
                f"mcp server {d.name!r} declares allowed_tools "
                f"{sorted(d.allowed_tools)}, but ACP has no per-server tool "
                "filter — the server's FULL toolset is exposed to the agent"
            )
        if d.transport == "http":
            if not http_ok:
                warnings.append(
                    f"mcp server {d.name!r} (http) attached, but the agent "
                    "advertises no HTTP MCP capability — it may be ignored"
                )
            url = rewrite_for_container(d.url) if locus == "container" else d.url
            servers.append(HttpMcpServer(
                type="http", name=d.name, url=url,
                headers=[HttpHeader(name=str(k), value=str(v))
                         for k, v in (d.headers or {}).items()],
            ))
        elif locus == "container":
            servers.append(McpServerStdio(
                name=d.name, command=d.command[0], args=list(d.command[1:]),
                env=[EnvVariable(name=str(k), value=str(v))
                     for k, v in (d.env or {}).items()],
            ))
        else:
            warnings.append(
                f"mcp server {d.name!r} (stdio) skipped — command servers "
                "never run on the host; they need a containerized harness"
            )
    return servers, warnings


# ──────────────────────────────────────────────────────────────────────
# The ACP client (fs handlers + permissions + update sink).
# ──────────────────────────────────────────────────────────────────────


def _log(msg: str) -> None:
    try:
        _secret_log(msg)
    except Exception:  # noqa: BLE001 — bridge-free context
        pass


# Raw-input fields that name a filesystem path: claude's (``file_path``,
# ``path``, ``notebook_path``) and opencode's (``filepath`` / ``filePath``,
# and its file tools' ``external_directory`` ask's ``parentDir``).
_RAW_PATH_KEYS = ("file_path", "path", "notebook_path", "filepath", "filePath",
                  "parentDir")


def _request_paths(tool_call, *, glob: bool = True) -> list:
    """Every filesystem path a permission request names: its ``locations``
    (claude-agent-acp also appends the CLI's ``blockedPath`` there) and the
    path fields of its raw input — plus, with ``glob``, the directory a Glob
    ``pattern`` reaches (a file tool's field; elsewhere ``pattern`` need not
    be a path)."""
    out: list = []
    for loc in getattr(tool_call, "locations", None) or []:
        p = loc.get("path") if isinstance(loc, dict) else getattr(loc, "path", None)
        if p:
            out.append(str(p))
    raw = getattr(tool_call, "raw_input", None)
    if isinstance(raw, dict):
        for key in _RAW_PATH_KEYS:
            v = raw.get(key)
            if isinstance(v, str) and v:
                out.append(v)
        root = _glob_pattern_root(tool_call, raw) if glob else ""
        if root:
            out.append(root)
    return out


# Keys only the claude CLI's Grep takes: its ``pattern`` is a regex, not a
# path, and its ``glob`` only filters inside the (already checked) ``path``.
_GREP_ONLY_KEYS = frozenset({
    "glob", "type", "output_mode", "-i", "-n", "-A", "-B", "-C", "multiline",
    "head_limit", "offset",
})


def _glob_pattern_root(tool_call, raw: dict) -> str:
    """The directory a Glob ``pattern`` reaches, as a path to check, or "".

    The CLI's Glob carries its target in ``pattern`` alone (``/etc/*``,
    ``~/.ssh/*``, ``../../**/*.pem``) with no ``locations``, so without this
    a host-locus Glob could list any directory tree on the machine. The
    pattern's static segments are kept, a ``**`` segment is dropped (it may
    match zero directories, the shallowest reach) and every other wildcard
    segment stands for one literal name — so a ``..`` after a wildcard is
    weighed too. A relative pattern is taken from Glob's ``path`` (itself
    checked); a plain one (``**/*.py``) stays inside."""
    pat = raw.get("pattern")
    if not isinstance(pat, str) or not pat:
        return ""
    if str(getattr(tool_call, "title", "") or "").startswith("grep") \
            or _GREP_ONLY_KEYS.intersection(raw):
        return ""
    segs = []
    for seg in pat.split("/"):
        if seg == "**":
            continue
        segs.append("_" if any(c in seg for c in "*?[{") else seg)
    root = "/".join(segs)
    if pat.startswith("/") and not root.startswith("/"):
        root = "/" + root
    base = raw.get("path")
    if isinstance(base, str) and base and not pat.startswith(("/", "~")):
        # A relative pattern is resolved against Glob's own search dir.
        root = os.path.join(base, root) if root else base
    return root or "."




def _refusal(session: "AcpSession", options):
    """Refuse a permission request by SELECTING a reject_once option (the
    protocol has no standalone "rejected" outcome — a refusal is choosing a
    reject option), else answer cancelled. Never an auto-picked
    reject_always (or a harness's persisting id): harnesses keep those as
    standing rules."""
    from acp.schema import AllowedOutcome, DeniedOutcome, RequestPermissionResponse
    rej = next((o for o in options or []
                if getattr(o, "kind", "") == "reject_once" and not acp_policy._persists(session, o)),
               None)
    if rej is not None:
        return RequestPermissionResponse(outcome=AllowedOutcome(
            outcome="selected", option_id=rej.option_id))
    return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))


# A URI's scheme (``file:``, ``https:``) before its path; two characters or
# more, so a Windows drive letter (``C:/``) is not one.
_URI_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]+:/")


class _CollimateClient:
    """Our ``Client`` implementation for :class:`ClientSideConnection`.

    ``fs/read_text_file`` / ``fs/write_text_file`` are the staged-write
    workspace impls with path translation (the harness's cwd-absolute paths ↔
    cell-relative); everything else the workspace offers stays on the MCP
    server. ``terminal/*`` runs commands in the harness's container, served
    only when the grant's ``commands`` level is not deny (carded under ask).
    Permission requests are answered by the grant gate, then the human
    (``approvals: ask``, or any command request under ``commands: ask``), else
    the documented auto rule: first ``allow_once``, else first
    ``allow_always``, else cancelled."""

    def __init__(self, session: "AcpSession") -> None:
        self._s = session

    # ── session/update ────────────────────────────────────────────
    async def session_update(self, session_id: str, update, **kwargs) -> None:
        try:
            wire = update.model_dump(by_alias=True, exclude_none=True)
        except Exception:  # noqa: BLE001 — already a dict (tests)
            wire = update if isinstance(update, dict) else {}
        self._s._on_update(wire)

    # ── permissions ───────────────────────────────────────────────
    async def request_permission(self, session_id: str, tool_call, options,
                                 **kwargs):
        from acp.schema import (
            AllowedOutcome,
            DeniedOutcome,
            RequestPermissionResponse,
        )
        if self._s.stopping():
            return RequestPermissionResponse(
                outcome=DeniedOutcome(outcome="cancelled"))
        tool_call = self._s.announced(tool_call)
        # Defense in depth: the auto-allow answers the harness's own approval
        # flow, but it must NOT open a surface the run disallowed. The pinned
        # claude adapter silently ignores our `permissionMode` _meta and runs
        # its own permission flow, so this handler — not permissionMode — is
        # the second gate behind the by-name tool lists. A COMMAND request
        # (see acp_policy._is_command_request — not only kind ``execute``) follows the
        # grant's ``commands`` level on every locus: deny refuses it; ask and
        # allow are answered below. codex's MCP tool approvals arrive as kind
        # ``execute`` too, tagged in the request's _meta (which the SDK
        # router spreads into kwargs): they are a tool call, not a command.
        # The write kinds are refused where file tools are off, ``fetch``
        # where web search is off, and the host locus path-checks the rest.
        kind = str(getattr(tool_call, "kind", "") or "")
        command = (not acp_policy._is_mcp_tool_approval(self._s, kwargs)
                   and acp_policy._is_command_request(self._s, tool_call))
        reason = ""
        if command:
            if self._s.commands == "deny":
                reason = "the runner's commands permission is deny"
        elif kind in ("edit", "delete", "move") and not self._s.file_tools_enabled:
            reason = "file tools are off for this run"
        elif kind == "fetch" and not self._s.websearch_enabled:
            reason = "web access is off for this run"
        elif self._s.locus == "host":
            reason = self._host_refusal(kind, tool_call)
        if reason:
            _log(f"acp[{self._s.backend}]: refused a {kind!r} permission "
                 f"request — {reason}")
            return _refusal(self._s, options)
        # Never offer or pick an option that switches the adapter session's
        # permission MODE (see acp_policy._switches_permission_mode): past it the CLI
        # stops asking, so the grant gate above and the approvals cards go
        # quiet for everything the new mode auto-approves.
        options = [o for o in options or []
                   if not acp_policy._switches_permission_mode(getattr(o, "option_id", ""))
                   and not self._leaves_the_grant(o)]
        cmd_text = acp_policy._command_text(self._s, tool_call) if command else ""
        if command and self._s.commands == "ask":
            # commands: ask — every command is the human's call, whatever
            # `approvals` says, and only for THIS command: an allow_always
            # (codex's execpolicy amendment / allow-for-session, goose's
            # permission.yaml rule, claude's allow-with-updates, qwen's
            # proceed_always_*) or a harness's persisting option id would
            # leave a standing rule, so none is offered.
            options = [o for o in options
                       if getattr(o, "kind", "") != "allow_always"
                       and not acp_policy._persists(self._s, o)]
            resp = await self._ask_human(tool_call, options, is_command=True,
                                         command_text=cmd_text)
            self._note_command_answer(tool_call, options, resp, cmd_text)
            return resp
        if self._s.approvals == "ask":
            # approvals: ask (shared/docs/agent-runtime.md, grants and tools) — surface the request as a card
            # the human answers over the surface WebSocket. The grant gate
            # above already refused anything the run doesn't permit, so ask
            # only ever offers what auto would have allowed.
            return await self._ask_human(tool_call, options, is_command=command,
                                         command_text=cmd_text)
        # allow_once FIRST. An adapter's allow_always is not just "yes" — it
        # carries side effects the grant never asked for: claude-agent-acp's
        # `allow-with-updates` persists a rule into the run workspace's
        # `.claude/settings.local.json` (harness scaffolding in the user's
        # cell, and a live rule for their own claude there), or adds a
        # directory / switches the session to acceptEdits, after which the CLI
        # stops asking and this gate goes quiet. allow_always stays a choice
        # the human can make under approvals: ask.
        chosen = None
        for want in ("allow_once", "allow_always"):
            chosen = next(
                (o for o in options if getattr(o, "kind", "") == want
                 and not acp_policy._persists(self._s, o)),
                None,
            )
            if chosen is not None:
                break
        if chosen is None:
            return RequestPermissionResponse(
                outcome=DeniedOutcome(outcome="cancelled"))
        return RequestPermissionResponse(outcome=AllowedOutcome(
            outcome="selected", option_id=chosen.option_id))

    def _leaves_the_grant(self, option) -> bool:
        """Whether picking ``option`` would switch the session to a mode the
        grant's ``commands`` level rules out (:data:`acp_policy._MODE_SWITCH_OPTIONS`)."""
        from .acp_policy import mode_allowed

        target = acp_policy._MODE_SWITCH_OPTIONS.get(self._s.backend, {}).get(
            str(getattr(option, "option_id", "") or ""))
        return target is not None and not mode_allowed(
            self._s.backend, self._s.commands, target)

    def _note_command_answer(self, tool_call, options, resp, cmd_text: str) -> None:
        """Remember an APPROVED command request under ``commands: ask`` (its
        tool call and command line), so the one ``terminal/create`` that
        runs it is not carded a second time. Each answer replaces only its
        own tool call's entry: approvals of several calls in one message
        stay pending side by side, and a refusal withdraws no other. Nothing
        is remembered while the turn is stopping."""
        picked = getattr(getattr(resp, "outcome", None), "option_id", None)
        kinds = {getattr(o, "option_id", None): getattr(o, "kind", "")
                 for o in options or []}
        approved = picked is not None and kinds.get(picked, "").startswith("allow")
        call_id = acp_policy._tool_call_id(tool_call)
        pending = self._s._approved_commands
        pending[:] = [entry for entry in pending if entry[0] != call_id]
        if approved and not self._s.stopping():
            pending.append((call_id, cmd_text))

    # The kinds a host-locus claude request may carry and still be answered
    # yes (each is gated above or path-checked below). Anything else — a CLI
    # built-in newer than this list, `other`-kind tools that are not an MCP
    # server's — is refused: the claude CLI's tool set keeps growing, the host
    # locus has no container behind it, and a gate that allows what it does
    # not know fails open.
    _HOST_KNOWN_KINDS = frozenset({
        "read", "search", "edit", "delete", "move", "fetch", "think",
        "switch_mode",
    })
    _HOST_PATH_KINDS = frozenset({"read", "search", "edit", "delete", "move"})

    def _host_refusal(self, kind: str, tool_call) -> str:
        """Why a host-locus request is refused, or "" to let it through.

        On the host locus nothing but this answer stands between the harness's
        own file tools and the user's machine: the claude adapter never routes
        its Read/Write/Edit through ``fs/*`` (so :meth:`_rel` never runs for
        it), its CLI asks exactly when a path falls outside its working
        directory, and opencode writes to disk itself, asking only through
        its ``external_directory`` permission (kind ``other``). So every path
        a request names, whatever its kind, must pass the same containment
        the fs handlers apply — a read of ``~/.ssh`` or a write to
        ``~/.bashrc`` or ``~/.opencode/tool/`` is refused however the grant
        is set. A command request (opencode's shell asking about the
        directories its command reaches included) never gets here: it
        follows the ``commands`` level. Nor does an MCP server's tool outside
        the path kinds: its paths are in that server's namespace, and
        Collimate's bridge contains its own."""
        path_kind = kind in self._HOST_PATH_KINDS
        mcp = acp_policy._is_mcp_tool_call(tool_call)
        if path_kind or not mcp:
            for p in _request_paths(tool_call, glob=path_kind):
                try:
                    self._rel(os.path.expanduser(p))
                except Exception:  # noqa: BLE001 — RequestError: an escape
                    return f"{p} is outside the workspace"
        if path_kind:
            return ""
        if not self._s.profile.strict_permission_kinds or kind in self._HOST_KNOWN_KINDS:
            return ""
        if kind == "other" and mcp:
            # Collimate's bridge and the cell's own .mcp.json servers — the
            # only MCP servers the CLI is given (strictMcpConfig).
            return ""
        return f"a {kind or 'kind-less'} tool is not permitted on the host"

    async def _ask_human(self, tool_call, options, *, is_command: bool = False,
                         command_text: str = ""):
        """One permission card through the shared approval helper
        (:func:`approval.ask_human` over :func:`gen.ask` — the card, the
        ``awaiting_approval`` phase, the bounded wait, the fast refusal when
        nobody can answer). A stopping turn refuses at once (the protocol's
        own cancel rule); every refusal is answered ``cancelled``.

        A command request (``is_command``) is a command card: it shows the
        full ``command_text`` — or, when the harness's key for the command
        is not one :func:`acp_policy._command_text` knows, the WHOLE raw input — never
        the capped tool summary, and one too long to show whole is refused."""
        from acp.schema import (
            AllowedOutcome,
            DeniedOutcome,
            RequestPermissionResponse,
        )

        from .approval import ask_human

        # The harness's options are untrusted input: a blank or repeated
        # option_id is dropped (the first of an id wins), and a request left
        # with none is refused without a card — gen.ask's ValueError is for a
        # generator's own malformed options, not the harness's.
        card_options: list = []
        for o in options or []:
            oid = str(getattr(o, "option_id", "") or "")
            if oid and all(c["option_id"] != oid for c in card_options):
                card_options.append({"option_id": oid,
                                     "name": str(getattr(o, "name", "") or ""),
                                     "kind": str(getattr(o, "kind", "") or "")})
        if not card_options:
            _log(f"acp[{self._s.backend}]: refused a permission request with no "
                 "option to offer the human")
            return RequestPermissionResponse(
                outcome=DeniedOutcome(outcome="cancelled"))
        raw_input = getattr(tool_call, "raw_input", None)
        chosen = await ask_human(
            title=str(getattr(tool_call, "title", "") or "") or "Permission",
            kind=str(getattr(tool_call, "kind", "") or ""),
            options=card_options,
            raw_input=(command_text or raw_input) if is_command else raw_input,
            command=is_command,
            is_cancelled=self._s.stopping,
            log_prefix=f"acp[{self._s.backend}]: ",
        )
        if chosen is None:
            return RequestPermissionResponse(
                outcome=DeniedOutcome(outcome="cancelled"))
        return RequestPermissionResponse(outcome=AllowedOutcome(
            outcome="selected", option_id=chosen))

    # ── fs ────────────────────────────────────────────────────────
    def _rel(self, path: str) -> str:
        """Translate a harness path to cell-relative; refuse escapes. The
        harness sees the workspace at ``self._s.agent_cwd`` (``/workspace`` in
        a container, the host workspace dir for the claude locus).

        Containment is physical, not just lexical: a container harness is root
        in the bind-mounted workspace and can plant a symlink whose target the
        host generator would otherwise dereference — so after stripping the
        cwd prefix the workspace-relative path passes through
        :func:`contain_ws_path`, which resolves symlinks and re-checks the
        real path is inside the workspace root. A URI (``file:///etc/passwd``)
        is refused outright: it is no workspace path, and the harness that
        resolves it would not read what the lexical check saw."""
        from acp import RequestError

        from .workspace_tools import PathEscape, contain_ws_path
        p = str(path or "")
        if _URI_SCHEME.match(p):
            raise RequestError.invalid_params({"path": f"not a workspace path: {p}"})
        cwd = self._s.agent_cwd
        if p.startswith(cwd.rstrip("/") + "/"):
            rel = p[len(cwd.rstrip("/")) + 1:]
        elif p == cwd.rstrip("/"):
            rel = "."
        elif not os.path.isabs(p):
            rel = p
        else:
            raise RequestError.invalid_params(
                {"path": f"outside the workspace: {p}"})
        try:
            return contain_ws_path(rel)
        except PathEscape as e:
            raise RequestError.invalid_params({"path": str(e)}) from e

    async def read_text_file(self, session_id: str, path: str,
                             line=None, limit=None, **kwargs):
        return await asyncio.to_thread(self._read_text_file, path, line, limit)

    def _read_text_file(self, path: str, line, limit):
        from acp import RequestError
        from acp.schema import ReadTextFileResponse
        rel = self._rel(path)
        if not gen.has_file(rel):
            raise RequestError.resource_not_found(rel)
        content = gen.read_file(rel)
        if isinstance(content, bytes):
            raise RequestError.invalid_params({"path": f"binary file: {rel}"})
        if line is not None or limit is not None:
            all_lines = content.splitlines(keepends=True)
            start = max(0, (int(line) - 1)) if line else 0
            end = start + int(limit) if limit else len(all_lines)
            content = "".join(all_lines[start:end])
        return ReadTextFileResponse(content=content)

    async def write_text_file(self, session_id: str, path: str, content: str,
                              **kwargs):
        from acp import RequestError
        from acp.schema import WriteTextFileResponse
        if not self._s.file_tools_enabled:
            raise RequestError(
                403, "file writes are disabled for this run (text-only turn)")
        from .run_files import write_file
        rel = self._rel(path)
        try:
            # Runs' interface files under _runs/ follow the run-folder rules:
            # another run's are read-only, and the run's own folder holds
            # only its interface files, which the harness never writes.
            write_file(rel, content or "")
        except (PermissionError, ValueError) as exc:
            raise RequestError(403, str(exc)) from exc
        return WriteTextFileResponse()

    # ── ACP command terminals (§2.1 Phase 2) ──────────────────────
    # The client-implemented five, over PIPES (never PTYs), executing where
    # the harness's own exec surface already lives: its container. The host
    # locus serves none, and neither does `commands: deny`
    # (`advertises_terminal`).

    def _terminals_enabled(self) -> bool:
        return self._s.advertises_terminal()

    def _get_terminal(self, terminal_id: str) -> "_CommandTerminal":
        from acp import RequestError
        term = self._s._terminals.get(str(terminal_id or ""))
        if term is None or term.released:
            raise RequestError.invalid_params(
                {"terminalId": f"unknown or released: {terminal_id}"})
        return term

    def _covered_by_approval(self, command, args, kwargs: dict) -> bool:
        """Consume the first pending approved command request this terminal
        runs: True when one approved the command it runs (verbatim or
        shell-wrapped — see acp_policy._same_command) and, if the agent names the tool
        call in the request's _meta, it is that call. A request that named no
        command line covers nothing: the card never showed what the terminal
        would run."""
        named = str(kwargs.get("toolCallId") or kwargs.get("tool_call_id") or "")
        pending = self._s._approved_commands
        for i, (call_id, text) in enumerate(pending):
            if ((not named or named == call_id) and text
                    and acp_policy._same_command(text, command, args)):
                del pending[i]
                return True
        return False

    async def create_terminal(self, session_id: str, command: str,
                              args=None, env=None, cwd=None,
                              output_byte_limit=None, **kwargs):
        import uuid as _uuid

        from acp import RequestError
        from acp.schema import CreateTerminalResponse
        if not self._terminals_enabled():
            # Not advertised: the host locus, or commands: deny.
            raise RequestError.method_not_found("terminal/create")
        if self._s.stopping():
            raise RequestError.invalid_request(
                {"command": "refused: the turn is being stopped"})
        # A bare `command` is a shell command LINE, the way the reference
        # client (Zed) runs it: goose sends the whole line —
        # `ls -la /workspace | wc -l`, `/usr/bin/bash -lc '…'` — with no
        # args, and exec'ing that as one program name fails with 127 for
        # anything but a single word. With args, `command` names the program
        # and the argv is exec'd as given. No new surface: the agent could
        # already send `sh` with `["-c", line]` to the same terminal.
        if args:
            argv = [str(command)] + [str(a) for a in args]
        else:
            argv = ["sh", "-c", str(command)]
        if self._s.commands == "ask" and not self._covered_by_approval(command, args, kwargs):
            # A terminal is a command in its own right: under ask it runs only
            # with the human's yes — unless an approved permission request of
            # this turn is what it runs (goose sends every approved shell
            # command here). terminal/create carries no toolCallId in
            # the schema, so an approval covers ONE terminal, and only one
            # that runs the approved command line (matched on a _meta
            # toolCallId too when the agent sends one) — an approval is never
            # reused for a second command.
            from .approval import ask_human, command_card_text, command_too_long
            line = command_card_text((shlex.join(argv) if args else str(command))
                                     + (f"\n(in {cwd})" if cwd else ""))
            too_long = command_too_long(line)
            if too_long:
                _log(f"acp[{self._s.backend}]: terminal refused — {too_long}")
                raise RequestError.invalid_request({"command": f"refused: {too_long}"})
            chosen = await ask_human(
                title="Run a command", kind="execute",
                raw_input=line, command=True,
                is_cancelled=self._s.stopping,
                log_prefix=f"acp[{self._s.backend}]: ",
            )
            if chosen != "allow_once":
                raise RequestError.invalid_request(
                    {"command": "refused: the user did not approve this command"})
        tid = f"term_{_uuid.uuid4().hex[:12]}"
        term = _CommandTerminal(tid, byte_limit=output_byte_limit)
        env_pairs = {
            str(getattr(e, "name", "") or ""): str(getattr(e, "value", "") or "")
            for e in (env or []) if getattr(e, "name", "")
        }
        try:
            await _spawn_container_terminal(self._s, term, argv, env_pairs, cwd)
        except Exception as exc:  # noqa: BLE001 — a bad command is the
            # agent's error, not a transport failure
            raise RequestError.invalid_params(
                {"command": f"failed to start: {exc}"})
        self._s._terminals[tid] = term
        _log(f"acp[{self._s.backend}]: terminal {tid} ← "
             f"{' '.join(argv)[:120]}")
        return CreateTerminalResponse(terminal_id=tid)

    async def terminal_output(self, session_id: str, terminal_id: str,
                              **kwargs):
        from acp.schema import TerminalExitStatus, TerminalOutputResponse
        term = self._get_terminal(terminal_id)
        status = None
        if term.exited.is_set():
            status = TerminalExitStatus(
                exit_code=term.exit_code, signal=term.signal)
        return TerminalOutputResponse(
            output=term.output_text(), truncated=term.truncated,
            exit_status=status,
        )

    async def wait_for_terminal_exit(self, session_id: str, terminal_id: str,
                                     **kwargs):
        from acp.schema import WaitForTerminalExitResponse
        term = self._get_terminal(terminal_id)
        await term.exited.wait()
        return WaitForTerminalExitResponse(
            exit_code=term.exit_code, signal=term.signal)

    async def kill_terminal(self, session_id: str, terminal_id: str,
                            **kwargs):
        from acp.schema import KillTerminalResponse
        term = self._get_terminal(terminal_id)
        await asyncio.to_thread(term.kill)
        return KillTerminalResponse()

    async def release_terminal(self, session_id: str, terminal_id: str,
                               **kwargs):
        from acp.schema import ReleaseTerminalResponse
        term = self._get_terminal(terminal_id)
        # Release kills a still-running command and invalidates the id for
        # further agent calls. No renderer consumes released output.
        term.released = True
        await asyncio.to_thread(term.kill)
        term.buf.clear()
        return ReleaseTerminalResponse()

    # ── elicitation / extensions ──────────────────────────────────
    async def create_elicitation(self, *a, **k):
        from acp import RequestError
        raise RequestError.method_not_found("elicitation/create")

    async def complete_elicitation(self, *a, **k):
        return None

    async def ext_method(self, method: str, params: dict) -> dict:
        from acp import RequestError
        raise RequestError.method_not_found(method)

    async def ext_notification(self, method: str, params: dict) -> None:
        return None

    def on_connect(self, conn) -> None:
        return None


def probed_capabilities(caps: dict) -> dict:
    """The capabilities an agent's ``initialize`` advertises, as the flat
    facts the session acts on: whether a session can be loaded (the in-run
    reopen), resumed, listed, forked and closed, whether the bridge can ride
    HTTP or SSE MCP (else the stdio proxy), and which prompt content blocks
    it takes. Absent means no."""
    caps = caps if isinstance(caps, dict) else {}
    session = caps.get("sessionCapabilities") or {}
    mcp = caps.get("mcpCapabilities") or {}
    prompt = caps.get("promptCapabilities") or {}
    return {
        "load": bool(caps.get("loadSession")),
        "resume": "resume" in session, "list": "list" in session,
        "fork": "fork" in session, "close": "close" in session,
        "mcp_http": bool(mcp.get("http")), "mcp_sse": bool(mcp.get("sse")),
        "image": bool(prompt.get("image")), "audio": bool(prompt.get("audio")),
        "embedded_context": bool(prompt.get("embeddedContext")),
    }


@functools.lru_cache(maxsize=None)
def _normalize_hook(backend: str):
    """The harness's ``normalize_update`` hook (adapter noise kept out of the
    reply), or ``None``."""
    from .harnesses import HARNESS, resolve
    row = HARNESS.get(backend)
    hook = row.hooks.normalize_update if row else None
    return resolve(hook) if hook else None


def _route_unknown_notifications(conn, client) -> None:
    """Hand a notification the router has no route for to
    ``client.ext_notification`` instead of letting it raise.

    The SDK routes only ``_``-prefixed methods to the extension handler;
    anything else unknown raises method_not_found, which the connection logs
    as an ERROR traceback. qwen-code sends ``qwen/notify/session/mode-update``
    without the extension prefix on every mode change; a notification has no
    reply to carry an error, so it is routed to ``ext_notification``. Wraps
    the pinned SDK's ``Connection._handler``; a layout without it is left
    alone."""
    inner_conn = getattr(conn, "_conn", None)
    inner = getattr(inner_conn, "_handler", None)
    if inner is None:
        return

    async def _handler(method, params, is_notification):
        try:
            return await inner(method, params, is_notification)
        except Exception as e:  # noqa: BLE001 — re-raised unless it is the one case
            if not is_notification or getattr(e, "code", None) != -32601:
                raise
            return await client.ext_notification(
                str(method), params if isinstance(params, dict) else {})

    inner_conn._handler = _handler


# ──────────────────────────────────────────────────────────────────────
# The session.
# ──────────────────────────────────────────────────────────────────────


@dataclass
class _TurnSink:
    """Per-turn accumulation the update handler writes into."""
    emit: Callable[[dict], None]
    stream: bool
    text: list = field(default_factory=list)
    trace: list = field(default_factory=list)
    open_tools: dict = field(default_factory=dict)   # id → name


def _append_trace_chunk(trace: list, ev: dict) -> None:
    """Append one normalized trace event, folding a ``text`` / ``thinking``
    CHUNK into the previous event of the same type.

    The wire streams ``agent_message_chunk`` / ``agent_thought_chunk`` per
    token group; recording one trace event per chunk would make every
    consumer that treats trace events as BLOCKS (the transcript's
    ``thinking`` is the thinking events joined with paragraph breaks) splice
    blank lines mid-sentence — the "odd spacing under Reasoning" a reloaded
    chat showed. One event per contiguous block: a tool call, a plan, or a
    type change opens a new one, matching the live thought-section split."""
    kind = ev.get("type")
    if (kind in ("text", "thinking") and trace
            and trace[-1].get("type") == kind and "text" in ev):
        trace[-1]["text"] = (trace[-1].get("text") or "") + ev["text"]
        return
    trace.append(ev)


@dataclass(frozen=True)
class SpawnProfile:
    """What backend preparation and host-profile owners hand to
    :class:`AcpSession` in a spawn profile: where the
    adapter runs and how to start it, the commands level the harness really
    runs at, and the per-harness facts the session acts on."""
    # "host" (a subprocess on this machine) or "container" (a docker exec in
    # the harness's own container, the exec boundary).
    locus: str
    # The agent's working directory, in the locus's own filesystem.
    cwd: str
    # ``async () -> AdapterProcess``.
    spawn: Callable[[], Any]
    # The commands level the harness runs at: the grant's effective level,
    # or a stricter one the harness forces (``commands_notice`` says why).
    # The session, its record and the client gate all take this one.
    commands: str
    commands_notice: str = ""
    # The container environment of a container-locus profile, so command
    # terminals run in the harness's own container.
    env_obj: Any = None
    # The adapter version, recorded in ``_agent/session.json``.
    adapter_version: str = ""
    # Extra ``_meta`` for session/new and session/load (the claude options
    # seam).
    session_meta: dict = field(default_factory=dict)
    # Whether the system prompt is folded into a new session's first prompt
    # (the harness has no instruction seam the profile could write).
    fold_system_prompt: bool = True
    # The address the run's tool bridge binds (a host locus reaches it on the
    # loopback); None is the bridge's own default.
    bind_host: Optional[str] = None
    # Private native intake binding, after the shared gateway is available and
    # before adapter spawn. A harness may otherwise rely on ACP mcpServers.
    bind_bridge: Optional[Callable[[Any], Any]] = None
    # Refuse permission requests of a kind the host gate does not know (the
    # claude profile: its CLI's built-in tool set grows release to release —
    # see _CollimateClient._host_refusal).
    strict_permission_kinds: bool = False
    # The profile already applied the model through its own env/config seam,
    # so set_config_option is skipped. claude's adapter implements the model
    # option by injecting a `/model` slash command whose caveat-wrapped
    # output makes a small model refuse the NEXT prompt; where a seam exists
    # it wins.
    model_applied: bool = False
    # The value sent to the model config option where the harness's option
    # ids differ from the collimate model id (pi speaks `provider/model`);
    # empty → the session's model. Also what a loaded (or verify_model)
    # session is checked against.
    model_value: str = ""
    # ``(option_value, target) -> bool`` for a harness that reports the model
    # in its own vocabulary (qwen's ``$runtime|openai|<id>(openai)``, agy's
    # blank "agy picks"); without it the check compares literally.
    model_matches: Optional[Callable[[str, str], bool]] = None
    # The model seam can be SILENTLY overridden (cline falls back to its own
    # default for an id missing from its catalog): the model in effect is
    # checked on a NEW session too, so no turn runs — and bills — a model
    # nobody picked.
    verify_model: bool = False
    # ``async (session) -> str`` run right after a successful session/load:
    # a non-empty answer (why the loaded session does not match this leg)
    # drops it for a new session in a fresh adapter.
    verify_loaded: Optional[Callable[["AcpSession"], Any]] = None
    # The adapter's process must build one session only (qwen re-reads the
    # workspace ``.env`` into its process env as each session opens): a new
    # session after a failed session/load opens in a fresh adapter.
    one_session_per_process: bool = False
    # The session mode selected at open (qwen's and goose's mode for the
    # commands level). Applied at the END of start(), inside its
    # BaseException teardown — a cancellation mid-apply tears the adapter
    # down like any other start failure. Also the mode a drift past the grant
    # is switched back to (_hold_mode).
    session_mode: str = ""
    # The commands level rests on that mode (goose's approve under ask): a
    # session left in another mode does not start, and one that drifted out
    # of it and could not be switched back takes no further prompt.
    session_mode_required: bool = False


class AcpSession:
    """One persistent ACP session per conversation ``(backend, session_key)``
    (§2.1's session model).

    Owns the adapter child, the ``ClientSideConnection``, the normalizer sink
    and ``_agent/session.json``. The transport-failure policy (ONE bounded
    respawn + ``session/load`` where advertised, else fail — never a dead
    session parked as alive) lives in ``acp_run.run_acp_turn``."""

    def __init__(
        self, *, backend: str, profile: SpawnProfile,
        mcp_servers_for: Callable[[dict], tuple],  # caps → (servers, warnings)
        auth: str = "api_key", model: str = "",
        file_tools_enabled: bool = True,
        websearch_enabled: bool = True,
        approvals: str = "auto",
    ) -> None:
        self.backend = backend
        self.profile = profile
        self.auth = auth
        self.model = model
        self.file_tools_enabled = file_tools_enabled
        # Whether a ``fetch``-kind permission request may be allowed — the
        # enforcement seam for a websearch:false run on a harness whose web
        # tools have no config off-switch (cline). Harnesses with a real
        # seam (claude disallowedTools, qwen settings exclusions,
        # codex/opencode/goose config) never ask. Nor does
        # antigravity — but for the opposite reason: agy has no off-switch
        # AND Collimate's own shim never sends a permission request (agy's
        # headless protocol has no approval channel), so this gate cannot
        # fire there; its spec hides the toggle (websearch=False) instead.
        self.websearch_enabled = websearch_enabled
        # "auto" answers permission requests from the grant; "ask" surfaces
        # each grant-permitted request as a card the human answers over the
        # surface WebSocket (shared/docs/agent-runtime.md, grants and tools). The grant gate above runs FIRST
        # either way — ask never offers what the grant forbids.
        self.approvals = approvals
        # ``_approved_commands`` holds the ``(tool call id, command line)`` of
        # each APPROVED command request under ask whose terminal/create has
        # not come yet — each covers that one terminal, uncarded; the list
        # lives for one turn.
        # ``_tool_names`` maps tool call ids to the harness's own tool name
        # where an update's _meta carries it (goose), for the recognisers.
        # ``_tool_calls`` keeps what each announced tool call said (kind,
        # title, raw input) and the terminal output its updates' _meta
        # streamed: a later update or permission request for the same id may
        # omit them (codex-acp 2.x updates, vibe's permission requests).
        self._approved_commands: list = []
        self._tool_names: dict = {}
        self._tool_calls: dict = {}
        # ACP command terminals (§2.1 Phase 2): live per-session records,
        # retained after release for the display record.
        self._terminals: dict = {}
        self._mcp_servers_for = mcp_servers_for
        self.acp_session_id = ""
        self.agent_caps: dict = {}
        # What initialize says the agent can do (probed_capabilities); the
        # harness declaration keeps only what no probe reveals.
        self.capabilities: dict = {}
        self.config_options: list = []
        self.adapter: "AdapterProcess | None" = None
        self.conn = None
        # The drift guard's switch-back tasks, held until they finish.
        self._mode_tasks: set = set()
        # Why a session whose commands level rests on its mode drifted out of
        # it for good (SpawnProfile.session_mode_required).
        self._mode_failure = ""
        # The deprecated session-state twins of config_options, absorbed from
        # session/new|load for agents that expose no configOptions (§2.1's
        # modes fallback). The initialize _meta rides along for
        # adapter-specific state.
        self.session_modes: dict = {}
        self.session_models: dict = {}
        self.init_meta: dict = {}
        # True when start() attached to a prior session via session/load —
        # the caller then skips its first-turn system-prompt fold.
        self.loaded = False
        # The MCP servers this leg handed the session (a verify_loaded
        # check's allowance for them).
        self.mcp_server_names: tuple = ()
        # The system prompt the harness last received (seam, fold, or a
        # directive delta) — acp_run._fold_prompt's baseline for telling it
        # what changed since (None until the first turn).
        self.delivered_system = None
        # When the harness last received the wall-clock line (monotonic;
        # 0.0 = never / unknown, e.g. a session/load'ed history whose clock
        # may be days old). acp_run's prompt fold refreshes it only when
        # stale — the line is NOT repeated on every prompt, since every
        # harness stores the prompt text verbatim and shows it back in its
        # own TUI and session export.
        self.clock_at = 0.0
        self._sink: "_TurnSink | None" = None
        self._replaying = False
        self._cancelling = False
        # The session/prompt of a turn cancelled from OUTSIDE (run_agent's
        # Stop watchdog), left pending so its response still lands; the next
        # turn waits on it before sending its own (see _prompt_locked).
        self._abandoned_prompt: "asyncio.Future | None" = None
        self._lock = asyncio.Lock()
        # The loop this session's transport + receive task live on. A session
        # must NEVER be driven from another loop: the connection's futures and
        # tasks are loop-affine, so a cross-loop prompt sends bytes into a
        # connection whose receive loop can no longer run — a silent hang.
        # run_acp_turn checks this and reprovisions (via session/load).
        self.loop = None

    @property
    def locus(self) -> str:
        """Where the adapter runs (``host`` | ``container``) — the profile's."""
        return self.profile.locus

    @property
    def agent_cwd(self) -> str:
        """The agent's working directory — the profile's ``cwd``."""
        return self.profile.cwd

    @property
    def commands(self) -> str:
        """The effective ``commands`` level (deny | ask | allow) the profile
        runs the harness at: how the gate answers a command request
        (acp_policy._is_command_request) and whether terminal/* is served at all."""
        return self.profile.commands

    def stopping(self) -> bool:
        """Whether the turn is being stopped: a session/cancel is in flight,
        or a Stop-abandoned turn's prompt is still outstanding. What such a
        turn still sends — a permission request, a terminal — is refused,
        never answered as if the turn were live."""
        abandoned = self._abandoned_prompt
        return self._cancelling or (abandoned is not None and not abandoned.done())

    # ── update routing ────────────────────────────────────────────
    def _rel_path(self, p: str) -> str:
        """A harness path relativized against the session cwd — tool
        locations and diff blocks arrive cwd-absolute (``/workspace/…`` in a
        container, the host workspace dir for the claude host locus), while
        everything downstream (the renderer's reveal-file chips, the durable
        transcript, an exported cell) speaks cell-relative. A path outside
        the cwd passes through untouched."""
        cwd = (self.agent_cwd or "").rstrip("/")
        if cwd and p.startswith(cwd + "/"):
            return p[len(cwd) + 1:]
        return p

    def _relativize_tool_event(self, ev: dict) -> dict:
        locs = ev.get("locations")
        if isinstance(locs, list) and locs:
            ev = {**ev, "locations": [
                {**loc, "path": self._rel_path(str(loc.get("path") or ""))}
                for loc in locs if isinstance(loc, dict)
            ]}
        blocks = ev.get("content")
        if isinstance(blocks, list) and blocks:
            ev = {**ev, "content": [
                ({**b, "path": self._rel_path(str(b.get("path") or ""))}
                 if isinstance(b, dict) and b.get("type") == "diff" else b)
                for b in blocks
            ]}
        return ev

    def _note_tool_name(self, wire: dict) -> None:
        """Record a tool call's harness-side tool name when its update's
        _meta carries one (goose's ``_meta.goose.toolCall.toolName``) — a
        later permission request for that call may not say it is a shell —
        and what the call announced (:meth:`announced`)."""
        if wire.get("sessionUpdate") not in ("tool_call", "tool_call_update"):
            return
        meta = wire.get("_meta") if isinstance(wire.get("_meta"), dict) else {}
        name = acp_policy._goose_tool_name(meta)
        tid = str(wire.get("toolCallId") or "")
        if not tid:
            return
        if name:
            if len(self._tool_names) >= 256:
                self._tool_names.pop(next(iter(self._tool_names)))
            self._tool_names[tid] = name
        record = self._tool_calls.get(tid)
        if record is None:
            if len(self._tool_calls) >= 256:
                self._tool_calls.pop(next(iter(self._tool_calls)))
            record = self._tool_calls[tid] = {"kind": "", "title": "", "raw_input": None, "output": ""}
        for key, field_name in (("kind", "kind"), ("title", "title"), ("raw_input", "rawInput")):
            if wire.get(field_name) not in (None, ""):
                record[key] = wire[field_name]
        delta = meta.get("terminal_output_delta")
        if isinstance(delta, dict) and isinstance(delta.get("data"), str):
            record["output"] = (record["output"] + delta["data"])[-TERMINAL_OUTPUT_CAP:]

    def announced(self, tool_call):
        """``tool_call`` (a permission request's ToolCallUpdate) with the
        kind, title and raw input its announcement carried filled in where
        the request leaves them out — so the grant gate recognises a command
        whose request names only its id (vibe's)."""
        record = self._tool_calls.get(str(getattr(tool_call, "tool_call_id", "") or ""))
        if record is None:
            return tool_call
        update = {key: record[key] for key in ("kind", "title", "raw_input")
                  if record[key] not in (None, "") and getattr(tool_call, key, None) in (None, "")}
        return tool_call.model_copy(update=update) if update else tool_call

    def _complete_from_announcement(self, wire: dict, norm: dict) -> None:
        """Fill what a finishing ``tool_call_update`` leaves out from its
        announcement: the tool name on its ``tool_result`` (codex-acp 2.x
        sends no title on updates), and the result itself when the update
        carries none but its _meta streamed the command's terminal output."""
        if wire.get("sessionUpdate") != "tool_call_update":
            return
        record = self._tool_calls.get(str(wire.get("toolCallId") or ""))
        if record is None:
            return
        results = [t for t in norm["trace"] if t.get("type") == "tool_result"]
        for result in results:
            if not result.get("tool"):
                result["tool"] = str(record["title"] or "")
        status = _STATUS_MAP.get(str(wire.get("status") or ""), "")
        if not results and status in ("done", "error") and record["output"]:
            from .agent_events import trace_event
            norm["trace"].append(trace_event("tool_result", tool=str(record["title"] or ""),
                                             output=record["output"]))

    def _on_update(self, wire: dict) -> None:
        if self._replaying:
            # session/load replays the whole history BEFORE its response —
            # reconciliation only: no live deltas, no duplicate transcript
            # (both records already persist from the run that produced them).
            return
        self._note_tool_name(wire)
        norm = normalize_update(wire)
        hook = _normalize_hook(self.backend)
        if hook is not None:
            norm = hook(norm)
        self._complete_from_announcement(wire, norm)
        sink = self._sink
        for tr in norm["trace"]:
            if sink is not None:
                _append_trace_chunk(sink.trace, tr)
        for ev in norm["events"]:
            et = ev.get("type")
            if et == "config_option":
                self._merge_config_option(ev.get("update") or {})
                ev = {**ev, "update": {"configOptions": self.ui_config_options()}}
            elif et == "current_mode":
                # Keep BOTH mode-state holders fresh: the modes fallback (the
                # synthesized select's currentValue) AND any real
                # category-"mode" config option — the per-turn re-emit sends
                # ui_config_options(), so a stale currentValue here would
                # overwrite the correctly patched value in every consumer.
                mode_id = str(ev.get("modeId") or "")
                self.session_modes = {**(self.session_modes or {}),
                                      "currentModeId": mode_id}
                if mode_id:
                    self.config_options = [
                        {**o, "currentValue": mode_id}
                        if o.get("category") == "mode" else o
                        for o in self.config_options
                    ]
                self._hold_mode(mode_id)
            if sink is None:
                continue
            if et == "text_delta":
                sink.text.append(ev.get("text") or "")
                if sink.stream:
                    sink.emit(ev)
            elif et == "tool":
                ev = self._relativize_tool_event(ev)
                if ev.get("id"):
                    if ev.get("name"):
                        sink.open_tools[ev["id"]] = ev["name"]
                    elif ev["id"] in sink.open_tools:
                        ev = {**ev, "name": sink.open_tools[ev["id"]]}
                    if ev.get("status") in ("done", "error"):
                        sink.open_tools.pop(ev["id"], None)
                sink.emit(ev)
            else:
                sink.emit(ev)

    def _hold_mode(self, mode_id: str) -> None:
        """The harness moved itself into a mode the grant's ``commands`` level
        rules out (``acp_run.mode_allowed`` — e.g. qwen switching to
        auto-edit on a plan exit): switch it straight back to the profile's
        mode. The option that would do it is never offered or picked
        (request_permission), so this is the second layer."""
        from .acp_policy import mode_allowed

        if (not mode_id or not self.profile.session_mode
                or mode_allowed(self.backend, self.commands, mode_id)):
            return
        _log(f"acp[{self.backend}]: the session entered mode {mode_id!r}, "
             f"beyond this runner's commands permission ({self.commands}) — "
             f"switching back to {self.profile.session_mode!r}")
        try:
            task = asyncio.get_running_loop().create_task(
                self._apply_session_mode_request())
        except RuntimeError:   # no loop: nothing is driving this session
            return
        self._mode_tasks.add(task)
        task.add_done_callback(self._mode_held)

    def _mode_held(self, task) -> None:
        self._mode_tasks.discard(task)
        if task.cancelled() or task.exception() is None:
            return
        _log(f"acp[{self.backend}]: {task.exception()}")
        if self.profile.session_mode_required:
            self._mode_failure = str(task.exception())

    def _merge_config_option(self, upd: dict) -> None:
        # The wire payload is the FULL set of options (ConfigOptionUpdate's one
        # field, `configOptions`), not a single delta — so this is a wholesale
        # REPLACE, not a by-id splice: an option the agent withdraws must
        # disappear. (An earlier draft read a singular `configOption`/`option`
        # key that no ACP agent ever emits.)
        opts = upd.get("configOptions")
        if isinstance(opts, list):
            self.config_options = [o for o in opts if isinstance(o, dict)]

    # ── lifecycle ─────────────────────────────────────────────────
    async def start(self, *, load_acp_session_id: str = "") -> None:
        """Spawn, initialize, and mint (or load) the session.

        Any failure after the adapter is spawned (a first-contact
        ``-32000 auth required``, an initialize/new_session timeout, a torn
        transport) tears the child down before re-raising — the session is
        never in the run's session cache until ``start`` returns, so a
        half-started adapter is unreachable by every reaper and would leak its
        process tree plus a spinning receive task."""
        try:
            await self._start_inner(load_acp_session_id=load_acp_session_id)
        except BaseException:
            with contextlib.suppress(Exception):
                if self.conn is not None:
                    await asyncio.wait_for(
                        asyncio.shield(self.conn.close()), timeout=5)
            await self._discard_off_loop()
            raise

    async def _start_inner(self, *, load_acp_session_id: str = "") -> None:
        servers = await self._connect()
        loaded = False
        if load_acp_session_id and self.agent_caps.get("loadSession"):
            loaded = await self._load_session(load_acp_session_id, servers)
            reason = ""
            if loaded and self.profile.verify_loaded is not None:
                try:
                    reason = await self.profile.verify_loaded(self)
                except Exception as e:  # noqa: BLE001 — unverifiable is a no
                    reason = f"its tools could not be checked ({e})"
            if reason:
                _log(f"acp[{self.backend}]: not resuming session "
                     f"{load_acp_session_id} — {reason}; starting a new session")
                loaded = False
            if not loaded and (reason or self.profile.one_session_per_process):
                await self.close()
                servers = await self._connect()
        if not loaded:
            resp = await asyncio.wait_for(self.conn.new_session(
                cwd=self.agent_cwd, mcp_servers=servers, **self.profile.session_meta,
            ), timeout=120)
            self.acp_session_id = str(resp.session_id)
            self._absorb_session_response(resp)
        self.loaded = loaded
        await self._write_session_file()
        await self._apply_model()
        await self._apply_session_mode_request()

    async def _load_session(self, acp_session_id: str, servers: list) -> bool:
        """``session/load`` of ``acp_session_id``; False (logged) when it fails."""
        self._replaying = True
        try:
            resp = await asyncio.wait_for(self.conn.load_session(
                cwd=self.agent_cwd, session_id=acp_session_id,
                mcp_servers=servers, **self.profile.session_meta,
            ), timeout=120)
            self.acp_session_id = acp_session_id
            self._absorb_session_response(resp)
            _log(f"acp[{self.backend}]: session {acp_session_id} loaded")
            return True
        except Exception as e:  # noqa: BLE001
            _log(
                f"acp[{self.backend}]: session/load failed ({e}) — "
                "new session; previous history is display-only"
            )
            return False
        finally:
            self._replaying = False

    async def _connect(self) -> list:
        """Spawn the adapter, connect and initialize it; returns the MCP
        servers this leg hands its session."""
        from acp import connect_to_agent
        from acp.schema import ClientCapabilities, FileSystemCapabilities

        self.loop = asyncio.get_running_loop()
        self.adapter = await self.profile.spawn()
        client = _CollimateClient(self)
        self.conn = connect_to_agent(client, self.adapter.transport)
        _route_unknown_notifications(self.conn, client)
        init = await asyncio.wait_for(self.conn.initialize(
            protocol_version=_PROTOCOL_VERSION,
            client_capabilities=ClientCapabilities(
                fs=FileSystemCapabilities(
                    read_text_file=True, write_text_file=True),
                # Command terminals run where the harness's exec surface
                # already lives: a container locus (its own container), never
                # the host locus (§2.3). Without them agents degrade
                # gracefully by running commands internally. THE SAME
                # predicate gates the terminal/* handlers and the execute
                # permission gate (advertise ⇔ serve, by construction).
                terminal=self.advertises_terminal(),
            ),
        ), timeout=60)
        caps = {}
        try:
            caps = init.agent_capabilities.model_dump(
                by_alias=True, exclude_none=True)
        except Exception:  # noqa: BLE001
            pass
        self.agent_caps = caps
        self.capabilities = probed_capabilities(caps)
        _log(f"acp[{self.backend}]: capabilities "
             + ", ".join(f"{k}={'yes' if v else 'no'}" for k, v in self.capabilities.items()))
        # The initialize _meta, verbatim — adapter-specific state lives here
        # (the pinned SDK's session responses predate the top-level `models`
        # field, so this is the one client-side source for it).
        try:
            self.init_meta = dict(init.field_meta or {})
        except Exception:  # noqa: BLE001
            self.init_meta = {}
        servers, warnings = self._mcp_servers_for(caps)
        for w in warnings:
            _log(f"acp[{self.backend}]: {w}")
        self.mcp_server_names = tuple(
            str(getattr(srv, "name", "") or "") for srv in servers)
        return servers

    async def _apply_session_mode_request(self) -> None:
        """Select the profile-requested session mode through the session's
        own mode select — a no-op when none was requested or the agent
        offers no such mode, best-effort on an agent-side refusal (the run
        proceeds in the agent's default mode). Runs inside start()'s
        teardown scope: see ``SpawnProfile.session_mode``."""
        mode_id = self.profile.session_mode
        if not mode_id:
            return
        opt = next((o for o in self.ui_config_options()
                    if o.get("category") == "mode"), None)
        values = {str(v.get("value")) for v in ((opt or {}).get("options") or [])
                  if isinstance(v, dict)}
        if opt is not None and mode_id in values and str(opt.get("currentValue") or "") != mode_id:
            try:
                await self.apply_config_change(str(opt.get("id")), mode_id)
            except Exception as e:  # noqa: BLE001 — CancelledError (a
                # BaseException) passes through to start()'s teardown guard.
                _log(f"acp[{self.backend}]: could not select mode "
                     f"{mode_id!r} ({e}) — the agent's default mode stands")
        if self.profile.session_mode_required:
            opt = next((o for o in self.ui_config_options()
                        if o.get("category") == "mode"), None)
            current = str((opt or {}).get("currentValue") or "")
            if current != mode_id:
                raise RuntimeError(
                    f"{self.backend} could not be put in its {mode_id!r} mode (it "
                    f"reports {current or 'no mode'!r}), which this runner's "
                    f"commands permission needs")

    def _absorb_session_response(self, resp) -> None:
        try:
            opts = getattr(resp, "config_options", None) or []
            self.config_options = [
                o.model_dump(by_alias=True, exclude_none=True) for o in opts
            ]
        except Exception:  # noqa: BLE001
            self.config_options = []
        # The deprecated modes/models fields — kept as the FALLBACK state for
        # agents that expose no configOptions (the spec says clients SHOULD
        # prefer configOptions where present).
        for attr, name in (("session_modes", "modes"),
                            ("session_models", "models")):
            try:
                val = getattr(resp, name, None)
                setattr(self, attr,
                        val.model_dump(by_alias=True, exclude_none=True)
                        if val is not None else {})
            except Exception:  # noqa: BLE001
                setattr(self, attr, {})

    def advertises_terminal(self) -> bool:
        """Whether initialize advertises the client ``terminal`` capability —
        the SAME predicate the terminal/* handlers gate on
        (:meth:`_CollimateClient._terminals_enabled` delegates here), so the
        advertisement can never drift from what the client actually serves.
        Served only where the harness's exec surface lives (its container)
        and only when the grant's ``commands`` level is not deny."""
        return self.locus == "container" and self.commands != "deny"

    async def _write_session_file(self) -> None:
        # The record serves THIS run only — an idle park's re-provision, a
        # transport respawn and any other in-run session/load reopen
        # the session it names — so it is a run LOG artifact: read_file /
        # has_file see it for the rest of the run, and it never hydrates into
        # a later run (every run starts a new session). It carries only the
        # auth METHOD name, never a credential, and no commands level: the
        # agent can write it, so the level the reopen decision reads is the
        # host-side record (harness_homes.record_resume_level). Off the loop: it takes the
        # state dir's flock, which a store copy-back can hold for a while.
        await asyncio.to_thread(harness_homes.record_resume_level, self.backend,
                                self.acp_session_id, self.commands)
        try:
            write_session_file(
                self.backend, self.acp_session_id, auth=self.auth,
                cwd=self.agent_cwd,
                adapter_version=self.profile.adapter_version)
        except Exception:  # noqa: BLE001
            pass

    async def _apply_model(self) -> None:
        """Model selection: ``set_config_option`` against the
        ``category: "model"`` option — but only for a harness whose spawn
        profile did NOT already apply the model through its own env/config
        seam (``model_applied``). Where the seam exists it is the primary
        path: it is side-effect-free, while at least claude's adapter
        implements the option as an injected ``/model`` command whose
        caveat-wrapped output poisons the next prompt for small models."""
        # A profile with no seam may translate the model id into the
        # harness's own option vocabulary (pi's `provider/model` ids).
        target = self.profile.model_value or self.model
        if not target:
            return

        def _matches(value) -> bool:
            value = str(value or "")
            if value == target:
                return True
            fn = self.profile.model_matches
            return bool(fn and fn(value, target))

        # More than one option can carry category "model" (cline pairs a
        # provider select with the model select) — prefer the literal
        # id "model", then fall back to the first of the category.
        model_opts = [o for o in self.config_options
                      if o.get("category") == "model"]
        opt = next((o for o in model_opts if str(o.get("id")) == "model"),
                   model_opts[0] if model_opts else None)
        if self.profile.model_applied and (
                (not self.loaded and not self.profile.verify_model) or not opt
                or _matches(opt.get("currentValue"))):
            return
        if not opt:
            raise ValueError(f"acp[{self.backend}]: cannot apply selected model {target!r}; the harness exposes no model control")
        if _matches(opt.get("currentValue")):
            return
        # The option value to send: the target itself, or the value the
        # profile's matcher recognises as it (qwen lists `<id>(<authType>)`).
        pick = next((str(v.get("value")) for v in (opt.get("options") or [])
                     if isinstance(v, dict) and _matches(v.get("value"))),
                    None)
        if pick is None:
            if self.profile.verify_model:
                raise ValueError(
                    f"acp[{self.backend}]: selected model {target!r} is not in "
                    f"{self.backend}'s model catalog (it would silently run "
                    f"{str(opt.get('currentValue') or '')!r}); pick a model "
                    f"{self.backend} lists")
            raise ValueError(f"acp[{self.backend}]: selected model {target!r} is not offered by this harness")
        try:
            await asyncio.wait_for(self.conn.set_config_option(
                config_id=str(opt.get("id")), session_id=self.acp_session_id,
                value=pick,
            ), timeout=30)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"acp[{self.backend}]: could not apply selected model {target!r}: {e}") from e

    # ── session config options (shared/docs/agent-runtime.md, grants and tools's Phase-4 UI) ────────────
    # The synthetic id for the mode select synthesized from the deprecated
    # `modes` session field when an agent exposes no category-"mode" config
    # option — apply routes it to session/set_mode instead of
    # set_config_option.
    MODE_FALLBACK_ID = "__mode__"

    def ui_config_options(self) -> list:
        """The config options the surface UI renders: the agent's
        mode-category ``configOptions`` only (model and other options are
        fixed by the runner), plus a mode select synthesized from the
        deprecated ``modes`` field where the agent exposes no mode-category
        option (the spec's own fallback ordering — configOptions are
        preferred, modes will be removed)."""
        opts = [o for o in self.config_options if o.get("category") == "mode"]
        modes = self.session_modes or {}
        avail = [m for m in (modes.get("availableModes") or [])
                 if isinstance(m, dict) and m.get("id")]
        if not opts and avail:
            opts.insert(0, {
                "id": self.MODE_FALLBACK_ID, "name": "Mode",
                "category": "mode", "type": "select",
                "currentValue": str(modes.get("currentModeId") or ""),
                "options": [
                    {"value": str(m["id"]), "name": str(m.get("name") or m["id"]),
                     **({"description": str(m["description"])}
                        if m.get("description") else {})}
                    for m in avail
                ],
            })
        return opts

    async def apply_config_change(self, config_id: str, value) -> list:
        """Apply one UI-originated config change — ``session/set_mode`` for
        the synthesized mode select, ``session/set_config_option``
        otherwise — update the local state, and return the refreshed
        :meth:`ui_config_options`. Raises on a dead/never-opened session or
        a refused change (the caller reports it)."""
        if self.conn is None or not self.acp_session_id:
            raise RuntimeError("no live ACP session")
        cid = str(config_id or "")
        if not any(str(o.get("id")) == cid for o in self.ui_config_options()):
            raise PermissionError("Only session-mode controls may change during a run; edit the runner/account for model or other configuration changes")
        if cid == self.MODE_FALLBACK_ID:
            await asyncio.wait_for(self.conn.set_session_mode(
                mode_id=str(value), session_id=self.acp_session_id,
            ), timeout=30)
            self.session_modes = {**(self.session_modes or {}),
                                  "currentModeId": str(value)}
            return self.ui_config_options()
        resp = await asyncio.wait_for(self.conn.set_config_option(
            config_id=cid, session_id=self.acp_session_id, value=value,
        ), timeout=30)
        # The response carries the refreshed full option set (the same
        # wholesale-replace semantics as config_option_update); absorb it
        # when present — an agent may instead follow up with the update
        # notification, which _merge_config_option handles.
        refreshed: list = []
        try:
            opts = getattr(resp, "config_options", None) or []
            refreshed = [
                o.model_dump(by_alias=True, exclude_none=True) for o in opts
            ]
        except Exception:  # noqa: BLE001
            refreshed = []
        if refreshed:
            self.config_options = refreshed
        else:
            # The agent accepted the change but returned no set (and may
            # never send the update notification): reflect the value we
            # KNOW is in effect, or the per-turn re-emit would show the old
            # one — a successful switch rendered as a revert.
            self.config_options = [
                {**o, "currentValue": value} if str(o.get("id")) == cid else o
                for o in self.config_options
            ]
        return self.ui_config_options()

    # ── the turn ──────────────────────────────────────────────────
    async def prompt(
        self, text: str, *, emit: Callable[[dict], None], stream: bool,
        should_stop: "Callable[[], bool] | None" = None,
        attachments: "list | None" = None,
    ) -> tuple:
        """One prompt turn. Returns ``(reply_text, trace, stop_reason)``.
        Single-flight per session (the protocol's own rule): the lock enforces
        it; the turn queue upstream serializes callers, and
        ``acp_run.run_acp_turn`` is the only caller."""
        async with self._lock:
            return await self._prompt_locked(
                text, emit=emit, stream=stream, should_stop=should_stop,
                attachments=attachments,
            )

    async def _prompt_locked(self, text, *, emit, stream, should_stop,
                             attachments) -> tuple:
        from .agent_events import TurnRefused, service_refusal, trace_event

        if self._mode_failure:
            raise RuntimeError(self._mode_failure)
        await self._settle_abandoned_prompt()
        sink = _TurnSink(emit=emit, stream=stream)
        sink.trace.append(trace_event("user", text=text))
        self._sink = sink
        self._cancelling = False
        blocks = self._prompt_blocks(text, attachments)
        task = None
        try:
            task = asyncio.ensure_future(
                self.conn.prompt(session_id=self.acp_session_id, prompt=blocks)
            )
            cancel_sent_at = None
            while True:
                done, _ = await asyncio.wait({task}, timeout=0.25)
                if done:
                    break
                if cancel_sent_at is None and should_stop and should_stop():
                    self._cancelling = True
                    self._approved_commands.clear()
                    cancel_sent_at = time.monotonic()
                    with contextlib.suppress(Exception):
                        await self.conn.cancel(session_id=self.acp_session_id)
                elif (cancel_sent_at is not None
                      and time.monotonic() - cancel_sent_at > _CANCEL_GRACE_S):
                    # The agent ignored session/cancel — return the partial
                    # now. The finally block parks the still-pending request
                    # as ``_abandoned_prompt`` (no second cancel: _cancelling
                    # is set), and the next turn's _settle_abandoned_prompt
                    # bounds the wait for it.
                    self._mark_open_tools_cancelled(sink)
                    sink.trace.append(trace_event(
                        "system_note", text="turn interrupted by user"))
                    return "".join(sink.text), sink.trace, "cancelled"
            try:
                resp = task.result()
            except Exception as e:
                raise self._transport_error(e) from e
            stop_reason = str(getattr(resp, "stop_reason", "") or "")
            if stop_reason == "cancelled":
                self._mark_open_tools_cancelled(sink)
                sink.trace.append(trace_event(
                    "system_note", text="turn interrupted by user"))
            elif stop_reason and stop_reason != "end_turn":
                sink.trace.append(trace_event(
                    "system_note", text=f"turn ended: {stop_reason}"))
            reply = "".join(sink.text)
            refusal = "" if stop_reason == "cancelled" else service_refusal(reply)
            if refusal:
                # The service's 4xx error object relayed as the reply text
                # (codex): no answer, and every later turn gets the same.
                if stream:
                    emit({"type": "text_reset", "reason": refusal})
                raise TurnRefused(refusal)
            if not stream and reply:
                emit({"type": "text_delta", "text": reply})
            return reply, sink.trace, stop_reason
        finally:
            # If the coroutine is cancelled from OUTSIDE — run_agent's watchdog
            # hard-cancels this turn ~0.5s after Stop, well before the 8s
            # graceful path — the adapter is still inside the old turn: its
            # late session/update events must not reach the NEXT turn's sink,
            # and a second session/prompt before it answers the first would
            # violate ACP single-flight. Detach the sink NOW (updates with no
            # sink are dropped), close the turn's open tools client-side,
            # best-effort tell the agent to stop, and leave the request
            # pending as ``_abandoned_prompt`` — the next turn waits for its
            # answer (bounded) before sending its own. Dropping the local
            # future instead would only hide the answer, not the adapter's
            # still-running turn.
            self._sink = None
            if task is not None and not task.done():
                self._mark_open_tools_cancelled(sink)
                needs_cancel = not self._cancelling
                self._abandoned_prompt = task
                # Retrieve its outcome whatever it is, so a transport error
                # on a turn nobody awaits any more never logs as unretrieved.
                task.add_done_callback(
                    lambda t: t.cancelled() or t.exception())
                if needs_cancel:
                    with contextlib.suppress(BaseException):
                        await asyncio.shield(
                            self.conn.cancel(session_id=self.acp_session_id))
            self._approved_commands.clear()
            self._cancelling = False

    async def _settle_abandoned_prompt(self) -> None:
        """Wait (up to the cancel grace) for a Stop-abandoned turn's
        session/prompt to answer before a new one is sent; an adapter that
        ignores session/cancel past the grace gets its request dropped
        locally. The request stays recorded while it is waited on, so the
        old turn is still :meth:`stopping` meanwhile — and stays recorded if
        this wait is itself stopped, for whichever turn comes next."""
        task = self._abandoned_prompt
        if task is not None and not task.done():
            await asyncio.wait({task}, timeout=_CANCEL_GRACE_S)
            if not task.done():
                task.cancel()
        self._abandoned_prompt = None

    def _prompt_blocks(self, text: str, attachments) -> list:
        """The prompt content blocks: the text, plus inline image blocks where
        the agent's promptCapabilities allow — other media was already staged
        into the workspace by the caller (referenced in the text)."""
        from acp.schema import ImageContentBlock, TextContentBlock
        import base64

        blocks: list = [TextContentBlock(type="text", text=text)]
        caps = (self.agent_caps.get("promptCapabilities") or {})
        if attachments and caps.get("image"):
            from .agent_events import iter_attachments
            for data, mt, _name in iter_attachments(attachments):
                if not mt.startswith("image/"):
                    continue
                blocks.append(ImageContentBlock(
                    type="image", mime_type=mt,
                    data=base64.b64encode(data).decode("ascii"),
                ))
        return blocks

    def _mark_open_tools_cancelled(self, sink: _TurnSink) -> None:
        """v1 has no ``cancelled`` tool status — the CLIENT marks non-finished
        tool calls cancelled on ``session/cancel``."""
        for tid, name in list(sink.open_tools.items()):
            sink.emit({"type": "tool", "id": tid, "name": name,
                       "status": "error", "result": "(cancelled)"})
        sink.open_tools.clear()

    def _transport_error(self, exc: Exception) -> Exception:
        """Classify a failed prompt: a dead transport becomes a named error
        the caller's respawn policy can act on."""
        if isinstance(exc, (ConnectionError, EOFError, BrokenPipeError)):
            return AcpTransportDied(str(exc))
        proc = getattr(self.adapter, "proc", None)
        if proc is not None and proc.returncode is not None:
            return AcpTransportDied(
                f"adapter exited with code {proc.returncode}")
        return exc

    async def close(self) -> None:
        if self.loop is not None and self.loop is not asyncio.get_running_loop():
            # The session's asyncio objects belong to another loop — touching
            # them from here would corrupt that loop's state. Kill the child
            # directly; its transports die with it.
            await self._discard_off_loop()
            return
        await asyncio.gather(
            *(asyncio.to_thread(t.kill) for t in list(self._terminals.values())),
            return_exceptions=True)
        with contextlib.suppress(Exception):
            if self.conn is not None:
                await asyncio.wait_for(self.conn.close(), timeout=5)
        if self.adapter is not None:
            try:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(self.adapter.kill)
            finally:
                with contextlib.suppress(Exception):
                    if self.adapter.dispose is not None:
                        self.adapter.dispose()
        self.conn = None
        self.adapter = None

    async def _discard_off_loop(self) -> None:
        """:meth:`discard` from a coroutine, run in a worker thread: the host
        kill polls a returncode only the loop can set, and the container kill
        is a blocking exec, so neither may run on the loop thread."""
        try:
            await asyncio.shield(asyncio.to_thread(self.discard))
        except asyncio.CancelledError:
            pass            # the shielded kill carries on in its thread
        except Exception:  # noqa: BLE001 — executor already shut down
            self.discard()

    def discard(self) -> None:
        """Sync, loop-free teardown: kill the adapter child and drop the
        references. For teardown paths that cannot (or must not) run on the
        session's own loop — atexit, and the cross-loop reprovision."""
        for term in list(self._terminals.values()):
            term.kill()
        if self.adapter is not None:
            try:
                with contextlib.suppress(Exception):
                    self.adapter.kill()
            finally:
                with contextlib.suppress(Exception):
                    if self.adapter.dispose is not None:
                        self.adapter.dispose()
        self.conn = None
        self.adapter = None


class AcpTransportDied(RuntimeError):
    """The adapter's transport failed mid-run (EOF / exit / torn pipe)."""


def resumable_acp_session_id(record: dict, *, backend: str, commands: str) -> str:
    """The session id in a ``_agent/session.json`` record that a ``backend``
    leg at ``commands`` may load or resume, or "".

    The loosest level the session may have run under comes from this cell's
    host-side record (:func:`harness_homes.record_resume_level`), never from ``record``,
    which the agent can write. A session recorded looser than this leg (or
    one the record does not know, which counts as the loosest) is not
    resumed: a harness may restore what the session was opened with rather
    than what this leg sets — goose brings back the session's stored
    extensions on ``session/load`` and ``--resume``, shell included."""
    from collimate.permissions import COMMAND_LEVELS

    sid = str(record.get("acp_session_id") or "")
    if record.get("backend") != backend or not sid:
        return ""
    stored = harness_homes._resume_levels(backend).get(sid)
    # Unrecorded counts as the loosest; an unknown leg level as the strictest.
    stored_rank = harness_homes._level_rank(stored, unknown=len(COMMAND_LEVELS) - 1)
    current_rank = harness_homes._level_rank(commands, unknown=0)
    if stored_rank > current_rank:
        _log(f"acp[{backend}]: not resuming session {sid} — it may have run "
             f"under commands: {stored or 'unrecorded'}, looser than this "
             f"leg's {COMMAND_LEVELS[current_rank]}; starting a new session")
        return ""
    return sid


def write_session_file(backend: str, acp_session_id: str, *, auth: str,
                       cwd: str, adapter_version: str = "") -> None:
    """Write this run's ``_agent/session.json`` naming ``acp_session_id`` — the
    record an in-run reopen's ``session/load`` keys off (a run LOG artifact:
    it serves this run only). Raises when it cannot be written. The commands
    level the reopen decision reads is not in it (the agent can write it):
    the caller records that host-side first (:func:`harness_homes.record_resume_level`)."""
    _write_log(SESSION_FILE, json.dumps({
        "backend": backend,
        "acp_session_id": acp_session_id,
        "adapter_version": adapter_version,
        "cwd": cwd,
        "auth": auth,
    }, indent=2) + "\n", category="log")


def read_session_file() -> dict:
    """This run's ``_agent/session.json`` (or ``{}``) — the record an in-run
    reopen's ``session/load`` keys off."""
    try:
        if gen.has_file(SESSION_FILE):
            v = gen.read_file(SESSION_FILE)
            if isinstance(v, bytes):
                v = v.decode("utf-8", errors="replace")
            data = json.loads(v)
            return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        pass
    return {}
