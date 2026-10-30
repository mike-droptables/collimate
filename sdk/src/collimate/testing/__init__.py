# SPDX-License-Identifier: MIT
"""Host-free run engine for generators — the in-process backend of the
"run a generator → :class:`GeneratorResult`" contract.

This package executes a generator **in the current Python process** against
a :class:`~collimate.testing._bridge.RecordingBridge` (no IPC peer, no
host), capturing what the run produced. It's for pytest / CI / local SDK
iteration. The app runs generators in isolated processes with their declared dependencies.

Quick start::

    from collimate.testing import run_generator
    res = run_generator(
        "generators/merge/merge.py",
        options={"options": [
            {"name": "A", "root": "a", "files": ["**"]},
            {"name": "B", "root": "b", "files": ["**"]},
        ]},
        inputs={"a/source.txt": "A", "b/source.txt": "B"},
    )
    assert res.ok
    assert "x.txt" in res.files

Limitation: the in-process backend can only import packages already
installed in the current interpreter. A generator whose PEP 723 block pulls
in an uninstalled dependency fails at import — install it in the test env,
or run the generator in the app (host backend) for full dependency
resolution.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import importlib.util
import inspect
import itertools
import json
import os
import sys
import tempfile
import traceback
from typing import Optional

from collimate._results import (
    Annotation,
    ContextItem,
    Endpoint,
    GeneratorResult,
    HandlerType,
    HandlerInfo,
    ValidationResult,
)
from collimate.testing._bridge import UNANSWERED, RecordingBridge

__all__ = [
    "Annotation",
    "ContextItem",
    "Endpoint",
    "GeneratorResult",
    "HandlerType",
    "HandlerInfo",
    "ValidationResult",
    "RecordingBridge",
    "UNANSWERED",
    "make_test_bridge",
    "run_generator",
    "run_generator_callable",
]

_modname_counter = itertools.count(1)


# ---------------------------------------------------------------------------
# Bridge factory (single construction path; tests/conftest delegate here)
# ---------------------------------------------------------------------------


def make_test_bridge(
    workspace,
    *,
    prompt: str = "",
    selected_file: Optional[str] = None,
    accounts: Optional[dict] = None,
    messages: Optional[list] = None,
    permissions: Optional[dict] = None,
    client_caps: Optional[dict] = None,
    runner: Optional[dict] = None,
    run_id: Optional[str] = None,
    label: Optional[str] = None,
    channel: str = "",
    cell_id: str = "",
    foreign_runs: Optional[dict] = None,
    ui_files: Optional[list] = None,
    approvals=None,
    autocancel_on_serve: bool = True,
    packages: Optional[dict] = None,
    annotations: Optional[list] = None,
) -> RecordingBridge:
    """Build a :class:`RecordingBridge` rooted at ``workspace`` without an
    IPC peer. The one bridge-construction path — ``tests/conftest.py``
    delegates here. ``permissions`` is the grant dict ``gen.grant()`` reads;
    ``client_caps`` is what ``gen.client_caps()``
    reports (default: the dev-mode all-off answer); ``runner``
    (``{"runner": <id>, "name": <display name>}``), ``run_id``, ``label``,
    ``channel`` and ``cell_id`` are the identity ``gen.run_info()`` returns
    (defaults: no runner, a fresh run id, ``dev-<run_id[:8]>``, ``""``); ``foreign_runs`` seeds sibling runs'
    folders and ``ui_files`` the manifest's UI file suffixes (``None``, the
    default, accepts every suffix; see :meth:`RecordingBridge.create`);
    ``approvals`` scripts the
    human answering ``gen.ask`` cards (see :func:`run_generator`);
    ``autocancel_on_serve`` (default True) makes a ``gen.serve_port`` /
    ``gen.serve_terminal`` call signal Stop so a daemon loop ends — pass
    False when the test drives the loop itself; ``packages`` is what
    ``gen.packages()`` returns (default: no pins); ``selected_file`` is the
    cell-relative path ``gen.selected_file`` returns (default: none);
    ``annotations`` scripts the user's messages in the run's annotation
    context (see :func:`run_generator`)."""
    return RecordingBridge.create(
        str(workspace),
        prompt=prompt,
        selected_file=selected_file,
        accounts=accounts,
        messages=messages,
        permissions=permissions,
        client_caps=client_caps,
        runner=runner,
        run_id=run_id,
        label=label,
        channel=channel,
        cell_id=cell_id,
        foreign_runs=foreign_runs,
        ui_files=ui_files,
        approvals=approvals,
        autocancel_on_serve=autocancel_on_serve,
        packages=packages,
        annotations=annotations,
    )


# ---------------------------------------------------------------------------
# Workspace seeding
# ---------------------------------------------------------------------------


def _write_raw(ws: str, relpath: str, content) -> None:
    full = os.path.join(ws, relpath)
    os.makedirs(os.path.dirname(full) or ws, exist_ok=True)
    if isinstance(content, bytes):
        with open(full, "wb") as fh:
            fh.write(content)
    else:
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(content if isinstance(content, str) else str(content))


def _write_config(cfg_dir: str, name: str, values) -> None:
    full = os.path.join(cfg_dir, name)
    os.makedirs(os.path.dirname(full) or cfg_dir, exist_ok=True)
    if isinstance(values, (str, bytes)):
        # Pre-serialized YAML/JSON or a raw-text config file: verbatim.
        _write_raw(cfg_dir, name, values)
        return
    lower = name.lower()
    if lower.endswith(".json"):
        content = json.dumps(values)
    elif lower.endswith((".set_yaml", ".yaml", ".yml")):
        if values is not None and not isinstance(values, dict):
            raise TypeError(
                f"config {name!r}: expected a dict (or YAML text), got {type(values).__name__}"
            )
        import yaml  # bridge.config parses a plain YAML map straight through

        content = yaml.safe_dump(values or {})
    else:
        raise TypeError(f"config {name!r}: raw config files take str or bytes")
    with open(full, "w", encoding="utf-8") as fh:
        fh.write(content)


def _seed_options(ws, options) -> None:
    """Seed a root YAML declaration; actual option content comes from inputs."""
    if options is None:
        return
    import yaml
    if not isinstance(options, dict):
        raise TypeError("options must be an options declaration mapping")
    if os.path.exists(os.path.join(ws, ".options_stack")):
        raise ValueError("options fixture conflicts with inputs .options_stack")
    _write_raw(ws, ".options_stack", yaml.safe_dump(options, sort_keys=False))


def _seed_config_folders(ws, config_folders) -> None:
    """Stage runner config folders into ``.collrun/config/folders/<name>/…``
    — exactly where the host stages them, so ``gen.config_folder(name)`` reads
    them back and they never leak into ``gen.list_files()``. ``config_folders``
    is ``{folder_name: {relpath: content}}``."""
    base = os.path.join(ws, ".collrun", "config", "folders")
    for name, files in (config_folders or {}).items():
        folder = os.path.join(base, name)
        os.makedirs(folder, exist_ok=True)
        for rel, content in (files or {}).items():
            _write_raw(folder, rel, content)


def _seed_workspace(ws, inputs, options, config, prompt, config_folders=None,
                    selected_file=None) -> None:
    os.makedirs(os.path.join(ws, ".collrun"), exist_ok=True)
    with open(os.path.join(ws, ".collrun", "prompt.md"), "w", encoding="utf-8") as fh:
        fh.write(prompt or "")
    if selected_file:
        # The host's spelling: the cell-relative path, no trailing newline.
        with open(os.path.join(ws, ".collrun", "selected_file"), "w", encoding="utf-8") as fh:
            fh.write(selected_file)
    for path, content in (inputs or {}).items():
        _write_raw(ws, path, content)
    _seed_options(ws, options)
    if config:
        cfg_dir = os.path.join(ws, ".collrun", "config", "files")
        os.makedirs(cfg_dir, exist_ok=True)
        for name, values in config.items():
            _write_config(cfg_dir, name, values)
    _seed_config_folders(ws, config_folders)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def _build_result(bridge: RecordingBridge, *, ok: bool, error=None, tb=None) -> GeneratorResult:
    files: dict = {}
    for p in bridge.fs_list():
        try:
            files[p] = bridge.fs_read_any(p)
        except OSError:
            continue
    name = bridge.updates[-1]["name"] if bridge.updates else None
    # Unflushed log writes (a generator that wrote its trace and then
    # raised / was stopped never reached update_cell) still surface —
    # mirroring how `files` already reflects unflushed workspace writes.
    log_files = dict(bridge.log_files)
    for p_ in sorted(bridge.tracked_writes & bridge.log_paths):
        if p_ not in log_files:
            try:
                if bridge.fs_exists(p_):
                    log_files[p_] = bridge.fs_read_any(p_)
            except OSError:
                pass
    return GeneratorResult(
        ok=ok,
        files=files,
        log_files=log_files,
        name=name,
        endpoints=list(bridge._rec_endpoints),
        tier_b_calls=list(bridge._rec_tier_b),
        logs=list(bridge._rec_logs),
        phases=tuple(bridge.phases),
        error=error,
        traceback=tb,
        note=bridge.run_note,
        outcome=bridge.run_outcome,
        annotations=tuple(bridge.annotation_context()),
    )


class _RunTimedOut(Exception):
    """The run-level ``timeout`` expired (not a TimeoutError the generator
    raised itself)."""


async def _bounded(run_fn, timeout: Optional[float]) -> None:
    cm = asyncio.timeout(timeout)  # None = no deadline
    try:
        async with cm:
            await run_fn()
    except TimeoutError:
        if cm.expired():
            raise _RunTimedOut() from None
        raise


def _execute(run_fn, bridge: RecordingBridge, timeout: Optional[float]) -> GeneratorResult:
    """Run ``run_fn`` under ``bridge`` (singleton-swapped), mirroring
    ``native.main``'s dispatch + auto-flush, into a GeneratorResult."""
    from collimate import native
    from collimate._host.bridge import Cancelled

    prev = native.current_bridge()
    native.set_bridge(bridge)
    ok, error, tb = True, None, None
    try:
        if inspect.iscoroutinefunction(run_fn):
            asyncio.run(_bounded(run_fn, timeout))
        else:
            res = run_fn()
            if inspect.iscoroutine(res):
                asyncio.run(_bounded(lambda: res, timeout))
        # Auto-flush trailing writes, exactly like native.main.
        if bridge.tracked_writes or bridge.tracked_deletions:
            native.final_flush(bridge)
    except Cancelled:
        ok, error = False, "Stopped by user"
    except _RunTimedOut:
        ok, error = False, f"timed out after {timeout}s"
    except KeyboardInterrupt:
        raise
    except BaseException as exc:  # noqa: BLE001 — report any failure as a result
        ok = False
        error = str(exc) or exc.__class__.__name__
        tb = traceback.format_exc()
    finally:
        native.set_bridge(prev)
    return _build_result(bridge, ok=ok, error=error, tb=tb)


