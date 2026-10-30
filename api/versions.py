"""Immutable version groups referencing accepted file bodies.

Low-level writers use the caller transaction; standalone materializers commit
explicitly. Canvas publication remains owned by the caller's cell lock.
"""
import json
import posixpath
import uuid
from collections.abc import Iterable, Mapping

from cas import store_bytes
from database import get_db_connection

MAX_VERSION_CHAIN = 1_000_000

# The empty file that keeps an otherwise empty folder in a cell.
FOLDER_MARKER = ".collkeep"


class VersionChainTooLong(RuntimeError):
    """A ``previous_version`` chain longer than ``MAX_VERSION_CHAIN``."""


def find_tip(conn, start_cell_id: str) -> str:
    """Walk cell_edges forward via parent_key='previous_version' to the tip.

    A historical fork can have multiple forward edges. The newest edge
    wins, matching ``cell_files.walk_versions`` for history listing and
    checkpoint ancestry. Accepted current files are read from working refs;
    this history traversal does not select their bytes.

    Stable roots use a disposable SQL index maintained/invalidated by edge
    triggers. Uncached historical starts use the same recursive CTE: its
    UNION drops a repeated id, so a corrupt cycle
    ends the recursion. The tip is the one walked node with no outgoing
    ``previous_version`` edge. A cycle has none: one within the guard
    returns ``start_cell_id``. A chain (or cycle) longer than
    ``MAX_VERSION_CHAIN`` raises ``VersionChainTooLong``."""
    tip, walked = _tip_result(conn, start_cell_id)
    if tip is not None:
        return tip
    if walked >= MAX_VERSION_CHAIN:
        raise VersionChainTooLong(
            f"cell {start_cell_id}: version chain longer than {MAX_VERSION_CHAIN}"
        )
    return start_cell_id


def _tip_result(conn, start_cell_id: str):
    # The edge triggers keep this disposable root index current. An uncached
    # historical start always takes the canonical newest-edge walk.
    return conn.execute(
        """WITH RECURSIVE cached AS (
               SELECT tip_id, depth FROM cell_version_heads WHERE cell_id=? AND depth<=?),
           w(id) AS (
               SELECT ? WHERE NOT EXISTS (SELECT 1 FROM cached)
               UNION
               SELECT (SELECT child_cell_id FROM cell_edges
                       WHERE parent_cell_id = w.id
                         AND parent_key = 'previous_version'
                       ORDER BY rowid DESC LIMIT 1)
               FROM w WHERE w.id IS NOT NULL
               LIMIT ?)
           SELECT COALESCE((SELECT tip_id FROM cached), MAX(CASE WHEN id IS NOT NULL AND NOT EXISTS (
                      SELECT 1 FROM cell_edges e
                      WHERE e.parent_cell_id = w.id
                        AND e.parent_key = 'previous_version')
                  THEN id END)),
                  COALESCE((SELECT depth FROM cached), COUNT(*))
           FROM w""",
        (start_cell_id, MAX_VERSION_CHAIN, start_cell_id, MAX_VERSION_CHAIN),
    ).fetchone()


def synthesize_genesis_row(
    conn,
    cell_id: str,
    run_id: str,
    seq: int,
    current_files: list[dict] | None,
    changed_paths: set | None = None,
) -> bool:
    """Create a genesis ``cells`` row + content snapshot for a never-versioned
    cell, so FK references (previous_version edges, input_0 provenance edges,
    ``generator_cell_id`` stamps — foreign keys are enforced) have a real row
    to point at AND the cell's exact content at that moment is pinned in
    history. ``current_files`` contains explicit typed file descriptors or
    VirtualFiles; paths in ``changed_paths`` are excluded (they
    belong to the version being written on top, not the genesis). No-op if
    the row already exists — returns True only when a row was created."""
    if conn.execute("SELECT 1 FROM cells WHERE id=?", (cell_id,)).fetchone():
        return False
    conn.execute(
        "INSERT INTO cells (id, run_id, generator_name, sequence_num, "
        "version_name, source_type) "
        "VALUES (?, ?, 'genesis', ?, 'Initial', 'genesis')",
        (cell_id, run_id, seq),
    )
    from persistence import _entry
    from cas import require_blob
    skip = changed_paths or set()
    for entry in current_files or []:
        path, digest, language, encoding = _entry(entry)
        if path in skip:
            continue
        raw = require_blob(digest)
        conn.execute('INSERT OR IGNORE INTO blobs(hash,size) VALUES (?,?)', (digest,len(raw)))
        conn.execute('INSERT INTO cell_files(cell_id,filepath,blob_hash,encoding,language) VALUES (?,?,?,?,?)',
                     (cell_id,path,digest,encoding,language))
    initialize_file_snapshot(conn, cell_id)
    return True


