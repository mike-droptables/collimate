# SPDX-License-Identifier: MIT
"""Singleton runtime API for generators.

A generator script imports the top-level :mod:`gen` package, a plain
re-export of this module.

Entrypoint contract::

    import gen

    async def run():
        gen.update_file("output.md", "...")
        await gen.commit(name="Hello")

    if __name__ == "__main__":
        gen.main(run)

``gen.main`` constructs a single :class:`collimate._host.bridge.HostBridge`
(IPC + signal handling + filesystem + state tracking), hydrates the
singleton, dispatches ``run_fn`` (sync or async), and emits the finish
IPC on the way out.

A run reads and writes its own cell. :func:`commit` flushes staged writes
and :func:`reveal_file` shows a file; :data:`annotations` carries the user's
messages to the run and the run's replies back. Host-placed environments,
terminals and served chat sessions let a generator expose interactive work
in that same cell.

Writes staged with ``update_file`` and never flushed are committed when
``run()`` returns cleanly (:func:`main`'s auto-flush); a run that writes
nothing leaves its cell as it found it.

Several runs may work in one cell at once. Each has a run folder,
``_runs/<label>/`` (:func:`run_info`), whose files are its locked files —
the files that are the run's user interface. The run folder is for UI files
ONLY: a run writes it solely through :func:`update_ui_file` /
:func:`delete_ui_file`, for the suffixes its manifest declares under
``ui_files``, and its work output goes anywhere else in the cell through
:func:`update_file`. Every other run's folder is read-only to it.
"""
from __future__ import annotations

from collections import deque
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Iterator, Optional
import asyncio
import functools
import inspect
import json
import math
import os
import weakref

from collimate._host.bridge import (
    AnnotationsSealed as _AnnotationsSealed,
    Cancelled,
    HostBridge,
    FileConflictError,
    IPCDisconnectedError as _IPCDisconnectedError,
    IPCError as _IPCError,
    IPCNakError as _IPCNakError,
    IPCTimeoutError as _IPCTimeoutError,
    PartialRefusal,
)
from collimate.transcript import Message
from collimate._mirror import Mirror, LocalWorkspace, CellStore
from collimate import fs as _fs
from collimate._host import ipc as _ipc

# The single result/spec abstraction shared by the host-free testing
# harness. Re-exported here so generators can
# refer to them as ``gen.GeneratorResult`` etc. (same object identity as
# ``collimate.testing``'s re-exports).
from collimate._results import (  # noqa: F401
    Annotation,
    AnnotationNotice,
    ConfigField,
    ConfigValidation,
    ContextItem,
    DraftFileSchema,
    Endpoint,
    GeneratorResult,
    HandlerInfo,
    HandlerType,
    ValidationResult,
    RunInfo,
)
from collimate.options_stack import read_options_stack
from collimate.permissions import Grant
from collimate.pointers import (
    RUNS_PREFIX, is_run_path, norm_path, run_card_path,
)


# ──────────────────────────────────────────────────────────────────────
# Module-level singleton state.
#
# ``set_bridge()`` (called by :func:`main`) stores the live
# :class:`HostBridge`. Generators read/write through the public
# ``gen.*`` functions; they never touch ``_bridge`` directly.
# ──────────────────────────────────────────────────────────────────────

_bridge: Optional[HostBridge] = None


# ──────────────────────────────────────────────────────────────────────
# Public exception types
# ──────────────────────────────────────────────────────────────────────


IPCError = _IPCError
IPCNakError = _IPCNakError
IPCTimeoutError = _IPCTimeoutError
IPCDisconnectedError = _IPCDisconnectedError


# ──────────────────────────────────────────────────────────────────────
# Runner / lifecycle hooks
# ──────────────────────────────────────────────────────────────────────


def set_bridge(bridge: Optional[HostBridge]) -> None:
    """Hydrate the singleton with a live :class:`HostBridge` — a new run, so
    no phase, open approval card, served chat, transcript or annotation
    subscription carries over from a previous one."""
    global _bridge, _serving_chat
    _bridge = bridge
    history.reset()
    _last_phase[:] = ["", ""]
    _open_asks[0] = 0
    _phase_before_asks[0] = ("", "")
    _serving_chat = False
    _inbox_shows_cards[0] = False
    _session.reset()
    annotations.reset()
    _permission_answers.clear()
    _config_requests.clear()


def current_bridge() -> Optional[HostBridge]:
    """Return the current run bridge, or None between runs.

    Runtime support for host-free execution and forwarding; not a gen export.
    """
    return _bridge


def _clear_bridge() -> None:
    global _bridge
    _bridge = None


def require_bridge() -> HostBridge:
    if _bridge is None:
        raise RuntimeError(
            "gen.* used outside a generator run "
            "(call gen.main(run) at the entrypoint)"
        )
    return _bridge


# ──────────────────────────────────────────────────────────────────────
# Module-level dynamic attributes
#
# ``gen.prompt`` / ``gen.cancelled`` / ``gen.selected_file`` resolve
# against the current run's bridge. PEP 562 ``__getattr__`` at module level;
# ``gen`` forwards exactly these names (:data:`DYNAMIC_ATTRS`).
# ``gen.selected_file`` is the cell-relative path of the file the user had
# selected when they started the run (None when none was, or the generator
# does not take one); the generator reads its content itself.
# ──────────────────────────────────────────────────────────────────────

DYNAMIC_ATTRS: tuple = ("prompt", "cancelled", "selected_file")


def __getattr__(name: str) -> Any:
    if name == "prompt":
        return require_bridge().prompt
    if name == "selected_file":
        return require_bridge().selected_file
    if name == "cancelled":
        return require_bridge().cancelled
    raise AttributeError(f"module 'collimate.native' has no attribute {name!r}")


# ──────────────────────────────────────────────────────────────────────
# Inputs
# ──────────────────────────────────────────────────────────────────────


def options() -> dict | None:
    """Read the options declaration Split would choose, or None.

    Root definitions precede nested definitions, then path order breaks ties.
    Protected run files cannot be consumed as the selected declaration.
    """
    from collimate.pointers import is_run_path
    bridge = require_bridge()
    paths = bridge.fs_list()
    return read_options_stack(paths, bridge.fs_read_any,
                              protected_paths=(path for path in paths if is_run_path(path)))


def config(name: str) -> dict:
    """Read a draft file declared in the manifest's ``draft_files``.

    Always returns a dict. ``.set_yaml`` / ``.yaml`` / ``.yml`` parse
    as YAML (with ``fields:`` defaults merged into the ``values:``
    block). ``.json`` parses as JSON. Returns ``{}`` for a draft file of
    any other type and when the file is missing or malformed, so callers
    can use ``cfg.get(...)`` without try/except.
    """
    raw = require_bridge().config(name)
    return raw if isinstance(raw, dict) else {}


# ──────────────────────────────────────────────────────────────────────
# Config folders (runner-provided skill/mcp/script folders)
#
# A runner can provide named config folders — real file trees the host
# stages into ``.collrun/config/folders/<name>/…`` BEFORE input hydration,
# so their content never mixes into the input files. The names a runner may
# provide are the target generator's declared ``config_folders`` (manifest).
# These read the staged content (``.collrun`` read-guard exempt, like
# :func:`config`); a folder the runner didn't provide reads empty.
# ──────────────────────────────────────────────────────────────────────


def config_folder(name: str) -> dict:
    """Return every file the runner staged under config folder ``name`` as
    ``{relpath: str | bytes}`` (relpath relative to the folder root; ``str``
    when utf-8 decodable else ``bytes``). ``{}`` when the folder was not
    provided."""
    return require_bridge().config_folder(name)


def config_folder_path(name: str) -> str:
    """Real filesystem path to a staged config folder — for a subprocess cwd,
    a Docker mount, or a tool that takes a directory. Points at
    ``.collrun/config/folders/<name>`` whether or not it exists."""
    return require_bridge().config_folder_path(name)


def config_folders() -> list[str]:
    """Sorted names of the config folders the runner staged for this run.
    ``[]`` when none were provided."""
    return require_bridge().config_folders()


# ──────────────────────────────────────────────────────────────────────
# Permissions (the run's effective grant)
# ──────────────────────────────────────────────────────────────────────


def grant() -> Grant:
    """This run's effective grant as a typed :class:`Grant` — one attribute
    per axis and ``.at_least(axis, level)`` for the ladders. A generator
    shapes what it offers from it (which tools it arms, which sub-features it
    serves). Which axes the host enforces and which the generator must
    enforce itself is set out in :mod:`collimate.permissions`."""
    return Grant.from_dict(require_bridge().permissions)


def run_info() -> RunInfo:
    """Who this run is to the host: its ``run_id``, ``channel``, the
    ``cell_id`` of the cell it writes in, the saved runner it was started
    as (``runner_id``, ``runner_name``), its ``label``
    (``<runner_id>-<run_id[:8]>``) and its run ``folder`` (``_runs/<label>``).

    Several runs can work in one cell; the label is how each is told apart
    (and how its annotation posts are tagged), and every file inside the folder is the
    run's locked file. A host run always has its run id, channel, label and
    folder (the bridge refuses to start without them). A run with no host
    mints a run id and is labelled ``dev-<run_id[:8]>``."""
    b = require_bridge()
    return RunInfo(
        run_id=b.run_id, channel=b.channel, cell_id=b.cell_id,
        runner_id=b.runner_id, runner_name=b.runner_name,
        label=b.label, folder=b.folder,
    )


def run_path(rel: str) -> str:
    """The cell-relative path of ``rel`` inside this run's folder —
    ``gen.run_path("chat/conversation.chat")`` is
    ``"_runs/<label>/chat/conversation.chat"`` — the path a pointer is
    written and revealed by (:func:`update_ui_file` returns the same path for the
    file it writes). A ``rel`` that would leave the folder raises
    :class:`ValueError`."""
    norm = norm_path(str(rel or ""))
    if norm in ("", ".") or norm == ".." or norm.startswith("../"):
        raise ValueError(f"run_path({rel!r}): name a file inside the run folder")
    return f"{require_bridge().folder}/{norm}"


def proxy_url(port: int) -> str:
    """The app-origin path a browser client reaches a port this run serves
    through (register the port with :func:`serve_port` first). It is the one
    spelling of the host's proxy route — never build it by hand."""
    info = run_info()
    return f"/proxy/{info.channel}/{info.run_id}/{int(port)}/"


def account(role: str) -> dict:
    """Resolve a role bound in this runner to its private account snapshot.

    Metadata includes service, protocol, endpoint, model and capabilities.
    Secrets are scoped to this role and must never be written into cell files.
    Missing or ungranted roles raise LookupError or PermissionError; no prompt
    or provider fallback occurs.
    """
    return require_bridge().account(role)


def commit_account(role: str, *, expected_revision: int, secrets: dict) -> dict:
    """Persist rotated credentials for a bound role, compare-and-swap on
    ``expected_revision`` (the revision of the account snapshot this run
    holds; any other raises :class:`RuntimeError`). When the account
    changed in Key manager since the run started, nothing is written back
    (``committed`` is False); this run keeps its refreshed values either
    way. Returns ``{"committed", "revision"}``.

    A run replaces only credentials the account already stores (the fields
    of its snapshot's ``secrets``) and, unless the account is ``custom``,
    its ``expires_at`` and ``token_type``, each with a nonempty string. It
    cannot add a field or remove one: a ``None`` or an empty or non-string
    value raises :class:`RuntimeError` and writes nothing, and so does a
    field the account does not store (unless the account changed meanwhile,
    when nothing is written back anyway).
    """
    return require_bridge().commit_account(role, expected_revision, secrets)


def materialize_account(role: str) -> dict:
    """Lease host-managed temporary CLI credentials for a bound role."""
    return require_bridge().materialize_account(role)


def release_account(role: str, lease_id: str) -> dict:
    """Release a CLI credential lease after all consumers have stopped."""
    return require_bridge().release_account(role, lease_id)


def packages() -> dict:
    """Which version of each explicitly-named upstream artifact this run's
    runner asked for — ``{name: "latest" | "<version>"}``; ``{}`` when the
    runner named none.

    The division of labour: a RUNNER says *which version* it wants, a GENERATOR
    declares *where that artifact lives upstream* (its registry and the build
    arg carrying its version). So moving a harness forward is a runner edit, not
    a generator edit. Resolving ``latest`` against a registry reaches the
    network, so it is gated by the run's ``update_packages`` grant — see
    ``_env_shared.upstream.resolve_requests``."""
    return dict(require_bridge().packages)


# ──────────────────────────────────────────────────────────────────────
# Client capabilities (what the UI on the other end can do)
# ──────────────────────────────────────────────────────────────────────


