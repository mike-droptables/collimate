"""Start a saved runner on one cell, freezing its complete definition,
generator source, input and selected account snapshots for the run.

A start is accepted, then spawned. The accept (the POST) runs every gate,
freezes the run's inputs, commits the run card together with the starting
placeholders of the files the run opens, claims those files as starting
surfaces and answers ``queued``. The spawn continues in the background:
the workspace, the config capture, the account leases, the last gate and
the process. A spawn that fails ends the run as failed and unwinds what
the accept made.

Several runs may be live on one cell. Each gets a host-minted label and run
folder ``_runs/<label>/`` whose first file, the run card, is committed
before the generator starts; every unwind after that sweeps the folder
again. ``new_cell`` runs on a clone of the cell in a new stack to its right
(cell_commits.clone_cell_for_run), recorded in the lineage as a 'run_cell'
move from the cell to the clone; both are removed again when the start
fails.

No run ever holds a cell on the Capabilities stack: the accept refuses one
there (400), and refuses again (409) when the cell moved onto Capabilities
before the freeze took its commit lock, the lock a move onto Capabilities
also takes before it checks for runs on the cell."""

from cell_placement import find_cell, is_capabilities_stack

import asyncio
import json
import logging
import os
import re

from process_signals import signal_process_group
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field

import yaml
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from collimate.config_values import parse_set_yaml, resolve_form_values
from collimate._host.ipc import finish_note
from collimate.surfaces import resolve_opens
from set_yaml import config_violations, overlay_set_yaml_values
from cell_lineage import announce_lineage
from cell_moves import record_move
from database import get_db_connection
from process_manager import ProcessManager
from process_manager.optional_deps import resolve_optional_requirements
from process_manager.phases import QUEUED
from runners import ResolvedRunner, RunnerError, prompt_gate, resolve_runner_run, selected_file_gate
from cell_commits import (
    clone_cell_for_run,
    remove_stack,
    run_card,
    sweep_run_folder,
    write_run_start,
)
from run_folders import is_run_path, run_folder, mint_run_label
import served_surfaces

from run_staging import snapshot_generator_source, materialize_generator_source, hydrate_workspace, stage_runner_files
from versions import find_tip, freeze_cell_versions
from .helpers import _reap_process
from .models import RunRequest
from .refusals import RunRefused
from run_ends import mark_run_ended
from run_registry import (
    StartCancelled,
    active_managers,
    channel_busy,
    hold_is_live,
    is_shutting_down,
    reserve,
    release,
)

logger = logging.getLogger(__name__)

router = APIRouter()

CAPABILITIES_RUN_REFUSAL = "Runs never start on a Capabilities cell"
CAPABILITIES_MOVED_REFUSAL = "The run’s cell moved onto Capabilities before startup"

# The files a run opens that are pointers (a run's live session, written by
# the run once it serves it): before the process exists they hold
# ``collimate.pointers.starting_pointer``.
POINTER_SUFFIXES = (".chat", ".terminal", ".webview", ".website")


@dataclass
class StartingRun:
    """An accepted run whose spawn has not registered its manager yet: the
    fields a ``/runs/active`` row reads, in a manager's shape."""
    run_id: str
    channel: str
    cell_id: str
    runner_id: str | None
    runner_name: str | None
    label: str
    folder: str
    generator_name: str
    final_status: str | None = None
    current_phase: str = QUEUED
    current_phase_message: str = ""
    # The grant the run runs under, whether it was raised to its harness's
    # minimum (runners.adopt_harness_grant), and that harness; None for a
    # run no harness binds.
    effective_grant: dict | None = None
    adopted: bool = False
    harness: str | None = None
    # Where a CLI run's harness CLI runs (``host``: this machine, or
    # ``container``); None for any other run.
    cli_locus: str | None = None

    @property
    def stop_requested(self) -> bool:
        """A per-run Stop (stop_queued) or Stop all / a shutdown (its hold
        cancelled) reached the run: its spawn unwinds as stopped at its
        last gate."""
        return self.run_id in _stops or not hold_is_live(self.run_id)


# Accepted runs whose spawn is still in flight, in accept order, and their
# spawn tasks. A run leaves both once its manager is registered (it is then
# in active_managers) or its spawn has unwound.
_starting: dict[str, StartingRun] = {}
_spawns: dict[str, asyncio.Task] = {}
# The starting runs a per-run Stop reached before they registered.
_stops: set[str] = set()


def stop_queued(channel: str, run_id: str) -> bool:
    """Stop ``channel``'s run ``run_id`` if it is accepted and not registered
    yet: its spawn refuses at its last gate and unwinds as stopped, whenever
    the Stop's own row write lands. Synchronous, so a Stop calls it before
    its first await. Returns whether the run was starting."""
    run = _starting.get(run_id)
    if (run is None or run_id in active_managers or not hold_is_live(run_id)
            or run.channel != channel):
        return False
    _stops.add(run_id)
    return True


