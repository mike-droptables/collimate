"""Cell version, history and overview endpoints.

A cell's versions are rows linked in ``cell_edges`` by
``parent_key='previous_version'``. The canvas cell is the head of its chain;
walking forward from it yields every version that runs, checkpoints and
other writes produced. Fork and duplicate make new independent cells and
record the copy in ``cell_moves``.

Every cell address in this module uses the path parameter ``cell_id``.
The files pane's History rail and Fork address the canvas cell;
files-tree addresses an immutable version row without following its tip.
Fork's body ``target_cell_id`` selects the version to copy from that chain.

A canvas cell's metadata (name, icon, settings, pinned, tags) changes
through ``cells/{cell_id}/meta``; ``tags`` lists the channel's tag
vocabulary (``cell_tags``). Duplicate copies the source's name, icon and tags.
``outline`` lists a channel's capability cells and board stacks by id and
name; ``cells/{cell_id}/provenance`` lists where each package of a cell
came from (``provenance``). A built-in card (``Cell.builtin``) refuses fork, duplicate and metadata
edits, and the outline leaves it out.

The overview reads the Removed shelf (``cells/removed``), sets a removed
cell's tags (``cells/removed/{cell_id}/tags``), restores a removed cell
(``cells/{cell_id}/restore``), reads a cell's journal (or every
cell's) and the channel's lineage. Removing a cell by id (with a remove
zone's tag action) lives with the other layout mutations in
``routes/stacks.py``.
"""

from __future__ import annotations


import asyncio
import uuid
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from cell_journal import DEFAULT_LIMIT as DEFAULT_JOURNAL_LIMIT, MAX_LIMIT as MAX_JOURNAL_LIMIT
from cell_placement import BUILTIN_REFUSAL
from channel_db import open_channel_db


