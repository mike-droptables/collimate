"""Coverage for the request-body cap in main.py (BodySizeLimitMiddleware).

Defends every endpoint from a 1 GB POST that would OOM the worker: a
declared ``Content-Length`` over the cap is refused before the body is read,
and a chunked body is counted as it arrives and refused once it passes the
cap. Every route is capped, the generator proxy included (it buffers the
body it forwards).
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import BaseModel


def test_under_default_limit_passes(client):
    """A reasonably small POST to a normal route should not be rejected
    by the body-size middleware."""
    # /api/channels expects JSON with a "name" field. Body well under cap.
    r = client.post("/api/channels", json={"name": "test"})
    # We don't care about the route's own response shape — only that the
    # middleware didn't 413 us.
    assert r.status_code != 413


def test_oversize_default_route_rejected(client):
    """Default 64 MB cap. Send Content-Length larger and the middleware
    should 413 before the body is even read."""
    big = 100 * 1024 * 1024  # 100 MB
    r = client.post(
        "/api/channels",
        headers={"content-length": str(big), "content-type": "application/json"},
        # The actual body is small — we lie via the header. The middleware
        # rejects on the header alone, so the route never runs.
        content=b"{}",
    )
    assert r.status_code == 413
    body = r.json()
    assert "limit_bytes" in body
    assert body["limit_bytes"] == 64 * 1024 * 1024


@pytest.mark.parametrize("path", ["/api/channels/starting/import", "/proxy/starting/run1/5173/upload"])
def test_oversize_any_route_rejected__F0028(client, path):
    """Every buffered route has the one cap: the import commit, and the
    generator proxy, which buffers the body it forwards."""
    big = 200 * 1024 * 1024
    r = client.post(path, headers={"content-length": str(big)}, content=b"x")
    assert r.status_code == 413


def test_proxy_retains_the_64_mib_cap__F0329_F0386(client):
    response = client.post("/proxy/starting/run1/5173/upload", content=b"x",
                           headers={"content-length": str(64 * 1024 * 1024 + 1)})
    assert response.status_code == 413
    assert response.json()["limit_bytes"] == 64 * 1024 * 1024


def test_chunked_body_under_the_cap_passes__F0028(client):
    """A chunked body (no Content-Length) under the cap reaches the route."""
    r = client.post(
        "/api/channels",
        headers={"content-type": "application/json"},
        content=iter([b'{"name": ', b'"test"}']),
    )
    assert r.status_code != 413


def test_malformed_content_length_does_not_500(client):
    """A non-numeric Content-Length should be passed through to the
    framework's own validation, not crash the middleware."""
    r = client.post(
        "/api/channels",
        headers={"content-length": "not-a-number"},
        content=b'{"name": "test"}',
    )
    # Framework will reject the malformed header (400/422-ish) but not 5xx.
    assert r.status_code < 500


LIMIT = 1024


class _Payload(BaseModel):
    text: str


def _capped_app(hits: list | None = None) -> TestClient:
    """A bare app behind the middleware with a small cap: one route that
    parses JSON (FastAPI turns a failing body read into a 400), one that
    reads the raw body itself and notes each call in ``hits``."""
    from main import BodySizeLimitMiddleware

    app = FastAPI()

    @app.post("/json")
    async def json_route(payload: _Payload):
        return {"n": len(payload.text)}

    @app.post("/raw")
    async def raw_route(request: Request):
        if hits is not None:
            hits.append(request.url.path)
        return {"n": len(await request.body())}

    app.add_middleware(BodySizeLimitMiddleware, limit=LIMIT)
    return TestClient(app)


def _chunks(total: int, size: int = 100):
    sent = 0
    while sent < total:
        n = min(size, total - sent)
        yield b"x" * n
        sent += n


@pytest.mark.parametrize("route", ["/raw", "/json"])
def test_chunked_body_over_the_cap_is_refused__F0028(route):
    """No Content-Length to check up front: the bytes are counted as they
    arrive, and the client gets the 413 whatever the route made of the
    interrupted read."""
    client = _capped_app()
    r = client.post(route, content=_chunks(LIMIT * 3), headers={"content-type": "application/json"})
    assert r.status_code == 413
    assert r.json() == {"detail": "request body too large", "limit_bytes": LIMIT}


def test_chunked_body_at_the_cap_passes__F0028():
    client = _capped_app()
    r = client.post("/raw", content=_chunks(LIMIT))
    assert r.status_code == 200
    assert r.json() == {"n": LIMIT}


def test_declared_length_over_the_cap_is_refused_before_the_route__F0028():
    """The declared length alone decides: the body sent is tiny, so only the
    up-front check can refuse it, and the route never runs."""
    hits: list = []
    client = _capped_app(hits)
    r = client.post("/raw", content=b"x", headers={"content-length": str(LIMIT + 1)})
    assert r.status_code == 413
    assert r.json()["limit_bytes"] == LIMIT
    assert hits == []
    assert client.post("/raw", content=b"x").json() == {"n": 1}
    assert hits == ["/raw"]


def _streaming_app(hits: list) -> TestClient:
    """A bare app behind the middleware with a small cap, serving raw-body
    routes at the streaming upload paths and at paths that only resemble
    them."""
    from main import BodySizeLimitMiddleware

    app = FastAPI()
    for path in ("/api/blob/stream", "/api/blob/stream/extra", "/api/blob/streamed",
                 "/api/import/zip", "/api/import/zipped", "/api/channels/{channel}/import/zip"):
        async def raw_route(request: Request):
            hits.append(request.url.path)
            return {"n": len(await request.body())}
        app.add_api_route(path, raw_route, methods=["POST", "PUT"])
    app.add_middleware(BodySizeLimitMiddleware, limit=LIMIT)
    return TestClient(app)


@pytest.mark.parametrize("path", ["/api/blob/stream", "/api/import/zip"])
def test_streaming_upload_routes_are_exempt_from_the_buffered_cap_TR07(path):
    """The two streaming upload routes write their body to disk as it arrives
    and enforce their own cap, so neither a declared length nor the counted
    bytes past the buffered cap refuse them."""
    hits: list = []
    client = _streaming_app(hits)
    assert client.post(path, content=_chunks(LIMIT * 3)).json() == {"n": LIMIT * 3}
    big = client.post(path, content=b"x" * (LIMIT + 1), headers={"content-length": str(LIMIT + 1)})
    assert big.json() == {"n": LIMIT + 1}
    assert hits == [path, path]


@pytest.mark.parametrize("method,path", [
    ("POST", "/api/blob/stream/extra"), ("POST", "/api/blob/streamed"),
    ("POST", "/api/import/zipped"), ("POST", "/api/channels/demo/import/zip"),
    ("PUT", "/api/blob/stream"), ("PUT", "/api/import/zip"),
])
def test_only_the_exact_streaming_routes_are_exempt_TR07(method, path):
    """The exemption is an exact method and an anchored path: a route that
    shares a prefix, or another method on the same path, keeps the cap."""
    hits: list = []
    client = _streaming_app(hits)
    r = client.request(method, path, content=_chunks(LIMIT * 3))
    assert r.status_code == 413
    assert r.json()["limit_bytes"] == LIMIT


def test_the_server_caps_with_the_pure_asgi_counter_only__F0028(api_app):
    """The cap is the counting middleware, and no BaseHTTPMiddleware is left
    to route every response (SSE included) through an extra task."""
    from starlette.middleware.base import BaseHTTPMiddleware

    import main
    classes = [m.cls for m in api_app.user_middleware]
    assert main.BodySizeLimitMiddleware in classes
    assert BaseHTTPMiddleware not in classes
