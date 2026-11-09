"""What a new channel starts from: its config selection and the cell
preparation both creation paths share.

A config selection picks each part of a new channel on its own: the
capability cells (``packages``), the standard main stacks (``main_cells``)
and the cells that arrive on the Removed shelf (``history_cells``). Each
part comes from an existing channel, from an uploaded zip already expanded
into the CAS, or is left out (``None``). Cells arrive with their latest
files only, like an export: one 'Imported' root version each. A new
channel's settings are always the defaults: it follows the server's default
theme until it overrides it.

Every capability cell passes package admission into the new channel's
Capabilities stack (:func:`admit_packages`), whatever its source, and
everything except the Removed shelf publishes through one
:class:`import_plan.ImportPlan` on the still unregistered channel. Each
admitted package records its upstream: ``channel`` with the source
channel's id, or ``zip``. ``suggested.seed_suggested`` shares
:func:`specs_to_cells`, :func:`admit_packages` and :func:`admission_errors`.

A cell copied from a channel keeps its tip's repositories: their rows land
with its first version, after every object they name was copied from the
source channel's git store into the new channel's
(``git_store.copy_between_stores``); a zip's cell adopts its uploaded
``.git`` folders (``transfer_import.Adoptions``). Without usable host git a copy
writes no rows ("repository history not copied: git is not available on
this host").
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from typing import TYPE_CHECKING

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict

from collimate_server.models import Cell, Channel, Stack
from collimate_server.provenance import ZIP, Upstream
from collimate_server.transfer_import import GITDIR, Adoptions, ImportCell, ImportStack

if TYPE_CHECKING:
    from collimate_server.lifecycle_snapshots import ExportSnapshot
    from collimate_server.repository_state import RepositoryState

_logger = logging.getLogger(__name__)

NOT_COPIED = "repository history not copied: git is not available on this host"


class ChannelRef(BaseModel):
    """A registered channel to copy from."""
    model_config = ConfigDict(extra="forbid")
    channel_id: str


class TagRef(BaseModel):
    """The cells of a registered channel carrying one tag (casefold)."""
    model_config = ConfigDict(extra="forbid")
    channel_id: str
    tag: str


class PackagesZip(BaseModel):
    """A packages zip's ``Capabilities/<cell>`` cells."""
    model_config = ConfigDict(extra="forbid")
    cells: list[ImportCell]


class MainZip(BaseModel):
    """A main zip's ``<stack>/<cell>`` stacks."""
    model_config = ConfigDict(extra="forbid")
    stacks: list[ImportStack]


class HistoryZip(BaseModel):
    """A history zip's cells and the tag they all get ('' leaves them
    untagged)."""
    model_config = ConfigDict(extra="forbid")
    tag: str
    cells: list[ImportCell]


class ConfigSelection(BaseModel):
    """The Config tab's choice; every part ``None`` is an empty channel."""
    model_config = ConfigDict(extra="forbid")
    packages: ChannelRef | PackagesZip | None = None
    main_cells: ChannelRef | MainZip | None = None
    history_cells: TagRef | HistoryZip | None = None


# ---------------------------------------------------------------------------
# Shared cell preparation and admission
# ---------------------------------------------------------------------------

@contextmanager
def admission_errors() -> Iterator[None]:
    """Map a creation step's failures to HTTP answers: a refused package or
    document is a 400 (a runner error keeps its own status), missing or
    corrupt stored content a 409. Source freezes map their own conflicts
    (:func:`_freeze_source`)."""
    from collimate_server.runners import RunnerError
    try:
        yield
    except RunnerError as exc:
        raise HTTPException(exc.status, exc.reason) from exc
    except OSError as exc:
        raise HTTPException(409, "Stored content is missing or corrupt; retry") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def specs_to_cells(specs, store) -> list[Cell]:
    """Asset-tree cell specs as new channel cells, each under a fresh id,
    their binary bodies stored into ``store`` (the target channel's)."""
    from collimate_server.genesis import package_cell_vfiles
    return [Cell(id=uuid.uuid4(), name=spec.name, icon=spec.icon or None, files=package_cell_vfiles(spec, store))
            for spec in specs]


def _copied_path(cell: Cell, repo_path: str) -> str:
    """How ``skipped`` names a copied cell's repository: ``<cell>/<repo_path>/.git``."""
    return "/".join(part for part in (cell.name, repo_path, GITDIR) if part)


