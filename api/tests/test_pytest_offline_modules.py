"""Accepting a renderer freezes its import graph into the installation's store.

The in-app counterpart of the shipped renderers' frozen graphs (the bundled
module snapshot, ``test_every_shipped_bundle_url_is_frozen``): a renderer
made in a channel and added to Capabilities has every module its bundle
imports, and everything those import, fetched into the store
(``esm_modules``) right after admission while the app is online, so the
mirror serves it with the CDN gone, before anyone opened it. A fake CDN
behind ``module_mirror._http_client``; nothing reaches the network.
"""
from __future__ import annotations

import time

import httpx
import pytest

from collimate._host import module_mirror as sdk
from collimate_server.models import Cell, Stack, VirtualFile

JS = "application/javascript; charset=utf-8"
ROOT = "https://esm.sh/acme-chart@1.2.3?target=es2022"
DEP = "https://esm.sh/acme-chart@1.2.3/es2022/core.mjs"
LAZY = "https://esm.sh/acme-chart@1.2.3/es2022/lazy.mjs"
FONT = "https://cdn.jsdelivr.net/npm/acme-font@2.0.0/acme.woff2"
MAIN = (f"import {{ chart }} from '{ROOT}';\n"
        "import { defineRenderer } from '/api/sdk/react.js';\n"
        f"const FONT = '{FONT}';\n"
        "const Component = () => chart(FONT);\n"
        "export default defineRenderer({ component: Component });\n")


class Upstream:
    """A fake CDN: ``routes`` maps a canonical URL to (body, media type)."""

    def __init__(self):
        self.routes: dict[str, tuple[bytes, str]] = {}
        self.calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = sdk.canonical_url(str(request.url))
        self.calls.append(url)
        if url not in self.routes:
            return httpx.Response(404, text="Not Found")
        body, media_type = self.routes[url]
        return httpx.Response(200, headers={"content-type": media_type}, content=body)


@pytest.fixture
def upstream(monkeypatch):
    import collimate_server.module_mirror as module_mirror
    fake = Upstream()
    fake.routes = {
        ROOT: (b"export * from './acme-chart@1.2.3/es2022/core.mjs';\n", JS),
        DEP: (b"export const chart = () => import('./lazy.mjs');\n", JS),
        LAZY: (b"export const lazy = 1;\n", JS),
        FONT: (b"wOF2 font bytes", "font/woff2"),
    }
    monkeypatch.setattr(module_mirror, "_http_client",
                        lambda: httpx.Client(transport=httpx.MockTransport(fake.handler), follow_redirects=False))
    return fake


@pytest.fixture
def client(channel_client, upstream):
    channel_client[0].upstream = upstream
    return channel_client[0]


def _add_renderer(client, main=MAIN):
    from collimate_server.cell_commits import commit_source_cell
    from collimate_server.state import find_active_channel, state_lock
    channel = find_active_channel()
    cell = Cell(name="Work", files=[
        VirtualFile(name="chart/view.renderer", content=(
            "id: acme-chart\nname: Acme chart\nsummary: Draws Acme charts\nentrypoint: main.tsx\n"
            "embed: native\nextensions: [.acmechart]\n")),
        VirtualFile(name="chart/main.tsx", content=main),
    ])
    with state_lock:
        channel.main_stacks.append(Stack(name="Work", cells=[cell]))
    commit_source_cell(channel, cell, cell.files, persist=False)
    request = {"source_cell_id": str(cell.id), "definition_path": "chart/view.renderer", "target_cell_id": None}
    preview = client.post(f"/api/channels/{client.channel_id}/packages/preview", json=request)
    assert preview.status_code == 200, preview.text
    added = client.post(f"/api/channels/{client.channel_id}/packages/add", json=request | {
        "expected_content_hash": preview.json()["content_hash"],
        "expected_target_revision": preview.json()["target_revision"]})
    assert added.status_code == 200, added.text


def _offline(client, *, until_complete=False):
    deadline = time.monotonic() + 10
    while True:
        answer = client.get("/api/renderers/acme-chart/offline")
        assert answer.status_code == 200, answer.text
        if not until_complete or answer.json()["complete"] or time.monotonic() > deadline:
            return answer.json()
        time.sleep(0.05)


def test_adding_a_renderer_freezes_its_whole_graph_and_the_mirror_then_serves_it_offline(client):
    _add_renderer(client)
    state = _offline(client, until_complete=True)
    assert state == {"complete": True, "roots": 2, "modules": 4, "missing": [], "truncated": False}
    assert sorted(set(client.upstream.calls)) == sorted([ROOT, DEP, LAZY, FONT])
    # The CDN goes away: every module of the graph is served from the store.
    client.upstream.routes.clear()
    for path in ("esm.sh/acme-chart@1.2.3?target=es2022", "esm.sh/acme-chart@1.2.3/es2022/core.mjs",
                 "esm.sh/acme-chart@1.2.3/es2022/lazy.mjs", "cdn.jsdelivr.net/npm/acme-font@2.0.0/acme.woff2"):
        assert client.get(f"/api/mirror/{path}").status_code == 200, path
    assert client.upstream.calls.count(LAZY) == 1


def test_a_module_the_cdn_cannot_serve_is_reported_and_store_for_offline_use_retries(client):
    del client.upstream.routes[LAZY]
    _add_renderer(client)
    deadline = time.monotonic() + 10
    while LAZY not in client.upstream.calls and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.1)
    state = _offline(client)
    assert state["complete"] is False
    assert [item["url"] for item in state["missing"]] == [LAZY]
    client.upstream.routes[LAZY] = (b"export const lazy = 1;\n", JS)
    stored = client.post("/api/renderers/acme-chart/offline")
    assert stored.status_code == 200 and stored.json()["complete"] is True


def test_a_renderer_that_imports_nothing_upstream_works_offline_with_no_fetch(client):
    _add_renderer(client, main=("import { defineRenderer } from '/api/sdk/react.js';\n"
                                "export default defineRenderer({ component: () => null });\n"))
    assert _offline(client) == {"complete": True, "roots": 0, "modules": 0, "missing": [], "truncated": False}
    assert client.upstream.calls == []


def test_the_shipped_renderers_graphs_are_held_by_the_snapshot_without_a_fetch(client):
    """The official twin: a seeded (shipped) renderer's graph is the bundled
    snapshot's, so its offline line is complete and nothing is fetched."""
    from collimate_server.builtin_packages import builtin_renderers
    from collimate_server.offline_modules import graph
    from collimate_server.renderer_bundles import get_bundle
    for renderer in builtin_renderers().values():
        result = graph(get_bundle(renderer.source_hash))
        assert result.complete, (renderer.id, result.missing[:3])
    assert client.upstream.calls == []
