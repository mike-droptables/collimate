"""Copy to channel: a Capabilities cell, admitted onto another channel's
Capabilities stack.

The copy is the cell's accepted files (its runners included; nothing under
``_runs/``), under a fresh id, through the same admission every door takes
(``package_import.prepare_packages``: the built-in verifiers, the holds, id
and handler-type uniqueness, the libraries its ``cell.yaml`` names). Each
admitted package records the source channel as its upstream
(``provenance.Upstream("channel", <source channel id>)``), so the copy
follows the source: when the source channel's copy of its packages changes,
the copy shows an update (``definition_updates``).

**Libraries travel with the copy.** Each name the cell's ``cell.yaml`` lists
under ``shared:`` resolves on the target as it would at a run: a library the
copy itself holds, else one on the target's Capabilities, else one the app
ships. A name that resolves nowhere on the target brings the source
channel's Capabilities cell holding that library along, as a copy of its own
(and that cell's own names resolve the same way); a name the source cannot
supply either refuses the copy with the reason. The review lists every name
and where it resolves before anything is copied.

The review (:func:`plan_copy`) admits everything on a detached candidate and
answers a ``token`` over the source cells' accepted files; the copy itself
(:func:`copy_cell`) plans again, refuses a changed token (409) and publishes
through an ``ImportPlan`` on the target, whose own signature check refuses a
target that changed meanwhile. A cell's repositories are not copied
(Capabilities cells hold definitions, not checkouts).
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass

from fastapi import HTTPException

from collimate_server.models import Cell, Channel
from collimate_server.package_import import PreparedPackages


@dataclass
class FrozenCell:
    """A Capabilities cell's accepted files at one tip: the cell (its
    metadata), its tip and its files as cell path to bytes, with their
    languages and which are binary."""
    cell: Cell
    tip: str
    bodies: dict[str, bytes]
    languages: dict[str, str]
    binary_paths: set[str]

    def copy(self, store, *, keep=lambda path: True) -> Cell:
        """The cell under a fresh id holding the files ``keep`` accepts
        (never ``_runs/``), its binary bodies written into ``store``, the
        target channel's body store: the copy duplicates them."""
        from collimate_server.package_approval import vfiles
        files = {p: b for p, b in self.bodies.items() if not p.startswith("_runs/") and keep(p)}
        return Cell(id=uuid.uuid4(), name=self.cell.name, icon=self.cell.icon,
                    files=vfiles(files, store=store, languages=self.languages, binary_paths=self.binary_paths))


def capability_cell(channel: Channel, cell_id: str) -> Cell:
    """The non-built-in Capabilities cell ``cell_id`` of ``channel``; 404
    when it is no such cell."""
    from collimate_server.cell_placement import capabilities_stack_cells
    from collimate_server.state import state_lock
    with state_lock:
        cell = next((c for c in capabilities_stack_cells(channel) if str(c.id) == str(cell_id)), None)
        if cell is None:
            raise HTTPException(404, "No such cell on Capabilities")
        return cell.model_copy(deep=True)


def freeze(channel: Channel, cells: list[Cell]) -> list[FrozenCell]:
    """``cells`` of ``channel`` at their accepted tips, read under their
    commit locks."""
    from collimate_server.cell_commits import cell_commit_locks
    from collimate_server.package_approval import snapshot_cell
    ids = [str(c.id) for c in cells]
    with cell_commit_locks(ids):
        snapshots = [snapshot_cell(channel, cell_id) for cell_id in ids]
    return [FrozenCell(cell, snap.tip, snap.bytes(), dict(snap.languages), set(snap.binary_paths))
            for cell, snap in zip(cells, snapshots)]


def token_of(frozen: list[FrozenCell]) -> str:
    """What a review saw of the cells it read: each one's id, tip and file
    hashes."""
    body = [(str(f.cell.id), f.tip, sorted((p, hashlib.sha256(b).hexdigest()) for p, b in f.bodies.items()))
            for f in frozen]
    return hashlib.sha256(json.dumps(body).encode()).hexdigest()


@dataclass
class CopyPlan:
    prepared: PreparedPackages
    review: dict
    token: str


