"""What a new channel starts from: its config selection and the cell
preparation both creation paths share.

A config selection picks each part of a new channel on its own: the
capability cells (``packages``), the settings document, the
standard main stacks (``main_cells``) and the cells that arrive on the
Removed shelf (``history_cells``). Each part comes from an existing
channel, from an uploaded file (a zip already expanded into the CAS, or a
settings JSON document) or is left out (``None``). Cells arrive with their
latest files only, like an export: one 'Imported' root version each.

Every capability cell passes package admission into the new channel's
Capabilities stack (:func:`admit_packages`), whatever its source, and
everything except the settings and the Removed shelf publishes through one
:class:`import_plan.ImportPlan` on the still unregistered channel. Each
admitted package records its provenance: ``channel`` with the source
channel's id, or ``zip``. ``suggested.seed_suggested`` shares
:func:`specs_to_cells`, :func:`admit_packages` and :func:`admission_errors`.
"""
from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from typing import TYPE_CHECKING

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict

from models import Cell, Channel, Stack
from provenance import ZIP, Source
from transfer_import import ImportCell, ImportStack

if TYPE_CHECKING:
    from lifecycle_snapshots import ExportSnapshot


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


class SettingsDoc(BaseModel):
    """A channel-settings document, as the settings export writes it."""
    model_config = ConfigDict(extra="forbid")
    doc: dict


class ConfigSelection(BaseModel):
    """The Config tab's choice; every part ``None`` is an empty channel."""
    model_config = ConfigDict(extra="forbid")
    packages: ChannelRef | PackagesZip | None = None
    settings: ChannelRef | SettingsDoc | None = None
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
    from runners import RunnerError
    try:
        yield
    except RunnerError as exc:
        raise HTTPException(exc.status, exc.reason) from exc
    except OSError as exc:
        raise HTTPException(409, "Stored content is missing or corrupt; retry") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def specs_to_cells(specs) -> list[Cell]:
    """Asset-tree cell specs as new channel cells, each under a fresh id."""
    from genesis import package_cell_vfiles
    return [Cell(id=uuid.uuid4(), name=spec.name, icon=spec.icon or None, files=package_cell_vfiles(spec))
            for spec in specs]


def frozen_cell(cell, frozen_files) -> Cell:
    """A detached copy of ``cell``, under a fresh id, holding its frozen
    accepted files, run folders left behind (and a pinned run path cleared).
    Text bodies are read from the pinned CAS blobs so the layout cache holds
    them; binary files stay referenced by hash."""
    from cas import require_blob
    from models import VirtualFile
    from run_folders import is_run_path
    files = [
        VirtualFile(name=f.name, language=f.language, content="", blob_hash=f.body_hash)
        if f.encoding == "binary" else
        VirtualFile(name=f.name, language=f.language, content=require_blob(f.body_hash).decode("utf-8"))
        for f in frozen_files if not is_run_path(f.name)
    ]
    pinned = cell.pinned if cell.pinned and not is_run_path(cell.pinned) else None
    return Cell(id=uuid.uuid4(), name=cell.name, icon=cell.icon,
                settings=cell.settings, pinned=pinned, tags=list(cell.tags), files=files)


def admit_packages(ch: Channel, plan, incoming: list[Cell], source: Source) -> list[dict]:
    """Admit ``incoming``, from ``source``, into the Capabilities stack of
    ``plan``'s candidate (ensured first) for the new channel; returns the
    held packages."""
    from genesis import ensure_capabilities_stack
    from package_import import prepare_packages
    ensure_capabilities_stack(plan.candidate)
    if not incoming:
        return []
    prepared = prepare_packages(ch, incoming, source=source, source_state=(plan.candidate, plan.signature))
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
    from state import require_channel
    try:
        return require_channel(channel_id)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise HTTPException(400, "Unknown source channel") from exc
        raise


def _package_cells(channel: Channel) -> list[str]:
    """The channel's capability cells (the built-in cards never travel)."""
    from cell_placement import capabilities_stack_cells
    from state import state_lock
    with state_lock:
        return [str(cell.id) for cell in capabilities_stack_cells(channel)]


def _main_cells(channel: Channel) -> list[str]:
    from state import state_lock
    with state_lock:
        return [str(cell.id) for stack in channel.main_stacks if stack.stack_type == "standard"
                for cell in stack.cells]


def _imported(specs: list[ImportCell], channel_uuid: str) -> list[Cell]:
    from transfer_import import _cell
    return [_cell(spec, channel_uuid) for spec in specs]


def _settings_doc(selection) -> dict | None:
    """The settings document to apply, validated before anything is written."""
    from channel_config import check_renderer_preferences
    from channel_settings import load_channel_settings, validate
    from state import channel_db_name
    if selection is None:
        return None
    if isinstance(selection, ChannelRef):
        return load_channel_settings(channel_db_name(_source(selection.channel_id)))
    doc = validate(selection.doc)
    check_renderer_preferences(doc["renderer_preferences"])
    return doc