def write_version_from_overlay(
    conn,
    cell_id: str,
    run_id: str,
    changes: list,
    *,
    version_name: str,
    source_type: str,
    generator_name: str,
    tip_id: str | None = None,
    current_files: list[dict] | None = None,
) -> str:
    """Commit an immutable history version from a normalized change list.

    ``changes`` is a list of ``(filepath, op, value)``:
      - ``("set", str | bytes)`` — upsert a file (hashed + stored in the CAS)
      - ``("set_hash", typed_file_descriptor)`` — upsert a file already present in the CAS
      - ``("delete", None)`` — drop the path from the new version

    Walks to the version tip, synthesizes a genesis row for a never-versioned
    cell (seeding it from ``current_files`` MINUS the changed paths), inserts
    the new ``cells`` row + ``previous_version`` edge, overlays the changes on
    the tip's files, writes blobs + ``cell_files`` and records a commit event.
    Returns the new version cell_id.

    ``tip_id`` — optional pre-resolved tip id (callers that already walked the
    chain atomically pass it; otherwise we walk here).

    The caller holds the cell's commit lock (cell_commits.cell_commit_lock)
    from its tip read through ``conn.commit()``."""
    from cell_commits import assert_cell_lock_held
    assert_cell_lock_held(cell_id)
    from persistence import snapshot_files, restore_snapshot, _entry
    from models import language_for
    if tip_id is None:
        tip_id = find_tip(conn, cell_id)
    baseline = snapshot_files(conn, cell_id)
    seq = conn.execute(
        "SELECT COALESCE(MAX(sequence_num), 0) + 1 FROM cells WHERE run_id=?", (run_id,),
    ).fetchone()[0]
    synthesize_genesis_row(conn, tip_id, run_id, seq, baseline)
    parent_files = tip_file_map(conn, tip_id)
    entries = {f['path']: f for f in baseline}
    for path, op, value in changes:
        if op == 'delete':
            entries.pop(path, None)
        elif op == 'set_hash':
            actual_path, digest, language, encoding = _entry(value)
            if actual_path != path:
                raise ValueError('Overlay descriptor path does not match mutation path')
            entries[path] = dict(path=path, body_hash=digest, language=language, encoding=encoding)
        elif op == 'set' and isinstance(value, (str, bytes)):
            raw = value.encode('utf-8') if isinstance(value, str) else value
            entries[path] = dict(path=path, body_hash=store_bytes(raw),
                                 language=entries.get(path, {}).get('language', language_for(path)),
                                 encoding='utf-8' if isinstance(value, str) else 'binary')
        else:
            raise ValueError('Invalid overlay mutation')
    typed = list(entries.values())
    restore_snapshot(conn, cell_id, typed, writer_id='host:' + generator_name)
    new_id, _event = insert_version(
        conn, tip_id=tip_id, parent_files=parent_files, run_id=run_id,
        files={p: f['body_hash'] for p, f in entries.items()}, file_entries=typed,
        version_name=version_name, source_type=source_type,
        generator_name=generator_name, owner_id=cell_id,
    )
    return new_id