async def client_caps() -> dict:
    """What the client driving the app can do, as a flat dict.

    ``{"client": "electron"|"browser"|"dev", "native_guests": bool,
    "guest_capture": bool, "persistent_partitions": bool,
    "direct_loopback": bool, "external_open": bool,
    "custom_title_bar": bool, "custom_window_controls": bool}`` — the
    last two are page chrome (who draws the title bar), reported for
    completeness rather than for generators to act on.

    Orthogonal to :func:`grant`: that says what this run is *allowed*
    to do, this says what the surface rendering it is *able* to do. A
    generator that can drive a real browser view (Electron) and also has a
    degraded fallback (screenshots into a cell) picks between them here.

    Every key is always present, so a check needs no ``.get`` guard. Dev
    mode reports ``client="dev"`` with everything off — the browser answer,
    which is the safe default for a capability check."""
    bridge = require_bridge()
    return await asyncio.to_thread(bridge.client_caps)


# ──────────────────────────────────────────────────────────────────────
# Output stage (workspace filesystem)
# ──────────────────────────────────────────────────────────────────────


def update_file(path: str, content: "str | bytes", *, category: str = "file") -> None:
    """Stage a write to the output cell's file at ``path``.

    Accepts either ``str`` (utf-8 text) or ``bytes`` (binary). Writes
    immediately to the workspace (so subprocesses see it) and marks the
    path for inclusion in the next :func:`commit` flush. Multiple writes
    overwrite — last write wins.

    ``category`` selects the artifact's destination on flush:

    * ``'file'`` (the default) — ordinary cell content, exactly as before.
    * ``'log'`` — a run LOG artifact (a thinking trace, a session record,
      debug output). It rides the same :func:`commit` flush but the host
      files it under the run's Logs (``run_artifacts``) instead of the
      cell's Files, so it shows in the run node's Logs section and never
      hydrates into future runs. The path keeps working with
      :func:`read_file` / :func:`has_file`, but is excluded from
      :func:`list_files`, live-files mirroring, and workspace snapshots.
      A later plain (``'file'``) write to the same path reclassifies it
      back to cell content. The host reserves the names ``logs.txt`` (the
      run's own projected log) and ``annotations.json`` (its annotation
      context): a log write to either raises :class:`ValueError`.

    Every ``_runs/`` path raises :class:`PermissionError` at once: another
    run's folder is read-only to this one, the run card is the host's, and
    this run's own folder holds its UI files only, written with
    :func:`update_ui_file`. Work output goes anywhere else in the cell.
    """
    if category not in ("file", "log"):
        raise ValueError(
            f"update_file: category must be 'file' or 'log', got {category!r}",
        )
    path = norm_path(path)
    _refuse_reserved_log("update_file", path, category)
    require_bridge().stage_file(path, content, category=category)


def _refuse_reserved_log(what: str, path: str, category: str) -> None:
    """:class:`ValueError` for a log write to a name the host writes itself
    (:data:`collimate._host.ipc.RESERVED_LOG_NAMES`)."""
    if category == "log" and path in _ipc.RESERVED_LOG_NAMES:
        raise ValueError(f"{what}: {path} is the host's own run log name; "
                         "write the log under another path")


def track_file(path: str, *, category: str = "file", max_bytes: int | None = None) -> None:
    """Stage a file already written in the workspace without changing bytes.

    Flush with :func:`commit`. Ownership, log classification and the
    reserved log names match :func:`update_file`; symlinks and non-regular
    files are refused.
    ``max_bytes`` optionally refuses an oversize file before it is staged.
    """
    if category not in ("file", "log"):
        raise ValueError(f"track_file: category must be 'file' or 'log', got {category!r}")
    path = norm_path(path)
    _refuse_reserved_log("track_file", path, category)
    if is_run_path(path):
        raise PermissionError(f"Use update_ui_file for run-folder files: {path}")
    require_bridge().track_write(path, category=category, max_bytes=max_bytes)


def read_file(path: str) -> "str | bytes":
    """Read from the output stage. Raises :class:`FileNotFoundError`
    when the path is missing. Returns ``str`` when utf-8 decodable,
    else ``bytes``."""
    path = norm_path(path)
    bridge = require_bridge()
    if not bridge.fs_exists(path):
        raise FileNotFoundError(path)
    return bridge.fs_read_any(path)


def has_file(path: str) -> bool:
    """Predicate over the output stage."""
    return require_bridge().fs_exists(norm_path(path))


def list_files() -> list[str]:
    """Sorted list of every staged file's relative path."""
    return sorted(require_bridge().fs_list())


def delete_file(path: str) -> None:
    """Drop ``path`` from the output stage. The next :func:`commit`
    call passes it as ``deleted_files`` so the host strips it from the
    cell's carried-forward file set. The same ``_runs/`` rule as
    :func:`update_file` applies: every ``_runs/`` path raises
    :class:`PermissionError` (remove this run's own UI files with
    :func:`delete_ui_file`)."""
    path = norm_path(path)
    require_bridge().fs_unlink(path)


# ──────────────────────────────────────────────────────────────────────
# UI files (the run folder)
# ──────────────────────────────────────────────────────────────────────


def _ui_path(name: str) -> str:
    """The cell-relative path of the UI file ``name`` (folder-relative,
    subfolders allowed) or this run's own full path. Empty, absolute,
    parent-climbing and other-run paths raise :class:`ValueError`."""
    raw = str(name or "").replace("\\", "/")
    own = require_bridge().folder + "/"
    if raw.startswith(own):
        raw = raw[len(own):]
    parts = raw.split("/")
    if (not raw.strip() or raw.startswith("/") or ".." in parts
            or parts[0] == RUNS_PREFIX):
        raise ValueError(
            f"UI file name {name!r} must be a path inside the run folder, "
            "relative to it (e.g. 'chat/conversation.chat')")
    return run_path(raw)


def update_ui_file(name: str, content: "str | bytes") -> str:
    """Create or update this run's UI file ``name`` — the file
    ``<run folder>/<name>`` (``name`` is relative to the folder; subfolders
    are allowed: ``gen.update_ui_file("chat/conversation.chat", ...)``) — and return its
    cell-relative path. The returned full path is also accepted as ``name``.

    UI files are the run's interface to the user (a ``.chat`` pointer, a
    ``.terminal``, a ``.webview`` tab, the documents they show)
    and the only files a run writes into its folder. Their types are the
    filename suffixes the generator's manifest declares under ``ui_files``;
    any other suffix raises :class:`ValueError` naming that manifest key. The
    write is tracked like any other and committed by the next
    :func:`commit`; show it with :func:`reveal_file`. The files are locked
    while the run lives (other runs read them, never write them) and removed
    when it stops.

    The run folder is for UI files only: work output (edits, clones,
    summaries, captures) goes anywhere else in the cell with
    :func:`update_file`."""
    path = _ui_path(name)
    require_bridge().fs_write_ui(path, content)
    return path


def delete_ui_file(name: str, *, log: bool = False) -> None:
    """Remove this run's UI file ``name`` (relative to the run folder, as in
    :func:`update_ui_file`) from the cell with the next :func:`commit`. ``log=True``
    keeps its last content in the run's Logs under the same path — the
    record a run keeps of a surface it closes. The host removes whatever UI
    files a run leaves when it stops, so this is for closing a surface
    mid-run or leaving a tidy log."""
    require_bridge().fs_unlink_ui(_ui_path(name), log=log)


def list_ui_files() -> list[str]:
    """This run's UI files the SDK knows about: the files in its run folder
    whose type the manifest declares (the host's run card excepted), as
    folder-relative names, sorted — what :func:`delete_ui_file` takes."""
    return require_bridge().ui_file_names()


def require_ui_types(what: str, *paths: str) -> None:
    """:class:`ValueError` naming ``what`` when any of ``paths`` has a type
    the manifest's ``ui_files`` does not declare — raised before a surface
    starts, so an undeclared type never fails halfway through."""
    bridge = require_bridge()
    missing = [p for p in paths if not bridge.ui_type_declared(p)]
    if missing:
        declared = ", ".join(bridge.ui_suffixes or ()) or "none"
        raise ValueError(
            f"{what} writes {', '.join(missing)}: declare its suffix in the "
            f"generator manifest's ui_files (declared: {declared})")


# ──────────────────────────────────────────────────────────────────────
# Cell operations
# ──────────────────────────────────────────────────────────────────────


async def commit(*, name: Optional[str] = None) -> None:
    """Flush the current stage (tracked writes) to the run's cell.

    May be called many times during a run; each call refreshes the
    visible cell live. Writes still staged when ``run()`` returns are
    flushed by :func:`main`, so a closing call is needed only to set
    ``name``. The host applies ``name`` only to a fresh cell created for
    this run. A run targeting an existing cell cannot rename it.

    The host refuses a write or delete of another run's locked file, of
    this run's card, and of an own-folder file whose type the manifest's
    ``ui_files`` does not declare, and commits the rest; the refusal raises
    :class:`PermissionError` naming each path and its owner or the handler
    verifier's error. Accepted siblings have already committed.

    Any other failure (a failed host commit, timeout, NAK or closed channel)
    raises too, and nothing staged is lost: the flush's writes and
    deletions are staged again — a path written or deleted since keeps its
    newer state — so the next :func:`commit`, or the flush :func:`main`
    runs after ``run()`` returns, sends them again. Flushes run one at a
    time, in the order they were called. A cancel cannot stop a round trip
    already on its way, so a cancelled call waits for it, stages its files
    again if it failed, and then raises :class:`asyncio.CancelledError`.
    """
    bridge = require_bridge()
    async with _flush_lock():
        # Snapshot on the loop thread: loop tasks keep staging writes while
        # the flush runs on a worker thread.
        writes = bridge.take_writes()
        flight = asyncio.get_running_loop().run_in_executor(None, functools.partial(
            bridge.update_cell, name=name, writes=writes))
        # A cancel cannot stop the worker thread, which carries the only copy
        # of what this flush took: the lock is held until it returns, so no
        # later flush overtakes it, and a failure is staged again before the
        # cancel is raised. ``asyncio.wait`` never cancels ``flight``.
        cancel: Optional[asyncio.CancelledError] = None
        while not flight.done():
            try:
                await asyncio.wait((flight,))
            except asyncio.CancelledError as exc:
                cancel = exc
        failure = flight.exception()
        # A partial refusal committed the rest; anything else committed nothing.
        landed = failure is None or isinstance(failure, PartialRefusal)
        if not landed:
            bridge.restage_writes(writes)
        if cancel is not None:
            # The cancel still ends the task, and says whether the flush
            # landed for a caller that must not take a landed one for lost
            # (the live mirror's rollback, see Mirror._commit).
            cancel._flush_landed = landed
            if isinstance(failure, PartialRefusal):
                cancel._flush_refusal = failure
            raise cancel
        flight.result()


# One flush at a time on each event loop: :func:`commit` holds its loop's
# lock from the take of the staged sets to the restage of a failure, a
# cancelled flush's included. A second flush then never overtakes the
# first on the wire (a newer write of a path sent before the older delete
# of it), nor takes the newer state of a path that the first, failing,
# would then stage again over it.
_FLUSH_LOCKS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = (
    weakref.WeakKeyDictionary())


def _flush_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _FLUSH_LOCKS.get(loop)
    if lock is None:
        lock = _FLUSH_LOCKS[loop] = asyncio.Lock()
    return lock


def _flush_staged(bridge: HostBridge) -> None:
    """Flush everything staged to the run's cell, synchronously, staging
    it again when the flush fails with anything but a partial refusal.
    Runs once the run's event loop is gone, so no other flush is in
    flight."""
    writes = bridge.take_writes()
    try:
        bridge.update_cell(writes=writes)
    except PartialRefusal:
        raise
    except Exception:
        bridge.restage_writes(writes)
        raise


def final_flush(bridge: HostBridge) -> None:
    """The flush after ``run()`` returns (:func:`main`, and the testing
    harness the same way): commit everything still staged, or fail the run.

    A :class:`PartialRefusal` committed the rest, so it is logged and the
    run stands. Any other failure (a NAK's :class:`PermissionError`
    included) committed nothing, and a run whose output did not land is
    not a success: it raises :class:`RuntimeError`."""
    try:
        _flush_staged(bridge)
    except PartialRefusal as exc:
        bridge.log.warning("final flush: %s", exc)
    except Exception as exc:
        raise RuntimeError(
            "the final flush failed, so the files staged since the last "
            f"gen.commit() were not committed: {exc}") from exc


async def reveal_file(path: str) -> None:
    """Show the cell file ``path`` to the user: the clients open it as a tab
    of this run's cell — activated when the cell's active tab is empty or
    already one of this run's files, else opened beside it. It is how a run
    puts a surface (a pointer, a new web tab, a summary) in front of the
    user.

    The host shows only committed files, so flush first — or let
    ``reveal_file`` flush for you: a ``path`` still staged (written or
    deleted since the last :func:`commit`) is flushed before the reveal is
    sent."""
    path = norm_path(path)
    bridge = require_bridge()
    if path in bridge.tracked_writes or path in bridge.tracked_deletions:
        await commit()
    bridge.reveal_file(path)


