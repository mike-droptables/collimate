"""History adapters over the common accepted-file mutation service.

Every cell uses sorted cell locks before a brief state lock. Expensive body
admission runs outside the global lock, and memory/events follow SQL commit.
Host cards and sweeps are deliberate mutations; runs/users supply expected
revisions through persistence.py. Immutable history groups accepted snapshots.
"""

from __future__ import annotations

import versions
import cell_placement

import json
import logging
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Callable, Iterable

from cas import read_blob as blob_bytes
from database import get_db_connection
from run_folders import (
    RUNS_PREFIX,
    is_run_path,
    label_of,
    normalize,
    run_card_path,
    run_folder,
    strip_run_paths,
)

logger = logging.getLogger("cell_commits")


# ---------------------------------------------------------------------------
# Locks
# ---------------------------------------------------------------------------

class CellLock:
    """A reentrant per-cell commit lock that knows its owning thread (the
    version writers assert they hold it)."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._owner: int | None = None
        self._depth = 0

    def acquire(self) -> None:
        self._lock.acquire()
        self._owner = threading.get_ident()
        self._depth += 1

    def release(self) -> None:
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
        self._lock.release()

    def held_by_me(self) -> bool:
        return self._owner == threading.get_ident()

    def __enter__(self) -> "CellLock":
        self.acquire()
        return self

    def __exit__(self, *_exc) -> None:
        self.release()


_LOCKS: dict[str, CellLock] = {}
_LOCKS_GUARD = threading.Lock()


def cell_commit_lock(cell_id: str) -> CellLock:
    """The commit lock of ``cell_id`` (keyed by cell id alone — channel names
    are not canonical, cell ids are uuids)."""
    key = str(cell_id)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = _LOCKS[key] = CellLock()
        return lock


@contextmanager
def cell_commit_locks(cell_ids: Iterable[str]):
    """Hold several cells' commit locks, taken in sorted id order."""
    locks = [cell_commit_lock(cid) for cid in sorted({str(c) for c in cell_ids if c})]
    taken: list[CellLock] = []
    try:
        for lock in locks:
            lock.acquire()
            taken.append(lock)
        yield
    finally:
        for lock in reversed(taken):
            lock.release()


def assert_cell_lock_held(cell_id: str) -> None:
    """Every version write asserts its cell's commit lock is held by the
    calling thread."""
    if not cell_commit_lock(cell_id).held_by_me():
        raise RuntimeError(
            f"version write on cell {cell_id} without its commit lock "
            "(cell_commits.cell_commit_lock)"
        )


# ---------------------------------------------------------------------------
# File maps
# ---------------------------------------------------------------------------

def _store(raw: bytes) -> str:
    from cas import store_bytes
    return store_bytes(raw)


def file_content_hash(file) -> str:
    """A file's CAS digest without hashing unchanged text again.

    The private cache never changes public blob_hash: that field denotes
    explicit binary content. Keeping the source string in the key makes
    assignments safe, including direct edits to an existing VirtualFile.
    """
    if file.blob_hash:
        return file.blob_hash
    content = file.content or ""
    cached = file._content_hash_cache
    if cached is not None and (cached[0] is content or cached[0] == content):
        return cached[1]
    from cas import store_bytes
    digest = store_bytes(content.encode("utf-8"))
    file._content_hash_cache = (content, digest)
    return digest


def ensure_tip_row(conn, channel: str, tip_id: str, run_id: str) -> bool:
    """Pin accepted current files as the first history snapshot.
    A never-versioned cell needs this parent for its version edge. Returns
    whether it wrote that row."""
    seq = conn.execute(
        "SELECT COALESCE(MAX(sequence_num), 0) FROM cells WHERE run_id=?", (run_id,),
    ).fetchone()[0] + 1
    from persistence import snapshot_files
    current = snapshot_files(conn, tip_id)
    return versions.synthesize_genesis_row(conn, tip_id, run_id, seq, current)


def broadcast_cell_commit(channel: str, cell_id: str, run_id: str | None,
                          generator_name: str | None = None, *, files: list[str]) -> None:
    """``dag_commit`` for the STABLE canvas cell (renderers key on it) plus
    the coalesced state transport."""
    from database import canonical_channel_id
    from events import broadcast
    from state import broadcast_state_update
    channel_uuid = canonical_channel_id(channel)
    from collimate._host.channel_events import DagCommit
    broadcast(channel_uuid, DagCommit(cell_id, files, run_id, generator_name).to_dict())
    broadcast_state_update()


