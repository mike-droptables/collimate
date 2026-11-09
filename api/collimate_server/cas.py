"""
Body stores: content-addressed storage of file bodies.

Every channel has its own store, ``<channel folder>/objects/``
(:func:`channel_store`), and the installation has one more,
``.collimate/app-objects/`` (:func:`app_store`), for the bodies only
``app.db``'s own tables name: renderer bundles, the module mirror and
accepted handler builds. No channel row may name an app-store body, and no
body crosses between stores except through :func:`copy_bodies`, which
copies and verifies it.

Verified body admission for accepted file revisions, immutable history and
run artifacts. Publication and collection of one store share that store's
publication lock; pins are kept per store root.
"""

from __future__ import annotations

import codecs
import hashlib
import logging
import os
import shutil
import stat
import threading
import time
import uuid
from collections import Counter
from collections.abc import Iterable
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

# The app store, cwd-relative (the data dir is the process CWD by the
# cli_main contract).
APP_OBJECTS_DIR = os.path.join(".collimate", "app-objects")
# The staging store: the zips the new-channel page expands before its
# channel exists. Nothing names its bodies; the channel's creation copies
# the ones it takes (copy_bodies) and collection sweeps the rest after the
# publication floor.
STAGED_OBJECTS_DIR = os.path.join(".collimate", "staged-objects")

# The cap of in-app writes: generator output, the buffered blob upload and
# other bodies a handler holds in memory. File import streams instead.
MAX_BLOB_BYTES = 50 * 1024 * 1024

# The per-file cap of streamed imports (``store_stream`` callers).
IMPORT_MAX_FILE_BYTES = 4 * 1024 ** 3

# Imported text above this size is stored as binary: the layout cache keeps
# every UTF-8 body decoded in server memory.
TEXT_IMPORT_MAX_BYTES = 5 * 1024 * 1024

# How much of a body the NUL sniff reads.
_SNIFF_BYTES = 8 * 1024
_CHUNK_BYTES = 64 * 1024

# A private upload file not written to for this long was left by a process
# that died mid-upload; the next upload's sweep removes it.
STALE_UPLOAD_SECONDS = 24 * 3600

# One publication lock per store root: a sweep's age check and move cannot
# interleave with a store/dedupe touch of the same store.
_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()
# Pins keyed by (store root, digest), guarded by that store's lock.
_pins: Counter = Counter()


class CorruptBlobError(OSError):
    """The admitted path exists but its bytes do not match its CAS digest."""


class BlobTooLarge(ValueError):
    """A streamed body passed its caller's size cap; nothing was published."""

    def __init__(self, limit: int):
        super().__init__(f"Body exceeds {limit} bytes")
        self.limit = limit


def is_cas_hash(value: object) -> bool:
    """True for a well-formed CAS key (SHA-256 hex digest).

    Every value used to build a CAS path must pass this first: the path is
    ``objects/<h[:2]>/<h[2:]>``, and os.path.join drops everything before an
    absolute final component — so an unvalidated hash is an arbitrary-file
    read, not merely a miss.
    """
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value)
    )


def _lock_for(root: str) -> threading.RLock:
    with _locks_guard:
        lock = _locks.get(root)
        if lock is None:
            lock = _locks[root] = threading.RLock()
        return lock


def _verify_path(path: str, digest: str) -> None:
    sha256 = hashlib.sha256()
    with open(path, "rb") as source:
        while chunk := source.read(64 * 1024):
            sha256.update(chunk)
    if sha256.hexdigest() != digest:
        raise CorruptBlobError(f"CAS integrity failure for {digest}")


