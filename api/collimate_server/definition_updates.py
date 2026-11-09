"""The update path for definitions: a Capabilities cell follows the upstream
its packages were admitted from, and Review and replace brings the
upstream's current copy in.

A cell's upstream is what its provenance records name (``provenance``):
the kind and id every non-runner record of the cell shares, when that kind
is followed (``app``, ``catalog``, ``channel``). Inside the upstream the
cell's packages are found by kind and manifest id, and the upstream cell is
the one holding them:

- ``app``: the content installed with the app, every cell of its two asset
  trees and each library it ships (as the cell a suggestion seeds it in); an
  app update changes it.
- ``catalog``: the user catalog's entry (``user_catalog``); a republish
  changes it.
- ``channel``: the Capabilities cell of that channel holding the packages
  (Copy to channel, or a channel copied from it); an edit there changes it.

**Status** (:func:`updates`): ``current`` when every package of the upstream
cell, as admission copies it and without its runners, is recorded on the
cell with the same content hash; ``update`` otherwise; ``missing`` when the
upstream no longer holds the packages (an entry removed, a channel deleted,
a cell the app stopped shipping); ``mixed`` when the records name two
upstreams or two upstream cells. ``edited`` says the cell's own packages no
longer hash as recorded: replacing discards those edits. A cell without a
followed record (moved onto Capabilities, added by Add to or imported from
a file, or in a channel older than channel migration 30) follows nothing
and is not listed. Runner records never decide anything: a cell's runners
are the channel's own.

**Review and replace** (:func:`plan_replace`, :func:`replace`) runs admission
in Replace mode: the upstream cell's files (without its runner packages) and
this cell's own runner packages pass ``package_import.prepare_packages``
against Capabilities without this cell, and the admitted files become a new
version of the same cell (its id, name, slot, tags and history kept) through
``cell_commits.commit_cell_change``. The records of the upstream's packages
are rewritten from the admission; the runners keep theirs. Each admitted
renderer's bundle is stored and a renderer new to the channel is made
available. The review lists each package as it changes (Add to's summary
for the new copy), the runners kept, the libraries the new ``cell.yaml``
names and where each resolves, for each library the cell holds the cells
whose ``cell.yaml`` names it (they run the new copy from their next run),
whether local edits are discarded, and what admission would hold back
(which refuses the replace).
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass

from fastapi import HTTPException

from collimate_server.models import Cell, Channel
from collimate_server.package_import import PreparedPackages
from collimate_server.provenance import FOLLOWED, Upstream

VERSION_LABEL = "Replaced from its upstream"
GENERATOR_NAME = "replace"
_TREES = (("generator", "generators"), ("renderer", "renderers"))


# ---------------------------------------------------------------------------
# Upstream copies
# ---------------------------------------------------------------------------

@dataclass
class UpstreamCell:
    """The upstream's copy of a cell: its name and icon, its files without
    runner packages (cell path to bytes, with the binary ones), and what the
    UI calls the upstream."""
    name: str
    icon: str | None
    bodies: dict[str, bytes]
    binary_paths: set[str]
    label: str


def _spec_cell(spec) -> UpstreamCell:
    from collimate_server.user_catalog import _publishable
    bodies = {f.name: f.data if f.data is not None else f.content.encode("utf-8") for f in spec.files}
    binary = {f.name for f in spec.files if f.data is not None}
    kept = _publishable(bodies)
    return UpstreamCell(spec.name, spec.icon or None, kept, binary & set(kept), "the app")


def app_cells() -> list[UpstreamCell]:
    """The content installed with the app as cells: each cell of its
    generator and renderer asset trees, then each library it ships as the
    cell a suggestion seeds it in."""
    from pathlib import Path
    import collimate_server.genesis as genesis
    from collimate._host.packaging import CellRef, bundled_libraries, read_suggested_cell
    from collimate._host.packaging.asset_cells import read_asset_cells
    root = Path(genesis.bundled_dir("content"))
    cells = []
    for kind, folder in _TREES:
        if (root / folder).is_dir():
            cells.extend(_spec_cell(spec) for spec in read_asset_cells(root / folder, kind))
    if (root / "generators").is_dir():
        for name in bundled_libraries(root / "generators"):
            cells.append(_spec_cell(read_suggested_cell(root, CellRef(name, f"generators/{name}"), "library")))
    return cells


class _Upstreams:
    """The upstream copies one request reads, each read once."""

    def __init__(self) -> None:
        self._app: list[UpstreamCell] | None = None

    def app(self) -> list[UpstreamCell]:
        if self._app is None:
            self._app = app_cells()
        return self._app

    def candidates(self, upstream: Upstream) -> list[UpstreamCell] | None:
        """The cells of ``upstream`` a cell's packages may be in, or None
        when the upstream is gone."""
        if upstream.kind == "app":
            return self.app()
        if upstream.kind == "catalog":
            from collimate_server.user_catalog import entry
            found = entry(upstream.id)
            return None if found is None else [UpstreamCell(found.name, found.icon, found.bodies,
                                                            found.binary_paths, f"‘{found.name}’ in your catalog")]
        from collimate_server.cell_placement import capabilities_stack_cells
        from collimate_server.package_import import cell_bytes
        from collimate_server.state import channel_db_name, state, state_lock
        from collimate_server.user_catalog import _publishable
        with state_lock:
            channel = next((c for c in state.channels if channel_db_name(c) == upstream.id), None)
            cells = [] if channel is None else [c.model_copy(deep=True) for c in capabilities_stack_cells(channel)]
        if channel is None:
            return None
        from collimate_server.cas import channel_store
        store = channel_store(upstream.id)
        found = []
        for cell in cells:
            bodies = _publishable(cell_bytes(store, cell))
            found.append(UpstreamCell(cell.name, cell.icon, bodies, {f.name for f in cell.files if f.blob_hash} & set(bodies),
                                      f"‘{cell.name}’ in the channel ‘{channel.name}’"))
        return found


def _non_runner(records: list[dict]) -> list[dict]:
    return [r for r in records if r["package_kind"] != "runner"]


def _identities(bodies: dict[str, bytes]) -> set[tuple[str, str, str]]:
    from collimate_server.package_import import package_identities
    return {row for row in package_identities(bodies) if row[0] != "runner"}


@dataclass
class Follow:
    """What one Capabilities cell follows: its upstream, the upstream's copy
    (None unless ``status`` is ``current`` or ``update``), the status and
    whether the cell's own packages were edited since admission."""
    cell: Cell
    upstream: Upstream
    source: UpstreamCell | None
    status: str
    edited: bool

    def public(self) -> dict:
        return {"cell_id": str(self.cell.id), "upstream": {"kind": self.upstream.kind, "id": self.upstream.id,
                                                           "label": self.source.label if self.source else None},
                "status": self.status, "edited": self.edited}