def resolve_cell_version(conn, channel: str, cell_id: str, run_id: str, current_files=None) -> tuple[str, bool]:
    """Freeze accepted working refs into an immutable history group."""
    from persistence import snapshot_files
    entries = snapshot_files(conn, cell_id)
    files = {f['path']: f['body_hash'] for f in entries}
    tip = find_tip(conn, cell_id)
    seq = conn.execute('SELECT COALESCE(MAX(sequence_num),0)+1 FROM cells WHERE run_id=?',(run_id,)).fetchone()[0]
    synthesize_genesis_row(conn, tip, run_id, seq, entries)
    parent = tip_file_map(conn, tip)
    def descriptor(f):
        return f['path'], f['body_hash'], f['encoding'], f['language']
    if sorted(map(descriptor, version_file_entries(conn, tip))) == sorted(map(descriptor, entries)):
        return tip, False
    new_id, _event = insert_version(conn, tip_id=tip, parent_files=parent, run_id=run_id, files=files,
                                   file_entries=entries, version_name='Manual Edit', source_type='manual_edit',
                                   generator_name='manual_edit', owner_id=cell_id)
    return new_id, True


def freeze_cell_versions(
    channel: str, run_id: str, cell_ids: list[str],
) -> dict[str, str]:
    """Capture immutable accepted snapshots for provenance and workspace startup.

    Sorted cell locks serialize file mutation and baseline capture. Every
    resolved tip has a real history row, including empty logical cells.
    """
    if not cell_ids:
        return {}
    from cell_commits import cell_commit_locks

    resolved: dict[str, str] = {}
    promoted = False
    with cell_commit_locks(cell_ids):
        conn = get_db_connection(channel)
        try:
            for cell_id in cell_ids:
                effective, did_promote = resolve_cell_version(
                    conn, channel, cell_id, run_id,
                )
                promoted = promoted or did_promote
                resolved[cell_id] = effective
            conn.commit()
        finally:
            conn.close()
    if promoted:
        from state import persist_state
        persist_state()
    return resolved


def version_available(conn, version_id: str) -> bool:
    """Whether the frozen version row exists, including a zero-file version."""
    if not version_id:
        return False
    return bool(
        conn.execute("SELECT 1 FROM cells WHERE id=?", (version_id,)).fetchone()
    )


def tip_file_map(conn, version_id: str) -> dict[str, str]:
    """Exact immutable logical snapshot, never a later sibling's files.

    Current heads use the durable resolved index, including after restart.
    Cold historical versions replay their immutable deltas to a complete root.
    """
    # Match the version and read its resolved entries in ONE SQLite snapshot.
    # Two statements could observe a head before a concurrent append and its
    # now-advanced map afterwards, corrupting an immutable historical read.
    rows = conn.execute("""SELECT f.filepath,f.blob_hash FROM cell_file_heads h
        LEFT JOIN cell_file_current f ON f.root_id=h.root_id WHERE h.version_id=?""", (version_id,)).fetchall()
    if rows:
        return {path: digest for path, digest in rows if path is not None}
    files, seen = {}, set()
    current = version_id
    while current is not None:
        if current in seen or len(seen) >= MAX_VERSION_CHAIN:
            raise VersionChainTooLong(f"invalid file ancestry for {version_id}")
        seen.add(current)
        meta = conn.execute("SELECT parent_version_id FROM cell_file_versions WHERE version_id=?", (current,)).fetchone()
        if meta is None:
            if current == version_id and not version_available(conn, version_id):
                return {}
            raise ValueError(f"missing file metadata for {current}")
        for path, digest in conn.execute("SELECT filepath,blob_hash FROM cell_files WHERE cell_id=?", (current,)):
            files.setdefault(path, digest)
        current = meta[0]
    return {path: digest for path, digest in files.items() if digest is not None}



