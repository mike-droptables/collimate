"""Binary file support: CAS round-trip through serving, files-tree,
the binary-safe blob reader, and accepted binary checkpoint snapshots.

Synthetic fixtures only — PNG-shaped bytes and directly-seeded DB rows;
no on-disk generators or shipped assets.
"""
from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
import pytest

from database import get_db_connection

PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x02\xff\xfe"


def _cas_write_bytes(raw: bytes) -> str:
    h = hashlib.sha256(raw).hexdigest()
    d = Path(".collimate") / "objects" / h[:2]
    d.mkdir(parents=True, exist_ok=True)
    (d / h[2:]).write_bytes(raw)
    return h


def _seed_cell_with_file(channel_id: str, filepath: str, blob_hash: str, *, encoding='binary') -> str:
    """Insert a committed cell with one cell_files row → returns the cell id."""
    conn = get_db_connection(channel_id)
    try:
        run_id = str(uuid.uuid4())
        cid = str(uuid.uuid4())
        conn.execute("INSERT INTO runs (id, prompt, status) VALUES (?, 'seed', 'completed')", (run_id,))
        conn.execute(
            "INSERT INTO cells (id, run_id, generator_name, sequence_num, version_name, "
            "source_type) VALUES (?, ?, 'genesis', 1, 'v1', 'manual_edit')",
            (cid, run_id),
        )
        conn.execute("INSERT OR IGNORE INTO blobs (hash, size) VALUES (?, ?)", (blob_hash, 0))
        conn.execute(
            "INSERT INTO cell_files (cell_id, filepath, blob_hash,encoding,language) VALUES (?, ?, ?,?,'text')",
            (cid, filepath, blob_hash,encoding),
        )
        from persistence import initialize_files
        initialize_files(conn, cid, [{'path': filepath, 'body_hash': blob_hash,'encoding':encoding,'language':'text'}])
        from versions import initialize_file_snapshot
        initialize_file_snapshot(conn,cid)
        conn.commit()
        return cid
    finally:
        conn.close()


def test_get_cell_file_serves_binary_bytes(client):
    bh = _cas_write_bytes(PNG_BYTES)
    cid = _seed_cell_with_file(client.channel_id, "assets/logo.png", bh)
    r = client.get(f"/api/channels/{client.channel_id}/cells/{cid}/files/assets/logo.png")
    assert r.status_code == 200, r.text
    assert r.content == PNG_BYTES
    assert r.headers["content-type"].startswith("image/png")


def test_get_cell_file_honours_range_requests(client):
    # <video>/<audio> seeking needs byte ranges from the file endpoint.
    bh = _cas_write_bytes(PNG_BYTES)
    cid = _seed_cell_with_file(client.channel_id, "assets/range.png", bh)
    r = client.get(
        f"/api/channels/{client.channel_id}/cells/{cid}/files/assets/range.png",
        headers={"Range": "bytes=0-3"},
    )
    assert r.status_code == 206, r.text
    assert r.content == PNG_BYTES[:4]
    assert "bytes" in r.headers.get("accept-ranges", "")
    assert r.headers["cache-control"] == "no-store"


def test_files_tree_marks_binary_with_blob_hash(client):
    bh = _cas_write_bytes(PNG_BYTES)
    cid = _seed_cell_with_file(client.channel_id, "logo.png", bh)
    r = client.get(f"/api/channels/{client.channel_id}/cells/{cid}/files-tree")
    assert r.status_code == 200, r.text
    f = next(x for x in r.json()["files"] if x["name"] == "logo.png")
    assert f["body_hash"] == bh
    assert f["encoding"] == "binary"
    assert 'content' not in f and 'blob_hash' not in f


def test_typed_history_materialization_preserves_binary_F0286():
    from versions import build_virtual_files
    text = ('a' * (64 * 1024 - 1) + 'é' + 'tail').encode('utf-8')
    binary = b'a' * (100 * 1024) + b'\xff'
    cases=[('text',text,'utf-8'),('image',PNG_BYTES,'binary'),('late',binary,'binary'),
           ('compatible',b'valid UTF8','binary'),('empty',b'','binary')]
    for path,raw,encoding in cases:
        digest=_cas_write_bytes(raw)
        file=build_virtual_files([dict(path=path,body_hash=digest,encoding=encoding,language='custom')])[0]
        assert file.content == (raw.decode() if encoding=='utf-8' else '')
        assert file.blob_hash == (None if encoding=='utf-8' else digest)
    with pytest.raises(OSError):
        build_virtual_files([dict(path='missing',body_hash='0'*64,encoding='binary',language='binary')])


def test_accepted_binary_checkpoints_to_version_by_hash(client):
    bh = _cas_write_bytes(PNG_BYTES)
    cid = _seed_cell_with_file(client.channel_id, "keep.txt", _cas_write_bytes(b"keep"), encoding="utf-8")
    from models import Cell, Stack
    from state import find_active_channel
    find_active_channel().main_stacks.append(Stack(cells=[Cell(id=cid)]))
    response = client.post(f'/api/channels/{client.channel_id}/cells/{cid}/files/mutate', json={
        'operation_id': str(uuid.uuid4()), 'mutations': [{
            'path': 'pic.png', 'kind': 'put', 'blob_hash': bh, 'encoding': 'binary',
            'writer_id': 'binary-test', 'edit_seq': 1, 'base_revision': 0,
        }],
    })
    assert response.status_code == 200, response.text
    assert response.json()['outcome'] == 'accepted'
    conn = get_db_connection(client.channel_id)
    try:
        from persistence import snapshot_files
        accepted = next(f for f in snapshot_files(conn, cid) if f['path'] == 'pic.png')
    finally:
        conn.close()

    r = client.post(f"/api/channels/{client.channel_id}/cells/{cid}/checkpoint")
    assert r.status_code == 200, r.text

    conn = get_db_connection(client.channel_id)
    try:
        rows = dict(
            conn.execute(
                "SELECT cf.filepath, cf.blob_hash FROM cell_files cf "
                "JOIN cells c ON c.id = cf.cell_id "
                "WHERE c.source_type='manual_edit' AND cf.filepath='pic.png'"
            ).fetchall()
        )
        assert next(f for f in snapshot_files(conn, cid) if f['path'] == 'pic.png') == accepted
    finally:
        conn.close()
    assert rows.get("pic.png") == bh


def test_gc_preserves_accepted_binary_without_checkpoint(client):
    """Accepted current references retain bytes before a history checkpoint."""
    from cas_gc import run_gc
    bh = _cas_write_bytes(PNG_BYTES)
    conn = get_db_connection(client.channel_id)
    try:
        from persistence import initialize_files
        initialize_files(conn, str(uuid.uuid4()), [{'path': 'pending.png', 'body_hash': bh, 'encoding': 'binary','language':'binary'}])
        conn.commit()
    finally:
        conn.close()
    # grace_seconds=0 → anything swept to trash is purged immediately, so the
    # blob surviving in objects/ proves it was marked live (not swept).
    run_gc(grace_seconds=0)
    cas_path = Path(".collimate") / "objects" / bh[:2] / bh[2:]
    assert cas_path.exists()