def _follow(store, cell: Cell, records: list[dict], upstreams: _Upstreams) -> Follow | None:
    from collimate_server.package_import import cell_bytes
    from collimate_server.user_catalog import _publishable
    followed = [r for r in _non_runner(records) if r["upstream_kind"] in FOLLOWED]
    if not followed:
        return None
    links = {(r["upstream_kind"], r["upstream_id"]) for r in followed}
    upstream = Upstream(*sorted(links)[0])
    recorded = {(r["package_kind"], r["package_id"], r["content_hash"]) for r in followed}
    own = _identities(_publishable(cell_bytes(store, cell)))
    edited = not recorded <= own
    if len(links) > 1 or len(followed) != len(_non_runner(records)):
        return Follow(cell, upstream, None, "mixed", edited)
    candidates = upstreams.candidates(upstream)
    if candidates is None:
        return Follow(cell, upstream, None, "missing", edited)
    wanted = {(kind, ident) for kind, ident, _ in recorded}
    holders = [c for c in candidates if {(k, i) for k, i, _ in _identities(c.bodies)} & wanted]
    if not holders:
        return Follow(cell, upstream, None, "missing", edited)
    if len(holders) > 1:
        return Follow(cell, upstream, None, "mixed", edited)
    (source,) = holders
    status = "current" if _identities(source.bodies) <= recorded else "update"
    return Follow(cell, upstream, source, status, edited)


