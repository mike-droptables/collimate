"""The module mirror (``module_mirror`` + ``routes/mirror.py``).

Every test runs against a synthetic snapshot and a fake upstream (an httpx
MockTransport behind ``module_mirror._http_client``); none reaches the
network. The last tests read the bundled ``module-snapshot/`` the app ships.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from types import MappingProxyType

import httpx
import pytest

from collimate._host import module_mirror as sdk

REPO = Path(__file__).resolve().parents[2]


def _bundled_snapshot() -> Path:
    """The snapshot the app indexes at startup (``genesis.bundled_dir``)."""
    import genesis
    return Path(genesis.bundled_dir("module-snapshot"))
ROOT_JS = "application/javascript; charset=utf-8"


def _js(text: str) -> bytes:
    return text.encode("utf-8")


def _write_snapshot(directory: Path, modules: dict[str, tuple[bytes, str]], *,
                    unavailable: dict[str, str] | None = None) -> Path:
    """A snapshot folder holding ``modules`` ({url: (served bytes, media type)})."""
    (directory / "modules").mkdir(parents=True)
    entries = {}
    for url, (body, media_type) in sorted(modules.items()):
        digest = hashlib.sha256(body).hexdigest()
        (directory / "modules" / digest).write_bytes(body)
        entries[url] = sdk.SnapshotEntry(digest, media_type, digest, url, (), ())
    snapshot = sdk.Snapshot(sdk.contract_hash(), tuple(sorted(modules)), MappingProxyType(entries),
                            MappingProxyType(dict(sorted((unavailable or {}).items()))))
    (directory / "index.json").write_bytes(sdk.snapshot_index_bytes(snapshot))
    (directory / "LICENSES.json").write_text('{"packages": {}, "schema": 1}\n')
    return directory


class Upstream:
    """A fake CDN: ``routes`` maps a canonical URL to (status, headers, body)
    or to a callable taking the request; ``calls`` lists every request."""

    def __init__(self):
        self.routes: dict = {}
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def serve(self, url: str, body: bytes, media_type: str = ROOT_JS, status: int = 200, **headers):
        self.routes[url] = (status, {"content-type": media_type, **headers}, body)

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = sdk.canonical_url(str(request.url))
        with self._lock:
            self.calls.append(url)
        answer = self.routes.get(url)
        if answer is None:
            return httpx.Response(404, text="Not Found")
        if callable(answer):
            return answer(request)
        status, headers, body = answer
        return httpx.Response(status, headers=headers, content=body)

    def count(self, url: str) -> int:
        with self._lock:
            return self.calls.count(url)


@pytest.fixture
def upstream(monkeypatch):
    import module_mirror
    fake = Upstream()
    monkeypatch.setattr(module_mirror, "_http_client",
                        lambda: httpx.Client(transport=httpx.MockTransport(fake.handler), follow_redirects=False))
    return fake


@pytest.fixture
def mirror(client, tmp_path, upstream):
    """The live client over a one-module snapshot and the fake upstream."""
    import module_mirror
    snapshot = _write_snapshot(tmp_path / "snapshot", {
        "https://esm.sh/shipped@1.0.0?target=es2022": (_js("export const shipped = 1;\n"), ROOT_JS),
        "https://esm.sh/floating@^1.0.0?target=es2022": (_js("export * from './floating@1.4.2/es2022/floating.mjs';\n"), ROOT_JS),
        "https://cdn.jsdelivr.net/npm/pkg@1.0.0/dist/pkg.wasm": (b"\0asm\x01\0\0\0", "application/wasm"),
    })
    assert module_mirror.load_snapshot(snapshot) == 3
    client.upstream = upstream
    yield client
    module_mirror.load_snapshot(_bundled_snapshot())


# -- hits ----------------------------------------------------------------------
def test_snapshot_hit_serves_exact_bytes_type_etag_and_caching(mirror):
    response = mirror.get("/api/mirror/esm.sh/shipped@1.0.0?target=es2022")
    assert response.status_code == 200
    assert response.content == b"export const shipped = 1;\n"
    assert response.headers["content-type"] == ROOT_JS
    digest = hashlib.sha256(response.content).hexdigest()
    assert response.headers["etag"] == f'"{digest}"'
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert response.headers["x-content-type-options"] == "nosniff"
    again = mirror.get("/api/mirror/esm.sh/shipped@1.0.0?target=es2022", headers={"If-None-Match": f'W/"{digest}"'})
    assert again.status_code == 304 and again.content == b"" and again.headers["etag"] == f'"{digest}"'
    floating = mirror.get("/api/mirror/esm.sh/floating@%5E1.0.0?target=es2022")
    assert floating.status_code == 200 and floating.headers["cache-control"] == "no-cache"
    wasm = mirror.get("/api/mirror/cdn.jsdelivr.net/npm/pkg@1.0.0/dist/pkg.wasm")
    assert wasm.content == b"\0asm\x01\0\0\0" and wasm.headers["content-type"] == "application/wasm"
    assert mirror.upstream.calls == []


def test_the_snapshot_wins_over_a_stored_row(mirror):
    import cas
    from database import get_global_db_connection
    other = cas.store_bytes(b"export const shipped = 'stored';\n")
    conn = get_global_db_connection()
    with conn:
        conn.execute("INSERT INTO esm_modules VALUES (?, ?, ?, ?, ?, ?, ?)",
                     ("https://esm.sh/shipped@1.0.0?target=es2022", other, ROOT_JS, 1, other,
                      "https://esm.sh/shipped@1.0.0?target=es2022", time.time()))
    conn.close()
    assert mirror.get("/api/mirror/esm.sh/shipped@1.0.0?target=es2022").content == b"export const shipped = 1;\n"


# -- misses ----------------------------------------------------------------------
def test_a_miss_is_fetched_rewritten_stored_and_then_served_offline(mirror):
    import cas
    from database import get_global_db_connection
    url = "https://esm.sh/demo@1.0.0?target=es2022"
    mirror.upstream.serve(url, _js('export * from "/demo@1.0.0/es2022/demo.mjs";\nimport("https://esm.sh/demo@1.0.0/es2022/lazy.mjs");\n'))
    response = mirror.get("/api/mirror/esm.sh/demo@1.0.0?target=es2022")
    assert response.status_code == 200
    assert response.content == _js('export * from "./demo@1.0.0/es2022/demo.mjs";\nimport("./demo@1.0.0/es2022/lazy.mjs");\n')
    assert response.headers["content-type"] == ROOT_JS
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
    conn = get_global_db_connection()
    row = conn.execute("SELECT * FROM esm_modules WHERE url=?", (url,)).fetchone()
    conn.close()
    assert row["blob_hash"] == hashlib.sha256(response.content).hexdigest()
    assert row["media_type"] == ROOT_JS and row["bytes"] == len(response.content) and row["final_url"] == url
    assert cas.read_blob(row["blob_hash"]) == response.content

    mirror.upstream.routes.clear()  # upstream gone: the store answers
    mirror.upstream.routes[url] = lambda request: (_ for _ in ()).throw(httpx.ConnectError("offline"))
    assert mirror.get("/api/mirror/esm.sh/demo@1.0.0?target=es2022").content == response.content
    assert mirror.upstream.count(url) == 1


def test_a_floating_miss_freezes_at_its_first_fetch(mirror):
    url = "https://esm.sh/range@^2.0.0?target=es2022"
    mirror.upstream.serve(url, _js("export const v = 1;\n"))
    first = mirror.get("/api/mirror/esm.sh/range@^2.0.0?target=es2022")
    assert first.headers["cache-control"] == "no-cache"
    mirror.upstream.serve(url, _js("export const v = 2;\n"))
    assert mirror.get("/api/mirror/esm.sh/range@%5E2.0.0?target=es2022").content == first.content
    assert mirror.upstream.count(url) == 1


def test_non_javascript_passes_through_byte_for_byte(mirror):
    url = "https://cdn.jsdelivr.net/npm/katex@0.16.22/dist/katex.min.css"
    body = b'@font-face{src:url(fonts/KaTeX_Main.woff2)} .a{background:url("/npm/x@1.0.0/y.png")}'
    mirror.upstream.serve(url, body, "text/css; charset=utf-8")
    response = mirror.get("/api/mirror/cdn.jsdelivr.net/npm/katex@0.16.22/dist/katex.min.css")
    assert response.content == body and response.headers["content-type"] == "text/css; charset=utf-8"


def test_the_query_is_part_of_the_key_and_an_escaped_caret_is_the_same_key(mirror):
    for order in ("a@1.0.0,b@1.0.0", "b@1.0.0,a@1.0.0"):
        mirror.upstream.serve(f"https://esm.sh/q@1.0.0?deps={order}", _js(f"export const deps = '{order}';\n"))
    a = mirror.get("/api/mirror/esm.sh/q@1.0.0?deps=a@1.0.0,b@1.0.0")
    b = mirror.get("/api/mirror/esm.sh/q@1.0.0?deps=b@1.0.0,a@1.0.0")
    assert a.content != b.content
    assert mirror.upstream.count("https://esm.sh/q@1.0.0?deps=a@1.0.0,b@1.0.0") == 1
    assert mirror.upstream.count("https://esm.sh/q@1.0.0?deps=b@1.0.0,a@1.0.0") == 1
    import routes.mirror as route
    from starlette.requests import Request
    keys = {route._raw_key(Request({"type": "http", "method": "GET", "path": "/", "headers": [],
                                    "raw_path": raw, "query_string": b"target=es2022"}))
            for raw in (b"/api/mirror/esm.sh/c@%5E1.0.0", b"/api/mirror/esm.sh/c@^1.0.0", b"/api/mirror/esm.sh/c@%5e1.0.0")}
    assert keys == {"https://esm.sh/c@^1.0.0?target=es2022"}


def test_an_allowlisted_redirect_is_followed_and_recorded(mirror):
    from database import get_global_db_connection
    url = "https://esm.sh/moved@1.0.0"
    landed = "https://esm.sh/moved@1.0.0/es2022/moved.mjs"
    mirror.upstream.serve(url, b"", status=302, location="/moved@1.0.0/es2022/moved.mjs")
    mirror.upstream.serve(landed, _js('import "./dep.mjs";\n'))
    response = mirror.get("/api/mirror/esm.sh/moved@1.0.0")
    assert response.status_code == 200
    # Resolved where the redirect landed, emitted relative to the requested URL.
    assert response.content == _js('import "./moved@1.0.0/es2022/dep.mjs";\n')
    conn = get_global_db_connection()
    assert conn.execute("SELECT final_url FROM esm_modules WHERE url=?", (url,)).fetchone()[0] == landed
    conn.close()


def _boom(error):
    def answer(request):
        raise error
    return answer


@pytest.mark.parametrize("case", ["off-host redirect", "upstream 500", "timeout", "too large", "unlexable", "not utf-8"])
def test_upstream_failures_answer_502(mirror, monkeypatch, case):
    import module_mirror
    url = "https://esm.sh/fails@1.0.0"
    if case == "off-host redirect":
        mirror.upstream.serve(url, b"", status=301, location="https://evil.example/fails@1.0.0")
    elif case == "upstream 500":
        mirror.upstream.serve(url, b"mkdir: no space left on device", "text/plain", status=500)
    elif case == "timeout":
        mirror.upstream.routes[url] = _boom(httpx.ReadTimeout("slow"))
    elif case == "too large":
        monkeypatch.setattr(module_mirror.upstream, "CONTRACT", {**sdk.CONTRACT, "max_module_bytes": 8})
        mirror.upstream.serve(url, _js("export const big = 1;\n"))
    elif case == "unlexable":
        mirror.upstream.serve(url, _js('import "unterminated'))
    else:
        mirror.upstream.serve(url, b"export const a = '\xff';")
    response = mirror.get("/api/mirror/esm.sh/fails@1.0.0")
    assert response.status_code == 502
    assert response.json()["error"] == "upstream" and response.json()["detail"]
    assert response.headers["cache-control"] == "no-store"
    if case == "upstream 500":
        assert response.json()["status"] == 500


def test_an_upstream_404_is_a_404_that_is_not_cached(mirror):
    response = mirror.get("/api/mirror/esm.sh/absent@1.0.0")
    assert response.status_code == 404
    assert response.json() == {"error": "upstream", "status": 404,
                               "detail": "https://esm.sh/absent@1.0.0: HTTP 404: Not Found"}
    assert response.headers["cache-control"] == "no-store"
    mirror.upstream.serve("https://esm.sh/absent@1.0.0", _js("export {};\n"))
    assert mirror.get("/api/mirror/esm.sh/absent@1.0.0").status_code == 200


def test_an_unreachable_host_is_not_retried_for_a_while(mirror, monkeypatch):
    import module_mirror
    mirror.upstream.routes["https://esm.sh/a@1.0.0"] = _boom(httpx.ConnectError("no route to host"))
    mirror.upstream.serve("https://cdn.jsdelivr.net/npm/b@1.0.0/b.js", _js("export {};\n"))
    assert mirror.get("/api/mirror/esm.sh/a@1.0.0").status_code == 502
    second = mirror.get("/api/mirror/esm.sh/other@1.0.0")
    assert second.status_code == 502 and "unreachable" in second.json()["detail"]
    assert mirror.upstream.calls == ["https://esm.sh/a@1.0.0"]
    assert mirror.get("/api/mirror/cdn.jsdelivr.net/npm/b@1.0.0/b.js").status_code == 200
    monkeypatch.setattr(module_mirror, "UNREACHABLE_SECONDS", 0.0)
    module_mirror._unreachable.clear()
    assert mirror.get("/api/mirror/esm.sh/other@1.0.0").status_code == 404


def test_static_imports_are_prefetched_and_dynamic_ones_are_not(mirror):
    base = "https://esm.sh/app@1.0.0/es2022/"
    mirror.upstream.serve(base + "app.mjs", _js('import "./b.mjs";\nimport "/app@1.0.0/es2022/c.mjs";\nimport("./d.mjs");\n'))
    for name in ("b", "c", "d"):
        mirror.upstream.serve(base + f"{name}.mjs", _js(f"export const {name} = 1;\n"))
    assert mirror.get("/api/mirror/esm.sh/app@1.0.0/es2022/app.mjs").status_code == 200
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not (mirror.upstream.count(base + "b.mjs") and mirror.upstream.count(base + "c.mjs")):
        time.sleep(0.02)
    assert mirror.upstream.count(base + "b.mjs") == 1 and mirror.upstream.count(base + "c.mjs") == 1
    assert mirror.get("/api/mirror/esm.sh/app@1.0.0/es2022/b.mjs").content == _js("export const b = 1;\n")
    assert mirror.upstream.count(base + "b.mjs") == 1
    assert mirror.upstream.count(base + "d.mjs") == 0


async def test_concurrent_requests_share_one_upstream_fetch(fresh_cwd, upstream, tmp_path):
    import module_mirror
    from database import get_global_db_connection
    get_global_db_connection().close()  # migrated, as at startup
    module_mirror.load_snapshot(tmp_path / "absent")
    url = "https://esm.sh/slow@1.0.0"

    def slow(request):
        time.sleep(0.3)
        return httpx.Response(200, headers={"content-type": ROOT_JS}, content=_js("export const slow = 1;\n"))

    upstream.routes[url] = slow
    try:
        modules = await asyncio.gather(*(module_mirror.get_module(url) for _ in range(12)))
    finally:
        module_mirror.shutdown()
        module_mirror.load_snapshot(_bundled_snapshot())
    assert {module.body for module in modules} == {_js("export const slow = 1;\n")}
    assert upstream.calls == [url]


def test_a_damaged_stored_object_is_fetched_again_and_repaired(mirror):
    import cas
    url = "https://esm.sh/fragile@1.0.0"
    body = _js("export const fragile = 1;\n")
    mirror.upstream.serve(url, body)
    assert mirror.get("/api/mirror/esm.sh/fragile@1.0.0").content == body
    digest = hashlib.sha256(body).hexdigest()
    path = cas.blob_path(digest)
    os.chmod(path, 0o644)
    Path(path).write_bytes(b"damaged")
    assert mirror.get("/api/mirror/esm.sh/fragile@1.0.0").content == body
    assert mirror.upstream.count(url) == 2
    assert cas.require_blob(digest) == body


@pytest.mark.parametrize("path", [
    "/api/mirror/evil.example/x@1.0.0",
    "/api/mirror/esm.sh/",
    "/api/mirror/esm.sh/a%2Fb@1.0.0",
    "/api/mirror/esm.sh/a/%2e%2e/b@1.0.0",
    "/api/mirror/esm.sh/a@1.0.0%00",
    "/api/mirror/esm.sh/bad%zzescape",
])
def test_invalid_keys_are_refused_without_a_fetch(mirror, path):
    response = mirror.get(path)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid" and response.json()["detail"]
    assert mirror.upstream.calls == []


# -- startup -------------------------------------------------------------------------
def test_the_snapshot_index_is_checked_at_load(tmp_path):
    import module_mirror
    try:
        assert module_mirror.load_snapshot(tmp_path / "absent") == 0
        broken = tmp_path / "broken"
        broken.mkdir()
        with pytest.raises(ValueError, match="index is missing"):
            module_mirror.load_snapshot(broken)
        (broken / "index.json").write_text("{not json")
        with pytest.raises(ValueError, match="unreadable"):
            module_mirror.load_snapshot(broken)
        good = _write_snapshot(tmp_path / "good", {"https://esm.sh/a@1.0.0": (b"export {};", ROOT_JS)})
        assert module_mirror.load_snapshot(good) == 1
        for stale in (good / "modules").iterdir():
            stale.unlink()
        with pytest.raises(ValueError, match="missing 1 module file"):
            module_mirror.load_snapshot(good)
        other = _write_snapshot(tmp_path / "other", {"https://esm.sh/a@1.0.0": (b"export {};", ROOT_JS)})
        index = json.loads((other / "index.json").read_text())
        index["contract"] = "0" * 64
        (other / "index.json").write_text(json.dumps(index))
        with pytest.raises(ValueError, match="another mirror contract"):
            module_mirror.load_snapshot(other)
    finally:
        module_mirror.load_snapshot(_bundled_snapshot())


def test_startup_loads_the_bundled_snapshot_and_a_broken_one_refuses_it(fresh_cwd, monkeypatch):
    import importlib
    import module_mirror
    from conftest import reset_api_modules
    from fastapi.testclient import TestClient
    seen = []
    real = module_mirror.load_snapshot
    monkeypatch.setattr(module_mirror, "load_snapshot", lambda directory: seen.append(Path(directory)) or real(directory))
    reset_api_modules()
    with TestClient(importlib.import_module("main").app):
        pass
    assert seen == [REPO / "module-snapshot"] == [_bundled_snapshot()]

    def refuse(directory):
        raise ValueError("Module snapshot index unreadable")

    monkeypatch.setattr(module_mirror, "load_snapshot", refuse)
    reset_api_modules()
    with pytest.raises(ValueError, match="unreadable"):
        with TestClient(importlib.import_module("main").app):
            pass


# -- middleware ----------------------------------------------------------------------
def test_the_token_is_required_and_an_opaque_origin_refused(fresh_cwd, monkeypatch):
    import importlib
    from conftest import reset_api_modules
    from fastapi.testclient import TestClient
    monkeypatch.setenv("COLLIMATE_TOKEN", "mirror-token")
    reset_api_modules()
    from collimate.shared import toolchain
    path = f"/api/mirror/esm.sh/react@{toolchain()['react']}?target=es2022"
    with TestClient(importlib.import_module("main").app) as client:
        assert client.get(path).status_code == 401
        auth = {"Authorization": "Bearer mirror-token"}
        assert client.get(path, headers=auth).status_code == 200
        assert client.get(path, headers={**auth, "Origin": "null"}).status_code == 403


# -- the bundled snapshot --------------------------------------------------------------
_UPSTREAM_LITERAL = re.compile(r"([\"'`])((?:https://esm\.sh/|https://cdn\.jsdelivr\.net/)[^\"'`\s]*)\1")


def _shipped_bundles() -> list[str]:
    """What admission and the built-in build compile for every shipped
    renderer package (``<group>/<package>/<id>.renderer`` in the bundled
    content and built-in trees)."""
    import genesis
    import yaml
    from collimate._host.renderer_bundle import bundle_renderer
    texts = []
    for root in (Path(genesis.RENDERERS_DIR), Path(genesis.BUILTIN_DIR)):
        for definition in sorted(root.glob("*/*/*.renderer")):
            package = definition.parent
            files = {path.relative_to(package).as_posix(): path.read_bytes() for path in package.rglob("*")
                     if path.is_file() and "__pycache__" not in path.parts and path.suffix not in (".pyc", ".pyo")}
            entrypoint = yaml.safe_load(definition.read_bytes())["entrypoint"]
            texts.append(bundle_renderer(files, entrypoint=entrypoint).code)
    return texts


def test_every_shipped_bundle_url_is_frozen():
    """Every module-CDN URL literal in the shipped renderers' compiled
    bundles and the renderer runtime is in the bundled snapshot, as is its
    whole static closure; what is not is exactly the snapshot's
    ``unavailable`` record (upstream failures the SDK's freeze could not
    fetch, re-run to fill)."""
    import genesis
    from collimate.shared import shared_path
    snapshot = sdk.read_snapshot(_bundled_snapshot(), verify=True)
    assert snapshot.contract == sdk.contract_hash()
    prefixes = set()
    for root in (Path(genesis.RENDERERS_DIR), Path(genesis.BUILTIN_DIR)):
        for package in root.glob("*/*"):
            bases, _ = sdk.read_mirror_assets(package)
            prefixes |= set(bases) | {base.rstrip("/") for base in bases}
    texts = _shipped_bundles()
    assert len(texts) == 19
    texts += [shared_path("renderer-runtime", name).read_text() for name in ("react.js", "session.js")]
    literals = {match.group(2) for text in texts for match in _UPSTREAM_LITERAL.finditer(text)
                if "${" not in match.group(2) and match.group(2) not in prefixes}
    assert literals, "no module-CDN URL in the shipped bundles"
    missing = set()
    for literal in literals:
        if sdk.canonical_url(literal) not in snapshot.entries:
            missing.add(sdk.canonical_url(literal))
    frontier = [url for url in snapshot.entries]
    for url in frontier:
        missing |= {target for target in snapshot.entries[url].imports if target not in snapshot.entries}
        missing |= {target for target in snapshot.entries[url].dynamic if target not in snapshot.entries}
    assert missing <= set(snapshot.unavailable), sorted(missing - set(snapshot.unavailable))
    assert {url for url in snapshot.unavailable if url not in missing} == set()