async def wait_for_spawns(timeout: float) -> list[str]:
    """Wait at most ``timeout`` seconds for every in-flight spawn to register
    its run or unwind; returns the run ids still spawning then. Server
    shutdown calls it after cancelling the starting holds, so every run
    accepted before the shutdown has unwound as stopped (its row ended, its
    claims released, its folder swept) before the persistence writer stops."""
    pending = list(_spawns.items())
    if not pending:
        return []
    await asyncio.wait([task for _run_id, task in pending], timeout=timeout)
    still = sorted(run_id for run_id, task in pending if not task.done())
    if still:
        logger.warning("shutdown: the spawns of %s did not settle within %gs",
                       ", ".join(still), timeout)
    return still


def starting_runs() -> list[tuple[str, StartingRun]]:
    """The accepted runs still spawning, ``(run_id, StartingRun)`` in accept
    order, excluding any already registered."""
    return [(run_id, run) for run_id, run in list(_starting.items()) if run_id not in active_managers]


async def spawn_settled(run_id: str) -> None:
    """Wait until ``run_id``'s spawn has registered its run or unwound."""
    task = _spawns.get(run_id)
    if task is not None:
        await asyncio.wait({task})


@router.post("/api/channels/{channel}/runs")
async def create_run(channel: str, req: RunRequest):
    """Accept the saved runner ``req.runner`` on the cell ``req.cell_id`` (or
    on a clone of it in a new stack, with ``new_cell``) and spawn it.

    The answer comes once the run is accepted: its inputs frozen, its run
    card and starting surfaces committed and claimed. It is ``queued``; the
    generator is spawned in the background and its outcome arrives as
    ``run_status`` / ``run_phase`` events."""
    try:
        return await start_run(channel, req)
    except RunRefused as exc:
        return JSONResponse(status_code=exc.status_code, content=exc.body, headers=exc.headers)


async def start_run(channel: str, req: RunRequest):
    """Accept a run and schedule its spawn.

    The accept marks its channel busy until it returns, and the spawn until
    it registers or unwinds (registry.channel_busy), so the channel is not
    renamed or deleted under the db writes either makes."""
    try:
        from state import require_channel
        channel = str(require_channel(channel).id)
        with channel_busy(channel):
            return await _start_run(channel, req)
    except HTTPException as exc:
        raise RunRefused(exc.status_code, {"detail": exc.detail}, headers=exc.headers) from exc


def _selected_file_path(raw: str) -> str:
    """A selected file is a canonical cell-relative path outside the run
    folders and the private run staging."""
    parts = raw.split("/") if isinstance(raw, str) else []
    if (not parts or "\\" in raw or "\x00" in raw or re.match(r"^[A-Za-z]:", raw)
            or any(part in ("", ".", "..") for part in parts)
            or parts[0] in ("_runs", ".collrun")):
        raise HTTPException(400, "selected_file must be a relative cell file path outside "
                                 "_runs/ and .collrun/")
    return raw


def _cell_paths(channel: str, cell_id: str) -> set[str]:
    """The cell's current accepted file paths."""
    from persistence import snapshot_files
    conn = get_db_connection(channel)
    try:
        return {f["path"] for f in snapshot_files(conn, cell_id)}
    finally:
        conn.close()


def _selected_file_missing(path: str) -> RunRefused:
    return RunRefused(409, {"error": "selected_file_missing", "path": path,
                            "message": f"{path} is no longer in this cell"})


def _start_placeholders(opens: list[str], runner_name: str) -> dict[str, dict[str, str]]:
    """The starting placeholders of the files a run opens, grouped by the
    opened path: pointers get ``starting_pointer``; anything else none."""
    from collimate.pointers import starting_pointer
    return {path: {path: json.dumps(starting_pointer(runner_name))}
            for path in opens if path.endswith(POINTER_SUFFIXES)}