# ──────────────────────────────────────────────────────────────────────
# Chat history (per-thread, open-format transcript files)
# ──────────────────────────────────────────────────────────────────────


def _read_transcript(bridge: HostBridge, path: str) -> Optional[list[Message]]:
    """The messages of the transcript file ``path``, or ``None`` when it does
    not exist. Read and parse errors propagate."""
    from collimate.transcript import parse_transcript
    if not bridge.fs_exists(path):
        return None
    raw = bridge.fs_read_any(path)
    text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    return parse_transcript(text)


class _History:
    """In-memory transcript for the ACTIVE chat thread, persisted to a visible
    open-format JSON file (``<threads_dir>/<thread>.json``; see
    :mod:`collimate.transcript`).

    A run can hold several transcripts (the serving conversation plus any
    ``gen.session.submit_turn`` side transcripts), each in its own file;
    :func:`serve_chat` selects the conversation once at boot via
    :meth:`switch_thread`. ``append(role, content)`` rewrites the active
    thread's file through the normal output stage; :meth:`seed` reloads it so
    a run over a cell that already carries a transcript continues it.

    ``persist`` is False while a :func:`serve_chat` runs with
    ``files=False``: the transcript then lives in memory for the run —
    nothing is written, and :meth:`seed` keeps what memory holds.

    ``threads_dir`` has no default: :func:`serve_chat` sets it (inside the
    run folder), and a persisted transcript has no file before that.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        """A new run's history: empty, persisted, no threads folder yet."""
        self._messages: list[Message] = []
        self.persist = True
        self.threads_dir: Optional[str] = None
        self.active = "main"

    @property
    def file(self) -> str:
        if not self.threads_dir:
            raise RuntimeError(
                "gen.history has no threads_dir yet: gen.serve_chat sets it")
        return f"{self.threads_dir}/{self.active}.json"

    def append(self, role: str, content: str, *, thinking: str = "",
               attachments: Optional[list] = None,
               tools: Optional[list] = None, stopped: bool = False) -> None:
        self._messages.append(Message(
            role=role, content=content, thinking=thinking,
            attachments=list(attachments or []),
            tools=list(tools or []), stopped=bool(stopped),
        ))
        self._flush()

    def adopt(self, messages: list) -> None:
        """Take ``messages`` as the active thread's transcript (in memory)."""
        self._messages = list(messages)

    def _flush(self) -> None:
        if not self.persist:
            return
        from collimate.transcript import serialize_transcript
        require_bridge().fs_write_ui(self.file, serialize_transcript(self._messages))

    def switch_thread(self, thread_id: str, *, threads_dir: Optional[str] = None,
                      load: bool = True) -> None:
        """Make ``thread_id`` the active thread and (by default) load its
        transcript from disk (empty when the thread is new, and always empty
        for an in-memory transcript)."""
        if threads_dir:
            self.threads_dir = threads_dir
        self.active = thread_id
        if load and self.persist:
            self.seed()
        else:
            self._messages = []

    def seed(self) -> bool:
        """(Re)load the active thread's transcript from disk. False when none
        exists yet (and the in-memory list is reset to empty). An in-memory
        transcript has no disk copy: it keeps what it holds. Read or parse
        errors propagate without replacing the retained messages."""
        if not self.persist:
            return bool(self._messages)
        bridge = require_bridge()
        msgs = _read_transcript(bridge, self.file)
        self._messages = msgs or []
        return msgs is not None

    def __iter__(self) -> Iterator[Message]:
        return iter(self._messages)

    def __len__(self) -> int:
        return len(self._messages)

    def __getitem__(self, idx):  # type: ignore[no-untyped-def]
        return self._messages[idx]


history = _History()


# ──────────────────────────────────────────────────────────────────────
# The run's annotation context (gen.annotations)
# ──────────────────────────────────────────────────────────────────────

# How often the one poller of a run asks the host for its context's notice,
# and the longest it waits after a run of refused polls.
_ANNOTATIONS_POLL_INTERVAL_S = 0.25
_ANNOTATIONS_POLL_BACKOFF_MAX_S = 5.0


class AnnotationSubscription:
    """What :meth:`gen.annotations.subscribe <_Annotations.subscribe>`
    returns: ``close()`` ends it (idempotent), and it is a context manager
    that closes on exit."""

    def __init__(self, owner: "_Annotations", on_new, on_interrupt) -> None:
        self._owner = owner
        self.on_new = on_new
        self.on_interrupt = on_interrupt
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._owner._unsubscribe(self)

    def __enter__(self) -> "AnnotationSubscription":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _cancel_task(task) -> None:
    if task is not None and not task.done():
        try:
            task.cancel()
        except RuntimeError:  # its event loop is gone
            pass


class _Annotations:
    """``gen.annotations`` — the run's annotation context: the user's
    messages to the run and the run's replies (see
    ``shared/docs/api-reference.md``, Annotations).

    A host run has one; a dev run (no host) and a forwarded
    ``test_generator`` child have none: :attr:`available` is False,
    :meth:`pull` and :meth:`list` return ``[]``, :meth:`post` raises
    :class:`LookupError` and a subscription never fires.

    The watching state lives here, not in the poller, so a poller that
    restarts (the last subscription closed, a new one opened) carries on
    from it: ``_rev_seen`` is the revision ``on_new`` last looked at and
    ``_interrupt_seen`` the highest ``interrupt_seq`` already dispatched or
    answered by a pull. ``_pulls`` counts the pulls in flight, during which
    no interrupt is dispatched: the pull's answer says which it answers."""

    def __init__(self) -> None:
        self._poller = None
        self.reset()

    def reset(self) -> None:
        """A new run: no subscription, poller, notice, watching state or
        undelivered batch carries over."""
        _cancel_task(self._poller)
        self._subscriptions: list = []
        self._poller = None
        self._notice = AnnotationNotice(0, 0, 0)
        self._pull_notice = AnnotationNotice(0, 0, 0)
        self._rev_seen: Optional[int] = None
        self._interrupt_seen = 0
        self._pulls = 0
        self._undelivered: list = []
        self._stopped = False

    @property
    def available(self) -> bool:
        """Whether this run has an annotation context."""
        return require_bridge().annotations_available()

    @property
    def notice(self) -> AnnotationNotice:
        """The newest :class:`AnnotationNotice` the run has seen: the last
        poll's, or the last pull's when that one is newer. A notice never
        goes back to an older revision."""
        return self._notice

    @property
    def pull_notice(self) -> AnnotationNotice:
        """The context's :class:`AnnotationNotice` as the last pull left it
        (``AnnotationNotice(0, 0, 0)`` before the first): its
        ``interrupt_seq`` counts every interrupt that pull's batch answers,
        and its ``unread`` what the pull left waiting."""
        return self._pull_notice

    def subscribe(self, on_new, *, on_interrupt=None) -> AnnotationSubscription:
        """Watch the context: ``on_new(notice)`` fires on a poll whose
        revision changed while unread messages wait, and ``on_interrupt(notice)``
        (first) whenever ``interrupt_seq`` passes the highest one already
        dispatched or answered by a pull. Both are plain synchronous
        callables run on the event loop's thread; a coroutine function raises
        :class:`TypeError`, and a callback that raises is logged while
        watching continues. Call it from the run's event loop. Every
        subscription shares one poller (every 0.25 s), which stops at the last
        ``close()``, at Stop, when the context is sealed, or after a transport
        failure (logged once); any other failed poll is logged and retried.
        Without a context the subscription never fires."""
        for name, fn in (("on_new", on_new), ("on_interrupt", on_interrupt)):
            if fn is None and name == "on_interrupt":
                continue
            if not callable(fn) or inspect.iscoroutinefunction(fn):
                raise TypeError(f"gen.annotations.subscribe: {name} must be a plain "
                                "(synchronous) callable")
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            raise RuntimeError("gen.annotations.subscribe: call it from the run's "
                               "event loop") from None
        bridge = require_bridge()
        subscription = AnnotationSubscription(self, on_new, on_interrupt)
        self._subscriptions.append(subscription)
        if bridge.annotations_available() and not self._stopped:
            task = self._poller
            if task is None or task.done() or task.get_loop() is not loop:
                self._poller = loop.create_task(self._poll_loop(bridge))
        return subscription

    def _unsubscribe(self, subscription: AnnotationSubscription) -> None:
        try:
            self._subscriptions.remove(subscription)
        except ValueError:
            return
        if not self._subscriptions:
            poller, self._poller = self._poller, None
            _cancel_task(poller)

    async def _poll_loop(self, bridge: HostBridge) -> None:
        failing = ""
        delay = _ANNOTATIONS_POLL_INTERVAL_S
        while self._subscriptions and not bridge.cancelled:
            try:
                notice = await asyncio.to_thread(bridge.annotations_poll)
            except _AnnotationsSealed:
                self._stopped = True
                return
            except (_IPCDisconnectedError, _IPCTimeoutError) as exc:
                self._stopped = True
                bridge.push_log(f"annotations: stopped watching ({exc})")
                return
            except Exception as exc:  # noqa: BLE001 — a host refusal; retried
                if str(exc) != failing:
                    failing = str(exc)
                    bridge.push_log(f"annotations: a poll failed ({exc}); retrying")
                delay = min(delay * 2, _ANNOTATIONS_POLL_BACKOFF_MAX_S)
                await asyncio.sleep(delay)
                continue
            failing = ""
            delay = _ANNOTATIONS_POLL_INTERVAL_S
            # An answer older than what a pull already reported says nothing new.
            if notice.rev >= self._notice.rev:
                self._notice = notice
                if notice.interrupt_seq > self._interrupt_seen and not self._pulls:
                    self._interrupt_seen = notice.interrupt_seq
                    self._dispatch(bridge, "on_interrupt", notice)
                if notice.unread > 0 and notice.rev != self._rev_seen:
                    self._dispatch(bridge, "on_new", notice)
                self._rev_seen = notice.rev
            await asyncio.sleep(_ANNOTATIONS_POLL_INTERVAL_S)

    def _dispatch(self, bridge: HostBridge, which: str, notice: AnnotationNotice) -> None:
        for subscription in list(self._subscriptions):
            fn = getattr(subscription, which)
            if fn is None or subscription.closed:
                continue
            try:
                result = fn(notice)
            except Exception:  # noqa: BLE001 — one callback never ends watching
                bridge.log.exception("annotations: %s callback failed", which)
                continue
            if inspect.iscoroutine(result):
                result.close()
                bridge.log.error("annotations: %s returned a coroutine; callbacks "
                                 "must be synchronous", which)

    def _pulled(self, notice: AnnotationNotice) -> None:
        """A pull landed, leaving the context at ``notice``."""
        self._pull_notice = notice
        self._interrupt_seen = max(self._interrupt_seen, notice.interrupt_seq)
        if notice.rev >= self._notice.rev:
            self._notice = notice

    async def pull(self) -> "list[Annotation]":
        """Every waiting message — interrupt-mode first, then queue order; an
        unread root's unread replies follow it — now read and locked.

        Exactly once: a pull whose answer was lost is replayed before anything
        newer is pulled, and a pull whose awaiting task is cancelled keeps its
        batch for the next one. Its notice is :attr:`pull_notice`, and an
        interrupt it answers never fires ``on_interrupt``. ``[]`` without a
        context."""
        if self._undelivered:
            batch, self._undelivered = self._undelivered, []
            return batch
        bridge = require_bridge()
        if not bridge.annotations_available():
            return []
        self._pulls += 1
        try:
            flight = asyncio.get_running_loop().run_in_executor(None, bridge.annotations_pull)
            # A cancel cannot stop the worker thread, which may already have
            # read the batch on the host: wait it out and keep what it read.
            cancel: Optional[asyncio.CancelledError] = None
            while not flight.done():
                try:
                    await asyncio.wait((flight,))
                except asyncio.CancelledError as exc:
                    cancel = exc
        finally:
            self._pulls -= 1
        if flight.exception() is not None:
            if cancel is not None:
                raise cancel
            flight.result()
        batch, notice = flight.result()
        self._pulled(notice)
        if cancel is not None:
            self._undelivered = list(batch)
            raise cancel
        return batch

    async def post(self, text: str, *, reply_to: Optional[str] = None) -> str:
        """Post markdown ``text`` from this run, tagged with its label, and
        return the new message's id; ``reply_to`` names a top-level message
        (replies are one level deep).

        Empty text, more than 64 KiB of UTF-8, an empty ``reply_to`` or one
        naming a reply raise :class:`ValueError`; an unknown ``reply_to``
        raises :class:`LookupError`; a sealed context (the run is ending)
        raises :class:`RuntimeError`. A post the host may have taken is
        replayed under its id, never doubled. Without a context it raises
        :class:`LookupError`."""
        if not isinstance(text, str):
            raise TypeError("gen.annotations.post: text must be a str")
        if not text.strip():
            raise ValueError("gen.annotations.post: text must be non-empty")
        if len(text.encode("utf-8")) > _ipc.ANNOTATION_BODY_MAX_BYTES:
            raise ValueError("gen.annotations.post: text is over "
                             f"{_ipc.ANNOTATION_BODY_MAX_BYTES // 1024} KiB of UTF-8")
        if reply_to is not None and (not isinstance(reply_to, str) or not reply_to):
            raise ValueError("gen.annotations.post: reply_to must be a message id or None")
        bridge = require_bridge()
        if not bridge.annotations_available():
            raise LookupError("gen.annotations.post: this run has no annotation context "
                              "(no host, or a forwarded test child)")
        return await asyncio.to_thread(bridge.annotations_post, text, reply_to=reply_to)

    async def list(self) -> "list[Annotation]":
        """The whole context the run sees — its prompt, read and unread
        messages and its own posts — in context order; ``[]`` without a
        context."""
        bridge = require_bridge()
        if not bridge.annotations_available():
            return []
        return await asyncio.to_thread(bridge.annotations_list)