def version_file_hashes(conn, version_id: str, paths) -> dict[str, str]:
    """Exact selected immutable paths; current reads use the composite index.

    A LEFT JOIN head marker distinguishes a current snapshot with absent paths
    from an uncached historical version, in the same atomic SQL read. Cold
    ancestry visits only unresolved paths; tombstones resolve absence.
    """
    requested = list(dict.fromkeys(paths))
    result = {}
    for start in range(0, len(requested), 500):
        pending = set(requested[start:start + 500])
        placeholders = ','.join('?' * len(pending))
        rows = conn.execute(f"""SELECT h.version_id,f.filepath,f.blob_hash
            FROM cell_file_heads h LEFT JOIN cell_file_current f
              ON f.root_id=h.root_id AND f.filepath IN ({placeholders})
            WHERE h.version_id=?""", (*pending, version_id)).fetchall()
        if rows:
            result.update((path, digest) for _, path, digest in rows if path is not None)
            continue
        current, seen = version_id, set()
        while current is not None and pending:
            if current in seen or len(seen) >= MAX_VERSION_CHAIN:
                raise VersionChainTooLong(f"invalid file ancestry for {version_id}")
            seen.add(current)
            meta = conn.execute("SELECT parent_version_id FROM cell_file_versions WHERE version_id=?", (current,)).fetchone()
            if meta is None:
                if current == version_id and not version_available(conn, version_id):
                    break
                raise ValueError(f"missing file metadata for {current}")
            placeholders = ','.join('?' * len(pending))
            for path, digest in conn.execute(
                f"SELECT filepath,blob_hash FROM cell_files WHERE cell_id=? AND filepath IN ({placeholders})",
                (current, *pending),
            ):
                pending.remove(path)
                if digest is not None:
                    result[path] = digest
            current = meta[0]
    return result

def version_file_entries(conn, version_id: str) -> list[dict]:
    """Exact typed historical snapshot, from its head index or immutable deltas."""
    rows = conn.execute('SELECT f.filepath,f.blob_hash,f.encoding,f.language FROM cell_file_heads h '
                        'LEFT JOIN cell_file_current f ON f.root_id=h.root_id WHERE h.version_id=?', (version_id,)).fetchall()
    if rows:
        entries = {path:(digest,encoding,language) for path,digest,encoding,language in rows if path is not None}
    else:
        entries, seen, current = {}, set(), version_id
        while current is not None:
            if current in seen or len(seen) >= MAX_VERSION_CHAIN:
                raise VersionChainTooLong(f'invalid file ancestry for {version_id}')
            seen.add(current)
            meta = conn.execute('SELECT parent_version_id FROM cell_file_versions WHERE version_id=?',(current,)).fetchone()
            if meta is None:
                if current == version_id and not version_available(conn,version_id):
                    return []
                raise ValueError(f'missing file metadata for {current}')
            for path,digest,encoding,language in conn.execute('SELECT filepath,blob_hash,encoding,language FROM cell_files WHERE cell_id=?',(current,)):
                entries.setdefault(path,(digest,encoding,language))
            current = meta[0]
    out = []
    for path,(digest,encoding,language) in sorted(entries.items()):
        if digest is None:
            continue
        if encoding not in ('utf-8','binary') or not isinstance(language,str):
            raise ValueError(f'missing historical file type for {version_id}/{path}')
        out.append(dict(path=path,body_hash=digest,encoding=encoding,language=language))
    return out


def version_file_rows(conn, version_id: str) -> list[dict]:
    return [dict(filepath=f['path'],blob_hash=f['body_hash'],encoding=f['encoding'],language=f['language'])
            for f in version_file_entries(conn,version_id)]


def version_file_counts(conn, version_ids: list[str]) -> dict[str, int]:
    counts = {}
    for start in range(0, len(version_ids), 500):
        chunk = version_ids[start:start+500]
        counts.update(conn.execute(
            f"SELECT version_id,file_count FROM cell_file_versions WHERE version_id IN ({','.join('?' * len(chunk))})", chunk))
    return counts