class BodyStore:
    """One content-addressed store at ``root`` (``<root>/<h[:2]>/<h[2:]>``),
    with its streamed uploads in ``<root>/.incoming`` and its collected
    bodies in ``<root>.trash``. ``boundary`` is the directory up to which a
    publication confirms its ancestors' names: the channel's location for a
    channel store, the data dir for the app store."""

    __slots__ = ("root", "boundary", "incoming", "trash", "publication_lock")

    def __init__(self, root: str | os.PathLike, *, boundary: str | os.PathLike):
        self.root = os.path.abspath(root)
        self.boundary = os.path.abspath(boundary)
        self.incoming = os.path.join(self.root, ".incoming")
        self.trash = self.root + ".trash"
        self.publication_lock = _lock_for(self.root)

    def __repr__(self) -> str:
        return f"BodyStore({self.root!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, BodyStore) and other.root == self.root

    def __hash__(self) -> int:
        return hash(self.root)

    # --- reading -----------------------------------------------------------------------------

    def blob_path(self, blob_hash: str) -> str:
        """Where this store keeps ``blob_hash``'s bytes."""
        if not is_cas_hash(blob_hash):
            raise ValueError("Invalid CAS digest")
        return os.path.join(self.root, blob_hash[:2], blob_hash[2:])

    def has(self, blob_hash: str) -> bool:
        """Whether this store holds a file for ``blob_hash`` (unverified)."""
        return os.path.isfile(self.blob_path(blob_hash))

    def verify_blob(self, blob_hash: str) -> None:
        """Check required CAS bytes without materializing the body."""
        _verify_path(self.blob_path(blob_hash), blob_hash)

    def require_blob(self, blob_hash: str) -> bytes:
        """Read required immutable bytes, refusing missing or corrupt content."""
        path = self.blob_path(blob_hash)
        with open(path, "rb") as source:
            raw = source.read()
        if hashlib.sha256(raw).hexdigest() != blob_hash:
            raise CorruptBlobError(f"CAS integrity failure for {blob_hash}")
        return raw

    def read_blob(self, blob_hash: str | None) -> bytes | None:
        """Raw CAS bytes for ``blob_hash`` (binary-safe, never decoded), or None
        if the hash is empty or malformed or the object is missing."""
        if blob_hash is None or not is_cas_hash(blob_hash):
            return None
        try:
            return self.require_blob(blob_hash)
        except FileNotFoundError:
            return None

    def blob_is_utf8(self, blob_hash: str) -> bool:
        """Whether a stored body decodes as UTF-8, read in bounded chunks."""
        decoder = codecs.getincrementaldecoder("utf-8")()
        try:
            with open(self.blob_path(blob_hash), "rb") as body:
                while chunk := body.read(_CHUNK_BYTES):
                    decoder.decode(chunk)
            decoder.decode(b"", final=True)
        except UnicodeDecodeError:
            return False
        return True

    # --- pins --------------------------------------------------------------------------------

    def pinned_hashes(self) -> set[str]:
        with self.publication_lock:
            return {digest for (root, digest), count in _pins.items() if root == self.root and count}

    def holds_pins(self) -> bool:
        """Whether a read or export holds any pin in this store (a channel
        delete refuses while one does)."""
        return bool(self.pinned_hashes())

    @contextmanager
    def pin_blobs(self, hashes, *, verify: bool = True):
        """Keep a materializing read/export alive across collection.

        A caller freezing SQL references under cell locks may pin first and
        verify the pinned bytes after releasing those locks. Every ordinary
        caller gets the integrity preflight here.
        """
        hashes = set(hashes)
        with self.publication_lock:
            if verify:
                for digest in hashes:
                    _verify_path(self.blob_path(digest), digest)
            else:
                for digest in hashes:
                    self.blob_path(digest)
            for digest in hashes:
                _pins[(self.root, digest)] += 1
        try:
            yield
        finally:
            with self.publication_lock:
                for digest in hashes:
                    key = (self.root, digest)
                    _pins[key] -= 1
                    if not _pins[key]:
                        del _pins[key]

    def retain_blob(self, blob_hash: str) -> None:
        """Admit an existing binary reference before publishing it in layout/SQL.

        Refresh its publication age under the sweep lock, or refuse if collection
        already removed it. Call on new external references, not unchanged files.
        """
        with self.publication_lock:
            _verify_path(self.blob_path(blob_hash), blob_hash)
            os.utime(self.blob_path(blob_hash), None)

    # --- writing -----------------------------------------------------------------------------

    def hash_and_store_file(self, abs_path: str) -> str:
        """Publish a required file or raise on size, read or storage failure."""
        found = os.stat(abs_path)
        if found.st_size > MAX_BLOB_BYTES:
            raise ValueError(f"CAS file exceeds {MAX_BLOB_BYTES} bytes")
        sha256 = hashlib.sha256()
        with open(abs_path, "rb") as f:
            while chunk := f.read(65536):
                sha256.update(chunk)
        blob_hash = sha256.hexdigest()
        self._store_blob(blob_hash, lambda dst: shutil.copy2(abs_path, dst))
        return blob_hash

    def hash_and_store_bytes(self, raw: bytes) -> str:
        """Publish required capped bytes, raising before any reference is accepted."""
        if len(raw) > MAX_BLOB_BYTES:
            raise ValueError(f"CAS body exceeds {MAX_BLOB_BYTES} bytes")
        return self.store_bytes(raw)

    def store_bytes(self, raw: bytes) -> str:
        """Store bytes without a size cap; return their hash or raise OSError.

        Imports and seeds use this policy. Ingresses with a size limit apply it
        before storage; ``hash_and_store_bytes`` is the capped, optional writer.
        """
        blob_hash = hashlib.sha256(raw).hexdigest()
        self._store_blob(blob_hash, lambda dst: _write_bytes(dst, raw))
        return blob_hash

    def store_stream(self, chunks: Iterable[bytes], *, max_bytes: int | None = None) -> tuple[str, int]:
        """Store a body read chunk by chunk; return ``(hash, size)``.

        The bytes go to a private file under the store's ``.incoming`` while
        their digest is computed, never into one in-memory buffer, then publish
        atomically under the digest. Passing ``max_bytes`` raises
        :class:`BlobTooLarge`; an error from ``chunks`` propagates. Either way
        the private file is removed and nothing is published.
        """
        os.makedirs(self.incoming, exist_ok=True)
        part = os.path.join(self.incoming, f"{uuid.uuid4().hex}.part")
        sha256 = hashlib.sha256()
        size = 0
        try:
            with open(part, "wb") as out:
                for chunk in chunks:
                    size += len(chunk)
                    if max_bytes is not None and size > max_bytes:
                        raise BlobTooLarge(max_bytes)
                    sha256.update(chunk)
                    out.write(chunk)
                out.flush()
                # Flushed outside the publication lock, so the publisher's own
                # fsync of the moved file finds nothing left to write.
                os.fsync(out.fileno())
            blob_hash = sha256.hexdigest()
            self._store_blob(blob_hash, lambda dst: os.replace(part, dst), verify_written=False)
            return blob_hash, size
        finally:
            try:
                os.unlink(part)
            except FileNotFoundError:
                pass

    def remove_stale_uploads(self) -> int:
        """The stale ``.part`` files of this store's streamed uploads."""
        return remove_stale_uploads(self.incoming, ".part")

    def write_blob_atomic(self, blob_hash: str, write_fn, *, expected_hash: str | None = None) -> None:
        """Write ``blob_hash``'s object through a temp file only this writer
        uses, then rename it into place (:func:`_write_atomic`)."""
        _write_atomic(self.blob_path(blob_hash), write_fn, self.boundary, expected_hash=expected_hash)

    def _store_blob(self, blob_hash: str, write_fn, *, verify_written: bool = True) -> None:
        """Verify and durably publish bytes before callers publish SQL references.

        ``verify_written=False`` is for a writer that hashed the exact bytes it
        hands over (``store_stream``), so a large body is not read twice under
        the publication lock.
        """
        with self.publication_lock:
            cas_path = self.blob_path(blob_hash)
            try:
                if os.path.exists(cas_path):
                    _verify_path(cas_path, blob_hash)
                    sync_cas_file(cas_path)
                    for directory in publication_directories(os.path.dirname(cas_path), self.boundary):
                        sync_cas_directory(directory)
                    os.utime(cas_path, None)
                    return
                _write_atomic(cas_path, write_fn, self.boundary,
                              expected_hash=blob_hash if verify_written else None)
                # copy2 preserves source times; GC needs this publication's age.
                os.utime(cas_path, None)
            except OSError as exc:
                logger.error("CAS write failed for blob %s: %s", blob_hash, exc)
                raise OSError(f"CAS write failed for blob {blob_hash}: {exc}") from exc


