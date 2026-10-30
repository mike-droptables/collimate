# SPDX-License-Identifier: MIT
"""Internal host-protocol bridge for generators.

This module is **not user-facing**. Generators consume the singleton
:mod:`collimate.native` (re-exported as ``import gen``); ``native``
internally delegates IPC + filesystem + lifecycle to a single
:class:`HostBridge` instance constructed by :func:`gen.main`.

The bridge owns:

  * the localhost TCP IPC channel (line-delimited JSON, with
    request/ack and NAK frames);
  * SIGTERM-driven cancellation (a self-pipe so the handler stays
    async-signal-safe);
  * the workspace mixin and its output-stage tracking
    (``tracked_writes`` and ``tracked_deletions``) that drive
    artifact selection on :meth:`update_cell`;
  * the ``log_paths`` registry that routes ``category='log'`` writes
    into the run's Logs artifacts instead of the cell's Files;
  * the durable write and annotation journals
    (``.collrun/persistence/journal.json``), so an update, an annotation
    pull or an annotation post whose answer was lost is replayed under the
    same operation id;
  * named methods for every IPC op the host understands (cell ops,
    terminal sessions, port binding, action polling,
    chat-stream events, status / progress / log, subprocess streaming,
    snapshot).

A new instance is created exactly once per generator process by
:func:`collimate.native.main`, which then calls
:func:`collimate.native.set_bridge` and invokes ``run()``.
"""
from __future__ import annotations

import base64
import hashlib
import shutil
import json
import logging
import os
import select
import signal
import socket
import sys
import threading
import time
import uuid
from typing import Any, Iterable, Optional

# Leaf modules (import nothing from native/host) — safe, no cycle.
from collimate._host import ipc
from collimate._results import Annotation, AnnotationNotice, annotation_from_wire
from collimate._workspace import WorkspaceMixin
from collimate.pointers import RUNS_PREFIX


# ──────────────────────────────────────────────────────────────────────
# Exception types (re-exported as ``gen.IPCError`` etc.)
# ──────────────────────────────────────────────────────────────────────


class IPCError(Exception):
    """Base for IPC-protocol failures surfaced to generator code.

    Generators that want to handle a failed ack-await (retry, downsize,
    abort) catch this. The three subclasses below let callers
    distinguish *why* the wait failed.
    """


class IPCNakError(IPCError):
    """The host actively rejected the message via a NAK frame."""

    def __init__(self, reason: str, for_op: str = "unknown"):
        super().__init__(f"host NAK for {for_op!r}: {reason}")
        self.reason = reason
        self.for_op = for_op


class IPCTimeoutError(IPCError):
    """No ack arrived within the wall-clock budget."""

    def __init__(self, op: str, timeout: float):
        super().__init__(f"timed out waiting for ack on {op!r} after {timeout:.1f}s")
        self.op = op
        self.timeout = timeout


class IPCDisconnectedError(IPCError):
    """The host closed the IPC socket before sending an ack."""


class PartialRefusal(PermissionError):
    """The host refused some of an update's writes or deletes and committed
    the rest (``update_cell``'s ack names them in ``refused``). Generator
    code catches it as the :class:`PermissionError` it is; the SDK tells it
    apart from a NAK's ``PermissionError``, after which nothing was
    committed. ``paths`` identifies the refused members; accepted siblings
    must not be resent over later user edits. ``errors`` maps each path to
    its verifier error or ownership explanation."""

    def __init__(self, message: str, *, paths: Iterable[str] = (), refused: Iterable[dict] = ()):
        super().__init__(message)
        self.paths = frozenset(paths)
        self.errors = {entry['path']: str(entry.get('error') or ipc.refused_message([entry]))
                       for entry in refused if isinstance(entry, dict) and entry.get('path')}


class FileConflictError(IPCError):
    """A competing accepted revision preserved this run's candidate for review."""

    def __init__(self, receipt: dict):
        super().__init__("file update conflicts with another writer; resolve the preserved versions in the editor")
        self.receipt = receipt


class Cancelled(Exception):
    """Raised when the user stops the run.
    Re-exported as ``gen.Cancelled``."""


class AnnotationsSealed(RuntimeError):
    """The run's annotation context is sealed (the run is ending): nothing
    more is pulled or posted."""


# The exception each annotation refusal code raises; any other code raises
# RuntimeError. Every refusal is definitive: the host stored nothing.
_ANNOTATION_REFUSALS: dict = {
    "empty_text": ValueError, "too_large": ValueError, "reply_depth": ValueError,
    "invalid": ValueError, "unknown_message": LookupError, "sealed": AnnotationsSealed,
}


def _terminal_session(ack: dict, what: str) -> dict:
    """The ``{terminal_id, ws_path, token}`` a terminal op's ack carries, or
    a :class:`RuntimeError` naming the host's refusal."""
    if not ack.get("ok", False):
        raise RuntimeError(f"{what} failed: {ack.get('error', 'unknown')}")
    return {
        "terminal_id": ack.get("terminal_id", ""),
        "ws_path": ack.get("ws_path", ""),
        "token": ack.get("token", ""),
    }


def _annotation_refusal(op: str, ack: dict) -> Exception:
    """The exception for an annotation op's refusal ack (``ok: false``)."""
    error = str(ack.get("error") or "unknown")
    detail = str(ack.get("message") or "")
    return _ANNOTATION_REFUSALS.get(error, RuntimeError)(
        f"{op} refused: {error}" + (f" ({detail})" if detail else ""))


# ──────────────────────────────────────────────────────────────────────
# HostBridge
# ──────────────────────────────────────────────────────────────────────


_DEFAULT_ACK_TIMEOUT_SECONDS: float = 60.0

# How a hosted run reaches the host's IPC port: one connect attempt, then a
# retry after each of these pauses (a host still binding, a slow docker DNS
# answer). A run that cannot connect after the last one stops.
_IPC_CONNECT_RETRY_DELAYS: tuple = (0.5, 1.0, 2.0)
_IPC_CONNECT_TIMEOUT_SECONDS: float = 10.0

# The longest failure message ``finish`` sends.
_FINISH_ERROR_MAX_CHARS: int = 65_536



