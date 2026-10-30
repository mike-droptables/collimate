"""Run-owned terminal sessions, independent of websocket routes.

A session runs its command in one of the run's containers (``docker exec``)
or, for a host session, directly on this machine: the IPC op that registers
it checked the run's grant and placement (``process_manager/ipc.py``). The
PTY child leads its own process group either way; teardown signals the
group. A host session's child carries :data:`HOST_TERMINAL_LABEL`, so a
server that restarts after being killed can find and end what it left
(``startup_cleanup.reap_orphan_host_terminals``).
"""
from __future__ import annotations
import asyncio
import secrets
import uuid
from dataclasses import dataclass
from typing import Optional
import os
import re
import signal
from process_signals import signal_process_group

KEEP_ALIVE_RING_CAP = 262_144

#: Where a session's command runs.
LOCI = ("container", "host")

#: The environment variable a host session's child carries:
#: ``<server instance>:<server pid>:<run id>``.
HOST_TERMINAL_LABEL = "COLLIMATE_HOST_TERMINAL"

@dataclass
class TerminalSession:
    terminal_id: str
    # The run that registered the PTY — its lifetime, liveness and ownership
    # checks are by run (several runs may serve terminals on one cell).
    run_id: str
    cell_id: str
    # The run's container, or None for a host session.
    container_id: Optional[str]
    command: list[str]
    cwd: Optional[str]
    env: dict
    token: str
    # ``container`` (docker exec in container_id) or ``host`` (this machine).
    locus: str = "container"
    # Detach-tolerant PTY (agent-v2 §4.3.5): with keep_alive the PTY (and
    # the program in it) SURVIVES a WebSocket detach — output drains into a
    # bounded ring that replays on re-attach. Without it (the default), a
    # detach kills the process, as always. Either way run-teardown kills.
    keep_alive: bool = False
    proc: Optional[asyncio.subprocess.Process] = None
    master_fd: Optional[int] = None
    # True from the moment the attach check passes (set before the accept
    # await). Prevents a second concurrent client from attaching to the
    # same session even before the PTY has actually spawned.
    attached: bool = False
    # A keep_alive session's bounded output ring, which a re-attach replays,
    # and whether the program already ended: set when the bridge sees it
    # exit, cleared when a plain session's re-attach spawns it afresh.
    ring: bytearray = None  # type: ignore[assignment]
    ended: bool = False
    # A PTY was spawned at least once. With ``proc`` back to None it tells
    # "killed" (a plain session's detach, see kill_session) apart from
    # "waiting for the first attach" — only the latter is a live surface.
    spawned: bool = False

    def __post_init__(self):
        if self.ring is None:
            self.ring = bytearray()

    def feed_ring(self, chunk: bytes) -> None:
        self.ring += chunk
        if len(self.ring) > KEEP_ALIVE_RING_CAP:
            del self.ring[: len(self.ring) - KEEP_ALIVE_RING_CAP]

_sessions: dict[str, TerminalSession] = {}

_CONTAINER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

def register_session(
    run_id: str,
    cell_id: str,
    container_id: Optional[str],
    command: list[str],
    cwd: Optional[str] = None,
    env: Optional[dict] = None,
    keep_alive: bool = False,
    locus: str = "container",
) -> dict:
    """Allocate a session id + auth token and stash the spawn parameters.

    A ``container`` session's ``container_id`` is the id of a container the
    caller already resolved as the run's own (``IPCMixin.owned_container``);
    this registry checks only its shape. A ``host`` session has none: its
    ``command`` runs on this machine with ``env`` as its whole environment
    (the label aside) in ``cwd``, a directory the caller resolved inside
    the run's workspace.

    ``keep_alive`` opts into the detach-tolerant PTY: a renderer disconnect
    (a tab reload, a closed cell) leaves the process running (output rings
    for replay on re-attach) instead of killing it. Returns a
    dict suitable for writing straight into the generator's `.terminal` file
    plus the token the IPC ack passes back to the SDK.
    """
    if locus not in LOCI:
        raise ValueError(f"locus must be one of {', '.join(LOCI)}")
    if locus == "host":
        if container_id is not None:
            raise ValueError("a host session runs in no container")
        if not (isinstance(cwd, str) and os.path.isabs(cwd) and os.path.isdir(cwd)):
            raise ValueError("a host session's cwd must be an existing absolute directory")
    else:
        if not container_id:
            raise ValueError("container_id is required")
        if not isinstance(container_id, str) or not _CONTAINER_ID.fullmatch(container_id):
            raise ValueError("container_id must be a container name or id")
        if cwd is not None and not (isinstance(cwd, str) and cwd.startswith("/")):
            raise ValueError("cwd must be an absolute path in the container")
    if not command or not all(isinstance(part, str) and part and "\0" not in part for part in command):
        raise ValueError("command must be a non-empty list of strings")
    for name, value in (env or {}).items():
        if not isinstance(name, str) or not _ENV_NAME.fullmatch(name) or name == HOST_TERMINAL_LABEL:
            raise ValueError(f"invalid environment variable name: {name!r}")
        if not isinstance(value, str) or "\0" in value:
            raise ValueError(f"environment variable {name} must be a string")

    terminal_id = f"tmnl_{uuid.uuid4().hex[:16]}"
    token = secrets.token_urlsafe(24)
    session = TerminalSession(
        terminal_id=terminal_id,
        run_id=run_id,
        cell_id=cell_id,
        container_id=container_id,
        command=list(command),
        cwd=cwd,
        env=dict(env or {}),
        token=token,
        keep_alive=keep_alive,
        locus=locus,
    )
    _sessions[terminal_id] = session
    return {
        "terminal_id": terminal_id,
        "ws_path": f"/api/terminals/{terminal_id}",
        "token": token,
    }