async def _start_run(channel: str, req: RunRequest):
    """start_run's body, inside its channel's busy mark."""
    # A run created while the lifespan teardown is sweeping would spawn a
    # generator (its own session — detached from the server's process
    # group) after stop_all_runs already ran, leaving it orphaned past
    # server exit. Refuse instead.
    if is_shutting_down():
        raise HTTPException(503, "Server is shutting down; not accepting new runs")
    from state import require_channel
    ch_obj = require_channel(channel)

    run_id = str(uuid.uuid4())

    source_stack, _ = find_cell(ch_obj, req.cell_id)

    # Resolve the runner against THIS channel's Capabilities — unknown
    # runner → 404; a degrade-don't-hide ``invalid`` marker → 400 with the
    # linter/cross-check reason. Runner ids and generator ids are SEPARATE
    # namespaces, so a runner named ``web`` targeting a generator ``web`` works.
    try:
        resolved = await asyncio.to_thread(
            resolve_runner_run, ch_obj, req.runner, req.runner_revision,
        )
    except RunnerError as exc:
        raise HTTPException(exc.status, exc.reason)

    manifest = resolved.generator_manifest

    # The cell a run writes must exist and must not be on Capabilities: that
    # stack holds the channel's capabilities, and nothing runs there (not
    # even on a new-cell clone of one of its cells). The frontend hides every
    # run affordance on it; this is the server half, rechecked under the
    # cell's commit lock in _insert_and_freeze.
    if source_stack is None:
        raise HTTPException(
            400, f"Cell {req.cell_id!r} does not exist in channel {channel!r}.",
        )
    if is_capabilities_stack(source_stack):
        raise HTTPException(400, CAPABILITIES_RUN_REFUSAL)

    from accounts import resolve_accounts
    from runners import adopt_harness_grant, apply_account_settings
    try:
        account_context = await asyncio.to_thread(
            resolve_accounts, resolved.accounts, resolved.account_requirements,
        )
        apply_account_settings(resolved, account_context)
        # An Annotation run takes its harness's least grant: resolved.permissions
        # becomes the effective grant, and the placement record says so in the
        # run's config, its card and its /runs/active row. It probes the
        # machine's CLIs, so off the event loop.
        placement = await asyncio.to_thread(adopt_harness_grant, resolved, account_context)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from exc

    # The saved definition contains every main-config value. Supplemental
    # files are owned by the runner and staged from its frozen source below.
    try:
        seeded_files = {resolved.main_config: overlay_set_yaml_values(
            resolved.main_config_content, resolved.settings_overlay[resolved.main_config])}
    except ValueError as exc:
        raise HTTPException(400, f"Cannot apply saved runner settings: {exc}") from exc

    prompt = resolved.prompt
    main_fields, main_values = resolve_form_values(parse_set_yaml(seeded_files[resolved.main_config]))

    # Required gate — unconditional, even with zero draft files, and
    # visibility-first (a field hidden by an unmet show_if never blocks): any
    # visibly-required+empty field in the final seeded .set_yamls blocks the
    # run, as does a blank prompt the generator shows and requires for these
    # resolved values. The same visibility-first pass also flags a resolved
    # value that selects a currently-disabled dropdown option (enable_if unmet).
    missing_required: list[dict] = []
    invalid_options: list[dict] = []
    for rel, content in seeded_files.items():
        if not rel.endswith(".set_yaml"):
            continue
        fields, resolved_values = resolve_form_values(parse_set_yaml(content))
        file_missing, file_invalid = config_violations(rel, fields, resolved_values)
        missing_required.extend(file_missing)
        invalid_options.extend(file_invalid)
    if prompt_gate(manifest, main_fields, main_values)[1] and not prompt.strip():
        missing_required.append({"file": "prompt.md", "key": "prompt", "label": "Prompt"})
    # An invalid (selected-but-disabled) option is a distinct, stricter
    # failure than a bare missing_required — surface both under a
    # config_invalid code so the picker can render the disabled-option reason.
    if invalid_options:
        raise RunRefused(
            400, {
                "code": "config_invalid",
                "fields": missing_required,
                "invalid_options": invalid_options,
            },
        )
    if missing_required:
        raise RunRefused(400, {"code": "missing_required", "fields": missing_required})

    # The tag's permission grant. The one hard cross-axis invariant
    # (``internet != full`` requires ``docker == limited``) is normally caught by
    # the SDK runner linter during resolution (a 400 above); check it here too
    # as an explicit backstop before the run row exists.
    perms = resolved.permissions
    inv = perms.invariant_error()
    if inv:
        raise HTTPException(400, inv)

    # The selected file: only for a runner that takes one with these
    # settings, a plain cell path, present in the cell now (and again under
    # the freeze's locks, on the run's own cell).
    selected_file = None
    if req.selected_file is not None:
        if not selected_file_gate(manifest, main_fields, main_values):
            raise HTTPException(400, "runner does not take a selected file")
        selected_file = _selected_file_path(req.selected_file)
        if selected_file not in await asyncio.to_thread(_cell_paths, channel, req.cell_id):
            raise _selected_file_missing(selected_file)

    # The files the run opens, created as starting placeholders and claimed
    # as starting surfaces by the accept. A cell-scoped one another run
    # serves or is starting is refused here (and again under the locks).
    label = mint_run_label(resolved.runner_id, run_id)
    try:
        opens = resolve_opens(manifest, main_fields, main_values, folder=run_folder(label))
    except ValueError as exc:
        raise HTTPException(400, f"Cannot resolve the files this run opens: {exc}") from exc
    if not req.new_cell:
        _refuse_taken(req.cell_id, run_id, opens)
    placeholders = _start_placeholders(opens, resolved.runner_name)

    # --- End of rejection gates. Invariant: no 4xx rejection may fire after a
    # state mutation, unless the unwind below removes what it made — a
    # refused run never leaves a clone stack, a run row or a run card behind.
    # Keep new validation gates above this line.

    start = _RunStart(
        channel=channel, req=req, resolved=resolved, run_id=run_id, label=label,
        account_context=account_context, seeded_files=seeded_files,
        selected_file=selected_file, opens=opens,
        placeholders=placeholders, placement=placement,
    )
    try:
        if req.new_cell:
            start.made["cell_id"], start.made["stack_id"] = await asyncio.to_thread(
                clone_cell_for_run, channel, req.cell_id,
            )
        await start.accept()
    except BaseException as exc:
        try:
            await start.fail(exc)
        finally:
            release(run_id)
            _stops.discard(run_id)
        if isinstance(exc, RunnerError):
            raise HTTPException(exc.status, exc.reason) from exc
        if isinstance(exc, (HTTPException, RunRefused, StartCancelled)) or not isinstance(exc, Exception):
            raise
        raise HTTPException(500, f"Failed to start generator process: {exc}") from exc

    cell_id = start.made["cell_id"]
    _starting[run_id] = StartingRun(
        run_id=run_id, channel=channel, cell_id=cell_id, runner_id=resolved.runner_id,
        runner_name=resolved.runner_name, label=label, folder=run_folder(label),
        generator_name=resolved.generator_id,
        effective_grant=placement["effective_grant"] if placement else None,
        adopted=bool(placement and placement["adopted"]),
        harness=placement["harness"] if placement else None,
        cli_locus=cli_locus(placement),
    )
    from task_utils import track_task
    task = track_task(start.spawn(), name=f"spawn_run:{run_id}")
    _spawns[run_id] = task
    task.add_done_callback(lambda _t: (_spawns.pop(run_id, None), _starting.pop(run_id, None),
                                       _stops.discard(run_id)))

    # Announce the run to every client on the channel. The client that
    # POSTed attaches from this response; any other (another window)
    # attaches on this broadcast. Both reveal the first file it opens.
    started = {
        "run_id": run_id,
        "cell_id": cell_id,
        **({"stack_id": start.made["stack_id"]} if start.made["stack_id"] else {}),
        "runner_id": resolved.runner_id,
        "runner_name": resolved.runner_name,
        "label": label,
        "folder": run_folder(label),
        "generator_name": resolved.generator_id,
        "opens": list(opens),
    }
    from events import broadcast
    from collimate._host.channel_events import RunStarted
    from routes.system import broadcast_runs_active_changed
    broadcast(channel, RunStarted(**started).to_dict())
    broadcast_runs_active_changed(channel)
    if start.made["stack_id"]:
        announce_lineage(channel, [req.cell_id, cell_id])
    return {"status": QUEUED, **started}