annotations = _Annotations()


# ──────────────────────────────────────────────────────────────────────
# Status / control
# ──────────────────────────────────────────────────────────────────────


def log(line: str) -> None:
    """Append a line to the run log (visible in the events panel)."""
    require_bridge().push_log(line)


# The phase (and its message) this process last sent the host — written by
# :func:`phase` and serve_chat alike, so serve_chat's dedupe reflects what the
# HOST shows, not only its own transitions (a terminal leg's direct
# ``gen.phase("running")`` must not leave serve_chat believing the host still
# shows awaiting_input), and :func:`ask` can restore it when its card closes.
_last_phase: list = ["", ""]


def phase(name: str, message: str = "") -> None:
    """Set the run's lifecycle phase — drives the cell's loading indicator.

    Active phases a generator sets: ``"spinning_up"`` (booting — container
    pull, clone, model warm-up), ``"running"`` (real work), ``"finishing"``
    (committing results), and — for interactive daemons — ``"awaiting_input"``
    (session open, parked on the user; the host renders it calmly and pauses
    the wallclock watchdog). :func:`serve_chat` emits ``running`` /
    ``awaiting_input`` around its turns automatically. :func:`ask` sets
    ``"awaiting_approval"`` while an approval card waits: the host pauses the
    watchdog without restarting the turn's budget, and the phase before the
    card is restored when it closes. The backend drives a
    baseline machine on its own (queued → spinning_up → running → terminal),
    so calling this just sharpens the distinctions. Pair it with a matching
    :func:`log` line so the human and machine states agree.

    Failure is NOT a phase — raise and the run ends failed (a clear
    ``error.md`` first helps the user); the cell then renders the error
    state automatically. A verdict on the work (a check that did not pass)
    is :func:`finish`'s ``outcome``, not a failure.

    While an approval card is open the run stays on ``awaiting_approval``:
    a phase set meanwhile is held and shown when the last card closes.
    """
    _send_phase(name, message)


def _send_phase(name: str, message: str) -> None:
    """Show ``name`` on the host — the one path every phase change takes
    (:func:`phase`, serve_chat's park/run mirror, :func:`ask`). While an
    approval card is open the host keeps showing ``awaiting_approval`` (its
    watchdog stays paused for the human), and the newest phase set meanwhile
    is the one restored when the last card closes."""
    if _open_asks[0] and name != "awaiting_approval":
        _phase_before_asks[0] = (name, message)
        return
    require_bridge().set_phase(name, message)
    _last_phase[:] = [name, message]


def finish(*, note: Optional[str] = None, outcome: Optional[str] = None) -> None:
    """Record the run's own verdict for its end: a one-line ``note`` and an
    ``outcome``, ``"pass"`` or ``"fail"``.

    It does not end the run: the run ends when ``run()`` returns (or
    raises), and :func:`main`'s finish carries what the last call recorded,
    both fields replaced on each call. The note's whitespace is collapsed to
    one line and capped at 300 characters. The outcome is a verdict on the
    work, independent of whether the run completed: a test run that ran its
    checks and found one failing completes with ``outcome="fail"``. Raises
    ``ValueError`` for another outcome and ``TypeError`` for a note that is
    not a string.
    """
    if note is not None and not isinstance(note, str):
        raise TypeError(f"note must be a string, not {type(note).__name__}")
    if outcome is not None and outcome not in _ipc.FINISH_OUTCOMES:
        raise ValueError(f"outcome must be 'pass' or 'fail', not {outcome!r}")
    bridge = require_bridge()
    bridge.run_note = _ipc.finish_note(note)
    bridge.run_outcome = outcome


# ══════════════════════════════════════════════════════════════════════
# OS-level extensions (filesystem, subprocesses, ports, terminals, …)
# ══════════════════════════════════════════════════════════════════════


def handlers() -> list[HandlerInfo]:
    """The channel's enabled handlers, without executable or skill bodies."""
    return require_bridge().handlers()


def handler_for(ext: str) -> HandlerInfo | None:
    """Match a path or suffix by the longest case-folded handler suffix."""
    from collimate.handlers import handler_for as resolve
    return resolve(handlers(), ext)


def handler_skills(ext_or_id: str) -> dict[str, str]:
    """Accepted handler skills, keyed by namespaced relative skill paths."""
    return require_bridge().handler_skills(ext_or_id)


def validate(path: str, verifier: str | None = None) -> ValidationResult:
    """Verify a current workspace file; an explicit verifier is a workspace path.

    Without an applicable verifier, a readable file is valid. Host failures
    propagate; they never become successful validation.
    """
    return require_bridge().validate(path, verifier=verifier)


def workspace_path() -> str:
    """Real filesystem path to the workspace.

    Needed by Docker volume mounts, tools that take a cwd, and any path
    handed to a child process.
    """
    return require_bridge().workspace


def progress(current: int, total: int) -> None:
    """Record a progress counter (``current``/``total``) as a ``PROG`` line
    in the run's log. Fire-and-forget; nothing else in the UI shows it."""
    require_bridge().progress(current, total)


async def serve_port(
    port: int,
    *,
    name: str = "preview",
    path: str = "",
) -> None:
    """Register ``port`` as one this run serves, so the host's proxy route
    (:func:`proxy_url`) reaches it. Pair it with a ``.website`` pointer
    (:func:`collimate.pointers.website_pointer`) in the run folder. A host
    refusal raises here before the caller advertises the port."""
    bridge = require_bridge()
    await asyncio.to_thread(bridge.serve_port, port, name=name, path=path)


def snapshot(*, skip_dirs: Optional[set[str]] = None) -> dict[str, str]:
    """``{relpath: sha256_hex}`` for every workspace file."""
    return require_bridge().snapshot_workspace(skip_dirs=skip_dirs)


async def start_terminal(
    container_id: Optional[str],
    command: list[str],
    *,
    cwd: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
    keep_alive: bool = False,
) -> dict:
    """Allocate a WebSocket-backed PTY session running ``command`` in one of
    the run's containers (``container_id``), or on this machine
    (``container_id=None``, the host form).

    The host form runs the program directly on the host's PTY, as its own
    session with that PTY as its controlling terminal: ``env`` is its whole
    environment (nothing of the host's is inherited, so give it the ``PATH``
    and ``HOME`` it needs, or an absolute ``command[0]``) and ``cwd`` a
    directory of the run's workspace. The host refuses it unless the run's
    grant allows execution on this machine — ``docker: none`` or ``full``,
    ``commands`` not ``deny`` (nothing can hold a deny on an interactive
    TTY) — and, for a run bound to a harness, unless the harness's
    placement puts its CLI on this machine. A container session's ``env``
    reaches the program through ``docker exec``.

    Returns ``{terminal_id, ws_path, token}``. Most generators use
    :func:`serve_terminal`, which also writes, reveals and removes the
    pointer. By hand, write it as a UI file in the run folder:
    ``gen.update_ui_file(gen.run_path("shell.terminal"), json.dumps(terminal_pointer(session,
    label="Shell")))`` (:func:`collimate.pointers.terminal_pointer`; the
    manifest must declare ``.terminal`` under ``ui_files``), then
    :func:`commit` and :func:`reveal_file`.

    ``keep_alive`` opts into the detach-tolerant PTY: a renderer disconnect
    (a tab reload, a closed cell) leaves the process running — detached
    output rings host-side and replays on re-attach — instead of killing it.
    Run teardown still kills either way, the program's whole process group.
    """
    bridge = require_bridge()
    if container_id is None:
        return await asyncio.to_thread(
            bridge.start_host_terminal, command=command, cwd=cwd, env=env,
            keep_alive=keep_alive,
        )
    return await asyncio.to_thread(
        bridge.start_terminal,
        container_id=container_id, command=command, cwd=cwd, env=env,
        keep_alive=keep_alive,
    )


def stop_terminal(terminal_id: str) -> None:
    """Tear down a terminal. Idempotent."""
    require_bridge().stop_terminal(terminal_id)


def terminal_status(terminal_id: str) -> dict:
    """The liveness of a terminal this run serves — ``{"exists": bool,
    "ended": bool, "exit_code": int | None}``. ``ended`` is True once the
    program exited (a CLI that quit, a crashed TUI) or the session is gone.
    A generator parked on a terminal leg polls this to end the leg instead
    of sitting on a dead PTY (the renderer shows "the terminal's process has
    ended" meanwhile). The host op is cheap; poll on the order of a second."""
    return require_bridge().terminal_status(terminal_id)


async def wait_terminal_exit(terminal_id: str, *, poll: float = 1.0) -> int | None:
    """Wait until this terminal's program exits, the terminal disappears, or Stop.

    Return the observed exit code, or ``None`` if no exit code is available
    (including Stop). This does not stop the PTY: use it inside
    :func:`serve_terminal` so leaving the block owns that cleanup. A nonzero
    code is returned to the caller; host status failures propagate. Polling
    runs off the event loop so other run tasks remain responsive.
    """
    bridge = require_bridge()
    while not bridge.cancelled:
        status = await asyncio.to_thread(bridge.terminal_status, terminal_id)
        if status.get("ended") or not status.get("exists"):
            return status.get("exit_code")
        await asyncio.sleep(max(0.05, poll))
    return None


def _in_run_folder(path: str, what: str, *, folder_ok: bool = False) -> str:
    """``path`` normalized, when it lies inside this run's folder (or is the
    folder itself, with ``folder_ok``) and is not its run card; else a
    :class:`ValueError` naming ``what`` — every surface a run serves is one
    of its locked files."""
    bridge = require_bridge()
    folder = bridge.folder
    norm = norm_path(str(path or ""))
    inside = norm.startswith(folder + "/") or (folder_ok and norm == folder)
    if not inside or norm == run_card_path(bridge.label):
        raise ValueError(
            f"{what} {path!r} must lie inside this run's folder {folder}/ "
            "(build it with gen.run_path)")
    return norm


@asynccontextmanager
async def serve_terminal(
    container_id: Optional[str],
    command: list[str],
    *,
    pointer: str,
    cwd: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
    label: str = "Terminal",
    buttons: Optional[list[dict]] = None,
    name: Optional[str] = None,
    keep_alive: bool = False,
) -> AsyncIterator[dict]:
    """Surface a live terminal on the cell for the lifetime of an ``async
    with`` block — allocate the PTY, write and reveal its ``.terminal``
    pointer, and on exit stop the session and delete the pointer, all in one
    place::

        async with gen.serve_terminal(
            container_id, ["bash"], pointer=gen.run_path("shell.terminal"),
            cwd="/workspace", label="Shell", name="Container",
        ) as session:
            exit_code = await gen.wait_terminal_exit(session["terminal_id"])

    ``container_id=None`` serves the host form of :func:`start_terminal`: the
    program on this machine, under the grant checks that form carries.

    ``pointer`` is the pointer file's path, inside the run folder (build it
    with :func:`run_path`; a path elsewhere raises :class:`ValueError`) and
    ending in ``.terminal``, a type the manifest's ``ui_files`` must declare
    (else :class:`ValueError` before the PTY starts); the pointer is written
    as a UI file (:func:`update_ui_file`); ``label`` is the renderer's title bar text;
    ``buttons`` (a list of ``{"id", "label", "title"?, "confirm"?}``) adds
    toolbar buttons whose clicks arrive via :func:`actions` /
    :func:`wait_for_action` (``title`` is the hover text, ``confirm`` a
    question asked before the click is sent). ``name`` rides the
    :func:`commit` flush that commits the pointer. The yielded dict is the
    raw ``{terminal_id, ws_path, token}`` session for callers that need it. On
    exit the PTY is stopped and the pointer deleted even when the body
    raises; do container teardown in your own ``finally`` around this block.
    """
    from collimate.pointers import is_terminal_pointer, terminal_pointer

    pointer = _in_run_folder(pointer, "serve_terminal pointer")
    if not is_terminal_pointer(pointer):
        raise ValueError(f"serve_terminal pointer {pointer!r} must end in .terminal")
    require_ui_types("serve_terminal", pointer)
    bridge = require_bridge()
    session = await start_terminal(container_id, command, cwd=cwd, env=env,
                             keep_alive=keep_alive)
    try:
        bridge.fs_write_ui(
            pointer,
            json.dumps(terminal_pointer(session, label=label, buttons=buttons), indent=2),
        )
        await commit(name=name)
        await reveal_file(pointer)
        yield session
    finally:
        try:
            stop_terminal(session.get("terminal_id", ""))
        except Exception as exc:  # noqa: BLE001 — teardown must not mask the body's error
            log(f"stop_terminal failed: {exc}")
        try:
            bridge.fs_unlink_ui(pointer)
            await commit()
        except Exception as exc:  # noqa: BLE001 — the host clears the folder at run end
            log(f"serve_terminal: removing {pointer} failed: {exc}")