def session_status(terminal_id: str, *, run_id: str) -> dict:
    """The generator-facing liveness of a PTY session (the ``terminal_status``
    IPC op behind ``gen.terminal_status``): ``exists`` (still registered),
    ``ended`` (the program exited — recorded by the bridge, or visible on
    the process itself), and the ``exit_code`` when known. A generator serving
    a terminal leg polls this to notice its program died instead of parking
    on a dead PTY. Another run's session reads as not existing."""
    session = _sessions.get(terminal_id)
    if session is None or session.run_id != run_id:
        return {"exists": False, "ended": True, "exit_code": None}
    proc = session.proc
    code = proc.returncode if proc is not None else None
    ended = bool(session.ended) or (proc is not None and code is not None)
    return {"exists": True, "ended": ended, "exit_code": code}

def has_live_session(run_id: str) -> bool:
    """True while the run has a registered PTY whose program is still
    running, or that waits for its first attach — a user-driven surface the
    run's wallclock watchdog must not charge (the user, not the generator,
    decides how long it stays up). A plain session killed by its detach
    stays registered until the generator drops it, but nothing runs in it,
    so it no longer shields the run from the watchdog. Another run's PTY on
    the same cell never shields this one."""
    for session in list(_sessions.values()):
        if session.run_id != run_id or session.ended:
            continue
        proc = session.proc
        if proc is None:
            if session.spawned:
                continue
        elif proc.returncode is not None:
            continue
        return True
    return False

def unregister_session(terminal_id: str, *, run_id: str | None = None) -> None:
    """Drop a session from the registry and tear down its PTY if live. With
    ``run_id`` (a generator's destroy op) only that run's own session."""
    session = _sessions.get(terminal_id)
    if session is None or (run_id is not None and session.run_id != run_id):
        return
    _sessions.pop(terminal_id, None)
    kill_session(session)

def unregister_sessions_for_run(run_id: str) -> None:
    """Drop every session of a run. Called from the run-cleanup path in
    process_manager so stale sessions don't leak when a generator exits
    without calling stop_terminal; the cell's other runs keep theirs."""
    for sid, sess in list(_sessions.items()):
        if sess.run_id == run_id:
            unregister_session(sid)

def kill_session(session: TerminalSession) -> None:
    proc = session.proc
    if proc is not None:
        # The PTY child was spawned with start_new_session=True, so it
        # leads its own process group — signal the group (the docker-exec
        # client, or a host CLI and every child it started in it), and
        # escalate to SIGKILL if SIGTERM is ignored (nothing else ever
        # revisits this process).
        # A host session's child made its own session (setsid), so its pid
        # IS its group: signal that group by number even once the leader has
        # exited, for the CLI's own children that outlive it.
        host = session.locus == "host"

        def _signal(force: bool) -> None:
            sig = signal.SIGKILL if force else signal.SIGTERM
            signal_process_group(proc.pid, sig, known_group=host)

        _signal(force=False)

        def _escalate() -> None:
            if proc.returncode is None or host:
                _signal(force=True)

        try:
            asyncio.get_running_loop().call_later(5.0, _escalate)
        except RuntimeError:
            # No running loop (interpreter teardown) — nothing left to
            # escalate later, so don't leave survival to chance.
            _escalate()
    if session.master_fd is not None:
        # Deregister any reader or writer still bound to the fd (the
        # detached-drain reader of a keep_alive session, a live attach's
        # reader and stdin writer) BEFORE closing it: closing a registered fd
        # strips it from epoll but not from the loop's selector map, and the
        # next session whose PTY recycles the fd number would register a
        # reader that never fires.
        try:
            loop = asyncio.get_running_loop()
            loop.remove_reader(session.master_fd)
            loop.remove_writer(session.master_fd)
        except (RuntimeError, ValueError, OSError):
            pass
        try:
            os.close(session.master_fd)
        except OSError:
            pass
    session.proc = None
    session.master_fd = None
    session.attached = False

def shutdown_all_sessions() -> int:
    """Tear down every registered terminal session. Called from
    stop_all_runs so live PTY clients — their own session leaders, immune
    to the server's exit — can't outlive server shutdown. Returns the
    number of sessions dropped."""
    sids = list(_sessions)
    for sid in sids:
        unregister_session(sid)
    return len(sids)


def get_terminal_session(session_id: str) -> Optional[TerminalSession]:
    """Look up the registered session for a websocket attach."""
    return _sessions.get(session_id)