def follows(channel: Channel, cell_ids=None) -> list[Follow]:
    """What each non-built-in Capabilities cell of ``channel`` (those of
    ``cell_ids`` when given) follows, in stack order; a cell that follows
    nothing is left out."""
    from collimate_server.cell_placement import capabilities_stack_cells
    from collimate_server.provenance import channel_records
    from collimate_server.state import state_lock
    with state_lock:
        cells = [c.model_copy(deep=True) for c in capabilities_stack_cells(channel)
                 if cell_ids is None or str(c.id) in cell_ids]
    records = channel_records(channel, [str(c.id) for c in cells])
    upstreams = _Upstreams()
    found = []
    from collimate_server.cas import channel_store
    store = channel_store(channel.id)
    for cell in cells:
        follow = _follow(store, cell, records.get(str(cell.id), []), upstreams)
        if follow is not None:
            found.append(follow)
    return found


def updates(channel: Channel) -> list[dict]:
    """``GET …/capabilities/updates``: each followed cell's ``{cell_id,
    upstream: {kind, id, label}, status, edited}``."""
    return [follow.public() for follow in follows(channel)]


# ---------------------------------------------------------------------------
# Review and replace
# ---------------------------------------------------------------------------

@dataclass
class ReplacePlan:
    follow: Follow
    prepared: PreparedPackages
    before: dict[str, bytes]
    own_folders: list[str]
    review: dict


def _token(follow: Follow, before: dict[str, bytes]) -> str:
    assert follow.source is not None
    body = [sorted((p, hashlib.sha256(b).hexdigest()) for p, b in follow.source.bodies.items()),
            sorted((p, hashlib.sha256(b).hexdigest()) for p, b in before.items())]
    return hashlib.sha256(json.dumps(body).encode()).hexdigest()


def plan_replace(channel: Channel, cell_id: str) -> ReplacePlan:
    """Admit the upstream's current copy of the Capabilities cell
    ``cell_id`` with the cell's own runners, on a detached candidate: the
    review and the prepared packages. 404 for a cell not on Capabilities,
    409 for one that follows nothing, has no update, or whose replacement
    admission holds a package of; 400 for a refusal."""
    import collimate_server.libraries as libraries
    import collimate_server.runners as runners
    from collimate.definitions import discover_definitions
    from collimate_server.cell_copy import capability_cell
    from collimate_server.cell_placement import capabilities_stack, capabilities_stack_cells
    from collimate_server.package_approval import vfiles
    from collimate_server.package_import import capture_sources, cell_bytes, package_rows, prepare_packages
    from collimate_server.state import state_lock

    cell = capability_cell(channel, cell_id)
    (follow,) = follows(channel, {str(cell.id)}) or [None]
    if follow is None:
        raise HTTPException(409, "This cell follows no upstream")
    if follow.status != "update" or follow.source is None:
        raise HTTPException(409, {"current": "This cell is up to date with its upstream",
                                  "missing": "This cell's upstream no longer holds it",
                                  "mixed": "This cell's packages come from more than one upstream"}[follow.status])
    source = follow.source
    from collimate_server.cas import channel_store
    store = channel_store(channel.id)
    before = cell_bytes(store, cell)
    kept = {f.name for f in runners.without_runners(cell.files)}
    own_runners = [f for f in cell.files if f.name not in kept]
    own_folders = [p.prefix for p in discover_definitions([f.name for f in own_runners]).packages]
    incoming = Cell(id=cell.id, name=cell.name, icon=cell.icon,
                    files=vfiles(source.bodies, store=store, binary_paths=source.binary_paths) + own_runners)
    candidate, signature = capture_sources(channel)
    stack = capabilities_stack(candidate)
    stack.cells = [c for c in stack.cells if str(c.id) != str(cell.id)]
    prepared = prepare_packages(channel, [incoming], upstream=follow.upstream, source_state=(candidate, signature))
    if prepared.skipped:
        raise HTTPException(409, "Held back: " + "; ".join(dict.fromkeys(s["reason"] for s in prepared.skipped)))
    if not prepared.cells:
        raise HTTPException(409, "The upstream copy holds no package")

    old = {(row["kind"], row["id"]): row for row in package_rows(before) if row["kind"] != "runner"}
    new = [row for row in package_rows(source.bodies) if row["kind"] != "runner"]
    old_hashes = _identities(before)
    new_hashes = _identities(source.bodies)
    changes = []
    for row in new:
        key = (row["kind"], row["id"])
        was = {h for k, i, h in old_hashes if (k, i) == key}
        now = {h for k, i, h in new_hashes if (k, i) == key}
        changes.append(row | {"change": "added" if key not in old else "same" if was == now else "changed"})
    changes += [row | {"change": "removed"} for key, row in old.items() if key not in {(r["kind"], r["id"]) for r in new}]
    with state_lock:
        others = [c.model_copy(deep=True) for c in capabilities_stack_cells(channel) if str(c.id) != str(cell.id)]
    held_ids = libraries.ids_on(others)
    try:
        names = libraries.shared_of_bodies(source.bodies)
    except ValueError as exc:
        raise ValueError(f"The upstream copy's {exc}") from exc
    own_libraries = libraries.ids_in_bodies(source.bodies)
    shipped = libraries.bundled_ids()
    catalog = libraries.catalog([*others, cell])
    named_by = {row["id"]: row["named_by"] for row in catalog["libraries"]}
    names_of = {str(c.id): c.name for c in [*others, cell]}
    review = {
        "cell": {"id": str(cell.id), "name": cell.name},
        "upstream": follow.public()["upstream"],
        "edited": follow.edited,
        "packages": changes,
        "runners": sorted(runners_row["name"] for runners_row in package_rows(before) if runners_row["kind"] == "runner"),
        "libraries": [{"id": name, "resolves": "cell" if name in own_libraries else "capabilities" if name in held_ids
                       else "app" if name in shipped else None} for name in names],
        "named_by": [{"id": ident, "cells": [names_of.get(cid, cid) for cid in named_by.get(ident, []) if cid != str(cell.id)]}
                     for ident in sorted(own_libraries)],
        "token": _token(follow, before),
    }
    return ReplacePlan(follow, prepared, before, own_folders, review)