def seed_config(ch: Channel, selection: ConfigSelection) -> list[dict]:
    """Seed the unregistered channel ``ch`` from ``selection``; returns the
    held packages. Packages and main stacks publish through one ImportPlan,
    then the settings document replaces the defaults, then the history cells
    land on the Removed shelf. Source channels stay frozen (their blobs
    pinned) until everything has committed."""
    from cell_placement import capabilities_stack_cells
    from import_plan import ImportPlan
    from lifecycle_snapshots import export_snapshot
    from state import channel_db_name

    channel_uuid = channel_db_name(ch)
    with admission_errors(), ExitStack() as frozen:
        settings = _settings_doc(selection.settings)
        incoming: list[Cell] = []
        origin = ZIP
        stacks: list[Stack] = []
        packages, main = selection.packages, selection.main_cells
        if isinstance(packages, ChannelRef):
            source = _source(packages.channel_id)
            origin = Source("channel", channel_db_name(source))
            ids = _package_cells(source)
            snapshot = _freeze_source(frozen, lambda: export_snapshot(source, ids))
            incoming = [frozen_cell(cell, snapshot.files_by_cell[str(cell.id)])
                        for cell in capabilities_stack_cells(snapshot.channel)
                        if str(cell.id) in snapshot.files_by_cell]
        elif isinstance(packages, PackagesZip):
            incoming = _imported(packages.cells, channel_uuid)
        if isinstance(main, ChannelRef):
            source = _source(main.channel_id)
            ids = _main_cells(source)
            snapshot = _freeze_source(frozen, lambda: export_snapshot(source, ids))
            # Only the frozen cells: one that joined a stack after the
            # selection was read has no frozen files and stays behind.
            for stack in snapshot.channel.main_stacks:
                cells = [frozen_cell(cell, snapshot.files_by_cell[str(cell.id)]) for cell in stack.cells
                         if str(cell.id) in snapshot.files_by_cell]
                if stack.stack_type == "standard" and cells:
                    stacks.append(Stack(name=stack.name, cells=cells))
        elif isinstance(main, MainZip):
            for spec in main.stacks:
                if not spec.cells:
                    raise ValueError(f"Main stack {spec.name!r} needs at least one cell")
                stacks.append(Stack(name=spec.name.strip(), cells=_imported(spec.cells, channel_uuid)))
        history = _history_cells(selection.history_cells, frozen, channel_uuid)

        plan = ImportPlan(ch, new_channel=True)
        plan.candidate.main_stacks.extend(stacks)
        skipped = admit_packages(ch, plan, incoming, origin)
        plan.publish()
        if settings is not None:
            from channel_settings import apply_settings_import
            apply_settings_import(channel_uuid, settings, replace=True)
        if history:
            _shelve(channel_uuid, history)
    return skipped


def _history_cells(selection, frozen: ExitStack, channel_uuid: str) -> list[tuple[Cell, str | None]]:
    """The history cells and the stack each left: a source channel's cells
    carrying the tag (live and removed, frozen in ``frozen``) or a history
    zip's cells, each given its tag."""
    from cell_tags import normalize_tag, union_tags
    from lifecycle_snapshots import freeze_tag_cells
    if isinstance(selection, TagRef):
        source = _source(selection.channel_id)
        snapshot = _freeze_source(frozen, lambda: freeze_tag_cells(source, selection.tag))
        cells: list[tuple[Cell, str | None]] = [
            (frozen_cell(cell, snapshot.files_by_cell[str(cell.id)]), stack.name)
            for stack in snapshot.channel.main_stacks + snapshot.channel.nav_stacks
            for cell in stack.cells if str(cell.id) in snapshot.files_by_cell]
        cells += [(frozen_cell(cell, files), stack_name) for cell, files, stack_name in snapshot.removed]
        if not cells:
            raise ValueError(f"No cell of the source channel carries the tag {selection.tag!r}")
        for cell, _stack in cells:
            cell.tags = union_tags([cell.tags])
        return cells
    if isinstance(selection, HistoryZip):
        tag = normalize_tag(selection.tag) if selection.tag.strip() else ""
        if tag is None:
            raise ValueError(f"Invalid tag {selection.tag!r}")
        cells = [(cell, None) for cell in _imported(selection.cells, channel_uuid)]
        for cell, _stack in cells:
            cell.tags = [tag] if tag else []
        return cells
    return []


def _shelve(channel_uuid: str, cells: list[tuple[Cell, str | None]]) -> None:
    """Put ``cells`` on the new channel's Removed shelf in one transaction:
    each gets its one imported root version, its working files emptied as an
    in-app Remove leaves them, and a removed record (as
    ``cell_catalog.remember_removed_cells`` writes one) that restores to the
    board."""
    import removed_cells
    from database import channel_transaction
    from import_plan import initialize_imported_cells
    from persistence import restore_snapshot
    from time_wire import now_iso
    removed_at = now_iso()
    with channel_transaction(channel_uuid) as conn:
        for cell, stack_name in cells:
            initialize_imported_cells(conn, [cell])
            restore_snapshot(conn, str(cell.id), [], writer_id="host:remove")
            removed_cells.remember(conn, cell=cell, stack_name=stack_name,
                                   stack_cell_count=1, removed_at=removed_at, reason="removed", display=None)
