"""Cell version reads, byte-safe file materialization and copy bookkeeping."""
from __future__ import annotations
import json
import logging
import uuid
from typing import Optional
from cell_lineage import snapshot_name
from channel_db import open_channel_db
from versions import version_file_rows, version_file_counts, initialize_file_snapshot

_logger = logging.getLogger("cells")

def walk_versions(conn, start_cell_id: str) -> list[str]:
    """Walk cell_edges forward from start_cell_id via
    parent_key='previous_version'. Returns the ordered list of version
    cell ids, including start_cell_id at index 0.

    A single recursive CTE instead of one round-trip per edge. One child
    per parent — the NEWEST edge (max rowid), the same branch rule the tip
    walk uses (versions.find_tip), so the version list
    the UI shows is the lineage the rest of the system resolves. Each CTE
    row is one ``(id, prev)`` step and UNION drops a repeated step, so a
    corrupt cycle ends the recursion; the list stops before the first
    repeated id. A chain longer than ``MAX_VERSION_CHAIN`` raises
    ``VersionChainTooLong``, as the tip walk does, rather than end the
    list at an interior version.
    """
    from versions import MAX_VERSION_CHAIN, VersionChainTooLong

    rows = conn.execute(
        """
        WITH RECURSIVE chain(id, prev) AS (
            SELECT ?, NULL
            UNION
            SELECT (
                SELECT child_cell_id FROM cell_edges
                WHERE parent_cell_id = chain.id
                  AND parent_key = 'previous_version'
                ORDER BY rowid DESC LIMIT 1
            ), chain.id
            FROM chain WHERE chain.id IS NOT NULL
            LIMIT ?
        )
        SELECT id, prev FROM chain WHERE id IS NOT NULL AND prev IS NOT NULL
        """,
        (start_cell_id, MAX_VERSION_CHAIN + 1),
    ).fetchall()
    next_of = {row[1]: row[0] for row in rows}
    versions = [start_cell_id]
    seen = {start_cell_id}
    while (nxt := next_of.get(versions[-1])) is not None and nxt not in seen:
        seen.add(nxt)
        versions.append(nxt)
    if len(versions) > MAX_VERSION_CHAIN:
        raise VersionChainTooLong(
            f"cell {start_cell_id}: version chain longer than {MAX_VERSION_CHAIN}"
        )
    return versions


def read_version_name(conn, cell_id: str) -> Optional[str]:
    """Return a cell's version name from its cell row. O(1)."""
    row = conn.execute(
        "SELECT version_name FROM cells WHERE id=?", (cell_id,),
    ).fetchone()
    return row["version_name"] if row else None


def read_cell_files(conn, cell_id: str) -> list[dict]:
    return version_file_rows(conn, cell_id)


def bulk_version_rows(conn, version_ids: list[str]) -> list[dict]:
    """Fetch cell metadata + file count for every id in a single pass.

    Batched cell metadata and logical file counts, then zip back to the
    chain order. Rows missing from
    ``cells`` still appear (with all metadata None) so the return shape
    matches the old per-id loop exactly. Each entry also carries the
    producing ``run_id``, which the caller pops before responding.
    """
    if not version_ids:
        return []
    placeholders = ",".join("?" for _ in version_ids)
    meta_rows = conn.execute(
        f"SELECT id, run_id, version_name, source_type, created_at, "
        f"generator_name, generator_cell_id "
        f"FROM cells WHERE id IN ({placeholders})",
        version_ids,
    ).fetchall()
    meta_by_id = {r["id"]: r for r in meta_rows}
    count_by_id = version_file_counts(conn, version_ids)
    approvals = {row["version_id"]: {key:row[key] for key in ("source_cell_id","source_version_id","content_hash")}
                 for row in conn.execute(f"SELECT * FROM package_approvals WHERE version_id IN ({placeholders})",version_ids)}
    out: list[dict] = []
    for idx, cell_id in enumerate(version_ids):
        meta = meta_by_id.get(cell_id)
        out.append({
            "cell_id": cell_id,
            "index": idx,
            "created_at": meta["created_at"] if meta else None,
            "name": meta["version_name"] if meta else None,
            "source_type": meta["source_type"] if meta else None,
            **({"approval":approvals[cell_id]} if cell_id in approvals else {}),
            "file_count": count_by_id.get(cell_id, 0),
            "generator_name": meta["generator_name"] if meta else None,
            # Provenance: the generator SOURCE VERSION (a cells-row id on the
            # generator cell's version chain) that produced this version.
            "generator_cell_id": meta["generator_cell_id"] if meta else None,
            "run_id": meta["run_id"] if meta else None,
        })
    return out


