"""Persistent derived renderer bundles, addressed by the SDK source contract.

A row maps a renderer package's source hash to its compiled bundle in the
CAS and records who compiled it: ``server`` (admission, the built-in build,
a handler's verifier) or ``client`` (a browser upload of a package no
admission compiled). A server compilation replaces a client one; a client
upload never replaces a server one.
"""
from __future__ import annotations

import hashlib


def _digest(value: object) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
        raise ValueError('Invalid renderer bundle digest')
    return value


def get_bundle(source: str) -> bytes | None:
    """Return verified cached bytes; absent or damaged objects are cache misses."""
    from database import get_global_db_connection
    from cas import CorruptBlobError, read_blob
    source = _digest(source)
    conn = get_global_db_connection()
    try:
        row = conn.execute('SELECT blob_hash FROM renderer_bundles WHERE source_hash=?', (source,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    digest = _digest(row[0])
    try:
        body = read_blob(digest)
    except CorruptBlobError:
        return None
    if body is None or hashlib.sha256(body).hexdigest() != digest:
        return None
    return body


def publish_server_bundle(source: str, body: bytes) -> str:
    """Publish the server's own compilation of a source identity as its
    bundle, replacing whatever a client uploaded for it (an honest client
    compiles the same bytes). CAS always precedes SQL."""
    from cas import publication_lock, store_bytes
    from database import get_global_db_connection

    source = _digest(source)
    with publication_lock:
        # No durable reference can precede its actual object publication.
        digest = store_bytes(body)
        conn = get_global_db_connection()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO renderer_bundles(source_hash, blob_hash, origin) VALUES (?, ?, 'server') "
                    "ON CONFLICT(source_hash) DO UPDATE SET blob_hash=excluded.blob_hash, origin='server'",
                    (source, digest),
                )
        finally:
            conn.close()
    return digest


class BundleConflict(ValueError):
    """A version changed or another client already published different bytes."""


def package_bytes(*, prefix: str, entrypoint: str, channel: str, cell_id: str):
    """Read a frozen accepted package from canonical immutable body refs."""
    from cas import require_blob, pin_blobs
    from collimate._host.renderer_bundle import renderer_source_hash
    from channel_db import open_channel_db
    from persistence import snapshot_files
    conn = open_channel_db(channel)
    try:
        members = [f for f in snapshot_files(conn,cell_id) if f['path'].startswith(prefix)
                   and '_runs' not in f['path'][len(prefix):].split('/')]
    finally:
        conn.close()
    result, references = {}, {}
    with pin_blobs([f['body_hash'] for f in members]):
        for file in members:
            path = file['path'][len(prefix):]
            if path in result:
                raise ValueError('Duplicate renderer source path')
            references[file['path']] = ('blob',file['body_hash'])
            result[path] = require_blob(file['body_hash'])
    renderer_source_hash({p:hashlib.sha256(b).hexdigest() for p,b in result.items()},entrypoint=entrypoint)
    return result,references


def body_reference(conn,cell_id,name):
    from persistence import current_file
    row=current_file(conn,cell_id,name)
    if row is None or row['deleted']:
        raise ValueError('Renderer source body missing')
    return 'blob',_digest(row['body_hash'])


def current_body_references(channel: str, cell_id: str, names) -> dict:
    """Recheck SQL identities without reading or encoding source body objects."""
    if not names:
        return {}
    from channel_db import open_channel_db
    conn = open_channel_db(channel)
    try:
        conn.execute("BEGIN")
        return {name: body_reference(conn, cell_id, name) for name in names}
    finally:
        conn.close()


def bytes_source_hash(files: dict[str, bytes], *, entrypoint: str) -> str:
    from collimate._host.renderer_bundle import renderer_source_hash
    return renderer_source_hash({p: hashlib.sha256(b).hexdigest() for p, b in files.items()}, entrypoint=entrypoint)


def publish_client_bundle(source: str, body: bytes, *, validate_current) -> str:
    """Publish a browser's compilation of the current package, after
    current-source validation.

    It fills the bundle of a package no admission compiled: a cell admitted
    before admission stored bundles, or a package edited or moved onto
    Capabilities since. Executable code is trusted user input, not a server
    compilation: it never replaces a server bundle (the upload is a no-op
    answering the server's digest), and otherwise the first distinct upload
    for a source identity is kept. Publication serialization gives identical
    writes idempotence. CAS always precedes SQL.
    """
    from cas import publication_lock, store_bytes, retain_blob
    from database import get_global_db_connection

    source = _digest(source)
    digest = hashlib.sha256(body).hexdigest()
    validate_current()
    # Hash/write outside state_lock. Existing CAS publication and its age floor
    # protect this in-flight object; retention/ref commit share publication_lock.
    if store_bytes(body) != digest:
        raise ValueError('Renderer bundle publication digest mismatch')
    # Recheck after staging, then publish the exact-source-hash cache without
    # holding state_lock over CAS verification or the global SQL commit. A late
    # source change can leave harmless derived cache bytes; the final check
    # refuses the upload and the source route will not serve that stale hash.
    validate_current()
    with publication_lock:
        retain_blob(digest)
        conn = get_global_db_connection()
        try:
            row = conn.execute('SELECT blob_hash, origin FROM renderer_bundles WHERE source_hash=?',
                               (source,)).fetchone()
            if row is not None and row[1] == 'server':
                return row[0]
            if row is not None and row[0] != digest:
                raise BundleConflict('A different bundle already exists for this source version')
            with conn:
                conn.execute("INSERT OR IGNORE INTO renderer_bundles(source_hash, blob_hash, origin) "
                             "VALUES (?, ?, 'client')", (source, digest))
        finally:
            conn.close()
    validate_current()
    return digest