@contextmanager
def _prepared_run(*, workspace, inputs, options, config, config_folders, prompt,
                  selected_file=None, entrypoint=None, **bridge_kwargs):
    """Own one workspace/bridge lifetime, yielding an early result on setup failure."""
    tmp = tempfile.TemporaryDirectory(prefix="collimate-run-") if workspace is None else None
    ws = tmp.name if tmp is not None else str(workspace)
    bridge = None
    try:
        if entrypoint is not None and not os.path.isfile(entrypoint):
            yield GeneratorResult(ok=False, error=f"entrypoint not found: {entrypoint}")
            return
        try:
            _seed_workspace(ws, inputs, options, config, prompt, config_folders,
                            selected_file)
        except Exception as exc:  # noqa: BLE001 — always return a result
            yield GeneratorResult(ok=False, error=f"workspace seeding failed: {exc}",
                                  traceback=traceback.format_exc())
            return
        bridge = make_test_bridge(ws, prompt=prompt, selected_file=selected_file,
                                  **bridge_kwargs)
        yield bridge
    finally:
        if bridge is not None:
            bridge.close()
        if tmp is not None:
            tmp.cleanup()


def run_generator_callable(
    run_fn,
    *,
    inputs: Optional[dict] = None,
    options: Optional[dict] = None,
    config: Optional[dict] = None,
    config_folders: Optional[dict] = None,
    prompt: str = "",
    selected_file: Optional[str] = None,
    accounts: Optional[dict] = None,
    messages: Optional[list] = None,
    permissions: Optional[dict] = None,
    client_caps: Optional[dict] = None,
    runner: Optional[dict] = None,
    run_id: Optional[str] = None,
    label: Optional[str] = None,
    channel: str = "",
    cell_id: str = "",
    foreign_runs: Optional[dict] = None,
    ui_files: Optional[list] = None,
    approvals=None,
    autocancel_on_serve: bool = True,
    packages: Optional[dict] = None,
    annotations: Optional[list] = None,
    workspace=None,
    timeout: Optional[float] = 60.0,
) -> GeneratorResult:
    """Run an already-imported ``run`` coroutine/function host-free.

    Lower-level than :func:`run_generator` (which imports an entrypoint
    file first). Useful when you hold the callable directly.
    ``config_folders`` (``{name: {relpath: content}}``) stages runner config
    folders read by ``gen.config_folder(name)``; ``permissions`` is the grant dict
    ``gen.grant()`` types; ``client_caps`` is what ``gen.client_caps()``
    reports (default: the dev-mode all-off answer); ``runner``, ``run_id``,
    ``label``, ``channel``, ``cell_id``, ``foreign_runs`` and ``ui_files``
    shape the run's identity, its sibling runs and the UI file types it may
    write (see :func:`make_test_bridge`); ``approvals`` scripts the human
    answering ``gen.ask`` cards and ``annotations`` the user's messages (see
    :func:`run_generator`); ``selected_file``, ``packages`` /
    ``autocancel_on_serve``: see :func:`make_test_bridge`."""
    with _prepared_run(
        workspace=workspace, inputs=inputs, options=options, config=config,
        config_folders=config_folders, prompt=prompt, selected_file=selected_file,
        accounts=accounts, messages=messages,
        permissions=permissions, client_caps=client_caps,
        runner=runner, run_id=run_id, label=label, channel=channel,
        cell_id=cell_id, foreign_runs=foreign_runs, ui_files=ui_files,
        approvals=approvals, autocancel_on_serve=autocancel_on_serve,
        packages=packages, annotations=annotations,
    ) as prepared:
        if isinstance(prepared, GeneratorResult):
            return prepared
        return _execute(run_fn, prepared, timeout)