def load_runs(conn, run_ids: list[str]) -> dict[str, dict]:
    """``{run_id: row}``; ``runner_name`` is the name the run started under
    (its ``runner_snapshot``), None when it has none."""
    if not run_ids:
        return {}
    placeholders = ",".join("?" for _ in run_ids)
    rows = conn.execute(
        f"SELECT id, prompt, status, phase, created_at, kind, target_cell_id, "
        f"runner_id, runner_snapshot, generator_name FROM runs WHERE id IN ({placeholders})",
        run_ids,
    ).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        run = dict(r)
        run["runner_name"] = snapshot_name(run.pop("runner_snapshot"))
        out[run["id"]] = run
    return out


def run_artifact_listings(conn, run_ids: list[str]) -> dict[str, dict[str, list[dict]]]:
    """{run_id: {'config': [...], 'log': [...]}} — each entry
    {path, blob_hash, size}. Content is served by GET /api/blob/{hash}."""
    out: dict[str, dict[str, list[dict]]] = {}
    if not run_ids:
        return out
    placeholders = ",".join("?" for _ in run_ids)
    rows = conn.execute(
        f"SELECT ra.run_id, ra.category, ra.filepath, ra.blob_hash, b.size "
        f"FROM run_artifacts ra JOIN blobs b ON ra.blob_hash = b.hash "
        f"WHERE ra.run_id IN ({placeholders}) "
        f"ORDER BY ra.category, ra.filepath",
        run_ids,
    ).fetchall()
    for r in rows:
        bucket = out.setdefault(r["run_id"], {"config": [], "log": []})
        bucket[r["category"]].append({
            "path": r["filepath"],
            "blob_hash": r["blob_hash"],
            "size": r["size"],
        })
    return out


def checkpoint_cells(channel: str, cell_ids, prompt: str, *, best_effort=False) -> dict:
    """Group accepted SQL refs in history without changing live file revisions.

    A removed cell is skipped: its accepted files are removal tombstones, and
    a version of them would bury the removal checkpoint restore reads (an
    editor's unmount checkpoint lands right after its cell's removal)."""
    import removed_cells
    from cell_commits import cell_commit_locks
    from versions import resolve_cell_version
    ids = [str(c) for c in cell_ids]
    result: dict[str, str | None] = {cid: None for cid in ids}
    try:
        with cell_commit_locks(ids):
            conn = open_channel_db(channel)
            try:
                run_id = str(uuid.uuid4())
                conn.execute("INSERT INTO runs(id,prompt,status) VALUES (?,?,'completed')",(run_id,prompt))
                for cid in ids:
                    if removed_cells.get(conn, cid) is not None:
                        continue
                    version, changed = resolve_cell_version(conn,channel,cid,run_id)
                    result[cid] = version if changed else None
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()
    except Exception:
        if not best_effort:
            raise
        _logger.exception('Checkpoint failed for %s',ids)
        return {cid: None for cid in ids}
    return result


def copy_vfiles(cell) -> list:
    """A cell's in-memory files for a copy into another cell: binaries keep
    their blob hash, and a run folder (``_runs/``) never crosses."""
    from models import VirtualFile
    from run_folders import strip_run_paths

    return [
        VirtualFile(name=f.name, content=f.content, language=f.language, blob_hash=f.blob_hash)
        for f in strip_run_paths(list(cell.files))
    ]


def duplicate_from_version(conn, version_id: str, *, from_cell: str, name: str,
                            generator_name: str = "duplicate",
                            prompt: Optional[str] = None):
    """Create a new INDEPENDENT cell (fresh id) populated from a FROZEN
    version's CAS files — the core of :func:`fork_version`. ``from_cell`` is
    the canvas cell whose chain holds ``version_id``. Writes the new
    ``cells`` row, blobs + ``cell_files``, a 'commit' audit event and the
    'duplicate' lineage row (``from_version`` = ``version_id``) on the
    caller's OPEN connection (the caller owns the transaction / commit). Run
    folders (``_runs/``) of the version never cross, nor do annotation
    contexts. Returns ``(new_cell_id, [VirtualFile])``."""
    from cell_moves import record_move
    from versions import version_file_entries, build_virtual_files
    from persistence import initialize_files
    from cas import require_blob
    target_files = [f for f in version_file_entries(conn, version_id) if not f["path"].startswith("_runs/")]
    run_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO runs (id, prompt, status) VALUES (?, ?, 'completed')",
        (run_id, prompt or f"duplicate {version_id[:8]}"),
    )
    new_cell_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO cells (id, run_id, generator_name, sequence_num, "
        "version_name, source_type) "
        "VALUES (?, ?, ?, 1, ?, 'branch')",
        (new_cell_id, run_id, generator_name, name),
    )
    record_move(conn, op="duplicate", op_id=run_id, from_cell=from_cell,
                from_version=version_id, to_cell=new_cell_id, to_version=new_cell_id)
    for f in target_files:
        bh = f['body_hash']
        raw = require_blob(bh)
        conn.execute('INSERT OR IGNORE INTO blobs(hash,size) VALUES (?,?)', (bh,len(raw)))
        conn.execute('INSERT INTO cell_files(cell_id,filepath,blob_hash,encoding,language) VALUES (?,?,?,?,?)',
                     (new_cell_id,f['path'],bh,f['encoding'],f['language']))
    initialize_files(conn, new_cell_id, target_files)
    initialize_file_snapshot(conn, new_cell_id)
    conn.execute(
        "INSERT INTO run_events (run_id, event_type, payload) VALUES (?, 'commit', ?)",
        (
            run_id,
            json.dumps({
                "cell_id": new_cell_id, "name": name,
                "sequence_num": 1, "file_count": len(target_files),
            }),
        ),
    )
    vfiles = build_virtual_files(target_files)
    return new_cell_id, vfiles