def iter_version_files(conn):
    """Traverse the physical forest once, retaining one resolved map per root.

    History/GC already visit retained versions. Replaying each one's complete
    ancestor chain separately would make a long run quadratic. The reversible
    overlay stack shares ancestors without retaining every full historical map.
    """
    metadata = {r['version_id']: r for r in conn.execute(
        "SELECT v.version_id,v.parent_version_id FROM cells c LEFT JOIN cell_file_versions v ON c.id=v.version_id")}
    if None in metadata:
        raise ValueError("cell missing immutable file metadata")
    children, entries = {}, {}
    for version, row in metadata.items():
        parent = row['parent_version_id']
        if parent is not None and parent not in metadata:
            raise ValueError(f"missing file ancestor {parent}")
        children.setdefault(parent, []).append(version)
    for version, path, digest in conn.execute("SELECT cell_id,filepath,blob_hash FROM cell_files"):
        entries.setdefault(version, {})[path] = digest
    visited = set()
    for root in children.get(None, ()):
        files = {}
        stack: list[tuple[str, dict[str, str | None] | None]] = [(root, None)]
        while stack:
            version, undo = stack.pop()
            if undo is not None:
                for path, digest in undo.items():
                    if digest is None:
                        files.pop(path, None)
                    else:
                        files[path] = digest
                continue
            if version in visited:
                raise VersionChainTooLong(f"cyclic file ancestry at {version}")
            visited.add(version)
            delta = entries.get(version, {})
            undo = {path: files.get(path) for path in delta}
            for path, digest in delta.items():
                if digest is None:
                    files.pop(path, None)
                else:
                    files[path] = digest
            for path, digest in sorted(files.items()):
                yield {'cell_id': version, 'filepath': path, 'blob_hash': digest}
            stack.append((version, undo))
            stack.extend((child, None) for child in reversed(children.get(version, ())))
    if len(visited) != len(metadata):
        raise VersionChainTooLong("file ancestry has no complete root")


def initialize_file_snapshot(conn, version_id: str) -> None:
    """Finish an independent full snapshot in the caller's transaction."""
    conn.execute("UPDATE cell_file_versions SET file_count=(SELECT COUNT(*) FROM cell_files WHERE cell_id=?) WHERE version_id=?", (version_id, version_id))
    conn.execute("INSERT INTO cell_file_heads(root_id,version_id) VALUES (?,?) ON CONFLICT(root_id) DO UPDATE SET version_id=excluded.version_id", (version_id, version_id))
    conn.execute("DELETE FROM cell_file_current WHERE root_id=?", (version_id,))
    conn.execute("INSERT INTO cell_file_current(root_id,filepath,blob_hash,encoding,language) SELECT cell_id,filepath,blob_hash,encoding,language FROM cell_files WHERE cell_id=? AND blob_hash IS NOT NULL", (version_id,))
    if not conn.execute("SELECT 1 FROM cell_edges WHERE child_cell_id=? AND parent_key='previous_version'", (version_id,)).fetchone():
        tip, depth = _tip_result(conn, version_id)
        if tip is not None:
            conn.execute("INSERT OR IGNORE INTO cell_version_heads(cell_id,tip_id,depth) VALUES (?,?,?)", (version_id, tip, depth))

def build_virtual_files(entries) -> list:
    """Materialize explicit typed descriptors; missing/corrupt bytes are errors."""
    from models import VirtualFile
    from cas import require_blob
    out = []
    for entry in entries:
        path, digest, encoding, language = (entry[k] for k in ('path','body_hash','encoding','language'))
        if encoding not in ('utf-8','binary'):
            raise ValueError('Invalid historical encoding')
        raw = require_blob(digest)
        file = VirtualFile(name=path,content=raw.decode('utf-8') if encoding=='utf-8' else '',
                           blob_hash=digest if encoding=='binary' else None,language=language,encoding=encoding)
        if encoding=='utf-8':
            file._content_hash_cache = (file.content,digest)
        out.append(file)
    return out


def _dirs(files: Iterable[str]) -> set[str]:
    """Every proper folder prefix of the non-run paths in ``files``."""
    from run_folders import is_run_path
    out: set[str] = set()
    for path in files:
        if is_run_path(path):
            continue
        parent = posixpath.dirname(path)
        while parent:
            out.add(parent)
            parent = posixpath.dirname(parent)
    return out