def _refuse_taken(cell_id: str, run_id: str, opens: list[str]) -> None:
    """409 ``surface_taken`` for a cell-scoped opened path another run
    serves or is starting."""
    for path in opens:
        if is_run_path(path):
            continue
        owner = served_surfaces.get(cell_id, path)
        if owner is not None and owner.run_id != run_id:
            raise RunRefused(409, served_surfaces.SurfaceTaken(
                owner.owner_label, owner.run_id, path).response())


def cli_locus(placement: dict | None) -> str | None:
    """Where a CLI run's harness CLI runs, from its placement record:
    ``host`` (this machine) or ``container``; None for any other run."""
    if not placement or placement.get("surface") != "terminal" or not placement.get("runtime"):
        return None
    return "host" if placement["runtime"] == "host" else "container"


def _card_placement(placement: dict) -> dict:
    """The run card's line about the grant: what the run runs under, on
    which surface and where (a CLI run's ``runtime`` ``host`` is its CLI on
    this machine), and, when it was raised, from what and for which
    harness."""
    card = {"harness": placement["harness"], "surface": placement["surface"], "runtime": placement["runtime"],
            "effective_grant": {axis: placement["effective_grant"][axis] for axis in ("docker", "internet", "commands")},
            "adopted": placement["adopted"]}
    if placement["adopted"]:
        card["raised"] = placement["raised"]
    return card