def record_duplicates(channel: str, pairs: list[tuple[str, str]],
                      promoted: dict[str, str | None]) -> str:
    """Record one 'duplicate' lineage row per ``(source canvas id, copy id)``
    pair, ``ord`` its position, in one transaction. The routes insert the
    copies in memory first and, when this raises, take them back
    (:func:`take_back_copies`) before the layout is persisted.
    ``from_version`` is the source's tip after
    :func:`checkpoint_cells` (whose result is ``promoted``); a
    never-versioned source gets its genesis row now. ``op_id`` is the
    promotion's run when it made one, else a fresh 'duplicate' system run.
    Takes the sources' commit locks; returns the op id."""
    from cell_commits import ensure_tip_row
    from cell_moves import record_move
    from cell_write_scope import cell_write_scope
    from versions import find_tip

    sources = sorted({source for source, _ in pairs})
    with cell_write_scope(channel, sources):
        conn = open_channel_db(channel)
        try:
            made = [version for version in promoted.values() if version]
            row = conn.execute("SELECT run_id FROM cells WHERE id=?", made[:1]).fetchone() if made else None
            op_id = row[0] if row else str(uuid.uuid4())
            if row is None:
                conn.execute("INSERT INTO runs (id, prompt, status) VALUES (?, 'duplicate', 'completed')",
                             (op_id,))
            tips = {}
            for source in sources:
                tips[source] = find_tip(conn, source)
                ensure_tip_row(conn, channel, tips[source], op_id)
            from persistence import initialize_files
            from cell_placement import find_cell
            from state import require_channel
            for _source,copy in pairs:
                clone=find_cell(require_channel(channel),copy)[1]
                if clone is not None:
                    initialize_files(conn,copy,clone.files)
            for i, (source, copy) in enumerate(pairs):
                record_move(conn, op="duplicate", op_id=op_id, from_cell=source,
                            from_version=tips[source], to_cell=copy, ord=i)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
    return op_id


def take_back_copies(ch, selection, *, stack=None, cells=()) -> None:
    """Undo a Duplicate's in-memory insert when its lineage row could not be
    written: drop the new ``stack`` (or the copied ``cells`` from their
    stack) and put the selection back to ``selection``, so a copy is never on
    the canvas without its 'duplicate' move. The caller re-raises."""
    from cell_placement import iter_stacks
    from state import state_lock

    ids = {c.id for c in cells}
    with state_lock:
        if stack is not None and stack in ch.main_stacks:
            ch.main_stacks.remove(stack)
        for holder in iter_stacks(ch):
            if ids & {c.id for c in holder.cells}:
                holder.cells[:] = [c for c in holder.cells if c.id not in ids]
        ch.main_selection_id = selection


def materialize_stack_duplicate(ch, new_cells, *, stack_name, source_stack_id, anchor_stack_id):
    """Insert a fresh STANDARD stack of ``new_cells`` into ``ch.main_stacks``,
    placed right of a valid ``anchor_stack_id`` → else right of
    ``source_stack_id`` → else appended, and select it. Returns the new Stack.
    The placement rule of the cell Duplicate route."""
    from models import Stack
    from state import state_lock

    new_stack = Stack(name=stack_name or "", cells=new_cells)
    with state_lock:
        insert_idx = len(ch.main_stacks)
        for ref in (anchor_stack_id, source_stack_id):
            if not ref:
                continue
            idx = next(
                (i for i, s in enumerate(ch.main_stacks) if str(s.id) == str(ref)), None,
            )
            if idx is not None:
                insert_idx = idx + 1
                break
        ch.main_stacks.insert(insert_idx, new_stack)
        ch.main_selection_id = new_stack.id
    return new_stack