from cell_files import (
    walk_versions, read_version_name, bulk_version_rows,
    load_runs, run_artifact_listings,
    checkpoint_cells, copy_vfiles, duplicate_from_version, materialize_stack_duplicate,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# GET /history — the run-node view of a cell's version chain
# ---------------------------------------------------------------------------

@router.get("/api/channels/{channel}/cells/{cell_id}/history")
def get_cell_history(channel: str, cell_id: str) -> dict:
    """The run-node history of the canvas cell addressed by ``cell_id``.

    A NODE is either one generator run (kind='generator' AND
    target_cell_id == this cell: its config + every version it minted on
    this chain + its log) or a FILES node — a maximal consecutive group of
    versions produced by bookkeeping/system runs (manual checkpoints,
    forks, generator edits, …). A generator run with zero minted versions
    (crashed before its first emit, or still spinning up) still gets a
    node, placed by its created_at.

    Several runs can commit to one cell at once, so one run's versions may
    interleave with another's: each maximal run of consecutive versions from
    one run is its own node.

    Nodes are returned NEWEST FIRST. node_id is stable across refetches:
    ``run:<run_id>:<first version id of the node>`` for run nodes
    (``run:<run_id>`` for a run with no version yet),
    ``files:<newest version cell id>`` for files nodes — the frontend keys
    its selection on it, never on index, so a head insert (live run, idle
    checkpoint) can't steal the user's position. Config/log content is
    fetched via GET /api/blob/{hash}.
    """
    from cell_commits import run_node_id

    conn = open_channel_db(channel)
    try:
        version_ids = walk_versions(conn, cell_id)
        versions = bulk_version_rows(conn, version_ids)
        run_id_by_cell: dict[str, str | None] = {
            v["cell_id"]: v.pop("run_id") for v in versions
        }

        # Every generator run that targeted this cell also gets a node even
        # when it minted no version on the chain.
        target_runs = conn.execute(
            "SELECT id FROM runs WHERE kind='generator' AND target_cell_id=?",
            (cell_id,),
        ).fetchall()
        all_run_ids = sorted(
            {rid for rid in run_id_by_cell.values() if rid}
            | {r["id"] for r in target_runs}
        )
        runs = load_runs(conn, all_run_ids)
        artifacts = run_artifact_listings(conn, all_run_ids)

        def _is_run_node(rid: str | None) -> bool:
            run = runs.get(rid) if rid else None
            return bool(
                run
                and run.get("kind") == "generator"
                and run.get("target_cell_id") == cell_id
            )

        def _run_payload(rid: str) -> dict:
            run = runs.get(rid) or {}
            return {
                "run_id": rid,
                "status": run.get("status"),
                "phase": run.get("phase"),
                "created_at": run.get("created_at"),
                "prompt": run.get("prompt"),
                "runner_id": run.get("runner_id"),
                "runner_name": run.get("runner_name"),
                "generator_name": run.get("generator_name"),
            }

        # Group the chain (oldest→newest) into nodes.
        nodes: list[dict] = []
        for entry in versions:
            rid = run_id_by_cell.get(entry["cell_id"])
            if rid is not None and _is_run_node(rid):
                if nodes and nodes[-1]["kind"] == "run" and nodes[-1]["run"]["run_id"] == rid:
                    nodes[-1]["versions"].append(entry)
                else:
                    nodes.append({
                        "node_id": run_node_id(rid, entry["cell_id"]),
                        "kind": "run",
                        "run": _run_payload(rid),
                        "versions": [entry],
                        "config": (artifacts.get(rid) or {}).get("config", []),
                        "logs": (artifacts.get(rid) or {}).get("log", []),
                    })
            else:
                if nodes and nodes[-1]["kind"] == "files":
                    nodes[-1]["versions"].append(entry)
                else:
                    nodes.append({
                        "node_id": "",  # filled below (newest version id)
                        "kind": "files",
                        "run": None,
                        "versions": [entry],
                        "config": [],
                        "logs": [],
                    })
        for n in nodes:
            if n["kind"] == "files":
                n["node_id"] = f"files:{n['versions'][-1]['cell_id']}"

        # Zero-version generator runs slot in by timestamp.
        seen_run_ids = {
            n["run"]["run_id"] for n in nodes if n["kind"] == "run"
        }
        def _node_ts(n: dict) -> str:
            if n["versions"]:
                return n["versions"][-1]["created_at"] or ""
            return (n["run"] or {}).get("created_at") or ""
        for rid in all_run_ids:
            if rid in seen_run_ids or not _is_run_node(rid):
                continue
            node = {
                "node_id": run_node_id(rid, None),
                "kind": "run",
                "run": _run_payload(rid),
                "versions": [],
                "config": (artifacts.get(rid) or {}).get("config", []),
                "logs": (artifacts.get(rid) or {}).get("log", []),
            }
            ts = _node_ts(node)
            at = 0
            for i, existing in enumerate(nodes):
                if _node_ts(existing) <= ts:
                    at = i + 1
            nodes.insert(at, node)

        nodes.reverse()  # newest first
        current_node_id = nodes[0]["node_id"] if nodes else None
        # The tip version (the newest committed state of the cell).
        tip_cell_id = version_ids[-1] if version_ids else cell_id
        for n in nodes:
            if any(v["cell_id"] == tip_cell_id for v in n["versions"]):
                current_node_id = n["node_id"]
                break

        return {
            "nodes": nodes,
            "total_versions": len(version_ids),
            "current": {
                "node_id": current_node_id,
                "version_cell_id": tip_cell_id,
            },
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# GET /files-tree — full file tree for a specific immutable version
# ---------------------------------------------------------------------------

@router.get("/api/channels/{channel}/cells/{cell_id}/files-tree")
def get_cell_files_tree(channel: str, cell_id: str) -> dict:
    """Uniform lazy metadata for a pinned immutable history snapshot."""
    from versions import version_file_rows
    conn = open_channel_db(channel)
    try:
        rows = version_file_rows(conn, cell_id)
    finally:
        conn.close()
    from cas import require_blob
    files = []
    for row in rows:
        require_blob(row['blob_hash'])
        files.append(dict(name=row['filepath'],body_hash=row['blob_hash'],encoding=row['encoding'],
                          language=row['language'],file_revision=0,
                          writer_id='host:history',edit_seq=0,deleted=False))
    return {'cell_id':cell_id,'files':files}


# ---------------------------------------------------------------------------
# POST /checkpoint — group accepted revisions into a history entry
# ---------------------------------------------------------------------------

@router.post("/api/channels/{channel}/cells/{cell_id}/checkpoint")
async def checkpoint_cell(channel: str, cell_id: str) -> dict:
    """Group a cell's accepted file revisions into an immutable version.

    Called by the frontend on idle timeout (~60s), editor close, or other
    save triggers. If accepted files match the latest checkpoint, returns
    ``{"created": false}``. Otherwise creates a new version at the tip
    with ``source_type='manual_edit'``. Live file revisions remain unchanged.

    A cell with a live run (or a start's hold) is not checkpointed: the
    accepted file revisions remain readable while history creation waits for
    the next run start or idle checkpoint after the runs end.
    """
    from run_registry import running_cell_ids
    from state import channel_db_name, require_channel

    channel_uuid = channel_db_name(require_channel(channel))
    if cell_id in running_cell_ids():
        return {"created": False, "version_id": None}
    promoted = await asyncio.to_thread(checkpoint_cells, channel_uuid, [cell_id], "checkpoint")
    version_id = promoted.get(cell_id)
    return {"created": bool(version_id), "version_id": version_id}


# ---------------------------------------------------------------------------
# POST /fork — create a new cell in a new stack right of the source
# ---------------------------------------------------------------------------

class ForkRequest(BaseModel):
    target_cell_id: str  # the internal-version cell id to fork from


@router.post("/api/channels/{channel}/cells/{cell_id}/fork")
def fork_version(channel: str, cell_id: str, body: ForkRequest) -> dict:
    """Fork the canvas cell addressed by ``cell_id`` into a new stack
    immediately right of its stack, populated with ``target_cell_id``'s files.
    The branch has its own id — it's a separate chain from the source.
    """
    from cell_placement import insert_new_stack_right_of
    from state import channel_db_name, flush_persist_state, persist_state, require_channel, state_lock
    from models import Cell as CellModel, Stack

    # Resolve the channel and the source's MAIN stack before any DB write,
    # so a rejection leaves no orphan branch behind.
    with state_lock:
        target_channel = require_channel(channel)
        source_stack = next(
            (s for s in target_channel.main_stacks
             if any(str(c.id) == cell_id for c in s.cells)),
            None,
        )
        if source_stack is None:
            raise HTTPException(404, "source stack not found on the main canvas")
        if any(str(c.id) == cell_id and c.builtin for c in source_stack.cells):
            raise HTTPException(400, BUILTIN_REFUSAL)
        source_stack_id = source_stack.id
    channel_uuid = channel_db_name(target_channel)

    conn = open_channel_db(channel)
    try:
        version_ids = walk_versions(conn, cell_id)
        if body.target_cell_id not in version_ids:
            raise HTTPException(404, "target_cell_id is not in this cell's versions")
        branch_name = (read_version_name(conn, body.target_cell_id) or "Cell") + " (branch)"
        new_cell_id, vfiles = duplicate_from_version(
            conn, body.target_cell_id, from_cell=cell_id, name=branch_name,
            generator_name="history_scrubber_branch",
            prompt=f"branch {body.target_cell_id[:8]}",
        )
        conn.commit()
    finally:
        conn.close()

    new_cell = CellModel(
        id=new_cell_id,  # pyright: ignore[reportArgumentType] — pydantic coerces str→UUID
        name=branch_name,
        files=vfiles,
        channel=channel_uuid,  # registered UUID
    )
    # In-memory state update: new stack right of the source. The branch is
    # committed, so it always lands: appended if the source left meanwhile.
    # The channel is re-resolved: a restore or delete may have replaced it.
    with state_lock:
        target_channel = require_channel(channel)
        try:
            new_stack = insert_new_stack_right_of(target_channel, source_stack_id)
        except ValueError:
            new_stack = Stack(name="", cells=[])
            target_channel.main_stacks.append(new_stack)
        new_stack.cells.insert(0, new_cell)
    persist_state()
    flush_persist_state()
    from cell_lineage import announce_lineage
    announce_lineage(channel, [cell_id, new_cell_id])

    return {
        "ok": True,
        "new_cell_id": new_cell_id,
        "new_stack_id": str(new_stack.id),
        "forked_from": body.target_cell_id,
    }


# ---------------------------------------------------------------------------
# POST /cells/{cell_id}/duplicate — copy a live cell into a new stack
# ---------------------------------------------------------------------------

class DuplicateRequest(BaseModel):
    anchor_stack_id: Optional[str] = None


@router.post("/api/channels/{channel}/cells/{cell_id}/duplicate")
def duplicate_cell(channel: str, cell_id: str, body: Optional[DuplicateRequest] = None) -> dict:
    """Copy the canvas cell ``cell_id`` into a fresh stack beside the anchor or
    source stack, recording the copy in the lineage (op 'duplicate')."""
    from cell_files import record_duplicates, take_back_copies
    from cell_lineage import announce_lineage
    from cell_placement import find_cell
    from models import Cell as CellModel
    from state import channel_db_name, flush_persist_state, persist_state, require_channel
    from routes.layout import stripped_channel

    ch = require_channel(channel)
    src_stack, original = find_cell(ch, cell_id)
    if src_stack is None or original is None:
        raise HTTPException(404, f"cell {cell_id!r} not on the canvas")
    if original.builtin:
        raise HTTPException(400, BUILTIN_REFUSAL)
    channel_uuid = channel_db_name(ch)
    promoted = checkpoint_cells(channel_uuid, [cell_id], "duplicate", best_effort=True)
    new_cell = CellModel(
        name=f"{original.name} (Copy)", icon=original.icon, tags=list(original.tags),
        files=copy_vfiles(original),
    )
    selection = ch.main_selection_id
    new_stack = materialize_stack_duplicate(
        ch, [new_cell], stack_name="", source_stack_id=src_stack.id,
        anchor_stack_id=body.anchor_stack_id if body else None,
    )
    try:
        record_duplicates(channel_uuid, [(cell_id, str(new_cell.id))], promoted)
    except BaseException:
        take_back_copies(ch, selection, stack=new_stack)
        raise
    persist_state()
    announce_lineage(channel, [cell_id, new_cell.id])
    flush_persist_state()
    return {
        "ok": True, "new_stack_id": str(new_stack.id),
        "new_cell_id": str(new_cell.id), "channel": stripped_channel(ch),
    }


# ---------------------------------------------------------------------------
# PATCH /cells/{cell_id}/meta — a canvas cell's name, icon, settings, pin, tags
# ---------------------------------------------------------------------------

class CellMetaPatch(BaseModel):
    """A header metadata edit. An absent field is left alone; ``icon: null``
    clears the icon. The name and icon are bounded: the name rides every
    state broadcast, and an icon is a key of the frontend's icon registry.
    ``tags`` is the cell's full new list (``cell_tags`` rules), typed loosely
    so a non-string item is the route's 400, not a 422."""
    name: Optional[str] = Field(None, max_length=512)
    icon: Optional[str] = Field(None, max_length=64)
    pinned: Optional[str] = None
    settings: Optional[dict] = None
    tags: Optional[list] = None


@router.patch("/api/channels/{channel}/cells/{cell_id}/meta")
async def patch_cell_meta(channel: str, cell_id: uuid.UUID, body: CellMetaPatch) -> dict:
    """Set a canvas cell's metadata: the header's title editors, icon picker,
    pin, settings form and tag menu. Only the supplied fields change; file
    revisions belong to the file mutation service. Layout and rename history
    commit together before the new metadata becomes visible. ``tags``
    replaces the whole list, normalized; an invalid list (a non-string item
    included) or ``tags: null`` is a 400, as is any edit of a built-in card.
    A tags-only edit records no rename and announces no lineage change.
    Returns the cell's
    ``{id, name, icon, settings, pinned, tags, updated_at}``."""
    from cell_tags import normalize_tags
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(400, "nothing to update: send name, icon, settings, pinned or tags")
    if "name" in updates and updates["name"] is None:
        raise HTTPException(400, "name cannot be null")
    if "tags" in updates:
        if updates["tags"] is None:
            raise HTTPException(400, "tags cannot be null: send the full list")
        try:
            updates["tags"] = normalize_tags(updates["tags"])
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    def _apply() -> dict:
        from cell_metadata import publish_cell_metadata
        from cell_lineage import user_actor
        from cell_placement import find_cell
        from state import require_channel, state_lock
        with state_lock:
            cell = find_cell(require_channel(channel), cell_id)[1]
            if cell is not None and cell.builtin:
                raise HTTPException(400, BUILTIN_REFUSAL)
        return publish_cell_metadata(channel, str(cell_id), updates,
                                     source_type="manual_edit", actor=user_actor())

    return await asyncio.to_thread(_apply)


# ---------------------------------------------------------------------------
# The Removed shelf, restore, the journal and the lineage (the overview)
# ---------------------------------------------------------------------------

@router.get("/api/channels/{channel}/tags")
def get_channel_tags(channel: str) -> list[dict]:
    """The channel's tag vocabulary ``[{tag, live, removed}]``, sorted by
    casefold: every tag on a cell of the layout or on a removed cell's
    record, with how many of each carry it (``cell_tags``)."""
    from cell_tags import channel_tags
    return channel_tags(channel)


@router.get("/api/channels/{channel}/outline")
def get_channel_outline(channel: str) -> dict:
    """The channel's cells by name, read from the layout (no files, no
    freeze): ``{capabilities_cells: [{id, name}], main_stacks: [{id, name,
    cells: [{id, name}]}]}``. ``capabilities_cells`` are the Capabilities
    stack's cells after the built-in cards, open or hidden; ``main_stacks``
    are the board's other stacks in order."""
    from cell_placement import capabilities_stack_cells, is_capabilities_stack
    from state import require_channel, state_lock

    def named(item) -> dict:
        return {"id": str(item.id), "name": item.name}

    with state_lock:
        ch = require_channel(channel)
        return {
            "capabilities_cells": [named(c) for c in capabilities_stack_cells(ch)],
            "main_stacks": [named(s) | {"cells": [named(c) for c in s.cells]}
                            for s in ch.main_stacks if not is_capabilities_stack(s)],
        }


@router.get("/api/channels/{channel}/cells/{cell_id}/provenance")
def get_cell_provenance(channel: str, cell_id: uuid.UUID) -> dict:
    """Where each package of the cell came from: ``{sdk_version, records:
    [{folder, source_kind, source_id, package_kind, package_id,
    package_version, content_hash, sdk_version, admitted_at}]}``, the
    records admission wrote (``provenance``) ordered by folder, and the SDK
    version this server runs, which a record admitted under another SDK
    differs from. A cell without a record answers none."""
    from app_version import sdk_version
    from provenance import cell_records
    from state import require_channel
    return {"sdk_version": sdk_version(), "records": cell_records(require_channel(channel), str(cell_id))}


@router.get("/api/channels/{channel}/cells/removed")
def list_removed_channel_cells(
    channel: str,
    sort: Literal["created", "edited", "removed"] = "edited",
    dir: Literal["asc", "desc"] = "desc",
    q: str = "",
) -> list[dict]:
    """The Removed shelf: every removed cell with its tip, author, files,
    tags and a bounded preview, searched by ``q`` and sorted by created,
    last edited or last removed time (``cell_catalog``)."""
    from cell_catalog import list_removed_cells
    return list_removed_cells(channel, sort=sort, direction=dir, query=q)


class RemovedTagsPut(BaseModel):
    """A removed cell's full new tag list (``cell_tags`` rules), typed
    loosely so a non-string item is the route's 400, not a 422."""
    tags: list


@router.put("/api/channels/{channel}/cells/removed/{cell_id}/tags")
def put_removed_cell_tags(channel: str, cell_id: uuid.UUID, body: RemovedTagsPut) -> dict:
    """Set the tags of a cell on the Removed shelf (its record, which restore
    gives back): the shelf card's tag menu. 400 for an invalid list, 409 when
    the cell is on the canvas (its tags change through ``…/meta``), 404
    without a shelf record. Emits ``lineage_changed``. Returns ``{cell_id,
    tags}``, the tags normalized."""
    from cell_catalog import set_removed_cell_tags
    return set_removed_cell_tags(channel, str(cell_id), body.tags)


@router.post("/api/channels/{channel}/cells/{cell_id}/restore")
def restore_channel_cell(channel: str, cell_id: uuid.UUID) -> dict:
    """Bring a removed cell back under its own id at the right end of the
    board, selected (the shelf has no anchor). 409 when it is on the canvas."""
    from cell_catalog import restore_cell
    return restore_cell(channel, str(cell_id))


@router.get("/api/channels/{channel}/cells/{cell_id}/journal")
def get_cell_journal(
    channel: str, cell_id: uuid.UUID, before: Optional[str] = None,
    limit: int = Query(DEFAULT_JOURNAL_LIMIT, ge=1, le=MAX_JOURNAL_LIMIT),
) -> list[dict]:
    """The cell's journal, newest first, older than ``before`` (an ISO time
    with an offset): makes, edits, runs and moves (``cell_journal``)."""
    from cell_journal import journal
    return journal(channel, str(cell_id), before=before, limit=limit)


@router.get("/api/channels/{channel}/journal")
def get_channel_journal(
    channel: str, before: Optional[str] = None,
    limit: int = Query(DEFAULT_JOURNAL_LIMIT, ge=1, le=MAX_JOURNAL_LIMIT),
) -> list[dict]:
    """Every cell's journal at once (the overview's All cells state), in the
    shape of ``GET /cells/{cell_id}/journal``."""
    from cell_journal import journal
    return journal(channel, None, before=before, limit=limit)


@router.get("/api/channels/{channel}/lineage")
def get_channel_lineage(channel: str) -> dict:
    """``{cells, moves}`` from the lineage record and the catalog; never walks
    version chains or files (``cell_lineage``)."""
    from cell_lineage import lineage
    return lineage(channel)