async def wait_for_action(timeout: Optional[float] = None, *, source: str = "") -> Optional[str]:
    """Block until a renderer emits an action, or ``timeout`` elapses,
    or Stop is requested. Returns the action id or ``None``."""
    ev = await wait_for_action_event(timeout=timeout, source=source)
    return ev["action_id"] if ev else None


async def wait_for_action_event(
    *, source: str = "", action_id: str = "",
    timeout: Optional[float] = None,
) -> Optional[dict]:
    """Block until a renderer emits an action MATCHING the filters, or
    ``timeout`` elapses, or Stop is requested. Returns the tagged action
    ``{action_id, source, payload?}`` or ``None``.

    The filters are applied host-side over the keyed queue (agent-v2
    §4.3.4), so several concurrent waiters — a terminal button loop, a
    website tab's buttons — can each wait on their own ``source`` without
    stealing each other's entries; an empty filter matches anything.
    :func:`wait_for_action` stays the id-only sugar for the single-waiter
    case."""
    bridge = require_bridge()
    loop = asyncio.get_running_loop()
    deadline = None if timeout is None else loop.time() + timeout
    delay = 0.15
    while True:
        if bridge.cancelled:
            return None
        ev = await asyncio.to_thread(
            bridge.poll_renderer_action_event, source=source, action_id=action_id,
        )
        if ev:
            return ev
        if deadline is not None and loop.time() >= deadline:
            return None
        remaining = delay if deadline is None else min(delay, max(0.0, deadline - loop.time()))
        await asyncio.sleep(remaining)
        delay = min(1.0, delay * 1.5)


async def actions(*, source: str = "", poll: float = 1.0) -> AsyncIterator[str]:
    """Yield renderer action ids (cell button clicks) until the user stops
    the run — the idiomatic daemon loop for a cell with buttons::

        async with gen.serve_terminal(
            cid, ["bash"], pointer=gen.run_path("app.terminal"),
            buttons=[{"id": "capture", "label": "Capture"}],
        ):
            async for action in gen.actions(source=gen.run_path("app.terminal")):
                if action == "capture":
                    ...

    The iterator ends when the run is cancelled, so the ``async for`` body
    never needs its own ``while not gen.cancelled`` bookkeeping. A daemon
    with no buttons parks on :func:`wait_until_cancelled` instead. ``poll``
    bounds how long each internal :func:`wait_for_action` blocks — it is a
    responsiveness knob, not a rate limit.
    """
    bridge = require_bridge()
    while not bridge.cancelled:
        action = await wait_for_action(timeout=poll, source=source)
        if action:
            yield action


async def wait_until_cancelled(poll: float = 0.5) -> None:
    """Park a daemon generator until the user stops the run. The idiomatic
    body for a generator whose work is already running (a served port, an ``async with gen.live_files()`` mirror) and that just needs to
    stay alive::

        async with gen.live_files():
            ...
            await gen.wait_until_cancelled()
    """
    bridge = require_bridge()
    while not bridge.cancelled:
        await asyncio.sleep(max(0.05, poll))


def emit_stream_event(turn_id: str, event: dict) -> None:
    """Push a typed stream event to this run's inbox session — the one live
    session a run registers (:func:`serve_chat`).

    Fire-and-forget. The renderer reducer interprets ``event`` (e.g.
    ``{"type": "text_delta", "text": ...}``). Most generators use
    :class:`ChatTurn`'s ``delta``/``tool`` rather than calling this directly.
    """
    require_bridge().stream_event(turn_id, event)


# The generator-owned turn queue (agent-v2 §4.1): entries submitted while a
# turn is in flight (gen.session.submit_turn or a user message) wait here and
# start FIFO as turns settle. serve_chat drains it; ``_start_turn`` stays the
# sole turn starter. Reset when a serve_chat session boots.
_turn_queue: "deque" = deque()

# A generator-initiated graceful finish (a one-shot pass that has answered):
# the serving chat breaks its loop once the session is idle (no in-flight
# turn, empty queue) — the run then ends normally, after the generator's last
# turn. Distinct from Stop (bridge.cancelled), which is a hard kill. Reset
# when a serve_chat session boots.
_finish_requested: list = [False]


def request_finish() -> None:
    """Ask the serving chat to end gracefully once it is next idle (a
    one-shot pass that has answered). The current and queued turns drain
    first; then :func:`serve_chat` returns and the run completes normally —
    unlike Stop (a hard cancel), after the generator's own last turn has
    written what it leaves behind. A no-op when nothing is serving a chat."""
    _session.request_finish()


# Approval-card answers delivered by the renderer over the inbox WS
# request/response frame: serve_chat routes an inbound ``permission_answer``
# command here and the :func:`ask` waiting on that card's id takes it.
# Bounded — an unmatched answer can't grow the dict past a handful of stale
# entries.
_permission_answers: dict = {}
_PERMISSION_ANSWERS_MAX = 64


def deliver_permission_answer(answer: dict) -> None:
    """Record one approval-card answer (``{id, option_id?, cancelled?}``) for
    the :func:`ask` waiting on that card. serve_chat's command dispatch is
    the only producer (the testing harness stands in for it)."""
    rid = str((answer or {}).get("id") or "")
    if not rid:
        return
    if len(_permission_answers) >= _PERMISSION_ANSWERS_MAX:
        _permission_answers.pop(next(iter(_permission_answers)), None)
    _permission_answers[rid] = dict(answer)


def _pop_permission_answer(request_id: str) -> Optional[dict]:
    """Non-blocking pop of the answer to approval card ``request_id``
    (``{id, option_id?, cancelled?}``), or ``None``."""
    return _permission_answers.pop(str(request_id or ""), None)


# ── Approval cards ──────────────────────────────────────────────────────
#
# gen.ask is the one human-in-the-loop yes/no. The card wire (documented in
# shared/docs/api-reference.md) spells the option id ``option_id`` in the
# card's options, the renderer's answer, and the resolution.

_ASK_POLL_S = 0.15
_ASK_DEFAULT_OPTIONS = (
    {"option_id": "allow_once", "name": "Allow", "kind": "allow_once"},
    {"option_id": "reject_once", "name": "Reject", "kind": "reject_once"},
)
# Cards open right now, and the phase to show when the last one closes (the
# one before the first card, or the newest set while cards were open):
# several cards can wait at once (an agent's parallel tool calls), and the
# run stays on awaiting_approval until every one is answered.
_open_asks: list = [0]
_phase_before_asks: list = [("", "")]
# Whether serve_chat is serving this run's chat session: a card is shown
# through the run's inbox and its answer comes back through the session's
# command inbox, so a card can be answered only while it serves.
_serving_chat: bool = False
# Whether the inbox serve_chat registered can show a card: its .chat surface
# (files=True) or a broadcast transport-only inbox every viewer attaches to.
# A transport-only inbox without broadcast shows none, so a card is refused.
_inbox_shows_cards: list = [False]


def can_ask() -> bool:
    """Whether a human can answer approval cards in this run: a host is
    connected to carry the run's cards and answers.
    A card travels through the chat session :func:`serve_chat` serves, so
    :func:`ask` shows one only while that session is served; elsewhere, as
    where this is False, it refuses at once instead of waiting out its
    timeout."""
    bridge = require_bridge()
    # The host's IPC hello will carry this fact; that is where it plugs in.
    return bridge.answers_cards()


def _ask_options(options) -> list:
    """The card's options on the wire: ``{option_id, name, kind}`` each, the
    ids non-empty and distinct (a malformed option is a caller bug)."""
    out: list = []
    for o in (_ASK_DEFAULT_OPTIONS if options is None else options):
        oid = str((o or {}).get("option_id") or "") if isinstance(o, dict) else ""
        if not oid:
            raise ValueError(f"ask: every option needs a non-empty option_id, got {o!r}")
        if any(x["option_id"] == oid for x in out):
            raise ValueError(f"ask: option_id {oid!r} is offered twice")
        out.append({"option_id": oid, "name": str(o.get("name") or ""),
                    "kind": str(o.get("kind") or "")})
    if not out:
        raise ValueError("ask: a card needs at least one option")
    return out


def _ask_timeout(timeout) -> float:
    """``timeout`` as a finite, positive number of seconds — the only bound
    on a card's wait, since the watchdog is paused while it waits (anything
    else is a caller bug)."""
    try:
        seconds = float(timeout)
    except (TypeError, ValueError):
        raise ValueError(f"ask: timeout must be a number of seconds, got {timeout!r}") from None
    if not (math.isfinite(seconds) and seconds > 0):
        raise ValueError(f"ask: timeout must be finite and positive, got {timeout!r}")
    return seconds


def _open_ask(title: str) -> None:
    _open_asks[0] += 1
    if _open_asks[0] == 1:
        _phase_before_asks[0] = (_last_phase[0], _last_phase[1])
        _send_phase("awaiting_approval", f"awaiting approval: {title}")


def _close_ask() -> None:
    _open_asks[0] = max(0, _open_asks[0] - 1)
    if _open_asks[0] == 0 and _last_phase[0] == "awaiting_approval":
        name, message = _phase_before_asks[0]
        _send_phase(name or "running", message)


async def ask(
    title: str,
    *,
    kind: str,
    detail: Optional[str] = None,
    options: Optional[list] = None,
    timeout: float = 600,
) -> Optional[str]:
    """Show one approval card through the run's inbox and wait for the
    human's answer; return the chosen ``option_id``, or ``None`` when the card
    is refused.

    ``kind`` names what is being approved (``"execute"``, ``"edit"``,
    ``"fetch"`` …; the renderer picks its icon from it). ``detail`` is the
    text the card shows in full (a command line, a path). ``options`` are
    ``{"option_id", "name", "kind"}`` dicts whose ``kind`` is one of
    ``allow_once`` / ``allow_always`` / ``reject_once`` / ``reject_always``;
    ``None`` offers Allow (``allow_once``) and Reject (``reject_once``).
    ``timeout`` (seconds, finite and positive) bounds the wait. Malformed
    options or a bad timeout raise :class:`ValueError`.

    A card is answered through the run's inbox, the session
    :func:`serve_chat` serves, so ask from a turn (or a task running beside
    one) of a generator that serves chat. It shows on the ``.chat`` surface
    (``serve_chat(files=True)``) or on every viewer of a broadcast inbox
    (``serve_chat(files=False, broadcast=True)``).

    Refused (``None``): nobody can answer (:func:`can_ask` is False, no chat
    session is being served, or the inbox shows no card — a transport-only
    inbox without broadcast — and no card is shown), the user dismissed the
    card, the answer named an option never offered, the run was stopped, the
    chat session ended, or ``timeout`` seconds passed (the card then closes
    with an error notice). While the card waits the run's phase is
    ``awaiting_approval``. Cancelling the awaiting task closes the card as
    refused."""
    bridge = require_bridge()
    opts = _ask_options(options)
    wait_s = _ask_timeout(timeout)
    valid = {o["option_id"] for o in opts}
    if not can_ask():
        log(f"approval card {title!r} refused: nobody can answer in this run")
        return None
    if not _serving_chat:
        log(f"approval card {title!r} refused: no chat session is being served "
            "(a card is shown and answered through gen.serve_chat)")
        return None
    # A transport-only inbox without broadcast has no viewer: the card would
    # sit unseen until its timeout, so it is refused at once.
    if not _inbox_shows_cards[0]:
        log(f"approval card {title!r} refused: nobody can see it (the run's "
            "inbox is served with files=False and no broadcast)")
        return None
    import uuid

    rid = uuid.uuid4().hex[:16]
    card: dict = {"type": "permission_request", "id": rid,
                  "title": title or "Permission", "kind": kind, "options": opts}
    if detail:
        card["detail"] = str(detail)
    try:
        bridge.stream_event("", card)
    except Exception as exc:  # noqa: BLE001 — a card no one sees is refused
        log(f"approval card {title!r} refused: it could not be shown ({exc})")
        return None
    answer: Optional[dict] = None
    timed_out = False
    try:
        _open_ask(title or "Permission")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_s
        while True:
            answer = _pop_permission_answer(rid)
            if answer is not None or bridge.cancelled or not _serving_chat:
                break
            if loop.time() >= deadline:
                timed_out = True
                break
            await asyncio.sleep(_ASK_POLL_S)
    finally:
        _close_ask()
        chosen = ""
        if answer is not None and not answer.get("cancelled"):
            chosen = str(answer.get("option_id") or "")
        resolution: dict = {"type": "permission_resolved", "id": rid}
        if chosen in valid:
            resolution["option_id"] = chosen
        else:
            resolution["cancelled"] = True
            chosen = ""
        if timed_out:
            resolution["timed_out"] = True
        try:
            bridge.stream_event("", resolution)
            if timed_out:
                bridge.stream_event("", {
                    "type": "error",
                    "message": "approval request timed out — answered cancelled",
                })
        except Exception as exc:  # noqa: BLE001 — the answer stands
            log(f"approval card {title!r}: its resolution could not be sent ({exc})")
    if timed_out:
        log(f"approval card {title!r} timed out after {wait_s:g}s: refused")
    return chosen or None