# ---------------------------------------------------------------------------
# The general writer
# ---------------------------------------------------------------------------

def _hash_writes(writes: dict | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for path, value in (writes or {}).items():
        if not isinstance(value,(str,bytes)):
            raise ValueError('Host writes require explicit text or bytes')
        raw = value.encode("utf-8") if isinstance(value, str) else value
        out[path] = _store(raw)
    return out


def commit_cell_change(
    channel: str,
    cell_id: str,
    run_id: str,
    *,
    writes: dict | None = None,
    deletes: Iterable[str] = (),
    delete_prefix: str | None = None,
    label: str = "Manual Edit",
    source_type: str = "manual_edit",
    generator_name: str = "manual_edit",
    in_transaction: Callable | None = None,
    skip_unchanged: bool = True,
) -> str | None:
    """Host publication through the same accepted-file transaction service."""
    from persistence import snapshot_files, restore_snapshot, publish_files
    writes = writes or {}
    hashed = _hash_writes(writes)
    with cell_commit_lock(cell_id):
        conn = get_db_connection(channel)
        try:
            metadata = snapshot_files(conn,cell_id)
            from models import language_for
            entries = {f['path']:f for f in metadata}
            for path,digest in hashed.items():
                entries[path] = dict(path=path,body_hash=digest,encoding='utf-8' if isinstance(writes[path],str) else 'binary',
                                     language=entries.get(path,{}).get('language',language_for(path)))
            before = {f['path']: f['body_hash'] for f in metadata}
            after = dict(before)
            after.update(hashed)
            for path in deletes:
                after.pop(path,None)
            if delete_prefix:
                prefix = delete_prefix.rstrip('/') + '/'
                after = {p:h for p,h in after.items() if not normalize(p).startswith(prefix)}
            if skip_unchanged and after == before and all(entries[f['path']]['encoding'] == f['encoding'] for f in metadata if f['path'] in entries):
                if in_transaction:
                    in_transaction(conn,before,after)
                    conn.commit()
                return None
            if before.keys() - after.keys():
                # Freeze accepted bytes before a destructive host transition,
                # including edits newer than an existing explicit run log.
                tip_id, _ = versions.resolve_cell_version(conn,channel,cell_id,run_id)
            else:
                tip_id = versions.find_tip(conn,cell_id)
                ensure_tip_row(conn,channel,tip_id,run_id)
            tip_files = versions.tip_file_map(conn,tip_id)
            typed = [entries[p] for p in after]
            receipt = restore_snapshot(conn,cell_id,typed, writer_id='host:' + generator_name)
            version_id, _event = versions.insert_version(conn,tip_id=tip_id,parent_files=tip_files,run_id=run_id,
                files=after,file_entries=typed,version_name=label,source_type=source_type,generator_name=generator_name,owner_id=cell_id)
            if in_transaction:
                in_transaction(conn,before,after)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
        publish_files(channel,cell_id,receipt)
    broadcast_cell_commit(channel,cell_id,run_id,generator_name,files=sorted(before.keys() | after.keys()))
    return version_id


# ---------------------------------------------------------------------------
# Run card + sweeps
# ---------------------------------------------------------------------------

def write_source_version(conn, cell, files, run_id, *, label, source_type='manual_edit', generator_name='source-editor', replace_prefix=None):
    """Stage source changes and history; caller commits before publishing."""
    from persistence import snapshot_files,restore_snapshot
    current = snapshot_files(conn,str(cell.id))
    if replace_prefix is not None:
        selected = [f for f in files if f.name.startswith(replace_prefix)]
        entries = [f for f in current if not f['path'].startswith(replace_prefix)] + selected
    else:
        entries = files
    tip = versions.find_tip(conn,str(cell.id))
    ensure_tip_row(conn,cell.channel or '',tip,run_id)
    restore_snapshot(conn,str(cell.id),entries,writer_id='host:' + generator_name)
    accepted = {f['path']:f['body_hash'] for f in snapshot_files(conn,str(cell.id))}
    return versions.insert_version(conn,tip_id=tip,parent_files=versions.tip_file_map(conn,tip),run_id=run_id,
        files=accepted,file_entries=snapshot_files(conn,str(cell.id)),version_name=label,source_type=source_type,generator_name=generator_name,owner_id=str(cell.id))[0]


def commit_source_cell(channel, cell, files, *, label='Source edit',generator_name='source-editor',persist=True):
    from state import channel_db_name
    from persistence import publish_files
    db = channel_db_name(channel)
    with cell_commit_lock(str(cell.id)):
        conn = get_db_connection(db)
        try:
            run_id = str(uuid.uuid4())
            conn.execute("INSERT INTO runs(id,prompt,status) VALUES (?,?,'completed')",(run_id,label))
            write_source_version(conn,cell,files,run_id,label=label,generator_name=generator_name)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
        publish_files(channel,str(cell.id),dict(operation_id=run_id,outcome='accepted',files=[],conflicts=[]),persist=persist)


def run_card(*, run_id: str, runner_id: str | None, runner_name: str | None,
             generator: str, label: str) -> dict:
    return {
        "v": 1,
        "run_id": run_id,
        "runner": {"id": runner_id, "name": runner_name},
        "generator": generator,
        "label": label,
        "folder": run_folder(label),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }


def write_run_start(channel: str, cell_id: str, card: dict,
                    placeholders: dict[str, str | bytes]) -> str:
    """Commit the run card, the run's first locked file, together with the
    starting placeholders of the files it opens, in one version ("Run
    started"); returns its id. A placeholder whose path the cell already
    holds is left as it is (an existing cell-scoped document keeps its
    content)."""
    from persistence import snapshot_files
    label = card["label"]
    with cell_commit_lock(cell_id):
        conn = get_db_connection(channel)
        try:
            present = {f["path"] for f in snapshot_files(conn, cell_id)}
        finally:
            conn.close()
        writes: dict = {path: body for path, body in placeholders.items() if path not in present}
        writes[run_card_path(label)] = json.dumps(card, indent=2, sort_keys=True) + "\n"
        version_id = commit_cell_change(
            channel, cell_id, card["run_id"], writes=writes,
            label="Run started", source_type="generator", generator_name=card["generator"],
            skip_unchanged=False,
        )
    assert version_id is not None  # skip_unchanged=False always commits a version
    return version_id


def _log_swept(conn, run_id: str, files: dict[str, str]) -> None:
    """Keep each swept file in the run node's Logs unless the run already
    logged that path itself."""
    from run_artifacts import record_artifact_blob
    have = {
        r["filepath"] for r in conn.execute(
            "SELECT filepath FROM run_artifacts WHERE run_id=? AND category='log'",
            (run_id,),
        ).fetchall()
    }
    exists = conn.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone()
    if not exists:
        return
    for path, blob_hash in files.items():
        if path in have or not blob_hash:
            continue
        record_artifact_blob(conn, run_id, "log", path, blob_hash)


def sweep_run_folder(channel: str, cell_id: str, run_id: str, label: str,
                     *, generator_name: str = "host") -> str | None:
    """Delete current ``_runs/<label>/`` files and record their history,
    keeping their accepted bytes in the run's Logs (the backstop
    for a run that never logged its own surfaces). Idempotent: nothing to
    sweep writes nothing."""
    folder = run_folder(label)
    prefix = folder + "/"

    def _in_tx(conn,before,_after):
        _log_swept(conn,run_id,{p:h for p,h in before.items() if normalize(p).startswith(prefix)})

    return commit_cell_change(
        channel, cell_id, run_id, delete_prefix=folder,
        label="Run ended", source_type="generator", generator_name=generator_name,
        in_transaction=_in_tx,
    )


def sweep_orphan_run_folders() -> int:
    """Startup crash recovery, after the state restore: every visible cell
    goes through :func:`sweep_dead_run_folders`, and the state is persisted
    once. Returns how many cells changed."""
    from state import channel_db_name, persist_state, state

    changed = 0
    for ch in list(state.channels):
        channel_uuid = channel_db_name(ch)
        cells = [c for stack in cell_placement.iter_stacks(ch) for c in stack.cells]
        # Most mounted cells have no run folder at all. Discover that once
        # per channel instead of opening/migrating/querying once per cell.
        try:
            conn = get_db_connection(channel_uuid, create=False)
        except FileNotFoundError:
            has_stored_folders = False
        else:
            try:
                has_stored_folders = conn.execute(
                    "SELECT 1 FROM cell_files WHERE filepath GLOB '_runs/*' LIMIT 1"
                ).fetchone() is not None
            finally:
                conn.close()
        for cell in cells:
            if not has_stored_folders and not any(is_run_path(f.name) for f in cell.files):
                continue
            cid = str(cell.id)
            try:
                if sweep_dead_run_folders(channel_uuid, cid):
                    changed += 1
            except Exception:
                logger.exception("startup sweep failed for cell %s", cid)
    if changed:
        persist_state()
    return changed


def sweep_dead_run_folders(channel: str,cell_id: str) -> bool:
    from persistence import snapshot_files
    from run_registry import live_labels
    live = live_labels(cell_id)
    with cell_commit_lock(cell_id):
        conn = get_db_connection(channel)
        try:
            files = snapshot_files(conn,cell_id)
            dead = {f['path']:f['body_hash'] for f in files if is_run_path(f['path']) and label_of(f['path']) not in live}
            if not dead:
                return False
            run_id = str(uuid.uuid4())
            conn.execute("INSERT INTO runs(id,prompt,status) VALUES (?,'run folder sweep','completed')",(run_id,))
            conn.commit()
        finally:
            conn.close()
        def logs(conn,before,after):
            for label in {label_of(path) for path in dead}:
                owner = _run_for_label(conn,label)
                if owner:
                    _log_swept(conn,owner,{p:h for p,h in dead.items() if label_of(p)==label})
        commit_cell_change(channel,cell_id,run_id,deletes=dead,label='Run folders swept',generator_name='host',in_transaction=logs)
    return True


def _run_for_label(conn, label: str | None) -> str | None:
    short = label.rsplit("-", 1)[-1] if label else ""
    if len(short) != 8:
        return None
    rows = conn.execute(
        "SELECT id FROM runs WHERE substr(id, 1, 8)=?", (short,),
    ).fetchall()
    return rows[0]["id"] if len(rows) == 1 else None


# ---------------------------------------------------------------------------
# Run in a new cell
# ---------------------------------------------------------------------------

def clone_cell_for_run(channel: str, cell_id: str) -> tuple[str, str]:
    """Clone ``cell_id``'s exact file state into a new standard stack right of
    its stack, for a run started with ``new_cell``. The clone uses accepted
    SQL file references without ``_runs/`` and keeps the source's name and
    its user pin when that file survives; its annotation contexts never
    cross. Source revisions remain unchanged. Returns
    ``(new_cell_id, new_stack_id)``."""
    from state import get_channel_by_id
    from cell_placement import insert_new_stack_right_of
    from models import Cell
    from state import persist_state, state_lock

    from persistence import snapshot_files,initialize_files
    with cell_commit_lock(cell_id):
        conn=get_db_connection(channel)
        try:
            accepted=strip_run_paths({f['path']:f for f in snapshot_files(conn,cell_id)})
            vfiles=versions.build_virtual_files(accepted.values())
            with state_lock:
                ch=get_channel_by_id(channel)
                stack,source=cell_placement.find_cell(ch,cell_id)
                if ch is None or stack is None or source is None:
                    raise LookupError('Source cell not found')
                clone=Cell(name=source.name,icon=source.icon,files=vfiles,pinned=source.pinned if source.pinned in accepted else None,
                           settings=source.settings,channel=source.channel)
            initialize_files(conn,str(clone.id),list(accepted.values()))
            conn.commit()
        finally:
            conn.close()
        with state_lock:
            new_stack=insert_new_stack_right_of(ch,stack.id)
            new_stack.cells.append(clone)
            new_cell_id,new_stack_id=str(clone.id),str(new_stack.id)
    persist_state()
    return new_cell_id, new_stack_id


def remove_stack(channel: str, stack_id: str) -> None:
    """Drop a stack a refused ``new_cell`` start created."""
    from state import get_channel_by_id, persist_state, state_lock
    with state_lock:
        ch = get_channel_by_id(channel)
        if ch is None:
            return
        ch.main_stacks = [s for s in ch.main_stacks if str(s.id) != str(stack_id)]
    persist_state()


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

def run_node_id(run_id: str, first_version_id: str | None) -> str:
    """The files pane History rail's id for a run node: ``run:<run id>:<first version
    id>`` — two runs committing to one cell interleave their versions, so a
    run can own several nodes and the run id alone is not unique. A run with
    no version yet is ``run:<run id>``."""
    return f"run:{run_id}:{first_version_id}" if first_version_id else f"run:{run_id}"


__all__ = [
    "commit_source_cell",
    "CellLock", "RUNS_PREFIX",
    "cell_commit_lock", "cell_commit_locks", "assert_cell_lock_held",
    "blob_bytes",
    "ensure_tip_row",
    "broadcast_cell_commit",
    "commit_cell_change", "run_card", "write_run_start", "sweep_run_folder",
    "sweep_orphan_run_folders",
     "clone_cell_for_run", "remove_stack", "run_node_id",
]