def run_generator(
    entrypoint,
    *,
    inputs: Optional[dict] = None,
    options: Optional[dict] = None,
    config: Optional[dict] = None,
    config_folders: Optional[dict] = None,
    prompt: str = "",
    selected_file: Optional[str] = None,
    accounts: Optional[dict] = None,
    messages: Optional[list] = None,
    permissions: Optional[dict] = None,
    client_caps: Optional[dict] = None,
    runner: Optional[dict] = None,
    run_id: Optional[str] = None,
    label: Optional[str] = None,
    channel: str = "",
    cell_id: str = "",
    foreign_runs: Optional[dict] = None,
    ui_files: Optional[list] = None,
    approvals=None,
    autocancel_on_serve: bool = True,
    packages: Optional[dict] = None,
    annotations: Optional[list] = None,
    workspace=None,
    timeout: Optional[float] = 60.0,
) -> GeneratorResult:
    """Run a generator entrypoint file host-free and return what it produced.

    ``entrypoint`` is the path to the generator's ``.py``. ``inputs`` seed
    the workspace root; ``options`` seeds a root ``.options_stack`` from a YAML
    declaration mapping (options and optional rest). Option files, including
    binary bytes, belong in ``inputs``. ``config`` is
    ``{draft_filename: values_dict}`` (a str/bytes value is written
    verbatim — pre-serialized YAML/JSON or a raw-text draft). ``config_folders`` is
    ``{folder_name: {relpath: content}}`` staged where ``gen.config_folder``
    reads them (never mixed into the input files). ``messages`` is a scripted
    ``gen.serve_chat`` inbound queue (strings → user messages, or command
    dicts for interrupts). ``permissions`` is the grant dict
    ``gen.grant()`` types; ``client_caps`` is what ``gen.client_caps()``
    reports (default: the dev-mode all-off answer); ``runner``, ``run_id``,
    ``label``, ``channel``, ``cell_id``, ``foreign_runs`` and ``ui_files``
    shape the run's identity, its sibling runs and the UI file types it may
    write (see :func:`make_test_bridge`); ``selected_file`` is the
    cell-relative path ``gen.selected_file`` returns, written where the host
    writes it (``.collrun/selected_file``) — seed the file itself through
    ``inputs``; ``packages`` / ``autocancel_on_serve``: see
    :func:`make_test_bridge`. The run executes
    in-process against a RecordingBridge; async generators are bounded by
    ``timeout`` (seconds; ``None`` to disable).

    ``approvals`` scripts the human behind ``gen.ask``. ``None`` (the
    default) means nobody can answer: ``gen.can_ask()`` is False and every
    card is refused at once. A list answers the cards in order and a callable
    ``fn(card) -> answer`` answers each: an answer is the chosen
    ``option_id``, ``None`` to dismiss the card, or :data:`UNANSWERED` to leave
    it waiting until its timeout. Once a list runs out, further cards wait
    unanswered. As in a real run, a card is shown only while the generator's
    ``gen.serve_chat`` serves its chat session and the session can show it —
    its ``.chat`` surface, or a broadcast inbox (``files=False,
    broadcast=True``); elsewhere ``gen.ask`` refuses at once. The cards
    themselves are in ``tier_b_calls`` (op ``stream_event``).

    ``annotations`` scripts the user's messages in the run's annotation
    context (``gen.annotations``): a ``str`` is a queued top-level message, a
    dict takes ``text``, ``mode`` (``"queued"`` or ``"interrupt"``),
    ``reply_to``, ``context`` (``gen.ContextItem`` values or dicts) and
    ``refs``. A non-empty ``prompt`` is seeded first as a read
    ``kind="prompt"`` message. Unread scripted annotations hold a
    ``gen.serve_chat`` run open until pulled, and a run that polls its
    annotations stops on the second idle poll. The run's posts are in
    ``bridge.annotation_posts``, pulls and posts in ``tier_b_calls`` (ops
    ``annotations_pull`` and ``annotations_post``), and
    ``result.annotations`` holds the whole context at the end."""
    path = os.path.abspath(str(entrypoint))
    with _prepared_run(
        workspace=workspace, inputs=inputs, options=options, config=config,
        config_folders=config_folders, prompt=prompt, selected_file=selected_file,
        accounts=accounts, messages=messages,
        permissions=permissions, client_caps=client_caps,
        runner=runner, run_id=run_id, label=label, channel=channel,
        cell_id=cell_id, foreign_runs=foreign_runs, ui_files=ui_files,
        approvals=approvals, autocancel_on_serve=autocancel_on_serve,
        packages=packages, annotations=annotations,
        entrypoint=path,
    ) as prepared:
        if isinstance(prepared, GeneratorResult):
            return prepared
        bridge = prepared

        parent = os.path.dirname(path)
        paths_before = tuple(sys.path)
        added = parent not in sys.path
        if added:
            sys.path.insert(0, parent)
        modname = f"_generator_under_test_{next(_modname_counter)}"
        mods_before = set(sys.modules)
        try:
            try:
                spec = importlib.util.spec_from_file_location(modname, path)
                module = importlib.util.module_from_spec(spec)
                sys.modules[modname] = module
                spec.loader.exec_module(module)
            except KeyboardInterrupt:
                raise
            except BaseException as exc:  # noqa: BLE001 — surface import failure as a result
                return _build_result(
                    bridge, ok=False,
                    error=f"import of {os.path.basename(path)} failed: {exc}",
                    tb=traceback.format_exc(),
                )
            run_fn = getattr(module, "run", None)
            if not callable(run_fn):
                return _build_result(
                    bridge, ok=False,
                    error=f"{os.path.basename(path)} defines no top-level run() function",
                )
            return _execute(run_fn, bridge, timeout)
        finally:
            # Generator-owned code must reload, while unrelated libraries and
            # SDK modules keep their class/module identity across runs.
            roots = {os.path.realpath(parent)}
            roots.update(os.path.realpath(p) for p in sys.path if p not in paths_before)
            def in_owned_root(location):
                location = os.path.realpath(location)
                return any(os.path.commonpath((root, location)) == root for root in roots)

            for name in set(sys.modules) - mods_before:
                module = sys.modules.get(name)
                spec = getattr(module, "__spec__", None)
                filename = getattr(module, "__file__", None)
                locations = getattr(spec, "submodule_search_locations", None)
                if locations is None:
                    locations = getattr(module, "__path__", None)
                if name in sys.builtin_module_names or getattr(spec, "origin", None) in {"built-in", "frozen"}:
                    owned = False
                elif filename:
                    owned = in_owned_root(filename)
                elif locations is not None:
                    # Namespace parents share their children's search ownership.
                    # Mixed namespaces retain identity for their external portions.
                    locations = tuple(locations)
                    owned = bool(locations) and all(in_owned_root(p) for p in locations)
                else:
                    # Truly fileless injections have no import location to retain.
                    owned = True
                if owned:
                    sys.modules.pop(name, None)
                    parent_name, _, child_name = name.rpartition(".")
                    parent_module = sys.modules.get(parent_name)
                    if module is not None and getattr(parent_module, child_name, None) is module:
                        # A retained namespace must not hand out its evicted child
                        # through a later ``from package import child``.
                        delattr(parent_module, child_name)
            sys.path[:] = paths_before
            importlib.invalidate_caches()