# Session-config changes delivered by the renderer (agent-v2 §2.1's Phase-4
# configOptions UI — mode / model / thought_level / arbitrary selects):
# serve_chat routes an inbound ``config_set`` command here; the agent
# generator's companion drains ``pop_config_request`` and applies each to its
# live ACP session. Bounded FIFO — a burst of stale changes never grows it.
_config_requests: deque = deque()
_CONFIG_REQUESTS_MAX = 32


def _deliver_config_request(req: dict) -> None:
    """Record one config change (``{config_id, value}``) for the serving
    generator. Called by ``serve_chat``'s command dispatch — generators never
    call this."""
    cid = str((req or {}).get("config_id") or "")
    if not cid:
        return
    while len(_config_requests) >= _CONFIG_REQUESTS_MAX:
        _config_requests.popleft()
    _config_requests.append({"config_id": cid, "value": req.get("value")})


def pop_config_request() -> Optional[dict]:
    """Non-blocking pop of the oldest renderer-originated config change
    (``{config_id, value}``), or ``None``. The agent generator's companion
    polls this and applies each over its ACP session
    (``session/set_config_option`` / the mode fallback)."""
    return _config_requests.popleft() if _config_requests else None


# Whether the serving chat currently has a turn in flight (or queued) —
# maintained by serve_chat's own start/settle path so a companion can tell a
# busy session from an idle one.
_chat_inflight: list = [0]


def chat_busy() -> bool:
    """True while the serving chat has a turn in flight or queued — the
    busy/idle signal a companion gates on (controls that may act only on an
    idle session, an idle-park timer)."""
    return _session.busy()


class SessionState:
    """The serving chat's ONE state object (agent-v2 §9.2): the turn queue,
    the in-flight turn and the graceful-finish flag. The module-level slots
    (``_turn_queue``, ``_chat_inflight``, ``_finish_requested``) are this
    object's storage, kept as views for tests."""

    def __init__(self) -> None:
        self.queue = _turn_queue                # deque of queued-turn dicts
        self._inflight = _chat_inflight         # [count]
        self._finish = _finish_requested        # [bool]
        self.current: "Optional[ChatTurn]" = None
        self.pending: set = set()
        # An interrupt that arrived while a turn was starting (its task made,
        # its ChatTurn not yet): the turn starts cancelled.
        self._cancel_starting = False

    def reset(self) -> None:
        """A fresh session (serve_chat's boot): no stale queue, turn,
        interrupt or finish request from a prior serve_chat in this process."""
        self.queue.clear()
        self._inflight[0] = 0
        self._finish[0] = False
        self.current = None
        self.pending = set()
        self._cancel_starting = False

    # ── turns ─────────────────────────────────────────────────────
    def turn_started(self) -> None:
        self._inflight[0] += 1

    def turn_settled(self, _task=None) -> None:
        self._inflight[0] = max(0, self._inflight[0] - 1)
        self._cancel_starting = False

    def busy(self) -> bool:
        return bool(self._inflight[0]) or bool(self.queue)

    @property
    def idle(self) -> bool:
        """No turn running or starting — the only state a new turn may
        start from (``pending`` covers the window between task creation and
        the turn's first tick)."""
        return self.current is None and not self.pending

    def submit(self, content: str, *, source: str = "", thread_id: str = "",
               attachments: "Optional[list]" = None) -> None:
        self.queue.append({
            "content": content, "thread_id": thread_id,
            "attachments": list(attachments or []),
            "source": source,
        })

    def interrupt(self) -> bool:
        """Cancel the running turn, or mark a starting one to start
        cancelled; False when there is none or it is already cancelled."""
        cur = self.current
        if cur is not None:
            if cur.cancelled:
                return False
            cur.cancelled = True
            return True
        if self.pending and not self._cancel_starting:
            self._cancel_starting = True
            return True
        return False

    def take_cancel_starting(self) -> bool:
        """Whether the turn now starting was interrupted (and clear it)."""
        flag, self._cancel_starting = self._cancel_starting, False
        return flag

    # ── finish ────────────────────────────────────────────────────
    @property
    def finish_requested(self) -> bool:
        return self._finish[0]

    def request_finish(self) -> None:
        self._finish[0] = True


_session = SessionState()


def submit_turn(text: str, *, source: str = "", thread_id: str = "") -> None:
    """Queue a turn on the serving chat as if it had been typed — the
    programmatic entry every non-composer source uses (an annotation batch,
    a correction).

    The turn starts immediately when the session is idle, otherwise it QUEUES
    deterministically behind the in-flight turn — never dropped, never
    racing. A user message arriving mid-stream queues behind the turn.
    ``source`` labels the turn for the handler (``ChatTurn.source``);
    ``thread_id`` targets a thread other than the active one."""
    _session.submit(text, source=source, thread_id=thread_id)


def interrupt_turn() -> bool:
    """Cancel the turn :func:`serve_chat` is running — as the user's Stop
    does: its ``ChatTurn.cancelled`` turns True and a handler that watches it
    ends early; a turn that is starting starts cancelled. False when no turn
    is running or starting, or it is already cancelled. Queued turns are
    untouched."""
    return _session.interrupt()


class ChatTurn:
    """One chat turn handed to a :func:`serve_chat` handler.

    The handler streams the assistant reply via :meth:`delta` / :meth:`tool`
    and may observe :attr:`cancelled` (set on interrupt). :func:`serve_chat` brackets the handler with
    ``turn_start`` / ``turn_end`` events automatically, so the handler only
    emits the deltas in between.

    The turn is **bound to the thread active when it started**. Read the
    conversation via :attr:`messages` and record the exchange via
    :meth:`append` — both target the bound thread, so this turn's reply can
    never be misfiled into another transcript (a ``gen.session.submit_turn`` for a
    side transcript keeps generating into ITS file; every event the turn
    emits carries the bound ``thread_id`` so the renderer can scope it).
    :attr:`cancelled` flips only on an explicit interrupt.
    """

    def __init__(self, user_msg: str, turn_id: str, hist: "_History", emit_fn,
                 *, thread_id: Optional[str] = None,
                 messages: Optional[list] = None,
                 attachments: Optional[list] = None,
                 source: str = "") -> None:
        self.user_msg = user_msg
        # The label the generator passed to submit_turn, "" for the composer.
        self.source = source
        # Files the user attached to this turn — cell-relative references
        # ``{"path", "name", "media_type"}``. The bytes are in the cell's
        # workspace (the host flushes attachment drafts before the turn), so a
        # handler reads them with ``gen.read_file(a["path"])``. Empty for a
        # plain text turn.
        self.attachments: list = list(attachments or [])
        self.turn_id = turn_id
        self.history = hist
        self.cancelled = False
        self._emit = emit_fn
        # Snapshot the thread this turn belongs to at start. All reads/writes
        # for the turn use this snapshot + file, independent of history.active.
        self.thread_id = thread_id or hist.active
        self.threads_dir = hist.threads_dir
        self._file = f"{self.threads_dir}/{self.thread_id}.json"
        self._messages: list[Message] = (
            list(messages) if messages is not None else list(hist)
        )

    @property
    def messages(self) -> list["Message"]:
        """This turn's thread transcript so far (append the user message first)."""
        return list(self._messages)

    def append(self, role: str, content: str, *, thinking: str = "",
               attachments: Optional[list] = None,
               tools: Optional[list] = None, stopped: bool = False) -> None:
        """Append a message to THIS turn's thread transcript and persist it.

        Writes to the bound thread's file regardless of the currently-active
        thread, and keeps the shared :data:`history` singleton coherent only
        while the bound thread is still the active one. ``thinking`` carries
        the model's reasoning for an assistant message (optional).
        ``attachments`` are cell-file references (``{path, name, media_type}``)
        the message carries — e.g. a user's uploaded files, or files the model
        produced that were written into the cell. ``tools`` is the bounded
        display record of the turn's tool activity (``{name, status, input?,
        result?}`` summaries) and ``stopped`` marks an interrupted turn's
        partial reply — both assistant-message concepts, persisted only when
        non-empty/set.
        """
        from collimate.transcript import serialize_transcript
        self._messages.append(Message(
            role=role, content=content, thinking=thinking,
            attachments=list(attachments or []),
            tools=list(tools or []), stopped=bool(stopped),
        ))
        if self.history.persist:
            require_bridge().fs_write_ui(
                self._file, serialize_transcript(self._messages))
        if self.history.active == self.thread_id:
            self.history.adopt(self._messages)

    def emit(self, event: dict) -> None:
        # Every turn-scoped event carries the bound thread, so the renderer
        # can scope deltas/tools to the right transcript even for a
        # background side-transcript turn (gen.session.submit_turn).
        if "thread_id" not in event:
            event = {**event, "thread_id": self.thread_id}
        self._emit(self.turn_id, event)

    def delta(self, text: str) -> None:
        self.emit({"type": "text_delta", "text": text})

    def thinking_delta(self, text: str) -> None:
        """Stream a chunk of the model's reasoning for this turn (rendered as
        a collapsible section by the chat renderer)."""
        self.emit({"type": "thinking_delta", "text": text})

    def tool(self, name: str, status: str = "running", **kw) -> None:
        self.emit({"type": "tool", "name": name, "status": status, **kw})