def _library_rows(source_cells: list[Cell], target_cells: list[Cell], cell: Cell) -> tuple[list[dict], list[Cell]]:
    """Where each library the copy names resolves on the target, and the
    source cells it brings along for those that resolve nowhere there."""
    import collimate_server.libraries as libraries
    held_source = libraries.packages(source_cells)
    on_target = libraries.ids_on(target_cells)
    shipped = libraries.bundled_ids()
    chosen = [cell]
    rows: list[dict] = []
    queue = list(libraries.cell_shared(cell.files))
    seen: set[str] = set()
    while queue:
        name = queue.pop(0)
        if name in seen:
            continue
        seen.add(name)
        carrier = next((c for c in chosen if name in libraries.ids_on([c])), None)
        if carrier is not None:
            rows.append({"id": name, "resolves": "copy", "cell": carrier.name})
        elif name in on_target:
            rows.append({"id": name, "resolves": "capabilities", "cell": None})
        elif name in shipped:
            rows.append({"id": name, "resolves": "app", "cell": None})
        elif len(held_source.get(name, [])) == 1:
            brought = held_source[name][0][0]
            chosen.append(brought)
            rows.append({"id": name, "resolves": "copy", "cell": brought.name})
            queue.extend(libraries.cell_shared(brought.files))
        else:
            rows.append({"id": name, "resolves": None, "cell": None})
    return rows, chosen


def plan_copy(source: Channel, cell_id: str, target: Channel) -> CopyPlan:
    """Admit a copy of the Capabilities cell ``cell_id`` of ``source`` (with
    the library cells it brings) onto ``target``'s Capabilities, on a
    detached candidate: the review and the prepared packages. Raises
    ``ValueError`` (a 400 through ``admission_errors``) for a refusal, 409
    when every package of the cell is held on the target."""
    from collimate.definitions import discover_definitions
    from collimate_server.cell_placement import capabilities_stack_cells
    from collimate_server.package_import import package_rows, prepare_packages
    from collimate_server.provenance import Upstream
    from collimate_server.state import channel_db_name, state_lock

    if channel_db_name(source) == channel_db_name(target):
        raise HTTPException(400, "Copy to another channel")
    cell = capability_cell(source, cell_id)
    with state_lock:
        source_cells = [c.model_copy(deep=True) for c in capabilities_stack_cells(source)]
        target_cells = [c.model_copy(deep=True) for c in capabilities_stack_cells(target)]
    libraries, chosen = _library_rows(source_cells, target_cells, cell)
    unresolved = [row["id"] for row in libraries if row["resolves"] is None]
    if unresolved:
        quoted = ", ".join(f"‘{name}’" for name in unresolved)
        raise ValueError(f"‘{cell.name}’ names {quoted} under shared: in its cell.yaml, which neither "
                         f"‘{target.name}’ nor the app holds and no cell of this channel's Capabilities supplies")
    frozen = freeze(source, chosen)
    from collimate_server.cas import channel_store
    incoming = [f.copy(channel_store(target.id)) for f in frozen]
    names = {str(c.id): c.name for c in incoming}
    prepared = prepare_packages(target, incoming, upstream=Upstream("channel", channel_db_name(source)))
    first = str(incoming[0].id)
    # A cell whose every package is held: its other files are not worth a cell.
    prepared.cells = [c for c in prepared.cells if discover_definitions([f.name for f in c.files]).packages]
    if not any(str(c.id) == first for c in prepared.cells):
        reasons = "; ".join(dict.fromkeys(skip["reason"] for skip in prepared.skipped
                                          if skip["cell_id"] == first)) or "it holds no package"
        raise HTTPException(409, f"Nothing to copy: {reasons}")
    review = {
        "source": {"channel_id": channel_db_name(source), "channel": source.name, "cell_id": str(cell.id)},
        "target": {"channel_id": channel_db_name(target), "channel": target.name},
        "cells": [{"name": f.cell.name, "brought": at > 0, "packages": package_rows(f.bodies)}
                  for at, f in enumerate(frozen)],
        "libraries": libraries,
        "skipped": [{**skip, "cell": names.get(skip["cell_id"], "")} for skip in prepared.skipped],
        "token": token_of(frozen),
    }
    return CopyPlan(prepared, review, review["token"])


def copy_cell(source: Channel, cell_id: str, target: Channel, token: str) -> dict:
    """Copy as :func:`plan_copy` reviewed it: 409 when the source cells
    changed since the review (``token``). Answers ``{cell_id, cell_ids,
    package_skips}``: the copy's id on the target, then every cell landed
    (the copy, then each library cell it brought)."""
    from collimate_server.cell_lineage import announce_lineage
    from collimate_server.package_import import apply_packages
    from collimate_server.state import channel_db_name, persist_state

    plan = plan_copy(source, cell_id, target)
    if plan.token != token:
        raise HTTPException(409, "The cell changed; review the copy again")
    prepared = plan.prepared
    report = apply_packages(target, prepared)
    persist_state()
    ids = [str(c.id) for c in prepared.cells]
    announce_lineage(channel_db_name(target), ids)
    return {"cell_id": ids[0], "cell_ids": ids, "package_skips": report["skipped"]}
