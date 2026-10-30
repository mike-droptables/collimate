"""
CAS (Content Addressable Storage) utilities.

Verified body admission for accepted file revisions, immutable history and
run artifacts. Publication and collection share the same lock.
"""

import codecs
import hashlib
import logging
import os
import shutil
import threading
import time
import uuid
from collections import Counter
from collections.abc import Iterable
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

# The sharded object store, cwd-relative (the data dir is the process CWD
# by the cli_main contract).
OBJECTS_DIR = os.path.join(".collimate", "objects")

# The cap of in-app writes: generator output, the buffered ``POST /api/blob``
# and other bodies a handler holds in memory. File import streams instead.
MAX_BLOB_BYTES = 50 * 1024 * 1024

# The per-file cap of streamed imports (``store_stream`` callers).
IMPORT_MAX_FILE_BYTES = 4 * 1024 ** 3

# Imported text above this size is stored as binary: the layout cache keeps
# every UTF-8 body decoded in server memory.
TEXT_IMPORT_MAX_BYTES = 5 * 1024 * 1024

# How much of a body the NUL sniff reads.
_SNIFF_BYTES = 8 * 1024
_CHUNK_BYTES = 64 * 1024

# Streamed uploads land here before publication. The dot keeps the folder out
# of the GC's two-character shard walk.
INCOMING_DIR = os.path.join(OBJECTS_DIR, ".incoming")

# A private upload file not written to for this long was left by a process
# that died mid-upload; the next upload's sweep removes it.
STALE_UPLOAD_SECONDS = 24 * 3600

# A sweep's age check and move cannot interleave with a store/dedupe touch.
publication_lock = threading.RLock()
_pins = Counter()


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



def blob_path(blob_hash: str) -> str:
    """Where the CAS keeps ``blob_hash``'s bytes."""
    if not is_cas_hash(blob_hash):
        raise ValueError("Invalid CAS digest")
    return os.path.join(OBJECTS_DIR, blob_hash[:2], blob_hash[2:])


def _verify_path(path: str, digest: str) -> None:
    sha256 = hashlib.sha256()
    with open(path, "rb") as source:
        while chunk := source.read(64 * 1024):
            sha256.update(chunk)
    if sha256.hexdigest() != digest:
        raise CorruptBlobError(f"CAS integrity failure for {digest}")


def verify_blob(blob_hash: str) -> None:
    """Check required CAS bytes without materializing the body."""
    _verify_path(blob_path(blob_hash), blob_hash)


def require_blob(blob_hash: str) -> bytes:
    """Read required immutable bytes, refusing missing or corrupt content."""
    path = blob_path(blob_hash)
    with open(path, "rb") as source:
        raw = source.read()
    if hashlib.sha256(raw).hexdigest() != blob_hash:
        raise CorruptBlobError(f"CAS integrity failure for {blob_hash}")
    return raw


def pinned_hashes() -> set[str]:
    root = os.path.abspath(OBJECTS_DIR)
    with publication_lock:
        return {digest for (directory, digest), count in _pins.items() if directory == root and count}


@contextmanager
def pin_blobs(hashes, *, verify: bool = True):
    """Keep a materializing read/export alive across collection.

    A caller freezing SQL references under cell locks may pin first and
    verify the pinned bytes after releasing those locks. Every ordinary
    caller gets the integrity preflight here.
    """
    hashes = set(hashes)
    root = os.path.abspath(OBJECTS_DIR)
    with publication_lock:
        if verify:
            for digest in hashes:
                _verify_path(blob_path(digest), digest)
        else:
            for digest in hashes:
                blob_path(digest)
        for digest in hashes:
            _pins[(root, digest)] += 1
    try:
        yield
    finally:
        with publication_lock:
            for digest in hashes:
                key = (root, digest)
                _pins[key] -= 1
                if not _pins[key]:
                    del _pins[key]


def retain_blob(blob_hash: str) -> None:
    """Admit an existing binary reference before publishing it in layout/SQL.

    Refresh its publication age under the sweep lock, or refuse if collection
    already removed it. Call on new external references, not unchanged files.
    """
    with publication_lock:
        _verify_path(blob_path(blob_hash), blob_hash)
        os.utime(blob_path(blob_hash), None)