async def serve_chat(
    on_turn,
    *,
    label: str = "Chat",
    threads_dir: str,
    pointer_file: str,
    initial_message: str = "",
    on_ready=None,
    files: bool = True,
    broadcast: bool = False,
) -> None:
    """Serve one live streaming conversation against this run until Stop.

    Registers the run's inbox session, writes and reveals the ``.chat``
    session pointer (so the assistant-ui renderer mounts), seeds the active
    thread's history, then dispatches inbound user messages to
    ``on_turn(turn)``. Your handler streams the reply via ``turn.delta``
    and records
    the exchange with ``turn.append`` (the user message first, then the
    reply) — bound to the turn's thread, so it can never be misfiled.

    Required ``pointer_file`` and ``threads_dir`` declare the conversation's
    layout; both must lie inside the run folder (:func:`run_path`), else
    :class:`ValueError`. The pointer and the transcripts are UI files
    (:func:`update_ui_file`): with ``files`` the manifest's ``ui_files`` must declare
    ``.chat`` and ``.json``, else :class:`ValueError` before the session
    starts. The ``.chat`` surface shows the run's approval cards.

    ``files=False`` serves the conversation with no chat surface: the session
    is still registered at ``pointer_file`` — it is the run's inbox (user
    messages, interrupts, approval answers and config changes arrive there,
    and ``gen.session.submit_turn`` turns run on it) — but neither the ``.chat``
    pointer nor any thread file is written. The transcript lives in memory
    for the run (``gen.history`` reads it). Use it when the run's face is
    elsewhere — its annotation context, a terminal. ``broadcast=True`` (only
    with ``files=False``, else :class:`ValueError`: the ``.chat`` surface is
    single-attach) lets several viewers attach to the inbox, so approval
    cards reach every one of them.

    ``initial_message`` seeds the FIRST turn (the run's prompt, typically):
    it is dispatched exactly like an inbound user message, but only when the
    active thread's transcript is empty — a cell that already carries a
    conversation never re-sends it.

    One conversation per run: the chat serves a single thread (the default
    ``main``), persisted to its open-format transcript at
    ``<threads_dir>/<id>.json`` — a new conversation is a new run, which
    keeps chat consistent with the terminal and annotation surfaces (one
    harness session per run). The wire carries no thread controls: an inbound frame
    naming a different ``thread_id`` is a stale client and is clamped to the
    conversation. Turns still bind to their thread id internally (events all
    carry ``thread_id``), and ``gen.session.submit_turn`` remains free to target a
    side transcript programmatically — that is an SDK primitive, not a UI
    surface. Interrupts arrive on the in-flight turn as ``turn.cancelled``.
    Unsupported commands are refused without changing the transcript.

    ``on_ready`` (a plain callable, no args) fires once the session is
    booted — registered, pointer written (with ``files``), per-session state
    reset — and before the first turn can start. A concurrent companion task (a surface
    state machine, a control loop) keys off it: anything it does earlier
    would be clobbered by this boot.
    """
    import uuid

    from collimate.pointers import chat_session_pointer

    global _serving_chat
    if not isinstance(broadcast, bool):
        raise ValueError("serve_chat: broadcast must be a bool")
    if broadcast and files:
        raise ValueError("serve_chat: broadcast needs files=False (the .chat "
                         "surface is single-attach)")
    bridge = require_bridge()
    thread = "main"
    pointer_file = _in_run_folder(
        pointer_file, "serve_chat pointer_file")
    threads_dir = _in_run_folder(
        threads_dir, "serve_chat threads_dir", folder_ok=True)
    if files:
        require_ui_types("serve_chat", pointer_file,
                          f"{threads_dir}/{thread}.json")

    # Discover the threads the cell already carries + ensure the default
    # exists.
    def _discover_threads() -> list:
        found = []
        prefix = f"{threads_dir}/"
        for p in sorted(bridge.fs_list(threads_dir)):
            if p.startswith(prefix) and p.endswith(".json"):
                tid = p[len(prefix):-len(".json")]
                found.append({"id": tid, "title": _title_for(tid), "file": p})
        if not any(t["id"] == thread for t in found):
            found.insert(0, {
                "id": thread, "title": _title_for(thread),
                "file": f"{threads_dir}/{thread}.json",
            })
        return found

    history.persist = files
    threads = _discover_threads() if files else []
    history.switch_thread(thread, threads_dir=threads_dir, load=True)
    # Fresh session: no stale queued turns, answers or interrupt from a
    # prior serve_chat in this process (tests run several).
    st = _session
    st.reset()
    _permission_answers.clear()
    _config_requests.clear()

    info = await asyncio.to_thread(bridge.surface_register, pointer_file,
                                   label=label, transport_only=not files,
                                   broadcast=broadcast)
    _inbox_shows_cards[0] = files or broadcast

    def _write_pointer() -> None:
        bridge.fs_write_ui(pointer_file, json.dumps(chat_session_pointer(
            info, label=label, threads_dir=threads_dir,
            active_thread=history.active, threads=threads,
        )))

    if files:
        _write_pointer()
        # Materialize the active transcript so a history read resolves
        # pre-first-turn.
        history._flush()
        await reveal_file(pointer_file)

    # Boot is done — session registered, pointer written, state reset above.
    # Signal the companion task (if any) BEFORE the first turn can start.
    if on_ready is not None:
        try:
            on_ready()
        except Exception:
            bridge.log.exception("serve_chat: on_ready callback failed")

    # Phase mirror: "running" while a turn is in flight, "awaiting_input"
    # while the session is parked on the user. Deduped per transition so the
    # 0.2s poll loop never re-emits the same phase — against the module
    # record every gen.phase call also writes (reset here so this session's
    # first park always reaches the host).
    _last_phase[:] = ["", ""]

    def _set_phase(name: str, message: str) -> None:
        if _last_phase[0] == name:
            return
        _send_phase(name, message)

    def _park_awaiting_input() -> None:
        _set_phase("awaiting_input", "session open — awaiting your message")

    closing = [False]

    def _turn_settled(_task) -> None:
        # Once a turn settles, start the next queued turn in FIFO order,
        # else park —
        # unless a new turn already started or the run is stopping. Once
        # the session is closing nothing polls the inbox: start nothing.
        if closing[0]:
            return
        if not st.pending and not bridge.cancelled:
            if st.queue:
                _start_queued(st.queue.popleft())
            else:
                _park_awaiting_input()

    async def _run_turn(content: str, *, thread_id: Optional[str] = None,
                        seed: Optional[list] = None,
                        attachments: Optional[list] = None,
                        source: str = "") -> None:
        turn = ChatTurn(content, uuid.uuid4().hex, history, bridge.stream_event,
                        thread_id=thread_id, messages=seed,
                        attachments=attachments,
                        source=source)
        st.current = turn
        if st.take_cancel_starting():
            turn.cancelled = True
        bridge.stream_event(turn.turn_id, {
            "type": "turn_start", "thread_id": turn.thread_id,
        })
        try:
            await on_turn(turn)
        except Cancelled:
            raise
        except Exception as exc:
            bridge.log.exception("serve_chat: on_turn failed")
            bridge.stream_event(turn.turn_id, {
                "type": "error", "message": str(exc), "thread_id": turn.thread_id,
            })
        finally:
            bridge.stream_event(turn.turn_id, {
                "type": "turn_end", "thread_id": turn.thread_id,
            })
            st.current = None

    def _refuse_busy(tid: str = "", content: str = "",
                     message: str = "", *, queued: bool = False) -> None:
        # Sends a busy/queued notice to the renderer. ``content`` echoes
        # refused text so the renderer can restore it into the composer
        # instead of losing what the user typed. ``message`` overrides the
        # wording (a queued-turn acknowledgement).
        ev: dict = {
            "type": "busy", "thread_id": tid or history.active,
            "message": message
            or "The assistant is still replying — "
               "wait for it to finish or stop it first.",
        }
        if content:
            ev["content"] = content
        if queued:
            # Not a refusal: the message WILL run (it sits behind a
            # running turn) — the renderer shows a notice, not an error,
            # and keeps the message out of the composer.
            ev["queued"] = True
        bridge.stream_event("", ev)

    def _refuse_operation(kind: str, message: str) -> None:
        bridge.stream_event("", {
            "type": "refused", "op": kind, "thread_id": history.active,
            "message": message,
        })

    def _start_turn(content: str, attachments: Optional[list] = None, *,
                    thread_id: Optional[str] = None,
                    seed: Optional[list] = None,
                    source: str = "") -> None:
        _set_phase("running", "responding")
        t = asyncio.ensure_future(_run_turn(
            content, thread_id=thread_id, seed=seed, attachments=attachments,
            source=source))
        # ``pending`` gains the task SYNCHRONOUSLY — it is the idle signal
        # the command branches gate on, so a message processed before the
        # task's first tick can never see a false idle and start a second
        # concurrent turn.
        st.pending.add(t)
        st.turn_started()
        t.add_done_callback(st.turn_settled)
        t.add_done_callback(st.pending.discard)
        t.add_done_callback(_turn_settled)

    def _start_queued(item: dict) -> None:
        """One queued entry (gen.session.submit_turn / a deferred user message) →
        a turn. The queue is the ONLY path besides the live composer, and
        both funnel into _start_turn — the sole turn starter.

        Thread binding: an entry for the ACTIVE thread uses its in-memory
        history; an entry bound to ANOTHER thread must
        seed from THAT thread's transcript — ChatTurn would otherwise copy
        the active thread's history and overwrite the target's file with it
        on append."""
        tid = (item.get("thread_id") or "").strip() or None
        seed = None
        if tid is not None and tid == history.active:
            tid = None
        if tid is not None:
            seed = []
            try:
                seed = _read_transcript(bridge, f"{threads_dir}/{tid}.json") or []
            except Exception:  # noqa: BLE001 — unreadable ⇒ a fresh thread
                bridge.log.exception(
                    "serve_chat: queued-turn thread seed failed")
        _start_turn(
            item.get("content", ""), item.get("attachments") or None,
            thread_id=tid, seed=seed,
            source=item.get("source", ""),
        )

    def _clamp_thread(tid: str) -> str:
        """The single-conversation gate: the wire cannot address another
        thread. A frame naming one is a stale or foreign client — serve the
        active conversation rather than materializing a hidden thread."""
        tid = (tid or "").strip()
        if tid and tid != history.active:
            bridge.log.warning(
                "serve_chat: frame for thread %r clamped to the "
                "conversation (%r)", tid, history.active)
        return history.active

    def _any_transcript_content() -> bool:
        """True when ANY discovered thread already holds messages — the whole
        chat must be virgin for the run prompt to seed the first turn (the
        active thread alone isn't enough: the conversation may live in a side
        thread while the default one sits empty)."""
        if not files:
            return len(history) > 0
        for t in threads:
            try:
                if _read_transcript(bridge, t["file"]):
                    return True
            except Exception:  # noqa: BLE001 — unreadable file ≠ content
                continue
        return False

    # Seed the first turn from the run's prompt — but only into a virgin
    # chat, so a cell that already carries a conversation never re-sends it.
    if (initial_message or "").strip() and len(history) == 0 \
            and not _any_transcript_content():
        _start_turn(initial_message.strip())
    else:
        # No seeded first turn — the session parks idle from the start.
        _park_awaiting_input()

    try:
        # From here the session's command inbox is polled, so an approval
        # card (gen.ask) can be answered. No turn has run yet: the seeded
        # first turn is only scheduled above.
        _serving_chat = True
        while not bridge.cancelled:
            # A generator-requested graceful finish: end once the session is
            # fully idle — the last turn and any queued follow-up (a
            # validator correction) have drained. A hard Stop
            # (bridge.cancelled) still preempts this.
            if st.finish_requested and st.idle and not st.queue:
                break
            # Queued turns (gen.session.submit_turn while parked; user messages that
            # deferred behind the in-flight turn whose settle raced this
            # loop) start as soon as the session is idle.
            if st.idle and st.queue:
                _start_queued(st.queue.popleft())
            cmd = await asyncio.to_thread(bridge.chat_poll)
            if not cmd:
                await asyncio.sleep(_CHAT_POLL_INTERVAL_S)
                continue
            kind = cmd.get("kind", "")
            if kind == "user_msg":
                content = cmd.get("content", "") or ""
                # Files the user attached (cell-relative refs; bytes already
                # flushed into the workspace by the host before this command).
                attachments = cmd.get("attachments") or []
                # Single conversation: whatever thread the frame names, the
                # turn runs on the active one (see _clamp_thread).
                target = _clamp_thread(cmd.get("thread_id") or "")
                if st.idle:
                    _start_turn(content, attachments)
                else:
                    # A turn is streaming (or starting): the message QUEUES
                    # behind it (agent-v2 §9.1.3). The
                    # user cuts a reply short with Stop; a queued message
                    # runs right after, acknowledged so it is visibly kept.
                    st.submit(content, source="",
                              thread_id=target, attachments=attachments)
                    _refuse_busy(
                        target, content,
                        message="Queued — it will run right after the "
                                "current reply.", queued=True)
            elif kind == "interrupt":
                # Cancel only the turn the frame names. A frame carrying a
                # ``turn_id`` (a viewer's Stop — a broadcast inbox has N
                # tabs, and a late click must never cancel the NEXT turn)
                # matches that turn alone; one carrying only a ``thread_id``
                # (the chat composer) matches the in-flight turn of that
                # thread, so Stop in one thread never kills a background turn
                # generating into another. A frame naming neither is
                # malformed and is dropped rather than guessed at.
                tid = (cmd.get("thread_id") or "").strip()
                turn_id = (cmd.get("turn_id") or "").strip()
                cur = st.current
                if not tid and not turn_id:
                    bridge.log.warning(
                        "serve_chat: interrupt frame naming no turn or "
                        "thread — ignored"
                    )
                elif cur is not None and (
                        cur.turn_id == turn_id if turn_id
                        else cur.thread_id == tid):
                    cur.cancelled = True
            elif kind == "permission_answer":
                # An approval card's answer — route it to the gen.ask card
                # waiting on this id. Never a turn; never gated on busy (the
                # answer is what UNBLOCKS the turn).
                deliver_permission_answer({
                    "id": cmd.get("id", ""),
                    "option_id": cmd.get("option_id", ""),
                    "cancelled": bool(cmd.get("cancelled")),
                })
            elif kind == "config_set":
                # A session-config change from the surface's configOptions UI
                # (agent-v2 §2.1 Phase 4: mode / model / thought_level /
                # arbitrary selects). Never a turn — the generator's
                # companion drains pop_config_request and applies it over
                # its live ACP session, then re-emits the refreshed set.
                _deliver_config_request({
                    "config_id": cmd.get("config_id", ""),
                    "value": cmd.get("value"),
                })
            else:
                _refuse_operation(kind, "Unsupported chat command.")
    finally:
        closing[0] = True
        # Nobody polls the inbox any more: a card still waiting is refused.
        _serving_chat = False
        _inbox_shows_cards[0] = False
        if st.pending:
            await asyncio.wait(list(st.pending), timeout=2.0)
        for t in list(st.pending):
            t.cancel()
        if files:
            try:
                history._flush()
                _write_pointer()
                await commit()
            except Exception:
                bridge.log.exception("serve_chat: final transcript flush failed")


