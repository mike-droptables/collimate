"""BodyStore._store_blob under concurrency and failure: every writer uses its own
temp file, so concurrent stores of the same new content all succeed with
intact bytes, and a failed write leaves neither a blob nor a temp file."""
from __future__ import annotations

import glob
import hashlib
import os
import threading
import time
import pytest

PAYLOAD = os.urandom(1024 * 1024)


def _slow_write(path: str, raw: bytes) -> None:
    with open(path, "wb") as f:
        for i in range(0, len(raw), 128 * 1024):
            f.write(raw[i:i + 128 * 1024])
            f.flush()
            time.sleep(0.01)


def _run_concurrently(fn, n: int = 8) -> list:
    barrier = threading.Barrier(n)
    results: list = [None] * n

    def worker(i: int) -> None:
        barrier.wait()
        try:
            results[i] = fn()
        except Exception as exc:  # surfaced by the equality assert below
            results[i] = exc

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


@pytest.fixture
def store(fresh_cwd):
    """The body store of one channel (its folder's objects/)."""
    from conftest import channel_home
    from collimate_server.cas import channel_store
    return channel_store(channel_home())


def _assert_stored(store, h: str) -> None:
    with open(store.blob_path(h), "rb") as f:
        assert f.read() == PAYLOAD
    assert glob.glob(os.path.join(os.path.dirname(store.blob_path(h)), "*.tmp")) == []


def test_concurrent_same_content_stores_all_succeed(store, monkeypatch):
    import collimate_server.cas as cas
    monkeypatch.setattr(cas, "_write_bytes", _slow_write)
    h = hashlib.sha256(PAYLOAD).hexdigest()
    results = _run_concurrently(lambda: store.hash_and_store_bytes(PAYLOAD))
    assert results == [h] * len(results)
    _assert_stored(store, h)


def test_concurrent_public_byte_store__F0039(store, monkeypatch):
    import collimate_server.cas as cas
    monkeypatch.setattr(cas, "_write_bytes", _slow_write)
    h = hashlib.sha256(PAYLOAD).hexdigest()
    results = _run_concurrently(lambda: store.store_bytes(PAYLOAD))
    assert results == [h] * len(results)
    _assert_stored(store, h)


def test_failed_write_leaves_no_blob_or_tmp(store, monkeypatch):
    import collimate_server.cas as cas
    real_write = cas._write_bytes

    def failing_write(path: str, raw: bytes) -> None:
        with open(path, "wb") as f:
            f.write(raw[:10])
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(cas, "_write_bytes", failing_write)
    with pytest.raises(OSError, match="No space"):
        store.hash_and_store_bytes(PAYLOAD)
    h = hashlib.sha256(PAYLOAD).hexdigest()
    assert not os.path.exists(store.blob_path(h))
    assert glob.glob(os.path.join(os.path.dirname(store.blob_path(h)), "*.tmp")) == []

    monkeypatch.setattr(cas, "_write_bytes", real_write)
    assert store.hash_and_store_bytes(PAYLOAD) == h
    _assert_stored(store, h)


def test_public_uncapped_byte_store_keeps_binary_bytes__F0039(store, monkeypatch):
    import collimate_server.cas as cas
    monkeypatch.setattr(cas, "MAX_BLOB_BYTES", 3)
    raw = b"\x00\xffbinary payload"
    blob = store.store_bytes(raw)
    assert blob == hashlib.sha256(raw).hexdigest()
    assert store.read_blob(blob) == raw
    with pytest.raises(ValueError, match="exceeds"):
        store.hash_and_store_bytes(raw)


def test_public_byte_store_raises_and_cleans_failed_write__F0039(store, monkeypatch):
    import collimate_server.cas as cas
    import pytest

    def failing_write(path, raw):
        with open(path, "wb") as f:
            f.write(raw[:2])
        raise OSError(28, "disk full")

    monkeypatch.setattr(cas, "_write_bytes", failing_write)
    raw = b"unwritten content"
    with pytest.raises(OSError, match="CAS write failed"):
        store.store_bytes(raw)
    blob = hashlib.sha256(raw).hexdigest()
    assert store.read_blob(blob) is None
    assert glob.glob(os.path.join(os.path.dirname(store.blob_path(blob)), "*.tmp")) == []