def channel_store(channel_id) -> BodyStore:
    """The body store of the channel: its folder's ``objects/``. Raises
    :class:`channel_paths.ChannelUnavailable` for a channel with no
    available folder."""
    import collimate_server.channel_paths as channel_paths
    folder = channel_paths.channel_dir(channel_id)
    return BodyStore(folder / "objects", boundary=folder.parent)


def channel_store_of(conn) -> BodyStore:
    """The body store of the channel whose ``channel.db`` ``conn`` is open
    on (``database.get_db_connection``)."""
    channel_id = getattr(conn, "channel_id", None)
    if channel_id is None:
        raise TypeError("not a channel database connection")
    return channel_store(channel_id)


def app_store() -> BodyStore:
    """The installation's body store, ``.collimate/app-objects/``: renderer
    bundles, the module mirror and accepted handler builds, the bodies only
    ``app.db`` names."""
    return BodyStore(APP_OBJECTS_DIR, boundary=os.getcwd())


def staging_store() -> BodyStore:
    """``.collimate/staged-objects/``: uploads for a channel that does not
    exist yet (the new-channel page's zips), copied into the channel when it
    is created."""
    return BodyStore(STAGED_OBJECTS_DIR, boundary=os.getcwd())


def copy_bodies(src: BodyStore, dst: BodyStore, hashes: Iterable[str]) -> None:
    """Copy each body of ``hashes`` from ``src`` into ``dst`` and verify it:
    the one crossing between stores by hash (Config assembly from a source
    channel's or the staging store's bodies; the other crossings, copy to
    channel, split and merge, catalog add, definition updates and a handler
    build materialized into a cell, carry the bytes and store them anew).
    Never a hardlink: the two stores are collected independently. A body
    ``dst`` already holds is touched like any dedupe hit. The source bodies
    are pinned while they are read, so its collection cannot race the copy."""
    wanted = sorted({digest for digest in hashes if digest is not None})
    if not wanted or src == dst:
        return
    with src.pin_blobs(wanted, verify=False):
        for digest in wanted:
            source = src.blob_path(digest)
            dst._store_blob(digest, lambda target, source=source: shutil.copyfile(source, target))