def _title_for(thread_id: str) -> str:
    return "Main" if thread_id == "main" else f"Thread {thread_id}"


_CHAT_POLL_INTERVAL_S = 0.2



# ---------------------------------------------------------------------------
# live_files — one bidirectional, echo-suppressed file mirror behind every
# "keep two file stores in sync" relationship:
#   * gen.live_files()        the cell (Files tab) <-> this run's own workspace
# The collimate._mirror engine reconciles two file endpoints on a short interval; a
# shared path->hash map suppresses echoes so a value synced one way is never
# bounced back. The LEFT endpoint wins a genuine simultaneous edit.
# ---------------------------------------------------------------------------

# The private host band and log artifacts never enter the cell. Generators
# declare any additional private paths through live_files(skip_globs=...).
def live_files(
    *,
    to_cell: bool = True,
    from_cell: bool = True,
    exclude: tuple = (),
    interval: float = 0.5,
    skip_dirs: Optional[set] = None,
    skip_globs: tuple[str, ...] = (),
    on_refused=None,
    on_changed=None,
) -> Mirror:
    """Keep this run's workspace and its cell (the Files tab) in live,
    **bidirectional** sync for the lifetime of an ``async with`` block — the
    one-liner that makes a ``live_files`` generator foolproof::

        async with gen.live_files():
            term = await gen.start_terminal(...)    # or any daemon
            await gen.reveal_file(gen.update_ui_file("app.terminal", ptr))
            await gen.wait_until_cancelled()

    While the block is open, files the run writes to its workspace appear in the
    cell live (``to_cell``) and the user's Files-tab edits are written into the
    workspace the run reads (``from_cell``). The two directions are
    echo-suppressed, the workspace wins a genuine simultaneous edit, and a final
    reconcile on exit captures a last-moment write.

    A refused push is held at its attempted workspace hash: it is neither
    pushed again nor overwritten by the cell until that workspace file changes.
    The synchronous ``on_refused(path, error)`` callback runs once per refused
    attempt, after successful siblings commit. Refusals are also logged; an
    explicit ``flush()`` raises ``PermissionError`` and background failures are
    reported once by the next explicit flush.

    ``on_changed(path)`` runs synchronously after an external cell edit or
    deletion is pulled into the workspace. Local pushes and their host echoes
    do not invoke it. A file held after refusal remains untouched and therefore
    produces no change notification until it can actually be pulled.

    Set ``live_files_mode: "auto"`` in the manifest to have the runtime open
    this for the whole run with zero code; use ``"manual"`` + this call when you
    need a custom ``exclude`` (path suffixes such as ``(".log", ".chat")``,
    matched with ``str.endswith``), ``skip_globs`` (case-sensitive glob
    patterns over cell-relative paths), or a one-way mirror. No-op without
    a host. Generator-owned bookkeeping must be declared in ``skip_globs``.

    Run folders: this run's own folder is pulled from the cell and pushed to
    it only for the UI file types the manifest declares (``ui_files``) — a
    file of another type dropped there locally is removed again and logged;
    the run card and every other run's folder are read-only here — never
    pushed, and a local change to one is overwritten with the cell's copy."""
    sd = set(skip_dirs) if skip_dirs is not None else _fs.WORKSPACE_SKIP_DIRS
    return Mirror(
        LocalWorkspace(sd), CellStore(),
        own_folder=require_bridge().folder,
        to_remote=to_cell, from_remote=from_cell,
        exclude=tuple(exclude), interval=interval,
        guard=_has_host, skip_dirs=sd, skip_globs=skip_globs, on_refused=on_refused,
        on_changed=on_changed,
    )


def _has_host() -> bool:
    """True when a real host IPC channel is wired (so the mirror should run)."""
    try:
        bridge = require_bridge()
    except Exception:
        return False
    return bool(getattr(bridge, "has_host", False))


# ──────────────────────────────────────────────────────────────────────
# Host-placed environments — a container the host places for THIS run. The
# generator says what it needs; the host's execution backend decides where
# it lands and owns every docker call. ``_env_shared.DockerEnvironment``
# (shared generator code) wraps these into a container lifecycle. The host
# gates them at docker: full.
# ──────────────────────────────────────────────────────────────────────


def place_environment(spec: dict) -> tuple:
    """Ask the host to place a container for this run from ``spec`` (image or
    build context, the workspace mount, environment, ports, volumes, command
    — the keys ``_env_shared.DockerEnvironment`` fills) and return
    ``(env_id, container_id)``. A missing image is built first, so this can
    take minutes. Workspace and build contexts must resolve within this run's
    workspace or its staged app tree. Extra volumes expose declared
    env_passthrough sockets or host-derived account bindings. readonly_files
    maps container paths to content the host materializes read-only. Raises :class:`RuntimeError` when the
    host could not place it."""
    ack = require_bridge().env_place(dict(spec))
    if not ack.get("ok"):
        raise RuntimeError(ack.get("error") or "environment placement failed")
    return str(ack["env_id"]), str(ack.get("container_id") or "")


def exec_environment(
    env_id: str,
    command: list,
    *,
    cwd: Optional[str] = None,
    env: Optional[dict] = None,
    timeout: Optional[float] = None,
    user: Optional[str] = None,
    detach: bool = False,
) -> tuple:
    """Run ``command`` (an argv list) in a placed environment and return
    ``(rc, output)``; ``(-1, error)`` when the host could not run it.
    ``user`` (``"uid:gid"``) runs it as that user instead of the container's
    default; ``detach`` starts it and returns ``(0, "")`` without waiting.

    ``timeout`` bounds only the reply wait, with 30 seconds of collection
    allowance (120 seconds when unset). Bound the command itself in its argv;
    an IPC timeout does not terminate it. ``DockerEnvironment.exec`` does
    this with the container's ``timeout`` command.
    """
    ack = require_bridge().env_exec(
        env_id, [str(c) for c in command], cwd=cwd, env=env or None,
        timeout=timeout, user=user or None, detach=detach,
    )
    if not ack.get("ok"):
        return (-1, str(ack.get("error") or "exec failed"))
    if detach:
        return (0, "")
    return (int(ack.get("rc", -1)), str(ack.get("output") or ""))


def expose_environment_port(env_id: str, port: int) -> int:
    """Forward container ``port`` of a placed environment to a host port and
    return that host port — register it with :func:`serve_port` and reach it
    at :func:`proxy_url`. Raises :class:`RuntimeError` when the host could
    not forward it."""
    ack = require_bridge().env_expose_port(env_id, int(port))
    if not ack.get("ok"):
        raise RuntimeError(ack.get("error") or "expose_port failed")
    return int(ack["host_port"])


def close_environment(env_id: str) -> dict:
    """Ask the host to tear a placed environment down; return its
    ``{ok, error}`` acknowledgement. Transport failures raise :class:`IPCError`.
    The run's own teardown reaps any environment a generator leaves behind."""
    return require_bridge().env_close(env_id)


async def _run_with_auto_live_files(run_fn) -> None:
    """Dispatch an async ``run()``, wrapping it in a default :func:`live_files`
    mirror when the manifest asked for it via ``live_files_mode: "auto"`` (the
    host passes the mode through ``COLLIMATE_LIVE_FILES_MODE``). ``"manual"`` /
    unset leaves the generator to manage its own sync."""
    if os.environ.get("COLLIMATE_LIVE_FILES_MODE", "").strip().lower() == "auto" and _has_host():
        async with live_files():
            await run_fn()
    else:
        await run_fn()


def _send_finish(bridge: HostBridge, success: bool, error_message: str = "") -> None:
    """The run's one finish frame, with the verdict :func:`finish` recorded."""
    bridge.finish(success=success, error_message=error_message,
                  note=bridge.run_note, outcome=bridge.run_outcome)


def main(run_fn) -> None:
    """Run a generator's ``run`` function under the singleton API.

    Construct the IPC bridge, hydrate the singleton, dispatch ``run_fn``
    (sync or async), auto-flush any pending writes to the cell, and emit
    the finish IPC. Catches :class:`Cancelled` as graceful stop; every
    other exception is reported via the run's error channel.

    **Auto-flush**: if ``run_fn`` returns cleanly with writes staged via
    :func:`update_file` (chat transcripts included) that no :func:`commit`
    flushed, this commits them as the final cell state. A generator that
    writes nothing leaves its cell as it found it. When that flush fails
    the run fails with it (a host's partial refusal excepted: the rest was
    committed, and the refusal is logged).

    A failed or stopped run exits with status 1 once it has sent its
    finish, so a host that never got that frame (a closed channel) reads
    the same outcome from the exit status.
    """
    bridge = HostBridge()
    set_bridge(bridge)
    failed = True
    try:
        if inspect.iscoroutinefunction(run_fn):
            asyncio.run(_run_with_auto_live_files(run_fn))
        else:
            # A sync generator can't host the async mirror; auto live_files
            # requires an async run(). It can still call gen.live_files()
            # itself if it has its own loop.
            res = run_fn()
            if inspect.iscoroutine(res):
                # A sync callable that returns a coroutine (a lambda or a
                # wrapper around an async run) — await it, don't drop it.
                asyncio.run(_run_with_auto_live_files(lambda: res))
        if bridge.tracked_writes or bridge.tracked_deletions:
            final_flush(bridge)
        _send_finish(bridge, True)
        failed = False
    except Cancelled:
        bridge.log.info("Stopped gracefully by user.")
        _send_finish(bridge, False, "Stopped by user")
    except Exception as exc:
        if bridge.cancelled:
            # A graceful stop's SIGTERM goes to the whole process group, so a
            # child (e.g. the claude CLI) can die first and its error surface
            # before any gen.* checkpoint raises Cancelled. Once the cancel
            # flag is set, ANY exception is the stop unwinding — report it as
            # the user's stop, not a generator failure with a traceback.
            bridge.log.info("Stopped gracefully by user.")
            _send_finish(bridge, False, "Stopped by user")
        else:
            bridge.log.exception("Generator failed")
            _send_finish(bridge, False, str(exc))
    finally:
        _clear_bridge()
    if failed:
        raise SystemExit(1)


__all__ = [
    # Result / spec types (shared with collimate.testing)
    "Annotation",
    "AnnotationNotice",
    "ConfigField",
    "ConfigValidation",
    "ContextItem",
    "DraftFileSchema",
    "Endpoint",
    "GeneratorResult",
    "Grant",
    "Message",
    "RunInfo",
    # Host-placed containers
    "place_environment",
    "exec_environment",
    "expose_environment_port",
    "close_environment",
    # The run: its grant, identity, folder and accounts
    "grant",
    "run_info",
    "run_path",
    "packages",
    "account",
    "commit_account",
    "materialize_account",
    "release_account",
    # Client capabilities
    "client_caps",
    # Exceptions
    "Cancelled",
    "IPCDisconnectedError",
    "IPCError",
    "FileConflictError",
    "IPCNakError",
    "IPCTimeoutError",
    "PartialRefusal",
    # Inputs / config
    "config",
    "config_folder",
    "config_folder_path",
    "config_folders",
    "options",
    # Handler catalog and verification
    "HandlerInfo",
    "HandlerType",
    "ValidationResult",
    "handlers",
    "handler_for",
    "handler_skills",
    "validate",
    # Output stage
    "delete_file",
    "has_file",
    "list_files",
    "read_file",
    "update_file",
    "track_file",
    # UI files (the run folder)
    "update_ui_file",
    "delete_ui_file",
    "list_ui_files",
    "require_ui_types",
    # Cell ops
    "commit",
    "reveal_file",
    # Annotations
    "annotations",
    # Chat
    "ChatTurn",
    "history",
    "serve_chat",
    # Approval cards
    "ask",
    "can_ask",
    # Status / control
    "finish",
    "log",
    "phase",
    "progress",
    # Native filesystem / subprocess / ports / terminals
    "actions",
    "proxy_url",
    "serve_port",
    "serve_terminal",
    "snapshot",
    "start_terminal",
    "stop_terminal",
    "terminal_status",
    "wait_terminal_exit",
    "wait_for_action",
    "wait_for_action_event",
    "wait_until_cancelled",
    "workspace_path",
    # Live file sync (cell <-> workspace)
    "live_files",
    # Entrypoint
    "main",
]
