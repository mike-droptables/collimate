"""POST /api/channels/{c}/blob and GET /api/channels/{c}/blob/{hash}: status codes and headers.

An oversized body returns 413; durable storage failure returns 507.
Synthetic bytes only.
"""
from __future__ import annotations

import hashlib


def _blob(client, suffix=""):
    from collimate_server.state import state
    return f"/api/channels/{state.active_channel_id}/blob{suffix}"


def test_upload_over_the_cap_is_413(client):
    from collimate_server.cas import MAX_BLOB_BYTES

    r = client.post(_blob(client), content=b"x" * (MAX_BLOB_BYTES + 1))
    assert r.status_code == 413, r.text


def test_upload_write_failure_reports_insufficient_storage(client, monkeypatch):
    import collimate_server.cas as cas

    def fail_storage(*_a, **_k):
        raise OSError('storage unavailable')
    monkeypatch.setattr(cas.BodyStore, "_store_blob", fail_storage)
    r = client.post(_blob(client), content=b"small payload")
    assert r.status_code == 507, r.text


def test_upload_then_get_round_trips(client):
    raw = b"\x00\x01binary\xff"
    r = client.post(_blob(client), content=raw)
    assert r.status_code == 200, r.text
    blob_hash = r.json()["blob_hash"]
    assert blob_hash == hashlib.sha256(raw).hexdigest()

    got = client.get(_blob(client, f"/{blob_hash}"))
    assert got.status_code == 200, got.text
    assert got.content == raw
    assert got.headers["content-type"] == "application/octet-stream"
    assert got.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_blob_is_never_type_sniffed__F1986(client):
    """CAS bytes carry no type; ``nosniff`` keeps a browser from running an
    HTML blob as a document on the app origin."""
    r = client.post(_blob(client), content=b"<html><script>alert(1)</script></html>")
    got = client.get(_blob(client, f"/{r.json()['blob_hash']}"))
    assert got.status_code == 200, got.text
    assert got.headers["x-content-type-options"] == "nosniff"


def test_get_missing_blob_is_404(client):
    r = client.get(_blob(client, f"/{'0' * 64}"))
    assert r.status_code == 404, r.text