def remove_stale_uploads(directory: str, suffix: str) -> int:
    """Unlink the ``*suffix`` files directly in ``directory`` whose last write
    is older than :data:`STALE_UPLOAD_SECONDS`; return how many went. A live
    upload keeps writing, so its file stays fresh."""
    cutoff = time.time() - STALE_UPLOAD_SECONDS
    removed = 0
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return 0
    for name in names:
        if not name.endswith(suffix):
            continue
        path = os.path.join(directory, name)
        try:
            if os.path.islink(path) or not os.path.isfile(path) or os.stat(path).st_mtime >= cutoff:
                continue
            os.unlink(path)
            removed += 1
        except FileNotFoundError:
            continue
    return removed


def remove_stale_upload_dirs(directory: str) -> int:
    """Remove the real directories directly in ``directory`` (an uploaded
    repository's ``.git`` waiting for its import) whose mtime is older than
    :data:`STALE_UPLOAD_SECONDS`; return how many went. The upload sets the
    mtime as it makes each one, and a successful import removes its own."""
    cutoff = time.time() - STALE_UPLOAD_SECONDS
    removed = 0
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return 0
    for name in names:
        path = os.path.join(directory, name)
        try:
            found = os.lstat(path)
            if not stat.S_ISDIR(found.st_mode) or found.st_mtime >= cutoff:
                continue
            shutil.rmtree(path)
            removed += 1
        except FileNotFoundError:
            continue
    return removed


def _is_text(head: bytes, size: int, read_rest) -> bool:
    if size > TEXT_IMPORT_MAX_BYTES or b"\0" in head[:_SNIFF_BYTES]:
        return False
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        decoder.decode(head)
        for chunk in read_rest():
            decoder.decode(chunk)
        decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        return False
    return True


def encoding_for(source: bytes | str | os.PathLike) -> str:
    """The import encoding of a body given as bytes or a file path.

    ``utf-8`` when the whole body is valid UTF-8, its first 8 KiB hold no NUL
    and it is at most :data:`TEXT_IMPORT_MAX_BYTES`; ``binary`` otherwise.
    """
    if isinstance(source, bytes):
        return "utf-8" if _is_text(source, len(source), lambda: ()) else "binary"
    size = os.path.getsize(source)
    if size > TEXT_IMPORT_MAX_BYTES:
        return "binary"
    with open(source, "rb") as body:
        head = body.read(_SNIFF_BYTES)
        return "utf-8" if _is_text(head, size, lambda: iter(lambda: body.read(_CHUNK_BYTES), b"")) else "binary"


def _write_atomic(cas_path: str, write_fn, boundary: str, *, expected_hash: str | None = None) -> None:
    """Write ``cas_path`` through a temp file only this writer uses, then
    rename it into place. Concurrent writers of the same content never share
    a temp file, so none can truncate another's bytes or publish a half-written
    blob. Fsync the complete object before publication, then its directories
    up to ``boundary``.
    """
    directory = os.path.dirname(cas_path)
    os.makedirs(directory, exist_ok=True)
    tmp_path = f"{cas_path}.{uuid.uuid4().hex}.tmp"
    try:
        write_fn(tmp_path)
        if expected_hash is not None:
            _verify_path(tmp_path, expected_hash)
        sync_cas_file(tmp_path)
        os.replace(tmp_path, cas_path)
        for parent in publication_directories(directory, boundary):
            sync_cas_directory(parent)
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def fsync_directory(directory: str | os.PathLike) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publication_directories(directory: str | os.PathLike, boundary: str | os.PathLike) -> tuple[str, ...]:
    """Every published ancestor of ``directory`` through ``boundary`` (the
    data dir, or a channel's location), which must contain it. Named through
    different symlinks (SQLite's resolved database path under a location
    kept as given, a data dir under macOS's ``/var -> /private/var``), both
    are resolved and the walk follows the real folders."""
    root = Path(boundary).absolute()
    current = Path(directory).absolute()
    if not current.is_relative_to(root):
        root, current = Path(os.path.realpath(root)), Path(os.path.realpath(current))
        current.relative_to(root)
    parents = []
    while True:
        parents.append(str(current))
        if current == root:
            return tuple(parents)
        current = current.parent


def sync_cas_file(path: str) -> None:
    """Confirm complete admitted object bytes before publication."""
    with open(path, "rb") as source:
        os.fsync(source.fileno())


def sync_cas_directory(path: str) -> None:
    """Confirm a CAS publication name; database confirmation is separate."""
    fsync_directory(path)


def _write_bytes(path: str, raw: bytes):
    with open(path, "wb") as f:
        f.write(raw)