class Copied:
    """The repositories a new channel's cells take from source channels:
    ``rows`` by new cell id and repo path, each source channel's states,
    whose objects :meth:`copy_stores` copies into the new channel's store,
    and ``skipped``, the ones not copied (``{path: "<cell>/<repo_path>/.git",
    reason}``)."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, RepositoryState]] = {}
        self.sources: dict[str, list[RepositoryState]] = {}
        self.skipped: list[dict] = []
        self._cells: dict[str, Cell] = {}

    def take(self, source: str, cell: Cell, repositories: Mapping[str, RepositoryState],
             unreadable: Mapping[str, str] | None = None) -> None:
        """The new ``cell``'s frozen ``repositories`` from ``source``; a row
        that could not be read (``unreadable``, its reason by repo path) is
        skipped."""
        self.skipped += [{"path": _copied_path(cell, repo_path), "reason": f"repository history not copied: {reason}"}
                         for repo_path, reason in sorted((unreadable or {}).items())]
        if repositories:
            self.rows[str(cell.id)] = dict(repositories)
            self._cells[str(cell.id)] = cell
            self.sources.setdefault(source, []).extend(repositories.values())

    def copy_stores(self, channel_uuid: str) -> None:
        """Copy every source's objects into ``channel_uuid``'s store, or,
        without usable host git, drop every row, each one skipped with
        :data:`NOT_COPIED`. A failed copy raises ``git_store.StoreFailed``,
        unwinding the create."""
        import collimate_server.git_host as git_host
        import collimate_server.git_store as git_store
        if not self.rows:
            return
        if not git_host.capability().ok:
            _logger.warning("%s: %d cells of channel %s", NOT_COPIED, len(self.rows), channel_uuid)
            self.skipped += [{"path": _copied_path(self._cells[cell_id], repo_path), "reason": NOT_COPIED}
                             for cell_id, held in self.rows.items() for repo_path in sorted(held)]
            self.rows.clear()
            return
        for source, states in sorted(self.sources.items()):
            git_store.copy_between_stores(source, channel_uuid, states)


def frozen_cell(cell, frozen_files, stores, repositories: Mapping[str, RepositoryState] | None = None,
                copied: tuple[Copied, str] | None = None, unreadable: Mapping[str, str] | None = None) -> Cell:
    """A detached copy of ``cell``, under a fresh id, holding its frozen
    accepted files, run folders left behind (and a pinned run path cleared).
    Text bodies are read from the pinned CAS blobs so the layout cache holds
    them; binary files stay referenced by hash. Its frozen ``repositories``
    are re-keyed to the copy's id in ``copied`` (the holder and the source
    channel), where the rows that could not be read (``unreadable``) are
    skipped. ``stores`` is ``(source, target)``: a binary body is copied
    from the source channel's store into the target's (``copy_bodies``)."""
    from collimate_server.cas import copy_bodies
    from collimate_server.models import VirtualFile
    from collimate_server.run_folders import is_run_path
    source_store, target_store = stores
    kept = [f for f in frozen_files if not is_run_path(f.name)]
    copy_bodies(source_store, target_store, [f.body_hash for f in kept if f.encoding == "binary"])
    files = [
        VirtualFile(name=f.name, language=f.language, content="", blob_hash=f.body_hash)
        if f.encoding == "binary" else
        VirtualFile(name=f.name, language=f.language, content=source_store.require_blob(f.body_hash).decode("utf-8"))
        for f in kept
    ]
    pinned = cell.pinned if cell.pinned and not is_run_path(cell.pinned) else None
    copy = Cell(id=uuid.uuid4(), name=cell.name, icon=cell.icon,
                settings=cell.settings, pinned=pinned, tags=list(cell.tags), files=files)
    if (repositories or unreadable) and copied is not None:
        holder, source = copied
        holder.take(source, copy, repositories or {}, unreadable)
    return copy


def admit_packages(ch: Channel, plan, incoming: list[Cell], upstream) -> list[dict]:
    """Admit ``incoming``, from ``upstream`` (one ``provenance.Upstream``,
    or each cell's by its id), into the Capabilities stack of
    ``plan``'s candidate (ensured first) for the new channel; returns the
    held packages."""
    from collimate_server.genesis import ensure_capabilities_stack
    from collimate_server.package_import import prepare_packages
    ensure_capabilities_stack(plan.candidate)
    if not incoming:
        return []
    prepared = prepare_packages(ch, incoming, upstream=upstream, source_state=(plan.candidate, plan.signature))
    return plan.add_packages(prepared)["skipped"]


# ---------------------------------------------------------------------------
# The config selection
# ---------------------------------------------------------------------------

SOURCE_CHANGED = "Source channel changed; retry"


def _freeze_source(frozen: ExitStack, freeze: Callable[[], ExportSnapshot]) -> ExportSnapshot:
    """Enter a source channel's freeze on ``frozen``. A source whose cells
    moved under the freeze (``export_snapshot``/``freeze_tag_cells`` raise
    ValueError, or LookupError) is a 409 the client retries, not a refusal."""
    try:
        return frozen.enter_context(freeze())
    except (ValueError, LookupError) as exc:
        raise HTTPException(409, SOURCE_CHANGED) from exc


def _source(channel_id: str) -> Channel:
    from collimate_server.state import require_channel
    try:
        return require_channel(channel_id)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise HTTPException(400, "Unknown source channel") from exc
        raise


def _package_cells(channel: Channel) -> list[str]:
    """The channel's capability cells (the built-in cards never travel)."""
    from collimate_server.cell_placement import capabilities_stack_cells
    from collimate_server.state import state_lock
    with state_lock:
        return [str(cell.id) for cell in capabilities_stack_cells(channel)]


def _main_cells(channel: Channel) -> list[str]:
    from collimate_server.state import state_lock
    with state_lock:
        return [str(cell.id) for stack in channel.main_stacks if stack.stack_type == "standard"
                for cell in stack.cells]


def _imported(specs: list[ImportCell], channel_uuid: str, adopting: list) -> list[Cell]:
    """The zip's cells, their bodies copied from the staging store (where
    the new-channel page expanded the zip) into the new channel's; each
    one's uploaded repositories join ``adopting``."""
    from collimate_server.cas import channel_store, copy_bodies, is_cas_hash, staging_store
    from collimate_server.transfer_import import _cell
    copy_bodies(staging_store(), channel_store(channel_uuid),
                [item.blob_hash for spec in specs for item in spec.files
                 if is_cas_hash(item.blob_hash) and staging_store().has(item.blob_hash)])
    cells = [_cell(spec, channel_uuid) for spec in specs]
    adopting += [(cell, spec.repositories) for cell, spec in zip(cells, specs)]
    return cells


def seed_config(ch: Channel, selection: ConfigSelection) -> tuple[list[dict], list[dict]]:
    """Seed the unregistered channel ``ch`` from ``selection``; returns the
    held packages and the repositories left behind, ``{path, reason}``: a
    copied cell's that were not copied (:attr:`Copied.skipped`), then a
    zip cell's uploaded ``.git`` git would not adopt. Packages and main stacks publish
    through one ImportPlan, after the copied cells' git objects reached the
    new channel's store and the zip cells' were adopted, then the history
    cells land on the Removed shelf. Source channels stay frozen (their blobs pinned) until
    everything has committed."""
    from collimate_server.cell_placement import capabilities_stack_cells
    from collimate_server.database import get_db_connection
    from collimate_server.import_plan import ImportPlan
    from collimate_server.lifecycle_snapshots import export_snapshot
    from collimate_server.state import channel_db_name

    from collimate_server.cas import channel_store
    channel_uuid = channel_db_name(ch)
    target = channel_store(channel_uuid)
    copied = Copied()
    adopting: list = []
    with admission_errors(), ExitStack() as frozen, Adoptions(channel_uuid) as adoptions:
        incoming: list[Cell] = []
        origin: Upstream = ZIP
        stacks: list[Stack] = []
        packages, main = selection.packages, selection.main_cells
        if isinstance(packages, ChannelRef):
            source = _source(packages.channel_id)
            origin = Upstream("channel", channel_db_name(source))
            ids = _package_cells(source)
            snapshot = _freeze_source(frozen, lambda: export_snapshot(source, ids))
            incoming = [_frozen(snapshot, cell, copied, target) for cell in capabilities_stack_cells(snapshot.channel)
                        if str(cell.id) in snapshot.files_by_cell]
        elif isinstance(packages, PackagesZip):
            incoming = _imported(packages.cells, channel_uuid, adopting)
        if isinstance(main, ChannelRef):
            source = _source(main.channel_id)
            ids = _main_cells(source)
            snapshot = _freeze_source(frozen, lambda: export_snapshot(source, ids))
            # Only the frozen cells: one that joined a stack after the
            # selection was read has no frozen files and stays behind.
            for stack in snapshot.channel.main_stacks:
                cells = [_frozen(snapshot, cell, copied, target) for cell in stack.cells
                         if str(cell.id) in snapshot.files_by_cell]
                if stack.stack_type == "standard" and cells:
                    stacks.append(Stack(name=stack.name, cells=cells))
        elif isinstance(main, MainZip):
            for spec in main.stacks:
                if not spec.cells:
                    raise ValueError(f"Main stack {spec.name!r} needs at least one cell")
                stacks.append(Stack(name=spec.name.strip(), cells=_imported(spec.cells, channel_uuid, adopting)))
        history = _history_cells(selection.history_cells, frozen, channel_uuid, copied, adopting)

        plan = ImportPlan(ch, new_channel=True)
        plan.candidate.main_stacks.extend(stacks)
        skipped = admit_packages(ch, plan, incoming, origin)
        if copied.rows or adopting:
            # The store is made beside the new channel's DB.
            get_db_connection(channel_uuid).close()
        with _store_refusal():
            copied.copy_stores(channel_uuid)
        for cell, repositories in adopting:
            adoptions.add(cell, repositories)
        with adoptions.publishing() as landed:
            # Each publication writes the rows of its own cells only.
            plan.repositories = {**copied.rows, **landed}
            plan.publish()
            if history:
                _shelve(channel_uuid, history, plan.repositories)
    for item in adoptions.skipped:
        _logger.info("new channel %s: %s not adopted: %s", channel_uuid, item["path"], item["reason"])
    return skipped, [*copied.skipped, *adoptions.skipped]


@contextmanager
def _store_refusal() -> Iterator[None]:
    """A failed copy of the source's git objects as a 409 naming git's words."""
    import collimate_server.git_store as git_store
    try:
        yield
    except git_store.StoreFailed as exc:
        raise HTTPException(409, exc.reason) from exc


def _frozen(snapshot: ExportSnapshot, cell, copied: Copied, target) -> Cell:
    """``frozen_cell`` of a live layout cell of ``snapshot`` into the store
    ``target``, its repositories re-keyed into ``copied``."""
    from collimate_server.state import channel_db_name
    return frozen_cell(cell, snapshot.files_by_cell[str(cell.id)], (snapshot.store, target),
                       snapshot.repositories.get(str(cell.id)),
                       (copied, channel_db_name(snapshot.channel)), snapshot.unreadable.get(str(cell.id)))


def _history_cells(selection, frozen: ExitStack, channel_uuid: str, copied: Copied,
                   adopting: list) -> list[tuple[Cell, str | None]]:
    """The history cells and the stack each left: a source channel's cells
    carrying the tag (live and removed, frozen in ``frozen``, their
    repositories re-keyed into ``copied``) or a history zip's cells, each
    given its tag (their uploaded repositories joining ``adopting``)."""
    from collimate_server.cell_tags import normalize_tag, union_tags
    from collimate_server.lifecycle_snapshots import freeze_tag_cells
    from collimate_server.state import channel_db_name
    if isinstance(selection, TagRef):
        source = _source(selection.channel_id)
        snapshot = _freeze_source(frozen, lambda: freeze_tag_cells(source, selection.tag))
        source_uuid = channel_db_name(snapshot.channel)
        from collimate_server.cas import channel_store
        target = channel_store(channel_uuid)
        cells: list[tuple[Cell, str | None]] = [
            (_frozen(snapshot, cell, copied, target), stack.name)
            for stack in snapshot.channel.main_stacks + snapshot.channel.nav_stacks
            for cell in stack.cells if str(cell.id) in snapshot.files_by_cell]
        cells += [(frozen_cell(item.cell, item.files, (snapshot.store, target), item.repositories,
                               (copied, source_uuid), item.unreadable),
                   item.stack_name)
                  for item in snapshot.removed]
        if not cells:
            raise ValueError(f"No cell of the source channel carries the tag {selection.tag!r}")
        for cell, _stack in cells:
            cell.tags = union_tags([cell.tags])
        return cells
    if isinstance(selection, HistoryZip):
        tag = normalize_tag(selection.tag) if selection.tag.strip() else ""
        if tag is None:
            raise ValueError(f"Invalid tag {selection.tag!r}")
        cells = [(cell, None) for cell in _imported(selection.cells, channel_uuid, adopting)]
        for cell, _stack in cells:
            cell.tags = [tag] if tag else []
        return cells
    return []


def _shelve(channel_uuid: str, cells: list[tuple[Cell, str | None]],
            repositories: Mapping[str, Mapping[str, RepositoryState]]) -> None:
    """Put ``cells`` on the new channel's Removed shelf in one transaction:
    each gets its one imported root version (with its ``repositories``, by
    cell id), its working files emptied as an in-app Remove leaves them, and
    a removed record (as ``cell_catalog.remember_removed_cells`` writes one)
    that restores to the board."""
    import collimate_server.removed_cells as removed_cells
    from collimate_server.database import channel_transaction
    from collimate_server.import_plan import initialize_imported_cells, repository_rows
    from collimate_server.persistence import restore_snapshot
    from collimate_server.time_wire import now_iso
    removed_at = now_iso()
    rows = repository_rows(repositories)
    with channel_transaction(channel_uuid) as conn:
        for cell, stack_name in cells:
            initialize_imported_cells(conn, [cell], repositories=rows)
            restore_snapshot(conn, str(cell.id), [], writer_id="host:remove")
            removed_cells.remember(conn, cell=cell, stack_name=stack_name,
                                   stack_cell_count=1, removed_at=removed_at, reason="removed", display=None)