@dataclass
class _RunStart:
    """One start's state across its accept and its spawn. ``made`` records
    every mutation as it happens, for the unwind."""
    channel: str
    req: RunRequest
    resolved: ResolvedRunner
    run_id: str
    label: str
    account_context: dict
    seeded_files: dict
    selected_file: str | None
    opens: list[str]
    placeholders: dict[str, dict[str, str]]
    # runners.adopt_harness_grant's record, None for a run no harness binds.
    placement: dict | None = None
    made: dict = field(default_factory=dict)
    frozen: tuple | None = None

    def __post_init__(self):
        self.made = {"cell_id": self.req.cell_id, "stack_id": None, "row": False,
                     "card": False, "claims": False, "dirs": []}

    # ------------------------------------------------------------------
    # Unwind
    # ------------------------------------------------------------------

    def _unwind(self, status: str, reason: str | None) -> tuple[str, str | None] | None:
        """Undo what the start made; returns the run row's final
        ``(status, failure_reason)`` (None without a row)."""
        made, run_id = self.made, self.run_id
        for d in made["dirs"]:
            shutil.rmtree(d, ignore_errors=True)
        active_managers.pop(run_id, None)
        final = None
        if made["row"]:
            conn = get_db_connection(self.channel)
            try:
                mark_run_ended(conn, run_id, status=status, failure_reason=reason)
                if made["stack_id"]:
                    conn.execute("DELETE FROM cell_moves WHERE op='run_cell' AND op_id=?", (run_id,))
                conn.commit()
                row = conn.execute("SELECT status, failure_reason FROM runs WHERE id=?",
                                   (run_id,)).fetchone()
                final = (row[0], row[1]) if row is not None else None
            finally:
                conn.close()
            try:
                from process_manager.finalization import commit_run_annotations
                bells = commit_run_annotations(self.channel, run_id)
            except Exception:
                logger.exception("could not seal the annotations of refused run %s", run_id)
                bells = []
            from collimate._host.channel_events import AnnotationsChanged
            from events import broadcast
            for bell in bells:
                broadcast(self.channel, AnnotationsChanged(bell.cell_id, bell.context_id, bell.rev).to_dict())
        if made["claims"]:
            try:
                served_surfaces.release_run(run_id)
            except Exception:
                logger.exception("could not release the surfaces of run %s", run_id)
        if made["card"]:
            try:
                sweep_run_folder(self.channel, made["cell_id"], run_id, self.label,
                                 generator_name=self.resolved.generator_id)
            except Exception:
                logger.exception("could not sweep the run folder of refused run %s", run_id)
        if made["stack_id"]:
            remove_stack(self.channel, made["stack_id"])
        return final

    async def fail(self, exc: BaseException) -> tuple[str, str | None] | None:
        """Unwind after ``exc``: kill a spawned process, release the account
        leases, end the row (stopped for a cancelled start, else failed with
        why) and undo the rest. Returns the row's final ``(status,
        failure_reason)``."""
        # start() can fail (or be cancelled) AFTER the subprocess was
        # spawned; the unwind pops the registry, so without an explicit kill
        # the live process would be orphaned with nothing tracking it.
        manager = self.made.get("manager")
        proc = manager.process if manager is not None else None
        if proc is not None and proc.returncode is None:
            import signal as _sig
            signal_process_group(proc.pid, _sig.SIGKILL)
        # The spawn retains the run's account leases just before the last
        # gate; releasing leases never retained is a no-op.
        from accounts import release_run_account_leases
        try:
            await asyncio.to_thread(release_run_account_leases, self.run_id)
        except BaseException:
            logger.exception("Could not release account context after startup failure")
        # A Stop (or Stop all) that aborted the spawn leaves stop_requested
        # set: the run was stopped, not failed.
        status = ("stopped" if isinstance(exc, StartCancelled)
                  or (manager is not None and manager.stop_requested) else "failed")
        # A failed start keeps why (one line) as its failure_reason and note;
        # a stopped one takes its note from the stop event.
        reason = None
        if status == "failed":
            detail = exc.reason if isinstance(exc, RunnerError) else (
                exc.detail if isinstance(exc, HTTPException) else str(exc))
            reason = finish_note(detail if isinstance(detail, str) else str(detail))
        if isinstance(exc, Exception):
            return await asyncio.to_thread(self._unwind, status, reason)
        return self._unwind(status, reason)  # cancelled: undo synchronously, then propagate

    # ------------------------------------------------------------------
    # Accept
    # ------------------------------------------------------------------

    async def accept(self) -> None:
        """Freeze the run's inputs under its cells' locks: the run row, the
        run card and starting placeholders in one version, the starting
        claims, the frozen tips and the SDK write baseline."""
        frozen = asyncio.create_task(asyncio.to_thread(self._insert_and_freeze))
        try:
            self.frozen = await asyncio.shield(frozen)
        except asyncio.CancelledError:
            # The worker owns locks/hold and may still insert rows. Join it
            # before the outer unwind releases the hold or removes its files.
            await frozen
            raise

    def _flush_ids(self) -> list[str]:
        cell_id = self.made["cell_id"]
        flush_ids = [cell_id]
        for extra in (self.resolved.generator_cell_id or None, self.resolved.runner_cell_id):
            if extra and extra not in flush_ids:
                flush_ids.append(extra)
        return flush_ids

    def _insert_run_row(self) -> None:
        """Record the run. ``runner_id`` names the runner that ran;
        ``generator_name`` pins the TARGET generator the runner dispatched
        to; ``kind='generator'`` marks a real run (vs the bookkeeping
        pseudo-runs, which stay 'system'); ``target_cell_id`` is the durable
        run→cell link, which exists even when the run never emits a version.
        A new-cell run also records the 'run_cell' move from the cell it was
        asked for to its clone, and every run its annotation context (with
        the prompt as its first, already-read message), in the same
        transaction (``from_version``: that cell's tip, when it has a row)."""
        import annotations
        resolved, made = self.resolved, self.made
        conn = get_db_connection(self.channel)
        try:
            conn.execute(
                "INSERT INTO runs (id, prompt, status, phase, generator_name, "
                "runner_id, kind, target_cell_id) "
                "VALUES (?, ?, 'running', ?, ?, ?, 'generator', ?)",
                (self.run_id, resolved.prompt, QUEUED, resolved.generator_id,
                 resolved.runner_id, made["cell_id"]),
            )
            annotations.create_run_context(conn, made["cell_id"], self.run_id)
            if resolved.prompt.strip():
                annotations.seed_prompt(conn, made["cell_id"], self.run_id, resolved.prompt)
            if made["stack_id"]:
                tip = find_tip(conn, self.req.cell_id)
                versioned = conn.execute("SELECT 1 FROM cells WHERE id=?", (tip,)).fetchone()
                record_move(conn, op="run_cell", op_id=self.run_id, from_cell=self.req.cell_id,
                            from_version=tip if versioned else None, to_cell=made["cell_id"])
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
        made["row"] = True

    def _insert_and_freeze(self):
        from persistence import snapshot_files
        from state import get_channel_by_id, state_lock
        from runners import verify_runner_start
        from cell_write_scope import cell_write_scope
        channel, resolved, made, run_id = self.channel, self.resolved, self.made, self.run_id
        cell_id = made["cell_id"]
        flush_ids = self._flush_ids()
        with cell_write_scope(channel, flush_ids):
            # The cell's commit lock is held: a move onto Capabilities takes
            # it too, so the stack read here is the one the run starts on.
            with state_lock:
                stack, cell = find_cell(get_channel_by_id(channel), cell_id)
            if cell is None:
                raise HTTPException(409, "The run's cell was removed before startup")
            if stack is not None and is_capabilities_stack(stack):
                raise HTTPException(409, CAPABILITIES_MOVED_REFUSAL)
            verify_runner_start(get_channel_by_id(channel), resolved)
            source = snapshot_generator_source(
                resolved.generator_id, channel, cell_id=resolved.generator_cell_id or None)
            if source is None:
                raise RunnerError(404, "Generator source is unavailable")
            present = _cell_paths(channel, cell_id)
            if self.selected_file is not None and self.selected_file not in present:
                raise _selected_file_missing(self.selected_file)
            _refuse_taken(cell_id, run_id, self.opens)
            reserve(run_id, cell_id)
            self._insert_run_row()
            # The run card and the starting placeholders, in one version
            # before the freeze, so the frozen tip, the hydrated workspace
            # and the write baseline all hold them: the run's first write
            # to an opened file is not a conflict, and its live-files mirror
            # never reports one as an edit from outside the run. An opened
            # file the cell already holds keeps its content.
            placeholders: dict[str, str | bytes] = {}
            for target, files in self.placeholders.items():
                if target not in present:
                    placeholders.update(files)
            card = run_card(
                run_id=run_id, runner_id=resolved.runner_id, runner_name=resolved.runner_name,
                generator=resolved.generator_id, label=self.label,
            )
            if self.placement:
                card["placement"] = _card_placement(self.placement)
            made["card"] = True
            write_run_start(channel, cell_id, card, placeholders)
            made["claims"] = True
            served_surfaces.claim_starting(channel, cell_id, run_id, self.opens, self.label,
                                           label=resolved.runner_name or self.label)
            tips = freeze_cell_versions(channel, run_id, flush_ids)
            conn = get_db_connection(channel)
            try:
                refs = snapshot_files(conn, cell_id, include_deleted=True)
            finally:
                conn.close()
            baseline = {ref["path"]: ref["file_revision"] for ref in refs}
            return tips, source, baseline

    # ------------------------------------------------------------------
    # Spawn
    # ------------------------------------------------------------------

    async def spawn(self) -> None:
        """The background half: prepare and start the run, then hand it to
        its reaper. A failure ends the run (failed, or stopped when a Stop
        cancelled it) over ``run_status`` and unwinds the accept. The
        starting hold is released once the run registered or unwound."""
        from events import broadcast
        from process_manager.streams import run_status_event
        from routes.system import broadcast_runs_active_changed
        try:
            with channel_busy(self.channel):
                manager = await self._prepare_and_start()
        except BaseException as exc:
            try:
                final = await self.fail(exc)
            finally:
                release(self.run_id)
            if final is not None:
                broadcast(self.channel, run_status_event(self.channel, self.run_id, *final))
            broadcast_runs_active_changed(self.channel)
            if self.made["stack_id"]:
                announce_lineage(self.channel, [self.req.cell_id])
            if not isinstance(exc, Exception):
                raise
            logger.warning("run %s failed to start: %s", self.run_id, exc)
            return
        release(self.run_id)
        from task_utils import track_task
        track_task(_reap_process(self.run_id, manager), name=f"reap_process:{self.run_id}")
        broadcast_runs_active_changed(self.channel)

    def _row_running(self) -> bool:
        """Whether the run row is still 'running': a Stop that reached the
        run before it registered ended the row."""
        conn = get_db_connection(self.channel)
        try:
            row = conn.execute("SELECT status FROM runs WHERE id=?", (self.run_id,)).fetchone()
        finally:
            conn.close()
        return row is not None and row[0] == "running"

    async def _prepare_and_start(self):
        """The spawn's steps, in order: the workspace, the config capture,
        the account leases, the last gate, then the manager. Every step
        records what it made in ``made`` for the unwind."""
        from accounts import public_accounts_snapshot

        channel, resolved, made, run_id = self.channel, self.resolved, self.made, self.run_id
        seeded_files = self.seeded_files
        cell_id = made["cell_id"]
        assert self.frozen is not None
        resolved_tips, source_snapshot, frozen_revisions = self.frozen
        perms = resolved.permissions
        merged_overlay = resolved.settings_overlay
        prompt = resolved.prompt
        entrypoint = resolved.generator_manifest.get(
            "entrypoint", f"{resolved.generator_id}.py")
        gen_source_cell = resolved.generator_cell_id or None
        runner_source_cell = resolved.runner_cell_id

        generator_cell_version_id = (
            resolved_tips.get(gen_source_cell) if gen_source_cell else None
        )
        runner_cell_version_id = (
            resolved_tips.get(runner_source_cell) if runner_source_cell else None
        )
        input_tip_id = resolved_tips.get(cell_id) or cell_id

        if generator_cell_version_id or runner_cell_version_id:
            def _stamp_run_provenance():
                conn = get_db_connection(channel)
                try:
                    conn.execute(
                        "UPDATE runs SET generator_cell_id=?, runner_cell_id=? WHERE id=?",
                        (generator_cell_version_id, runner_cell_version_id, run_id),
                    )
                    conn.commit()
                finally:
                    conn.close()
            await asyncio.to_thread(_stamp_run_provenance)

        # Create isolated directories:
        #   app_dir   = copy of the generator code (CWD for the process, read-only intent)
        #   workspace = separate temp dir for hydrated input files (read/write)
        # These live under .collimate/tmp/ instead of the system tempdir so that
        # Docker-based generators can bind-mount them. Snap-confined Docker on
        # Ubuntu can't access /tmp/* but can access any path under the user's
        # home directory, and the collimate CWD is typically there.
        runs_tmp_root = os.path.abspath(os.path.join(".collimate", "tmp"))
        os.makedirs(runs_tmp_root, exist_ok=True)
        app_dir = tempfile.mkdtemp(prefix=f"app_{run_id[:8]}_", dir=runs_tmp_root)
        made["dirs"].append(app_dir)
        workspace_dir = tempfile.mkdtemp(prefix=f"ws_{run_id[:8]}_", dir=runs_tmp_root)
        made["dirs"].append(workspace_dir)
        from run_workspace_cleanup import stamp_directory
        stamp_directory(app_dir, run_id)
        stamp_directory(workspace_dir, run_id)

        # Materialize the frozen generator and its shared siblings directly
        # into the final app tree, once, outside the global state lock.
        gen_app_dir = os.path.join(app_dir, source_snapshot[0])

        def _prepare_workspace():
            """Sync workspace prep — generator code copy, then runner config-folder
            staging (from the FROZEN runner-cell tip) BEFORE the cell's hydrate so
            folder content never mixes into the cell's files. All
            filesystem-bound; running on the event loop would block every
            concurrent route handler for that window. Returns the folder_hashes
            for the run's runner_snapshot."""
            materialize_generator_source(source_snapshot, app_dir)

            fh = stage_runner_files(
                channel, runner_cell_version_id, resolved.runner_dir,
                resolved.folders, workspace_dir,
            )

            hydrate_workspace(channel, cell_id, workspace_dir, tip_id=resolved_tips.get(cell_id))
            baseline_path = os.path.join(workspace_dir, ".collrun", "file_revisions.json")
            os.makedirs(os.path.dirname(baseline_path), exist_ok=True)
            with open(baseline_path, "w", encoding="utf-8") as baseline_file:
                json.dump(frozen_revisions, baseline_file, sort_keys=True)
                baseline_file.flush()
                os.fsync(baseline_file.fileno())
            return fh

        try:
            preparing = asyncio.create_task(asyncio.to_thread(_prepare_workspace))
            try:
                folder_hashes = await asyncio.shield(preparing)
            except asyncio.CancelledError:
                await preparing
                raise
        except Exception as exc:
            raise HTTPException(500, f"Failed to prepare run workspace: {exc}") from exc

        # Write the run's pre-run config into .collrun/config/files/ — the
        # unified location for everything the user authored before the run
        # started. Read via `gen.config.get_file()` / `gen.config.list_files()` /
        # `gen.config.read_schema_values()`.
        if seeded_files:
            files_dir = os.path.join(workspace_dir, ".collrun", "config", "files")
            os.makedirs(files_dir, exist_ok=True)
            for rel, content in seeded_files.items():
                dst = os.path.join(files_dir, rel)
                os.makedirs(os.path.dirname(dst) or files_dir, exist_ok=True)
                with open(dst, "w", encoding="utf-8") as f:
                    f.write(content)

        # The saved prompt is available through gen.prompt; the selected
        # file's cell-relative path through gen.selected_file.
        res_dir = os.path.join(workspace_dir, ".collrun")
        if prompt:
            os.makedirs(res_dir, exist_ok=True)
            with open(os.path.join(res_dir, "prompt.md"), "w", encoding="utf-8") as pf:
                pf.write(prompt)
        if self.selected_file:
            os.makedirs(res_dir, exist_ok=True)
            with open(os.path.join(res_dir, "selected_file"), "w", encoding="utf-8") as sf:
                sf.write(self.selected_file)

        # Record the runner recipe this run resolved to, so it is reconstructable
        # even after the runner cell moves on.
        runner_snapshot = {
            "runner": resolved.runner_id,
            "name": resolved.runner_name,
            "generator": resolved.generator_id,
            "permissions": perms.to_dict(),
            **({"placement": self.placement} if self.placement else {}),
            "settings": merged_overlay,
            "folder_hashes": folder_hashes,
            "accounts": public_accounts_snapshot(self.account_context),
            "revision": resolved.revision,
            **({"selected_file": self.selected_file} if self.selected_file else {}),
        }

        # --- Config capture: the run node's Config artifacts, written at
        # kickoff from SOURCE DATA (never an end-of-run workspace walk, so the
        # record is byte-faithful to what the generator was given):
        #   prompt.md          — the user's full input (when non-empty)
        #   files/<name>       — each seeded settings file, effective content
        #   folders/<f>/<rel>  — staged runner config folders, REUSING the frozen
        #                        runner cell's blob hashes (no re-hash, no copies)
        #   run.yaml           — the materialized recipe
        run_yaml = yaml.safe_dump(
            {
                "runner": {
                    "id": resolved.runner_id,
                    "name": resolved.runner_name,
                },
                "generator": {
                    "name": resolved.generator_id,
                    "source_version_id": generator_cell_version_id,
                },
                "runner_source_version_id": runner_cell_version_id,
                # The grant the run ran under; ``placement`` says whether it is
                # the runner's own or was raised to the harness's minimum.
                "permissions": perms.to_dict(),
                **({"placement": self.placement} if self.placement else {}),
                "packages": resolved.packages or {},
                "settings_overlay": merged_overlay,
                "input_version_ids": [input_tip_id],
                **({"selected_file": self.selected_file} if self.selected_file else {}),
            },
            sort_keys=True,
            default_flow_style=False,
        )

        def _capture_config():
            from run_artifacts import record_artifact_blob, record_artifact_bytes

            conn = get_db_connection(channel)
            try:
                conn.execute(
                    "UPDATE runs SET runner_snapshot=? WHERE id=?",
                    (json.dumps(runner_snapshot, sort_keys=True), run_id),
                )
                if prompt:
                    record_artifact_bytes(
                        conn, run_id, "config", "prompt.md", prompt.encode("utf-8"),
                    )
                for rel, content in (seeded_files or {}).items():
                    record_artifact_bytes(
                        conn, run_id, "config", f"files/{rel}",
                        content.encode("utf-8"),
                    )
                for folder, hashes in (folder_hashes or {}).items():
                    for rel, blob_hash in hashes.items():
                        record_artifact_blob(
                            conn, run_id, "config", (f"files/{rel}" if folder == "__files__" else f"folders/{folder}/{rel}"),
                            blob_hash,
                        )
                record_artifact_bytes(
                    conn, run_id, "config", "run.yaml", run_yaml.encode("utf-8"),
                )
                conn.commit()
            finally:
                conn.close()

        try:
            capturing = asyncio.create_task(asyncio.to_thread(_capture_config))
            try:
                await asyncio.shield(capturing)
            except asyncio.CancelledError:
                await capturing
                raise
        except Exception as exc:
            raise HTTPException(500, f"Failed to capture run config: {exc}") from exc

        # The extras the target's manifest declares for the config value this run
        # actually resolved — resolved HERE, where manifest and values already
        # sit together, so neither spawn path has to re-read a file.
        optional_requirements = resolve_optional_requirements(
            resolved.generator_manifest, seeded_files)

        # The run's account leases (CLI login stores),
        # retained off the event loop: retaining takes file locks and reads the
        # keyring, which blocks for as long as a locked Secret Service does.
        # Taken before the last gate so nothing awaits between it and the
        # registration; a refusal from here on is unwound by the spawn's release.
        from process_manager.manager import retain_account_leases
        await retain_account_leases(self.account_context, run_id)

        # Last gate before the run goes live, re-checked here because the steps
        # above await: from this check to the registration below nothing else
        # runs, so a stop sweep sees either this run registered (and stops it)
        # or this start refused. A start whose own hold Stop all cancelled
        # (registry.cancel_holds), or that a per-run Stop reached while it was
        # queued (stop_queued, or its row already ended), unwinds as stopped;
        # a shutdown that began meanwhile refuses every start.
        if not hold_is_live(run_id) or run_id in _stops or not self._row_running():
            raise StartCancelled("the starting run was cancelled")
        if is_shutting_down():
            raise HTTPException(503, "Server is shutting down; not accepting new runs")

        manager = ProcessManager(
            channel=channel,
            run_id=run_id,
            app_dir=gen_app_dir,
            app_root_dir=app_dir,
            workspace_dir=workspace_dir,
            entrypoint=entrypoint,
            generator_name=resolved.generator_id,
            cell_id=cell_id,
            created_cell=bool(made["stack_id"]),
            permissions=perms,
            packages=resolved.packages,
            runner_id=resolved.runner_id,
            runner_name=resolved.runner_name,
            account_context=self.account_context,
            account_requirements=resolved.account_requirements,
            generator_cell_version_id=generator_cell_version_id,
            input_tip_id=input_tip_id,
            optional_requirements=optional_requirements,
        )
        made["manager"] = manager
        # What the run's /runs/active row reads, as a StartingRun carries it.
        setattr(manager, "effective_grant", self.placement["effective_grant"] if self.placement else None)
        setattr(manager, "adopted", bool(self.placement and self.placement["adopted"]))
        setattr(manager, "harness", self.placement["harness"] if self.placement else None)
        setattr(manager, "cli_locus", cli_locus(self.placement))
        # What the host terminal op checks a CLI on this machine against.
        setattr(manager, "placement", self.placement)
        active_managers[run_id] = manager
        await manager.start()
        return manager