def file_changes(before: Mapping[str, str], after: Mapping[str, str], paths: Iterable[str]) -> list[dict]:
    """The touches of one accepted run write, ``[{path, op, folder}]``:
    folders the write created (shallow first), the named ``paths`` it
    created, edited or deleted, then the folders it emptied (deep first).
    ``before``/``after`` are the accepted ``{path: blob_hash}`` maps around
    the write. ``_runs/`` paths are dropped, and a folder marker is never a
    file touch: a folder comes and goes with the set of folder prefixes."""
    from run_folders import is_run_path
    touched: list[dict] = []
    for path in sorted(set(paths)):
        if is_run_path(path) or posixpath.basename(path) == FOLDER_MARKER:
            continue
        was, now = before.get(path), after.get(path)
        if was is None and now is not None:
            touched.append({"path": path, "op": "create", "folder": False})
        elif was is not None and now is None:
            touched.append({"path": path, "op": "delete", "folder": False})
        elif was is not None and was != now:
            touched.append({"path": path, "op": "edit", "folder": False})
    old_dirs, new_dirs = _dirs(before), _dirs(after)
    created = sorted(new_dirs - old_dirs, key=lambda d: (d.count("/"), d))
    removed = sorted(old_dirs - new_dirs, key=lambda d: (-d.count("/"), d))
    return ([{"path": d, "op": "create", "folder": True} for d in created]
            + touched
            + [{"path": d, "op": "delete", "folder": True} for d in removed])