def replace(channel: Channel, cell_id: str, token: str) -> dict:
    """Replace as :func:`plan_replace` reviewed it (409 when the cell or its
    upstream changed since): a new version of the same cell holding the
    admitted files. Answers ``{cell_id, version_id}``."""
    import collimate_server.provenance as provenance
    from collimate_server.cas import channel_store
    from collimate_server.cell_commits import cell_commit_lock, commit_cell_change
    from collimate_server.channel_config import RENDERER_AVAILABILITY_KEY, read_renderer_availability, write_ui_state_json
    from collimate_server.database import get_db_connection
    from collimate_server.renderer_bundles import publish_server_bundle
    from collimate_server.state import channel_db_name, persist_state

    plan = plan_replace(channel, cell_id)
    if plan.review["token"] != token:
        raise HTTPException(409, "The cell or its upstream changed; review the replace again")
    (admitted,) = plan.prepared.cells
    store = channel_store(channel.id)
    writes: dict[str, str | bytes] = {f.name: store.require_blob(f.blob_hash) if f.blob_hash else f.content
                                      for f in admitted.files}
    channel_uuid = channel_db_name(channel)
    expected = {p: hashlib.sha256(b).hexdigest() for p, b in plan.before.items()}
    own = plan.own_folders
    records = [r for r in plan.prepared.provenance if r["folder"] not in own]
    renderer_ids = list(plan.prepared.renderer_ids)

    def in_transaction(conn, before, after):
        if before != expected:
            raise HTTPException(409, "The cell changed; review the replace again")
        conn.execute(f"DELETE FROM cell_provenance WHERE cell_id=? AND folder NOT IN ({','.join('?' * len(own))})",
                     (cell_id, *own))
        provenance.write(conn, records)
        if renderer_ids:
            available = read_renderer_availability(channel_uuid, conn=conn)
            for renderer_id in renderer_ids:
                available.setdefault(renderer_id, True)
            write_ui_state_json(channel_uuid, RENDERER_AVAILABILITY_KEY, available, conn=conn)

    with cell_commit_lock(cell_id):
        run_id = str(uuid.uuid4())
        conn = get_db_connection(channel_uuid)
        try:
            conn.execute("INSERT INTO runs(id,prompt,status) VALUES (?,?,'completed')", (run_id, VERSION_LABEL))
            conn.commit()
        finally:
            conn.close()
        deletes = [path for path in expected if path not in writes]
        version_id = commit_cell_change(channel_uuid, cell_id, run_id, writes=writes, deletes=deletes,
                                        label=VERSION_LABEL, generator_name=GENERATOR_NAME,
                                        in_transaction=in_transaction, skip_unchanged=False)
    for source_hash, body in plan.prepared.bundles:
        publish_server_bundle(source_hash, body)
    persist_state()
    return {"cell_id": cell_id, "version_id": version_id}

