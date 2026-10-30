# --- START FILE: api/database.py ---
import os
import sqlite3
import threading
from uuid import UUID
from pathlib import Path
from contextlib import contextmanager

import migrations as _migrations

# Track which database files have already had their schema initialised so
# we don't re-run DDL (which takes an EXCLUSIVE lock) on every connection.
# Keyed by absolute path so tests that chdir into different tmp dirs
# don't collide on the relative ``.collimate/channels/<x>.db`` form —
# every distinct DB file gets its own cache entry. Production behaviour
# is unchanged: a long-running server has one stable cwd.
_initialised_dbs: set[str] = set()
channel_files_lock = threading.RLock()
creating_channels: set[str] = set()
changing_channels: set[str] = set()


class DurabilityConfirmationError(OSError):
    """SQL committed, but its publication directory was not confirmed."""


def confirm_sql_durability(conn: sqlite3.Connection) -> None:
    """Check names after SQLite creates or recreates its database/WAL files.

    SQLite FULL synchronizes transaction bytes. Its Unix VFS may ignore a
    directory-sync error, so successful commit alone cannot confirm the names.
    Exact receipt replays call this too before returning a durable result.
    """
    from cas import fsync_directory, publication_directories
    path = next(row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main")
    if not path:
        return  # SQLite's explicit in-memory stores publish no filesystem names.
    try:
        for directory in publication_directories(str(Path(path).parent)):
            fsync_directory(directory)
    except OSError as exc:
        raise DurabilityConfirmationError(f"SQL durability confirmation failed for {path}: {exc}") from exc


class DurableConnection(sqlite3.Connection):
    """Both explicit and context-manager commits confirm directory entries."""

    def commit(self) -> None:
        super().commit()
        confirm_sql_durability(self)

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        if exc_type is not None:
            return super().__exit__(exc_type, exc_value, traceback)
        try:
            self.commit()
        except BaseException:
            # A confirmation error happens after commit; this cannot undo it.
            self.rollback()
            raise
        return False

def canonical_channel_id(value: str | UUID) -> str:
    """Validate a UUID identity without consulting the live channel catalog.

    Trusted creation and offline callers may pass a UUID object. Strings must
    already use the canonical lowercase, hyphenated representation.
    """
    if isinstance(value, UUID):
        return str(value)
    if not isinstance(value, str):
        raise ValueError("Channel identity must be a canonical UUID")
    try:
        identity = str(UUID(value))
    except ValueError as exc:
        raise ValueError("Channel identity must be a canonical UUID") from exc
    if value != identity:
        raise ValueError("Channel identity must be a canonical UUID")
    return identity


def get_global_db_connection() -> sqlite3.Connection:
    """Open the global (cross-channel) sqlite DB. Used for user-level
    preferences that apply to new-channel creation, i.e. anything that must
    be readable before any channel exists. Also owns the derived renderer bundle index."""
    os.makedirs(".collimate", exist_ok=True)
    db_path = ".collimate/app.db"
    cache_key = os.path.abspath(db_path)

    conn = sqlite3.connect(db_path, timeout=5.0, factory=DurableConnection)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = FULL;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        if cache_key not in _initialised_dbs:
            _migrations.upgrade_global_db(conn)
            _initialised_dbs.add(cache_key)
    except BaseException:
        conn.close()
        raise
    return conn


def read_global_json(key: str, default=None):
    """The JSON-decoded value of one global ``ui_state`` row, or ``default``
    when there is none."""
    import json
    conn = get_global_db_connection()
    try:
        row = conn.execute("SELECT value FROM ui_state WHERE key=?", (key,)).fetchone()
    finally:
        conn.close()
    return json.loads(row["value"]) if row and row["value"] is not None else default


def write_global_json(key: str, value) -> None:
    """Upsert one global ``ui_state`` row with the JSON encoding of ``value``;
    ``None`` deletes the row."""
    import json
    conn = get_global_db_connection()
    try:
        if value is None:
            conn.execute("DELETE FROM ui_state WHERE key=?", (key,))
        else:
            conn.execute("INSERT INTO ui_state (key, value) VALUES (?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))
        conn.commit()
    finally:
        conn.close()


def get_db_connection(channel_id: str | UUID, *, create: bool = True) -> sqlite3.Connection:
    """Open an explicitly identified channel database, independently of catalog
    admission. HTTP callers require a registered UUID before using this opener.

    ``create=False`` opens only a db file that is there: FileNotFoundError
    when there is none, or when it is renamed or unlinked during the open
    (``_connect_existing``). A rename or unlink after this returns leaves
    the connection on the file it opened (POSIX), so a write through it
    lands in that file."""
    channel_id = canonical_channel_id(channel_id)
    db_path = f".collimate/channels/{channel_id}.db"
    cache_key = os.path.abspath(db_path)

    if create:
        os.makedirs(".collimate/channels", exist_ok=True)
        conn = _connect(db_path)
    else:
        conn = _connect_existing(cache_key)

    try:
        if cache_key not in _initialised_dbs:
            # A fresh DB runs the single schema migration; an already-current DB
            # short-circuits on the applied-version lookup.
            _migrations.upgrade_channel_db(conn)
            _initialised_dbs.add(cache_key)
    except BaseException:
        conn.close()
        raise
    return conn


def _connect(target: str, *, uri: bool = False) -> sqlite3.Connection:
    """Connect to a channel db (a path, or a ``file:`` URI) with the channel
    pragmas; a pragma that fails closes the connection."""
    conn = sqlite3.connect(target, timeout=5.0, uri=uri, factory=DurableConnection)
    try:
        conn.row_factory = sqlite3.Row

        # Enforce High-Concurrency WAL mode
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = FULL;")
        conn.execute("PRAGMA busy_timeout = 5000;")
    except sqlite3.Error:
        conn.close()
        raise
    return conn


def _connect_existing(path: str) -> sqlite3.Connection:
    """``_connect`` to the db file at the absolute ``path`` without ever
    creating it (a ``mode=rw`` URI), or FileNotFoundError.

    The connect holds the file it found, but the journal pragma then opens
    the -wal and -shm beside the db by its name. So the name must still
    lead to that file once the pragmas ran: a rename or unlink in between
    (SQLite then fails the pragma) or another file put under the name
    refuses the open."""
    found = os.stat(path)

    def moved() -> bool:
        try:
            now = os.stat(path)
        except FileNotFoundError:
            return True
        return (now.st_dev, now.st_ino) != (found.st_dev, found.st_ino)

    try:
        conn = _connect(f"{Path(path).as_uri()}?mode=rw", uri=True)
    except sqlite3.Error:
        if moved():
            raise FileNotFoundError(f"{path} moved while it was opened") from None
        raise
    if moved():
        conn.close()
        raise FileNotFoundError(f"{path} moved while it was opened")
    return conn


def channel_db_holds_data(basename: str) -> bool:
    """Retain every application record; only a proven schema-only file is empty.

    Inspect the actual schema so new durable owners cannot silently become
    disposable. Migration bookkeeping and SQLite's internal tables are not
    application records. Any inspection failure preserves the file.
    """
    db_path = f".collimate/channels/{basename}.db"
    try:
        conn = sqlite3.connect(f"file:{os.path.abspath(db_path)}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error:
        return True   # unreadable: keep it, never guess it away
    try:
        conn.execute('BEGIN')
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        for (table,) in tables:
            if table.lower() == 'schema_migrations' or table.lower().startswith('sqlite_'):
                continue
            identifier = '"' + table.replace('"', '""') + '"'
            if conn.execute(f"SELECT 1 FROM {identifier} LIMIT 1").fetchone():
                return True
        return False
    except sqlite3.Error:
        return True
    finally:
        conn.close()


def set_aside_orphan_channel_db(basename: str) -> str:
    """Take a channel db file that NO registered channel owns out of the way,
    by its physical basename: a proven empty file is deleted, a file with data
    is moved to ``.collimate/channels/orphaned/<basename>-<stamp>.db`` where the
    user can still recover it. Returns what was done."""
    with channel_files_lock:
        return _park_or_remove_channel_db(basename)


def _park_or_remove_channel_db(basename: str) -> str:
    import shutil
    from datetime import datetime, timezone
    channels_dir = os.path.join(".collimate", "channels")
    db_path = os.path.join(channels_dir, f"{basename}.db")
    if channel_db_holds_data(basename):
        import uuid
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8]
        parked = os.path.join(channels_dir, "orphaned")
        os.makedirs(parked, exist_ok=True)
        for suffix in ("", "-wal", "-shm"):
            source = db_path + suffix
            if os.path.exists(source):
                shutil.move(source, os.path.join(parked, f"{basename}-{stamp}.db{suffix}"))
        outcome = f"moved the unowned channel database {basename}.db (it holds data) to channels/orphaned/{basename}-{stamp}.db"
    else:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db_path + suffix)
            except FileNotFoundError:
                pass
        outcome = f"removed the empty unowned channel database {basename}.db"
    _initialised_dbs.discard(os.path.abspath(db_path))
    from cas import fsync_directory
    fsync_directory(channels_dir)
    if os.path.isdir(os.path.join(channels_dir, 'orphaned')):
        fsync_directory(os.path.join(channels_dir, 'orphaned'))
    return outcome


def reconcile_orphan_channel_dbs(registered: set) -> list:
    """Reconcile explicit file basenames against registered UUID filenames."""
    import glob
    outcomes = []
    for db_path in sorted(glob.glob(".collimate/channels/*.db")):
        basename = os.path.splitext(os.path.basename(db_path))[0]
        if basename not in registered:
            outcomes.append(set_aside_orphan_channel_db(basename))
    return outcomes


def forget_db(channel_id: str | UUID) -> None:
    """Invalidate the schema cache after the UUID database is moved or deleted."""
    channel_id = canonical_channel_id(channel_id)
    db_path = f".collimate/channels/{channel_id}.db"
    _initialised_dbs.discard(os.path.abspath(db_path))


@contextmanager
def channel_transaction(channel_id, *, read_only=False):
    """Own a real connection; participants receive it explicitly."""
    connection = get_db_connection(channel_id)
    try:
        connection.execute("BEGIN" if read_only else "BEGIN IMMEDIATE")
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
# --- END FILE: api/database.py ---


def _legacy_channel_basename(name: str) -> str:
    """Interpret an old filename only inside the numbered identity migration."""
    return "".join(character for character in name.lower().replace(" ", "_")
                   if character.isalnum() or character in "_-")


def _validate_migration_database(path: Path) -> None:
    """Read an existing candidate without schema upgrades or file creation."""
    try:
        connection = sqlite3.connect(f"{path.absolute().as_uri()}?mode=ro", uri=True, timeout=5.0)
        try:
            result = connection.execute("PRAGMA quick_check").fetchall()
            if result != [("ok",)]:
                raise OSError(f"Channel identity migration found an invalid database: {path}")
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise OSError(f"Channel identity migration cannot read database: {path}") from exc


def migrate_channel_database_identities(channels, *, require_existing: bool = False) -> list[str]:
    """Preflight the whole layout, checkpoint every source, then move a prefix.

    The caller owns startup's data-directory exclusion. Completed target-only
    entries are retryable; no runtime opener interprets the old basename.
    """
    from cas import fsync_directory
    directory = Path(".collimate/channels")
    if directory.is_symlink() or directory.parent.is_symlink():
        raise OSError("Channel identity migration refuses symbolic links")
    if directory.exists() and not directory.is_dir():
        raise OSError("Channel identity migration requires a channels directory")
    plan = []
    identities = set()
    sources = {}
    targets = {}
    for channel in channels:
        identity = canonical_channel_id(channel.id)
        if identity in identities:
            raise OSError(f"Duplicate channel identity during migration: {identity}")
        identities.add(identity)
        target = directory / f"{identity}.db"
        legacy_basename = _legacy_channel_basename(channel.name)
        source = directory / f"{legacy_basename}.db" if legacy_basename else target
        targets[target] = identity
        for candidate in {source, target}:
            for suffix in ("", "-wal", "-shm"):
                if Path(str(candidate) + suffix).is_symlink():
                    raise OSError(f"Channel identity migration refuses symbolic links: {candidate}")
        if source != target and source.exists():
            if source in sources:
                raise OSError(f"Ambiguous legacy channel database: {source}")
            sources[source] = identity
        plan.append((source, target, identity))
    # Every collision is checked before inspecting/checkpointing any source.
    for source, target, identity in plan:
        if require_existing and not source.exists() and not target.exists():
            raise OSError(f"Missing channel database during identity migration: {identity}")
        if source != target and source.exists():
            if source in targets and targets[source] != identity:
                raise OSError(f"Legacy source collides with another channel's identity: {source}")
            if target.exists():
                raise OSError(f"Ambiguous channel databases for {identity}; preserve both for recovery")
        elif source != target and any(Path(str(source) + suffix).exists() for suffix in ("-wal", "-shm")):
            raise OSError(f"Unclassified legacy sidecars during identity migration: {source}")
    for source, target, _identity in plan:
        if source.exists():
            _validate_migration_database(source)
        if target != source and target.exists():
            _validate_migration_database(target)
    with channel_files_lock:
        movable = [(source, target) for source, target, _identity in plan
                   if source != target and source.exists()]
        for source, _target in movable:
            connection = sqlite3.connect(f"{source.absolute().as_uri()}?mode=rw", uri=True, timeout=5.0)
            try:
                result = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if result is None or result[0]:
                    raise OSError(f"Channel database is busy during identity migration: {source}")
            finally:
                connection.close()
        results = []
        for source, target in movable:
            if target.exists() or target.is_symlink():
                raise OSError(f"Channel identity target appeared during migration: {target}")
            # All source connections are closed and their WALs truncated.
            # Retiring sidecars before the rename leaves a retryable prefix.
            for suffix in ("-wal", "-shm"):
                Path(str(source) + suffix).unlink(missing_ok=True)
            os.rename(source, target)
            fsync_directory(str(directory))
            _initialised_dbs.discard(os.path.abspath(source))
            _initialised_dbs.discard(os.path.abspath(target))
            results.append(f"{source.name} -> {target.name}")
        for _source, target, _identity in plan:
            if target.exists():
                _validate_migration_database(target)
        return results