def read_blob(blob_hash: str | None) -> bytes | None:
    """Raw CAS bytes for ``blob_hash`` (binary-safe, never decoded), or None
    if the hash is empty or malformed or the object is missing or unreadable."""
    if blob_hash is None or not is_cas_hash(blob_hash):
        return None
    try:
        return require_blob(blob_hash)
    except FileNotFoundError:
        return None


def hash_and_store_file(abs_path: str) -> str:
    """Publish a required file or raise on size, read or storage failure."""
    stat = os.stat(abs_path)
    if stat.st_size > MAX_BLOB_BYTES:
        raise ValueError(f"CAS file exceeds {MAX_BLOB_BYTES} bytes")

    sha256 = hashlib.sha256()
    with open(abs_path, "rb") as f:
        while chunk := f.read(65536):
            sha256.update(chunk)

    blob_hash = sha256.hexdigest()
    _store_blob(blob_hash, lambda dst: shutil.copy2(abs_path, dst))
    return blob_hash


def hash_and_store_bytes(raw: bytes) -> str:
    """Publish required capped bytes, raising before any reference is accepted."""
    if len(raw) > MAX_BLOB_BYTES:
        raise ValueError(f"CAS body exceeds {MAX_BLOB_BYTES} bytes")
    return store_bytes(raw)


def store_bytes(raw: bytes) -> str:
    """Store bytes without a size cap; return their hash or raise OSError.

    Imports and seeds use this policy. Ingresses with a size limit apply it
    before storage; ``hash_and_store_bytes`` is the capped, optional writer.
    """
    blob_hash = hashlib.sha256(raw).hexdigest()
    _store_blob(blob_hash, lambda dst: _write_bytes(dst, raw))
    return blob_hash


def store_stream(chunks: Iterable[bytes], *, max_bytes: int | None = None) -> tuple[str, int]:
    """Store a body read chunk by chunk; return ``(hash, size)``.

    The bytes go to a private file under the objects dir while their digest is
    computed, never into one in-memory buffer, then publish atomically under
    the digest. Passing ``max_bytes`` raises :class:`BlobTooLarge`; an error
    from ``chunks`` propagates. Either way the private file is removed and
    nothing is published.
    """
    os.makedirs(INCOMING_DIR, exist_ok=True)
    part = os.path.join(INCOMING_DIR, f"{uuid.uuid4().hex}.part")
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
        _store_blob(blob_hash, lambda dst: os.replace(part, dst), verify_written=False)
        return blob_hash, size
    finally:
        try:
            os.unlink(part)
        except FileNotFoundError:
            pass


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


def blob_is_utf8(blob_hash: str) -> bool:
    """Whether a stored body decodes as UTF-8, read in bounded chunks."""
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        with open(blob_path(blob_hash), "rb") as body:
            while chunk := body.read(_CHUNK_BYTES):
                decoder.decode(chunk)
        decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        return False
    return True


def _store_blob(blob_hash: str, write_fn, *, verify_written: bool = True) -> None:
    """Verify and durably publish bytes before callers publish SQL references.

    ``verify_written=False`` is for a writer that hashed the exact bytes it
    hands over (``store_stream``), so a large body is not read twice under
    the publication lock.
    """
    with publication_lock:
        cas_path = blob_path(blob_hash)
        try:
            if os.path.exists(cas_path):
                _verify_path(cas_path, blob_hash)
                sync_cas_file(cas_path)
                for directory in publication_directories(os.path.dirname(cas_path)):
                    sync_cas_directory(directory)
                os.utime(cas_path, None)
                return
            write_blob_atomic(cas_path, write_fn, expected_hash=blob_hash if verify_written else None)
            # copy2 preserves source times; GC needs this publication's age.
            os.utime(cas_path, None)
        except OSError as exc:
            logger.error("CAS write failed for blob %s: %s", blob_hash, exc)
            raise OSError(f"CAS write failed for blob {blob_hash}: {exc}") from exc


def write_blob_atomic(cas_path: str, write_fn, *, expected_hash: str | None = None) -> None:
    """Write ``cas_path`` through a temp file only this writer uses, then
    rename it into place. Concurrent writers of the same content never share
    a temp file, so none can truncate another's bytes or publish a half-written
    blob. Fsync the complete object before publication, then its directory.
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
        for parent in publication_directories(directory):
            sync_cas_directory(parent)
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def fsync_directory(directory: str) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publication_directories(directory: str) -> tuple[str, ...]:
    """Confirm every published ancestor through the owned data directory."""
    root = Path.cwd()
    current = Path(directory).absolute()
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