def insert_version(
    conn,
    *,
    tip_id: str,
    parent_files: dict[str, str],
    run_id: str,
    files: dict[str, str],
    file_entries,
    version_name: str,
    source_type: str,
    generator_name: str,
    generator_cell_id: str | None = None,
    owner_id: str | None = None,
    changes: list[dict] | None = None,
) -> tuple[str, dict]:
    """Insert a version row on top of ``tip_id`` holding exactly ``files``
    (``{path: blob_hash}``), its ``previous_version`` edge, the file rows,
    and a run_events commit whose payload carries ``changes``, the run's
    own file touches (``file_changes``; empty for every host commit).
    Returns ``(version_id, commit_event_row)``; the caller commits."""
    from collimate.fs import relative_parts
    from reserved_paths import is_reserved_path
    from cas import is_cas_hash
    entries = {}
    for f in file_entries:
        path = f['path']
        if '/'.join(relative_parts(path)) != path or is_reserved_path(path):
            raise ValueError('Invalid historical file path')
        if path in entries or not is_cas_hash(f['body_hash']):
            raise ValueError('Invalid or duplicate historical file descriptor')
        entries[path] = dict(f)
    if {p:f['body_hash'] for p,f in entries.items()} != files:
        raise ValueError('Typed snapshot does not match history file hashes')
    if any(f.get('encoding') not in ('utf-8','binary') or not isinstance(f.get('language'),str) for f in entries.values()):
        raise ValueError('History entries require explicit encoding and language')
    parent_entries = {f['path']:f for f in version_file_entries(conn,tip_id)}
    new_id = str(uuid.uuid4())
    seq = conn.execute(
        "SELECT COALESCE(MAX(sequence_num), 0) FROM cells WHERE run_id=?", (run_id,),
    ).fetchone()[0] + 1
    conn.execute(
        "INSERT INTO cells (id, run_id, generator_name, generator_cell_id, sequence_num, "
        "version_name, source_type) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (new_id, run_id, generator_name, generator_cell_id, seq, version_name, source_type),
    )
    conn.execute(
        "INSERT OR IGNORE INTO cell_edges (parent_cell_id, child_cell_id, parent_key) "
        "VALUES (?, ?, 'previous_version')",
        (tip_id, new_id),
    )
    delta: dict[str, str | None] = {path: digest for path, digest in files.items()
                                  if parent_files.get(path) != digest or any(parent_entries.get(path,{}).get(k)!=entries[path][k] for k in ('encoding','language'))}
    delta.update((path, None) for path in parent_files.keys() - files.keys())
    meta = conn.execute("SELECT root_id FROM cell_file_versions WHERE version_id=?", (tip_id,)).fetchone()
    if meta is None:
        raise ValueError(f"missing parent snapshot {tip_id}")
    root = meta[0]
    head = conn.execute("SELECT version_id FROM cell_file_heads WHERE root_id=?", (root,)).fetchone()
    if head is None or head[0] != tip_id:
        # A genuine historical branch initializes its own current-map index.
        # This is branch bootstrap, never a periodic streaming checkpoint.
        root = new_id
        conn.executemany("INSERT INTO cell_file_current(root_id,filepath,blob_hash,encoding,language) VALUES (?,?,?,?,?)",
                         [(root,p,f['body_hash'],f['encoding'],f['language']) for p,f in parent_entries.items()])
    conn.execute("UPDATE cell_file_versions SET parent_version_id=?,root_id=?,file_count=? WHERE version_id=?",
                 (tip_id, root, len(files), new_id))
    hashes = list({digest for digest in delta.values() if digest is not None})
    known: set[str] = set()
    for i in range(0, len(hashes), 500):
        chunk = hashes[i:i + 500]
        known.update(r[0] for r in conn.execute(
            f"SELECT hash FROM blobs WHERE hash IN ({','.join('?' * len(chunk))})", chunk,
        ))
    blob_rows = []
    for blob_hash in hashes:
        from cas import require_blob
        size = len(require_blob(blob_hash))
        if blob_hash not in known:
            blob_rows.append((blob_hash, size))
    conn.executemany("INSERT OR IGNORE INTO blobs (hash, size) VALUES (?, ?)", blob_rows)
    conn.executemany(
        "INSERT OR REPLACE INTO cell_files (cell_id, filepath, blob_hash,encoding,language) VALUES (?, ?, ?,?,?)",
        [(new_id,path,digest,entries[path]['encoding'] if digest else None,entries[path]['language'] if digest else None) for path,digest in delta.items()],
    )
    conn.executemany("INSERT INTO cell_file_current(root_id,filepath,blob_hash,encoding,language) VALUES (?,?,?,?,?) ON CONFLICT(root_id,filepath) DO UPDATE SET blob_hash=excluded.blob_hash,encoding=excluded.encoding,language=excluded.language",
                     [(root,p,h,entries[p]['encoding'],entries[p]['language']) for p,h in delta.items() if h is not None])
    conn.executemany("DELETE FROM cell_file_current WHERE root_id=? AND filepath=?",
                     [(root, p) for p, h in delta.items() if h is None])
    conn.execute("INSERT INTO cell_file_heads(root_id,version_id) VALUES (?,?) ON CONFLICT(root_id) DO UPDATE SET version_id=excluded.version_id", (root, new_id))
    if owner_id is not None and not conn.execute(
        "SELECT 1 FROM cell_version_heads WHERE cell_id=? UNION ALL "
        "SELECT 1 FROM cell_edges WHERE child_cell_id=? AND parent_key='previous_version' LIMIT 1",
        (owner_id, owner_id),
    ).fetchone():
        tip, depth = _tip_result(conn, owner_id)
        if tip is not None:
            conn.execute("INSERT INTO cell_version_heads(cell_id,tip_id,depth) VALUES (?,?,?)", (owner_id, tip, depth))
    payload = json.dumps({
        "cell_id": new_id, "name": version_name, "sequence_num": seq,
        "file_count": len(files), "changes": changes or [],
    })
    cur = conn.execute(
        "INSERT INTO run_events (run_id, event_type, payload) VALUES (?, 'commit', ?)",
        (run_id, payload),
    )
    created = conn.execute(
        "SELECT created_at FROM run_events WHERE id=?", (cur.lastrowid,),
    ).fetchone()[0]
    return new_id, {"id": cur.lastrowid, "event_type": "commit", "payload": payload,
                    "created_at": created}