class HostBridge(WorkspaceMixin):
    """Single source of truth for everything that talks to the host.

    Lifecycle: constructed once by :func:`collimate.native.main` from
    process env vars (``COLLIMATE_WORKSPACE``, ``COLLIMATE_IPC_PORT``);
    :meth:`finish` is called on the way out and closes the IPC socket. A
    second instantiation in the same process is a programming error and is
    not supported.
    """

    #: The run's own verdict, recorded by ``gen.finish`` and carried by the
    #: finish frame :func:`collimate.native.main` sends. Class defaults, so a
    #: bridge built without ``__init__`` (the test harness's) has them too.
    run_note: Optional[str] = None
    run_outcome: Optional[str] = None

    def __init__(self) -> None:
        self.workspace: str = os.environ.get("COLLIMATE_WORKSPACE", os.getcwd())
        self.run_dir = os.path.abspath(os.path.join(
            self.workspace, os.environ.get("COLLIMATE_RUN_DIR") or ".collrun"))
        if self.run_dir == os.path.abspath(self.workspace):
            raise ValueError("COLLIMATE_RUN_DIR must not be the workspace itself")
        declared_ops = os.environ.get("COLLIMATE_IPC_OPS")
        self.ipc_ops = None
        if declared_ops is not None:
            value = json.loads(declared_ops)
            if not isinstance(value, list) or any(not isinstance(op, str) for op in value):
                raise ValueError("COLLIMATE_IPC_OPS must be a JSON list of operation names")
            self.ipc_ops = frozenset(value)
        hosted = bool(os.environ.get("COLLIMATE_IPC_PORT"))

        # Who this run is to the host (``gen.run_info()`` reads it). The host
        # names every run it starts with its run id and channel, which the
        # proxy route (``gen.proxy_url``) spells, and with its runner identity
        # (``COLLIMATE_RUNNER`` = ``{"runner", "name", "label", "folder"}``),
        # whose label and folder every run needs — the folder is where the
        # run's locked files live. A host run without them is a host bug and
        # stops here.
        if hosted and not all(os.environ.get(name, "").strip()
                              for name in ("COLLIMATE_RUN_ID", "COLLIMATE_CHANNEL")):
            raise RuntimeError(
                "COLLIMATE_RUN_ID and COLLIMATE_CHANNEL are required: the host "
                "names every run it starts, so this run cannot tell who it is")

        # The runner's package requests (``COLLIMATE_PACKAGES``, JSON object;
        # absent when the runner named none). See ``collimate.native.packages``.
        self.packages: dict = _parse_env_object("COLLIMATE_PACKAGES")

        # The run's capability grant (``COLLIMATE_PERMISSIONS``, JSON object)
        # as the host sent it; ``gen.grant()`` types it. The host sends every
        # run its grant (``{}`` for an unrestricted one), so a run connected
        # to a host without one is a host bug and stops here rather than run
        # unrestricted. Only a dev run (no host) defaults to ``{}``.
        if hosted and not os.environ.get("COLLIMATE_PERMISSIONS", "").strip():
            raise RuntimeError(
                "COLLIMATE_PERMISSIONS is missing: the host sends every run its "
                "grant, so this run cannot tell what it may do")
        self.permissions: dict = _parse_env_object("COLLIMATE_PERMISSIONS")
        self._set_identity(hosted)
        self._initialize_workspace()
        self._initialize_persistence(hosted=hosted)
        # The filename suffixes of the run's UI files — its manifest's
        # ``ui_files`` (``COLLIMATE_UI_FILES``, a JSON list). The host sends
        # every run the list (``[]`` when the manifest declares none), and a
        # hosted run writes only those types into its folder. A run with no
        # host reads the variable when it is set; unset, ``None`` accepts
        # every suffix.
        self.ui_suffixes: Optional[tuple[str, ...]] = _parse_ui_files(hosted)

        # Stderr log for the run-events panel. The host scrapes stderr
        # so a plain ``StreamHandler`` is enough; per-line ``[INFO] ...``
        # framing matches the existing run-log conventions.
        self.log = logging.getLogger("collimate.generator")
        if not self.log.handlers:
            handler = logging.StreamHandler(sys.stderr)
            handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
            self.log.addHandler(handler)
            self.log.setLevel(logging.DEBUG)

        # Primary user prompt — written to ``.collrun/prompt.md`` by the
        # host at run start when the run has one.
        self.prompt: str = ""
        prompt_path = os.path.join(self.run_dir, "prompt.md")
        try:
            if os.path.isfile(prompt_path):
                # newline="": the prompt's line endings come back as written.
                with open(prompt_path, "r", encoding="utf-8", newline="") as fh:
                    self.prompt = fh.read()
        except OSError:
            self.prompt = ""
        self.selected_file: Optional[str] = _read_selected_file(self.run_dir)

        # IPC socket. Dev mode (no port set) keeps everything else
        # working but makes ``emit`` a no-op so generators can be
        # exercised outside the runtime.
        self._initialize_ipc()
        port_str = os.environ.get("COLLIMATE_IPC_PORT")
        # The host binds IPC on 127.0.0.1 for a native subprocess and on
        # 0.0.0.0 for a CONTAINERIZED run (docker: limited), whose child reaches
        # the host over the docker gateway alias (``COLLIMATE_IPC_HOST`` =
        # ``host.docker.internal``). Either way the first frame must carry
        # the run's ``COLLIMATE_IPC_TOKEN``: the port serves the run's
        # account secrets, and any local process can reach it.
        ipc_host = os.environ.get("COLLIMATE_IPC_HOST") or "127.0.0.1"
        ipc_token = os.environ.get("COLLIMATE_IPC_TOKEN") or ""
        if port_str:
            self._connect_ipc(ipc_host, port_str, ipc_token)
        else:
            self.log.warning("COLLIMATE_IPC_PORT not set — running in dev mode")

        # Cancellation. A self-pipe so the SIGTERM handler is
        # async-signal-safe (Event.set() can deadlock under repeated
        # signal delivery; os.write on a pipe is reentrant by POSIX).
        self._stop_pipe_r, self._stop_pipe_w = os.pipe()
        os.set_blocking(self._stop_pipe_r, True)
        os.set_blocking(self._stop_pipe_w, False)
        signal.signal(signal.SIGTERM, self._handle_stop)
        if sys.platform == "win32":
            signal.signal(signal.SIGBREAK, self._handle_stop)

    def _connect_ipc(self, host: str, port_str: str, token: str) -> None:
        """Connect to the host's IPC port and send the run's token frame.

        A hosted run that cannot reach its host can report nothing — no
        commit, no status, no finish — so a failed attempt is retried after
        each of :data:`_IPC_CONNECT_RETRY_DELAYS` and the last failure raises
        :class:`RuntimeError`: the run stops before it does (and pays for)
        any work. A failed attempt leaves no half-open socket behind."""
        try:
            port = int(port_str)
        except ValueError:
            raise RuntimeError(
                f"COLLIMATE_IPC_PORT {port_str!r} is not a port number") from None
        attempts = len(_IPC_CONNECT_RETRY_DELAYS) + 1
        for attempt in range(attempts):
            try:
                sock = socket.create_connection(
                    (host, port), timeout=_IPC_CONNECT_TIMEOUT_SECONDS)
                # Blocking from here on: acks are awaited with select().
                sock.settimeout(None)
                self._socket = sock
                self._sock_rfile = sock.makefile("r", encoding="utf-8")
                self._sock_wfile = sock.makefile("w", encoding="utf-8")
                if token:
                    # First line on the wire: the host's authed-connection
                    # wrapper reads exactly this frame and validates the token
                    # before delegating to the normal IPC loop.
                    self._sock_wfile.write(
                        json.dumps({"op": "__auth__", "token": token}) + "\n")
                    self._sock_wfile.flush()
                return
            except OSError as exc:
                self._close_ipc()
                if attempt + 1 == attempts:
                    raise RuntimeError(
                        f"cannot reach the host's IPC port {host}:{port} after "
                        f"{attempts} attempts ({exc}): a hosted run with no "
                        "IPC channel can report nothing, so it stops here"
                    ) from exc
                delay = _IPC_CONNECT_RETRY_DELAYS[attempt]
                self.log.warning(
                    "IPC connect to %s:%s failed (%s); retrying in %.1fs",
                    host, port, exc, delay)
                time.sleep(delay)
            except BaseException:
                self._close_ipc()
                raise

    def _close_ipc(self) -> None:
        """Close and clear the IPC socket and both of its file objects.
        Cleared, not just closed: a later emit is then the dev-mode no-op,
        not a write to a closed file."""
        with self._write_lock():
            for fileobj in (self._sock_wfile, self._sock_rfile, self._socket):
                if fileobj is not None:
                    try:
                        fileobj.close()
                    except OSError:
                        pass
            self._sock_wfile = self._sock_rfile = self._socket = None

    def _set_identity(self, hosted: bool) -> None:
        """Set who this run is: ``run_id``, ``channel``, ``cell_id``,
        ``runner_id``, ``runner_name``, ``label`` and ``folder``.

        A host run takes them from the environment and must carry its label
        and folder (``folder == "_runs/<label>"``). A dev run (no host) mints
        a run id once and names itself ``dev-<run_id[:8]>``, so its folder is
        as real as a hosted one's."""
        runner = _parse_env_object("COLLIMATE_RUNNER")
        self.channel: str = os.environ.get("COLLIMATE_CHANNEL", "").strip()
        self.cell_id: str = os.environ.get("COLLIMATE_CELL_ID", "").strip()
        self.runner_id: str = str(runner.get("runner") or "")
        self.runner_name: str = str(runner.get("name") or "")
        if hosted:
            self.run_id: str = os.environ.get("COLLIMATE_RUN_ID", "").strip()
            label = str(runner.get("label") or "").strip()
            folder = str(runner.get("folder") or "").strip().rstrip("/")
            if not label or not folder:
                raise RuntimeError(
                    "COLLIMATE_RUNNER must name the run's label and folder: the "
                    "host names every run it starts, so this run cannot tell "
                    "where its locked files live")
            if folder != f"{RUNS_PREFIX}/{label}":
                raise RuntimeError(
                    f"COLLIMATE_RUNNER folder {folder!r} is not "
                    f"{RUNS_PREFIX}/{label}: a run's folder is named by its label")
        else:
            self.run_id = uuid.uuid4().hex
            label = f"dev-{self.run_id[:8]}"
            folder = f"{RUNS_PREFIX}/{label}"
        self.label: str = label
        self.folder: str = folder


    # ──────────────────────────────────────────────────────────────────
    # Cancellation
    # ──────────────────────────────────────────────────────────────────

    def _handle_stop(self, signum, frame) -> None:
        # Async-signal-safe: write a single byte to the self-pipe.
        # Non-blocking on the write side, so a full pipe (repeated
        # SIGTERMs) drops extra bytes silently rather than blocking.
        try:
            os.write(self._stop_pipe_w, b"x")
        except (BlockingIOError, OSError):
            pass

    @property
    def cancelled(self) -> bool:
        """Non-blocking poll for Stop. True if SIGTERM has been delivered,
        and after :meth:`finish` (the run is over)."""
        if self._stop_pipe_r < 0:
            return True
        ready, _, _ = select.select([self._stop_pipe_r], [], [], 0)
        return bool(ready)

    # ──────────────────────────────────────────────────────────────────
    # IPC primitives
    # ──────────────────────────────────────────────────────────────────

    def _initialize_ipc(self) -> None:
        """Initialize transport state before use, including test transports."""
        self._socket: Optional[socket.socket] = None
        self._sock_rfile = None
        self._sock_wfile = None
        # Requests share one reader; writes hold only the short write lock so
        # status and stream events can be emitted while a reply is pending.
        self._ipc_write_lock = threading.RLock()
        self._ipc_request_lock = threading.RLock()
        self._ipc_desynced = False

    def _write_lock(self) -> "threading.RLock":
        return self._ipc_write_lock

    def _request_lock(self) -> "threading.RLock":
        return self._ipc_request_lock

    def _call(self, op: str, *, wait_op: str | None = None,
              timeout: float | None = None, **values) -> dict:
        """One acknowledged call; operation wrappers own body refusals/dev policy."""
        return self.emit_and_wait({"op": op, **values},
                                  wait_op=wait_op or f"ack_{op}", timeout=timeout)

    def emit(self, payload: dict) -> None:
        """Write one frame to the host (the dev-mode no-op without one).

        A frame longer than :data:`collimate._host.ipc.IPC_MAX_LINE_BYTES` raises
        :class:`ValueError` naming the op, before anything is written: the
        host would close the connection without replying, ending the run."""
        self._send(_frame(payload))

    def _send(self, line: str) -> None:
        """Write one framed line (see :func:`_frame`)."""
        # Checked under the lock: a finish() on another thread clears the
        # socket files, and a late emit is then the dev-mode no-op.
        with self._write_lock():
            wfile = self._sock_wfile
            if wfile is None:
                self.log.debug("IPC (dev): %s", line.rstrip("\n"))
                return
            wfile.write(line)
            wfile.flush()

    def _read_line(self, deadline: float, wait_op: str, budget: float) -> str:
        """One line from the host (``""`` at EOF), or
        :class:`IPCTimeoutError` past ``deadline``. The socket's bytes are
        buffered here rather than in a file object: ``select`` sees only the
        fd, so a second line that arrived in the same ``recv`` as the first
        must be found in this buffer, not waited for."""
        sock = self._socket
        if sock is None:
            return self._sock_rfile.readline()
        buf = getattr(self, "_rbuf", None)
        if buf is None:
            buf = self._rbuf = bytearray()
        scanned = 0
        while True:
            nl = buf.find(b"\n", scanned)
            if nl >= 0:
                line = bytes(buf[:nl + 1])
                del buf[:nl + 1]
                return line.decode("utf-8", errors="replace")
            scanned = len(buf)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise IPCTimeoutError(wait_op, budget)
            ready, _, _ = select.select([sock], [], [], remaining)
            if not ready:
                raise IPCTimeoutError(wait_op, budget)
            chunk = sock.recv(65536)
            if not chunk:
                # EOF: a final unterminated line first, as readline() does.
                line = bytes(buf)
                buf.clear()
                return line.decode("utf-8", errors="replace")
            buf += chunk

    def emit_and_wait(
        self,
        payload: dict,
        wait_op: str,
        *,
        timeout: Optional[float] = None,
    ) -> dict:
        """Emit a message and block until the matching ack arrives.

        Failure modes (each raises a distinct subclass of :class:`IPCError`):

          * :class:`IPCNakError` — host returned ``{"op": "nak", ...}``.
          * :class:`IPCTimeoutError` — no ack within the budget.
          * :class:`IPCDisconnectedError` — host closed the socket.

        A frame over :data:`collimate._host.ipc.IPC_MAX_LINE_BYTES` raises :class:`ValueError`
        and is never sent.
        """
        budget = _DEFAULT_ACK_TIMEOUT_SECONDS if timeout is None else float(timeout)
        if self._sock_rfile is None:
            self.emit(payload)
            return {}

        req_id = uuid.uuid4().hex
        frame = _frame({**payload, "req_id": req_id})
        with self._request_lock():
            if getattr(self, "_ipc_desynced", False):
                raise IPCDisconnectedError(
                    "IPC channel is desynchronized after an unanswered request")
            if self._sock_rfile is None:
                # A finish() on another thread closed the channel meanwhile.
                self._send(frame)
                return {}
            # Time spent waiting behind an earlier request is not this
            # request's response budget.
            deadline = time.monotonic() + budget
            try:
                self._send(frame)
                while True:
                    line = self._read_line(deadline, wait_op, budget)
                    if not line:
                        raise IPCDisconnectedError(
                            f"host closed IPC channel while awaiting ack on {wait_op!r}")
                    try:
                        ack = json.loads(line.strip())
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(ack, dict) or ack.get("req_id") != req_id:
                        continue
                    ack_op = ack.get("op")
                    if ack_op == wait_op:
                        return ack
                    if ack_op == "nak":
                        if ack.get("for_op") != payload.get("op"):
                            self.log.warning(
                                "IPC: ignored NAK for %r while awaiting %r",
                                ack.get("for_op"), payload.get("op"))
                            continue
                        reason = str(ack.get("reason", "unspecified"))
                        for_op = str(payload.get("op"))
                        if reason == "permission_denied":
                            cap = ack.get("capability") or ack.get("required")
                            detail = f"; requires capability {cap!r}" if cap else ""
                            raise PermissionError(
                                f"permission denied for {for_op!r}{detail}")
                        raise IPCNakError(reason=reason, for_op=for_op)
            except (IPCTimeoutError, IPCDisconnectedError, OSError):
                # A late answer must never satisfy a subsequent same-op call.
                self._ipc_desynced = True
                raise

    # ──────────────────────────────────────────────────────────────────
    # Lifecycle
    # ──────────────────────────────────────────────────────────────────

    def finish(self, *, success: bool, error_message: str = "",
               note: Optional[str] = None, outcome: Optional[str] = None) -> None:
        """Send how the run ended and close its IPC channel and signal pipe.

        ``success`` and ``error_message`` are whether the run worked;
        ``note`` (one line, :data:`collimate._host.ipc.FINISH_NOTE_MAX_CHARS` at
        most) and ``outcome`` (one of :data:`collimate._host.ipc.FINISH_OUTCOMES`)
        are the run's own verdict, sent only when given. An unknown
        ``outcome`` raises ``ValueError`` before anything is sent."""
        if outcome is not None and outcome not in ipc.FINISH_OUTCOMES:
            raise ValueError(f"outcome must be 'pass' or 'fail', not {outcome!r}")
        if len(error_message) > _FINISH_ERROR_MAX_CHARS:
            error_message = error_message[:_FINISH_ERROR_MAX_CHARS] + " … (truncated)"
        frame: dict = {"op": "finish", "success": success, "error_message": error_message}
        if (line := ipc.finish_note(note)) is not None:
            frame["note"] = line
        if outcome is not None:
            frame["outcome"] = outcome
        with self._request_lock(), self._write_lock():
            try:
                self.emit(frame)
            except OSError as exc:
                self.log.warning("IPC finish not delivered: %s", exc)
            self._close_ipc()
        for fd in (self._stop_pipe_r, self._stop_pipe_w):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._stop_pipe_r = self._stop_pipe_w = -1


    # ──────────────────────────────────────────────────────────────────
    # Cell ops
    # ──────────────────────────────────────────────────────────────────


    def update_cell(self, *, name=None, writes) -> str:
        """Commit the write snapshot and drain the bridge's staged deletions.

        A failed commit restores deletions unless a newer ordinary write
        replaced that path. Log reclassification retains its cell deletion.
        """
        with self._stage_lock():
            deleted_files = sorted(self.tracked_deletions) or None
            self.tracked_deletions.clear()
        try:
            return self._commit_update(name=name, writes=writes, deleted_files=deleted_files)
        except PartialRefusal:
            raise
        except BaseException:
            with self._stage_lock():
                self.tracked_deletions.update(
                    p for p in deleted_files or ()
                    if p not in self.tracked_writes or p in self.log_paths)
            raise

    def _initialize_persistence(self, *, hosted: bool = False) -> None:
        """Load the frozen launch baseline and the exact uncertain operations."""
        self._persistence_mutex = threading.RLock()
        # Serializes annotation pulls and posts: each replays its own
        # uncertain operation before it sends a new one.
        self._annotation_lock = threading.RLock()
        self._persistence_dir = os.path.join(self.run_dir, "persistence")
        os.makedirs(self._persistence_dir, exist_ok=True)
        self._persistence_path = os.path.join(self._persistence_dir, "journal.json")
        if os.path.exists(self._persistence_path):
            with open(self._persistence_path, encoding="utf-8") as fh:
                self._persistence = json.load(fh)
        else:
            baseline_path = os.path.join(self.run_dir, "file_revisions.json")
            if os.path.exists(baseline_path):
                with open(baseline_path, encoding="utf-8") as fh:
                    revisions = json.load(fh)
            elif hosted:
                raise RuntimeError("host omitted the run's frozen file revision baseline")
            else:
                revisions = {}
            self._persistence = {
                "writer_id": f"run:{self.run_id}:{uuid.uuid4().hex}",
                "edit_seq": 0, "revisions": revisions, "pending": None,
                "pending_annotation_pull": None, "pending_annotation_post": None,
            }
            self._write_persistence()

    def _write_persistence(self) -> None:
        with self._persistence_mutex:
            self._write_persistence_locked()

    def _write_persistence_locked(self) -> None:
        temporary = self._persistence_path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as fh:
            json.dump(self._persistence, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, self._persistence_path)
        directory = os.open(self._persistence_dir, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _accept_file_revisions(self, descriptors) -> None:
        state = self._persistence
        with self._persistence_mutex:
            for item in descriptors:
                state["revisions"][item["path"]] = item["file_revision"]
            self._write_persistence()

    def _accept_clean_file_revisions(self, descriptors) -> None:
        with self._persistence_mutex, self._stage_lock():
            clean = [item for item in descriptors if item["path"] not in
                     (self.tracked_writes | self.tracked_deletions)]
            self._accept_file_revisions(clean)

    @staticmethod
    def _validate_file_receipts(payload, ack, expected, deleted=()) -> None:
        """An operation identity alone cannot acknowledge missing file writes."""
        refused = {entry.get('path') for entry in ack.get('refused', [])}
        files = ack.get('files')
        if not isinstance(files, list):
            raise IPCError('host returned an invalid file receipt')
        by_path = {item.get('path'): item for item in files if isinstance(item, dict)}
        if len(by_path) != len(files) or set(by_path) != set(expected) - refused:
            raise IPCError('host returned an invalid file receipt')
        for path, base in expected.items():
            if path in refused:
                continue
            item = by_path[path]
            revision = item.get('file_revision')
            if (type(revision) is not int or revision <= base
                    or item.get('writer_id') != payload['writer_id']
                    or item.get('edit_seq') != payload['edit_seq']
                    or item.get('deleted') is not (path in deleted)):
                raise IPCError('host returned an invalid file receipt')

    def _send_pending_update(self) -> dict:
        state = self._persistence
        pending = state["pending"]
        self._write_persistence()
        ack = self.emit_and_wait(pending["payload"], wait_op="ack_update")
        if ack.get("ok") is False:
            raise IPCError(f"host failed update_cell: {ack.get('error') or 'commit failed'}")
        if (ack.get("operation_id") != pending["payload"]["operation_id"]
                or ack.get("outcome") not in ("accepted", "conflict", "superseded")):
            raise IPCError("host returned an invalid update receipt")
        if ack.get("outcome") in ("conflict", "superseded"):
            raise FileConflictError(ack)
        self._validate_file_receipts(pending['payload'], ack,
                                     pending['payload']['base_revisions'],
                                     pending['payload'].get('deleted_files', []))
        self._accept_file_revisions(ack.get("files", []))
        state["pending"] = None
        try:
            self._write_persistence()
        except BaseException:
            state["pending"] = pending
            raise
        shutil.rmtree(pending["directory"], ignore_errors=True)
        refused = ack.get("refused") or []
        if refused:
            raise PartialRefusal(ipc.refused_message(refused),
                                 paths=(entry["path"] for entry in refused if entry.get("path")),
                                 refused=refused)
        return ack

    def _commit_update(
        self, *, name: Optional[str] = None, writes: Iterable[str],
        deleted_files: Optional[list[str]] = None,
    ) -> str:
        """Publish frozen bytes with conditional revisions and exact replay.

        A timeout keeps the journal and immutable artifact files. On retry the
        uncertain operation is acknowledged first, then newer workspace bytes
        are sent as a new operation based on that accepted receipt. Conflicts
        leave both the staging and server candidate intact for explicit review.
        """
        with self._persistence_mutex:
            return self._commit_update_locked(name=name, writes=writes, deleted_files=deleted_files)

    def _commit_update_locked(self, *, name, writes, deleted_files) -> str:
        state = self._persistence
        writes = set(writes)
        deletes = set(deleted_files or ())
        old = state["pending"]
        last_ack = {}
        if old:
            last_ack = self._send_pending_update()
            for path, digest in old["hashes"].items():
                if path in writes and hashlib.sha256(self.fs_read_bytes(path)).hexdigest() == digest:
                    writes.remove(path)
            deletes.difference_update(old["payload"].get("deleted_files", []))
            if not writes and not deletes and name == old["payload"]["metadata"].get("name"):
                return last_ack.get("cell_id", "")
        operation_id = uuid.uuid4().hex
        directory = os.path.join(self._persistence_dir, operation_id)
        os.mkdir(directory)
        artifacts, hashes = [], {}
        try:
            for ordinal, path in enumerate(sorted(writes)):
                with self._stage_lock():
                    raw = self.fs_read_bytes(path)
                    encoding = self._write_encodings.get(path)
                snapshot = os.path.join(directory, str(ordinal))
                with open(snapshot, "xb") as fh:
                    fh.write(raw)
                    fh.flush()
                    os.fsync(fh.fileno())
                hashes[path] = hashlib.sha256(raw).hexdigest()
                art = {"type": "workspace", "cell_path": path,
                       "local_path": os.path.relpath(snapshot, self.workspace)}
                if encoding is not None:
                    art["encoding"] = encoding
                if path in self.log_paths:
                    art["category"] = "log"
                artifacts.append(art)
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            state["edit_seq"] += 1
            payload = {
                "op": "update_cell", "artifacts": artifacts,
                "metadata": {} if name is None else {"name": name},
                "operation_id": operation_id, "writer_id": state["writer_id"],
                "edit_seq": state["edit_seq"],
                "base_revisions": {p: state["revisions"].get(p, 0)
                                   for p in (writes - self.log_paths) | deletes},
                "resolves": [],
            }
            if deletes:
                payload["deleted_files"] = sorted(deletes)
            state["pending"] = {"payload": payload, "hashes": hashes, "directory": directory}
            self._write_persistence()
        except BaseException:
            if state["pending"] is None:
                shutil.rmtree(directory)
            raise
        return self._send_pending_update().get("cell_id", "")

    def _handler_reply(self, op: str, **values) -> dict:
        reply = self._call(op, **values, wait_op=f'ack_{op}')
        if reply.get("error"):
            raise IPCError(f"{op} failed: {reply['error']}")
        return reply

    def handlers(self) -> list:
        return self._handler_reply("list_handlers")["handlers"]

    def handler_skills(self, ext_or_id: str) -> dict[str, str]:
        return self._handler_reply("handler_skills", ext_or_id=ext_or_id)["skills"]

    def validate(self, path: str, verifier: str | None = None) -> dict:
        return self._handler_reply("validate_file", path=path, verifier=verifier)["result"]

    def read_cell_descriptors(self, *, paths: list[str] | None = None) -> dict[str, dict]:
        """Read revisions without advancing a workspace's acknowledged baseline."""
        supported = getattr(self, "ipc_ops", None)
        op = "read_cell_hashes"
        if supported is not None and op not in supported:
            if "read_cell_files" not in supported:
                raise PermissionError("this IPC transport cannot read cell files")
            op = "read_cell_files"
        payload = {"op": op}
        if paths is not None:
            payload["paths"] = paths
        ack = self.emit_and_wait(payload, wait_op=f"ack_{op}")
        if ack.get("error"):
            raise IPCError(f"{op} failed: {ack['error']}")
        descriptors = ack.get("hashes" if op == "read_cell_hashes" else "files")
        if not isinstance(descriptors, dict):
            raise IPCError(f"{op} returned no revision snapshot")
        for item in descriptors.values():
            if not isinstance(item, dict) or not isinstance(item.get("file_revision"), int):
                raise IPCError(f"{op} returned an invalid revision descriptor")
        return descriptors

    def read_cell_hashes(self, *, paths: list[str] | None = None) -> dict[str, str]:
        return {path: item["body_hash"] for path, item in
                self.read_cell_descriptors(paths=paths).items() if not item.get("deleted")}

    def read_cell_files(self, *, paths: list[str] | None = None,
                        revisions: dict[str, int] | None = None) -> dict:
        """Read accepted file bodies with provenance; mirrors pin their snapshot.

        Failures are never interpreted as an empty cell.
        """
        payload = {"op": "read_cell_files"}
        if paths is not None:
            payload["paths"] = paths
        if revisions is not None:
            payload["revisions"] = revisions
        ack = self.emit_and_wait(payload, wait_op="ack_read_cell_files")
        if ack.get("error"):
            raise IPCError(f"read_cell_files failed: {ack['error']}")
        files = ack.get("files")
        if not isinstance(files, dict):
            raise IPCError("read_cell_files returned no revision snapshot")
        out, descriptors = {}, []
        for path, item in files.items():
            raw = base64.b64decode(item["content"], validate=True)
            if hashlib.sha256(raw).hexdigest() != item["body_hash"]:
                raise IPCError(f"read_cell_files returned corrupt bytes for {path}")
            if revisions is not None and item["file_revision"] != revisions.get(path):
                raise IPCError(f"read_cell_files returned the wrong revision for {path}")
            out[path] = raw.decode("utf-8") if item["encoding"] == "utf-8" else raw
            descriptors.append({"path": path, **item})
        self._accept_clean_file_revisions(descriptors)
        return out


    # ──────────────────────────────────────────────────────────────────
    # Status / control
    # ──────────────────────────────────────────────────────────────────

    def push_log(self, line: str) -> None:
        self.log.info(line)

    def set_phase(self, phase: str, message: str = "") -> None:
        self.emit({"op": "phase", "phase": phase, "message": message})

    def progress(self, current: int, total: int) -> None:
        self.emit({"op": "progress", "current": current, "total": total})

    # ──────────────────────────────────────────────────────────────────
    # User input mid-run
    # ──────────────────────────────────────────────────────────────────

    def poll_renderer_action_event(self, *, source: str = "",
                                   action_id: str = "") -> Optional[dict]:
        """Pop the first pending TAGGED action matching the filters (empty
        matches anything) — ``{action_id, source, payload}`` or ``None``.
        Filtering happens host-side so concurrent waiters never steal each
        other's entries."""
        msg: dict = {"op": "poll_renderer_action"}
        if source:
            msg["source"] = source
        if action_id:
            msg["action_id"] = action_id
        ack = self.emit_and_wait(msg, wait_op="ack_poll_renderer_action")
        if not ack.get("action_id"):
            return None
        out: dict = {"action_id": ack.get("action_id", ""),
                     "source": ack.get("source", "")}
        if isinstance(ack.get("payload"), dict):
            out["payload"] = ack["payload"]
        return out

    # ──────────────────────────────────────────────────────────────────
    # Ports + terminals + live chat
    # ──────────────────────────────────────────────────────────────────

    def serve_port(
        self,
        port: int,
        *,
        name: str = "preview",
        path: str = "",
    ) -> None:
        payload: dict[str, Any] = {"op": "port_bound", "port": port, "name": name}
        if path:
            payload["path"] = path
        self.emit_and_wait(payload, wait_op="ack_port_bound")

    # -- host-placed environments --------------------------------------------
    #
    # The generator declares WHAT environment it needs (image, mounts, env);
    # the host's execution backend decides WHERE the container lands and owns
    # the docker calls. Generator code never talks to a docker daemon — that
    # is what lets the same generator run under a backend whose compute is
    # not this machine. `_env_shared.DockerEnvironment` is the client of
    # these four ops; its public API is unchanged.

    def env_place(self, spec: dict) -> dict:
        """Ask the host to place an environment container for this run.
        Returns ``{ok, env_id, container_id, error}``."""
        return self._call('env_place', spec=dict(spec or {}), wait_op='ack_env_place', timeout=1800.0)

    def env_exec(
        self,
        env_id: str,
        command,
        *,
        cwd: Optional[str] = None,
        env: Optional[dict] = None,
        timeout: Optional[float] = None,
        user: Optional[str] = None,
        detach: bool = False,
    ) -> dict:
        """Run a command in a placed environment. ``detach=True`` fires and
        forgets (background launchers); otherwise returns
        ``{ok, rc, output, error}``.

        ``timeout`` sets only the response wait budget (plus 30 seconds for
        collection; 120 seconds when unset). The caller must bound the
        command itself, for example with the container's ``timeout`` command.
        A response timeout does not kill the command.
        """
        wait_budget = 120.0 if timeout is None else float(timeout) + 30.0
        return self._call('env_exec', env_id=env_id, command=command if isinstance(command, list) else str(command), cwd=cwd or '', env=dict(env or {}), user=user or '', detach=bool(detach), wait_op='ack_env_exec', timeout=wait_budget)

    def env_expose_port(self, env_id: str, port: int) -> dict:
        """Forward a container port; returns ``{ok, host_port, error}``.
        The caller still registers the host port via :meth:`serve_port` —
        the port_bound flow is unchanged."""
        return self._call('env_expose_port', env_id=env_id, port=int(port), wait_op='ack_env_expose_port')

    def env_close(self, env_id: str) -> dict:
        return self._call('env_close', env_id=env_id, wait_op='ack_env_close', timeout=60.0)

    def start_terminal(
        self,
        *,
        container_id: str,
        command: list[str],
        cwd: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
        keep_alive: bool = False,
    ) -> dict:
        if not self._host_available():
            raise RuntimeError("start_terminal needs a host")
        ack = self._call('create_terminal_session', container_id=container_id, command=list(command), cwd=cwd or '', env=dict(env or {}), keep_alive=bool(keep_alive), wait_op='ack_create_terminal_session')
        return _terminal_session(ack, "start_terminal")

    def start_host_terminal(
        self,
        *,
        command: list[str],
        cwd: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
        keep_alive: bool = False,
    ) -> dict:
        """A PTY running ``command`` on this machine: ``env`` is its whole
        environment, ``cwd`` a directory of the run's workspace."""
        if not self._host_available():
            raise RuntimeError("start_host_terminal needs a host")
        ack = self._call('create_host_terminal_session', command=list(command), cwd=cwd or '', env=dict(env or {}), keep_alive=bool(keep_alive), wait_op='ack_create_host_terminal_session')
        return _terminal_session(ack, "start_host_terminal")

    def stop_terminal(self, terminal_id: str) -> None:
        if not terminal_id:
            return
        self.emit({"op": "destroy_terminal_session", "terminal_id": terminal_id})

    def terminal_status(self, terminal_id: str) -> dict:
        """The PTY's liveness — ``{exists, ended, exit_code}``. ``ended``
        when the program exited (or the session is gone)."""
        if not self._host_available():
            raise RuntimeError("terminal_status needs a host")
        if not terminal_id:
            return {"exists": False, "ended": True, "exit_code": None}
        ack = self._call('terminal_status', terminal_id=terminal_id, wait_op='ack_terminal_status')
        if not ack.get("ok", False):
            raise RuntimeError(
                f"terminal_status failed: {ack.get('error', 'unknown')}"
            )
        return {
            "exists": bool(ack.get("exists")),
            "ended": bool(ack.get("ended")),
            "exit_code": ack.get("exit_code"),
        }

    def chat_poll(self) -> Optional[dict]:
        """Non-blocking pop of the next command from this run's inbox — the
        commands its viewers send (``{kind:
        "user_msg"|"interrupt"|"permission_answer"|…}``) — or
        ``None`` when the queue is empty."""
        ack = self._call('chat_poll', wait_op='ack_chat_poll')
        cmd = ack.get("command")
        return cmd if isinstance(cmd, dict) else None

    def stream_event(self, turn_id: str, event: dict) -> None:
        """Fire-and-forget: push a typed stream event to this run's inbox
        session, the one live session a run registers."""
        self.emit({"op": "stream_event", "turn_id": turn_id, "event": event})

    def answers_cards(self) -> bool:
        """Whether an approval card this run emits can reach a human: a host
        is connected to carry it through the run's inbox and the answer
        back. A dev run (no host) has no one to show it to."""
        return self.has_host

    def surface_register(self, path: str, *, label: str, transport_only: bool,
                         broadcast: bool) -> dict:
        """Register the run's inbox at ``path`` (in its run folder):
        ``transport_only`` when no ``.chat`` file is served there, and
        ``broadcast`` (transport-only only) when several viewers may attach.
        Returns ``{chat_id, ws_path, token}``."""
        declaration = _inbox_declaration(path, folder=self.folder, label=label,
                                         transport_only=transport_only,
                                         broadcast=broadcast)
        if not self._host_available():
            raise RuntimeError("surface_register needs a host")
        ack = self._call('surface_register', **declaration, transport_only=transport_only,
                         broadcast=broadcast, wait_op='ack_surface_register')
        if not ack.get("ok", False):
            error = ack.get("error", "unknown")
            detail = ack.get("message") or ack.get("detail") or ""
            raise RuntimeError(f"surface_register({path!r}) failed: {error}" +
                               (f": {detail}" if detail else ""))
        return {"chat_id": ack.get("chat_id", ""),
                "ws_path": ack.get("ws_path", ""), "token": ack.get("token", "")}

    def reveal_file(self, path: str) -> None:
        """Fire-and-forget: ask the clients to show the committed cell file
        ``path`` as a tab of this run's cell. The host checks the path
        against the committed tip, so flush the write first."""
        self.emit({"op": "reveal_file", "path": path})

    # ──────────────────────────────────────────────────────────────────
    # The run's annotation context
    # ──────────────────────────────────────────────────────────────────

    def annotations_available(self) -> bool:
        """Whether this run has an annotation context: a host is connected
        and its transport carries the annotation ops (a forwarded
        ``test_generator`` child's does not)."""
        if not self.has_host:
            return False
        supported = getattr(self, "ipc_ops", None)
        return supported is None or set(ipc.ANNOTATION_OPS) <= supported

    def _annotations_ack(self, op: str, **fields) -> dict:
        """One annotation op's ack (the testing bridge answers it in memory)."""
        return self._call(op, **fields)

    def _require_annotations(self, op: str) -> None:
        if not self.annotations_available():
            raise LookupError(f"{op}: this run has no annotation context "
                              "(no host, or a forwarded test child)")

    def _clear_annotation_pending(self, key: str) -> None:
        with self._persistence_mutex:
            self._persistence[key] = None
            self._write_persistence()

    @staticmethod
    def _notice_from_ack(op: str, ack: dict) -> AnnotationNotice:
        """The ``{rev, unread, interrupt_seq}`` an ack carries."""
        values = [ack.get(key) for key in ("rev", "unread", "interrupt_seq")]
        if any(type(value) is not int or value < 0 for value in values):
            raise IPCError(f"host returned an invalid {op} answer")
        return AnnotationNotice(*values)

    def annotations_poll(self) -> AnnotationNotice:
        """The context's revision, unread count and interrupt counter. The
        first poll marks the run as listening."""
        self._require_annotations("annotations_poll")
        ack = self._annotations_ack("annotations_poll")
        if ack.get("ok") is not True:
            raise _annotation_refusal("annotations_poll", ack)
        return self._notice_from_ack("annotations_poll", ack)

    def annotations_pull(self) -> tuple[list[Annotation], AnnotationNotice]:
        """Every waiting message, now read, and the context's notice as the
        pull left it (its ``interrupt_seq`` counts every interrupt the batch
        answers). The operation id is journaled before it is sent, so a pull
        whose answer was lost (a timeout, a restart) is replayed and the host
        answers the same batch."""
        self._require_annotations("annotations_pull")
        state = self._persistence
        with self._annotation_lock:
            with self._persistence_mutex:
                pending = state.get("pending_annotation_pull")
                if pending is None:
                    pending = state["pending_annotation_pull"] = {
                        "operation_id": uuid.uuid4().hex}
                    self._write_persistence()
            # A transport failure raises here and keeps the pending pull.
            ack = self._annotations_ack("annotations_pull",
                                        operation_id=pending["operation_id"])
            if ack.get("ok") is not True:
                # Sealed: nothing more will ever be pulled. Any other refusal
                # keeps the operation, whose retry is a replay.
                if ack.get("error") == "sealed":
                    self._clear_annotation_pending("pending_annotation_pull")
                raise _annotation_refusal("annotations_pull", ack)
            messages = ack.get("messages")
            if ack.get("operation_id") != pending["operation_id"] or not isinstance(messages, list):
                raise IPCError("host returned an invalid annotations_pull receipt")
            notice = self._notice_from_ack("annotations_pull", ack)
            try:
                batch = [annotation_from_wire(message) for message in messages]
            except ValueError as exc:
                raise IPCError(f"host returned a malformed annotation: {exc}") from None
            self._clear_annotation_pending("pending_annotation_pull")
            return batch, notice

    def annotations_post(self, body: str, *, reply_to: Optional[str] = None) -> str:
        """Post ``body`` from this run (a reply to the top-level message
        ``reply_to``) and return its id. The operation and the message id are
        journaled before they are sent: a post whose answer was lost is
        replayed under its id, never doubled, before a new one is sent, and a
        retry of the same post returns the replayed id."""
        self._require_annotations("annotations_post")
        request: dict = {"body": body}
        if reply_to:
            request["reply_to"] = reply_to
        state = self._persistence
        with self._annotation_lock:
            pending = state.get("pending_annotation_post")
            if pending:
                result = self._send_pending_annotation_post()
                previous = {key: value for key, value in pending.items()
                            if key not in ("operation_id", "message_id")}
                if previous == request:
                    return result
            with self._persistence_mutex:
                state["pending_annotation_post"] = {
                    "operation_id": uuid.uuid4().hex,
                    "message_id": ipc.new_annotation_id(), **request}
                self._write_persistence()
            return self._send_pending_annotation_post()

    def _send_pending_annotation_post(self) -> str:
        pending = self._persistence["pending_annotation_post"]
        ack = self._annotations_ack("annotations_post", **pending)
        if ack.get("ok") is not True:
            # A refusal is definitive: the host stored nothing.
            self._clear_annotation_pending("pending_annotation_post")
            raise _annotation_refusal("annotations_post", ack)
        if ack.get("operation_id") != pending["operation_id"] or ack.get("id") != pending["message_id"]:
            raise IPCError("host returned an invalid annotations_post receipt")
        self._clear_annotation_pending("pending_annotation_post")
        return pending["message_id"]

    def annotations_list(self) -> list[Annotation]:
        """The run's whole context (held messages excluded), in context order."""
        self._require_annotations("annotations_list")
        ack = self._annotations_ack("annotations_list")
        if ack.get("ok") is not True:
            raise _annotation_refusal("annotations_list", ack)
        messages = ack.get("messages")
        if not isinstance(messages, list):
            raise IPCError("host returned an invalid annotations_list answer")
        try:
            return [annotation_from_wire(message) for message in messages]
        except ValueError as exc:
            raise IPCError(f"host returned a malformed annotation: {exc}") from None


    @property
    def has_host(self) -> bool:
        """True when a real host IPC channel is wired."""
        return self._sock_wfile is not None

    def _host_available(self) -> bool:
        return self.has_host


    @staticmethod
    def _raise_account_refusal(ack: dict, default: str) -> None:
        """Map a refused account operation to its public exception type."""
        error = ack.get("error") or default
        reason = ack.get("reason")
        if reason == "permission_denied":
            raise PermissionError(error)
        if reason == "not_found":
            raise LookupError(error)
        if reason == "lint":
            raise ValueError(error)
        raise RuntimeError(error)

    def account(self, role: str) -> dict:
        if not role or not isinstance(role, str):
            raise ValueError("account role must be a nonempty string")
        if not self._host_available():
            raise LookupError(f"No account bound for role {role!r}")
        ack = self._call('account', role=role, wait_op='ack_account')
        if not ack.get("ok"):
            self._raise_account_refusal(ack, f"account({role!r}) unavailable")
        result = ack.get("account")
        if not isinstance(result, dict):
            raise LookupError(f"No account bound for role {role!r}")
        return result

    def commit_account(self, role: str, expected_revision: int, secrets: dict) -> dict:
        if not self._host_available():
            raise LookupError(f"No account bound for role {role!r}")
        ack = self._call('commit_account', role=role, expected_revision=expected_revision, secrets=dict(secrets), wait_op='ack_commit_account')
        if not ack.get("ok"):
            self._raise_account_refusal(ack, f"commit_account({role!r}) failed")
        return {"committed": bool(ack.get("committed")), "revision": ack.get("revision")}

    def materialize_account(self, role: str) -> dict:
        if not self._host_available():
            raise LookupError(f"No account bound for role {role!r}")
        ack = self._call('account_materialize', role=role, wait_op='ack_account_materialize')
        if not ack.get("ok"):
            self._raise_account_refusal(ack, f"materialize_account({role!r}) failed")
        return {k: ack[k] for k in ("path", "lease_id") if k in ack}

    def release_account(self, role: str, lease_id: str) -> dict:
        if not self._host_available():
            raise LookupError(f"No account bound for role {role!r}")
        ack = self._call('account_release', role=role, lease_id=lease_id, wait_op='ack_account_release')
        if not ack.get("ok"):
            self._raise_account_refusal(ack, f"release_account({role!r}) failed")
        return {k: ack[k] for k in ("committed", "revision") if k in ack}


    def client_caps(self) -> dict:
        """What the client driving the host can do (``gen.client_caps``).
        Dev mode has no client at all, so it reports ``client="dev"`` with
        every capability off — the same shape a generator gets from a
        browser, so capability checks need no dev-mode special case."""
        if not self._host_available():
            return dict(ipc.DEV_CLIENT_CAPS)
        ack = self._call('client_caps', wait_op='ack_client_caps')
        caps = ack.get("caps")
        return caps if isinstance(caps, dict) else dict(ipc.DEV_CLIENT_CAPS)


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def _frame(payload: dict) -> str:
    """``payload`` as one IPC line, or :class:`ValueError` naming its op
    when the line is longer than :data:`collimate._host.ipc.IPC_MAX_LINE_BYTES`."""
    # ensure_ascii (json.dumps's default) keeps the line pure ASCII, so its
    # length is its byte count on the wire.
    line = json.dumps(payload) + "\n"
    if len(line) > ipc.IPC_MAX_LINE_BYTES:
        raise ValueError(_oversize_message(payload.get("op"), len(line)))
    return line


def _oversize_message(op: Any, size: int) -> str:
    """The :class:`ValueError` text for a frame over the host's cap."""
    return (
        f"{op!r} is {size} bytes on the wire, over the host's "
        f"{ipc.IPC_MAX_LINE_BYTES}-byte IPC frame limit, so it was not sent: the "
        "content it carries inline is too large. Send less per call (fewer "
        "or smaller files); a large file belongs in this run's own cell "
        "(gen.update_file, then gen.commit), which the host reads from the "
        "workspace with no frame limit")




def _inbox_declaration(path: str, *, folder: str, label: str,
                       transport_only: bool, broadcast: bool) -> dict:
    """The run's inbox registration ``{path, label}``, validated before
    anything is sent: an own-run-folder path, bool flags, and ``broadcast``
    only with ``transport_only``."""
    from collimate.surfaces import surface_declaration
    declaration = surface_declaration(path, folder=folder, label=label)
    if not declaration["path"].startswith(folder + "/"):
        raise ValueError("the run's inbox lives in its run folder")
    if not isinstance(transport_only, bool) or not isinstance(broadcast, bool):
        raise ValueError("surface_register transport_only and broadcast must be bool")
    if broadcast and not transport_only:
        raise ValueError("a broadcast inbox is transport-only")
    return declaration


def _read_selected_file(run_dir: str) -> Optional[str]:
    """The cell-relative path the host wrote to ``<run_dir>/selected_file``
    (the file the user had selected when they started the run), or None
    when the run has none."""
    try:
        with open(os.path.join(run_dir, "selected_file"), "r", encoding="utf-8") as fh:
            value = fh.read().strip()
    except OSError:
        return None
    return value or None


def _parse_ui_files(hosted: bool) -> Optional[tuple[str, ...]]:
    """The manifest's ``ui_files`` suffixes from ``COLLIMATE_UI_FILES``.

    A hosted run always gets the list (``[]`` when nothing is declared), so a
    missing or malformed value there is a host bug and raises. Without a host
    the variable is optional: unset means ``None`` (every suffix accepted)."""
    raw = os.environ.get("COLLIMATE_UI_FILES", "").strip()
    if not raw:
        if hosted:
            raise RuntimeError(
                "COLLIMATE_UI_FILES is missing: the host sends every run the "
                "UI file types its manifest declares")
        return None
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError(f"COLLIMATE_UI_FILES is not JSON: {exc}") from exc
    if not isinstance(value, list) or not all(
            isinstance(v, str) and v.startswith(".") and len(v) > 1 for v in value):
        raise RuntimeError(
            "COLLIMATE_UI_FILES must be a JSON list of filename suffixes "
            "starting with a dot")
    return tuple(value)


def _parse_env_object(name: str) -> dict:
    """The JSON object the host passed in env var ``name``; ``{}`` when the
    variable is unset or blank. A value that is not a JSON object is a host
    bug and raises :class:`ValueError` — never read as ``{}``, which for the
    grant would mean unrestricted."""
    raw = os.environ.get(name, "")
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} is not valid JSON ({exc})") from None
    if not isinstance(parsed, dict):
        raise ValueError(f"{name} must be a JSON object, got {type(parsed).__name__}")
    return parsed


__all__ = [
    "AnnotationsSealed",
    "Cancelled",
    "HostBridge",
    "IPCDisconnectedError",
    "IPCError",
    "IPCNakError",
    "IPCTimeoutError",
]
