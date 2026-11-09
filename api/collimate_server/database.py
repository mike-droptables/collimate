# --- START FILE: api/collimate_server/database.py ---
import os
import sqlite3
import threading
from uuid import UUID
from pathlib import Path
from contextlib import contextmanager

import collimate_server.migrations as _migrations

# Track which database files have already had their schema initialised so
# we don't re-run DDL (which takes an EXCLUSIVE lock) on every connection.
# Keyed by absolute path: every distinct DB file (a channel folder's
# channel.db, a test's fresh data dir) gets its own cache entry.
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
    from collimate_server.cas import fsync_directory, publication_directories
    from collimate_server.channel_paths import boundary_of
    path = next(row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main")
    if not path:
        return  # SQLite's explicit in-memory stores publish no filesystem names.
    try:
        for directory in publication_directories(str(Path(path).parent), boundary_of(path)):
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


class ChannelConnection(DurableConnection):
    """A connection to one channel's ``channel.db``; ``channel_id`` names
    the channel, so a caller holding it reaches the channel's body store
    (``cas.channel_store_of``)."""

    channel_id: str


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


def get_db_connection(channel_id: str | UUID) -> ChannelConnection:
    """Open an explicitly identified channel's ``channel.db`` in its folder
    (``channel_paths.db_path``), independently of catalog admission. HTTP
    callers require a registered UUID before using this opener.

    Only a channel being created (``channel_paths.begin_create``) gets its
    database made here. Any other channel's must be there: a channel with no
    available folder raises ``channel_paths.ChannelUnavailable``, and one
    whose ``channel.db`` is gone FileNotFoundError, as does a file renamed
    or unlinked during the open (``_connect_existing``). A rename or unlink
    after this returns leaves the connection on the file it opened (POSIX),
    so a write through it lands in that file."""
    import collimate_server.channel_paths as channel_paths
    channel_id = canonical_channel_id(channel_id)
    db_path = channel_paths.db_path(channel_id)
    cache_key = str(db_path)
    home = channel_paths.home(channel_id)
    if home is not None and home.state == "creating":
        conn = _connect(cache_key)
    else:
        conn = _connect_existing(cache_key)
    conn.channel_id = channel_id
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


def _connect(target: str, *, uri: bool = False) -> ChannelConnection:
    """Connect to a channel db (a path, or a ``file:`` URI) with the channel
    pragmas; a pragma that fails closes the connection."""
    conn = sqlite3.connect(target, timeout=5.0, uri=uri, factory=ChannelConnection)
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


def _connect_existing(path: str) -> ChannelConnection:
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


def forget_db(channel_id: str | UUID) -> None:
    """Invalidate the schema cache of the channel's database before its
    folder is renamed, moved or deleted."""
    channel_id = canonical_channel_id(channel_id)
    for key in [key for key in _initialised_dbs
                if Path(key).parent.name in (channel_id, channel_id + ".moving")]:
        _initialised_dbs.discard(key)


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
