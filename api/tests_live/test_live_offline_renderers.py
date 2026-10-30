# SPDX-License-Identifier: MIT
"""The shipped renderers mount with both module CDNs out of reach.

The app serves the module CDNs (``collimate._host.module_mirror.HOSTS``) from
its own module mirror (``GET /api/mirror/{host}/{path}``, the shipped graph
frozen in the bundled ``module-snapshot/``). This scenario proves the shipped graph
is complete: the browser context aborts every request to either CDN, and the
server this module starts has no route upstream either (its HTTPS proxy is a
closed port, which the mirror's client follows), so a module missing from the
snapshot fails visibly instead of being fetched.

For each of md (milkdown), mermaid, terminal, excalidraw, pdf, monaco (.py)
and the assistant (.chat) a file opens in one cell and its renderer must
mount; afterwards the browser must have asked neither CDN for anything and
the server must have stored and attempted no upstream fetch. The chat's
composer must take typing (it is open while the run is starting). The markdown
file also holds a bullet list, which must render as list items: the milkdown
renderer's ``deps=`` list pins ``prosemirror-model`` 1.25.11 and the snapshot
freezes what it resolved (1.25.12 rejects Milkdown 7.21.2's list attributes
and renders every list as nothing).

Needs the built SPA (``api/static/index.html``) and python-playwright with a
Chromium, like the rest of this tree; it needs no model, account or Docker.
"""
from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request

import pytest
from collimate._host.module_mirror import HOSTS as CDN_HOSTS

from .conftest import API_DIR, STATIC_INDEX, CellView, LiveServer, _free_port

pytestmark = pytest.mark.live

# A closed local port: the server's upstream client (trust_env) connects to
# it for every CDN fetch and is refused at once.
DEAD_PROXY = "http://127.0.0.1:9"
STARTING = json.dumps({"starting": True, "label": "Offline"})


def _pdf() -> bytes:
    """A one-page PDF reading "Offline"."""
    content = b"BT /F1 24 Tf 20 40 Td (Offline) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return out


#: renderer -> (file, bytes, embed, a selector the mounted renderer shows, text it must contain)
RENDERERS = {
    "milkdown": ("offline.md", b"# Offline heading\n\nA *markdown* paragraph.\n\n- first offline item\n- second offline item\n",
                 "iframe", "h1", "Offline heading"),
    "mermaid": ("offline.mmd", b"graph TD; A[Offline] --> B[Mermaid]\n", "native", "svg", "Offline"),
    "terminal": ("offline.terminal", STARTING.encode(), "native", ".xterm", ""),
    "excalidraw": ("offline.excalidraw",
                   json.dumps({"type": "excalidraw", "version": 2, "elements": [], "appState": {}}).encode(),
                   "iframe", ".excalidraw", ""),
    "pdf": ("offline.pdf", _pdf(), "native", "img[src^='blob:']", ""),
    # Monaco paints a space as U+00A0, so the probe text has none.
    "monaco": ("offline.py", b"print('offline_monaco')\n", "iframe", ".monaco-editor .view-lines", "offline_monaco"),
    "assistant": ("offline.chat", STARTING.encode(), "native", "button[title='Send message']", ""),
}

#: renderer -> (a selector, the texts its matches must show, in order), checked once it mounted
CONTENT = {
    "milkdown": (".ProseMirror ul li", ["first offline item", "second offline item"]),
}

#: renderer -> (its text field, the control typing enables), typed into once it mounted: the
#: chat's composer is open while its run is starting (what is sent queues for the session).
TYPING = {
    "assistant": ("textarea[aria-label='Message input']", "button[title='Send message']"),
}

_NATIVE_PROBE = """([cell, path, selector, text]) => {
  const host = document.querySelector(`[data-cell-id='${cell}'] [data-file-path='${path}']`);
  if (!host) return false;
  const roots = [host, ...[...host.querySelectorAll('*')].map(node => node.shadowRoot).filter(Boolean)];
  return roots.some(root => [...root.querySelectorAll(selector)].some(node => (node.textContent || '').includes(text)));
}"""
_FRAME_PROBE = """([selector, text]) =>
  [...document.querySelectorAll(selector)].some(node => (node.textContent || '').includes(text))"""


@pytest.fixture(scope="module")
def offline_server(tmp_path_factory):
    """A uvicorn of this module's own whose module mirror cannot reach
    upstream: its HTTPS proxy is a closed port."""
    if not os.path.isfile(STATIC_INDEX):
        pytest.skip("the built SPA is missing (api/static/index.html): "
                    "cd frontend && npm run build && cp -r dist ../api/static")
    data_dir = tmp_path_factory.mktemp("collimate-offline")
    port = _free_port()
    env = dict(
        os.environ,
        PYTHONPATH=API_DIR + os.pathsep + os.environ.get("PYTHONPATH", ""),
        PYTHONUNBUFFERED="1",
        COLLIMATE_ACCOUNTS_CONFIG=str(data_dir / "accounts.json"),
        PYTHON_KEYRING_BACKEND="tests_live._memory_keyring.MemoryKeyring",
        HTTPS_PROXY=DEAD_PROXY, https_proxy=DEAD_PROXY, ALL_PROXY=DEAD_PROXY, all_proxy=DEAD_PROXY,
        NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost",
    )
    log_path = str(data_dir / "uvicorn.log")
    log = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(port)],
                            cwd=str(data_dir), env=env, stdout=log, stderr=subprocess.STDOUT)
    server = LiveServer(f"http://127.0.0.1:{port}", proc, str(data_dir), log_path)
    deadline = time.time() + 90
    while True:
        if proc.poll() is not None:
            pytest.fail(f"uvicorn died (rc={proc.returncode}):\n{server.log_tail()}")
        try:
            with urllib.request.urlopen(server.base_url + "/health", timeout=2):
                break
        except Exception:  # noqa: BLE001 — still booting
            if time.time() > deadline:
                pytest.fail(f"uvicorn never answered /health:\n{server.log_tail()}")
            time.sleep(0.5)
    yield server
    proc.send_signal(signal.SIGINT)  # the lifespan's shutdown, as the shared server
    try:
        proc.wait(timeout=60)
    except subprocess.TimeoutExpired:
        proc.kill()
    log.close()


def _call(base: str, method: str, path: str, body=None, raw: bytes | None = None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    headers = {"content-type": "application/octet-stream" if raw is not None else "application/json"}
    request = urllib.request.Request(base + path, method=method, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=180) as response:
        return json.load(response)


@pytest.fixture(scope="module")
def offline_cells(offline_server):
    """Per renderer, a Standard channel of its own (the shipped renderers)
    holding one cell with the renderer's file: {renderer: (channel, cell)}.
    A channel each, so a page shows only its own renderer: the app mounts a
    channel's open tabs on every load."""
    base = offline_server.base_url
    cells = {}
    for renderer, (path, body, *_) in RENDERERS.items():
        channel = _call(base, "POST", "/api/channels?name=" + urllib.parse.quote(f"Offline {renderer}"),
                        {"suggested": {"id": "standard", "tutorial": False}})["id"]
        ref = {"path": path, "blob_hash": _call(base, "POST", "/api/blob/stream", raw=body)["blob_hash"]}
        created = _call(base, "POST", f"/api/channels/{urllib.parse.quote(channel)}/import",
                        {"stacks": [{"name": "", "cells": [{"name": f"Offline {renderer}", "files": [ref], "dirs": []}]}]})
        cells[renderer] = (channel, created["cell_ids"][0])
    return cells


class OfflinePage:
    """A page whose context aborts every request to either CDN: ``upstream``
    lists each such request, ``refused`` each mirror answer that was not
    200 or 304."""

    def __init__(self, page):
        self.page = page
        self.upstream: list[str] = []
        self.refused: list[str] = []


@pytest.fixture
def offline_page(browser, offline_server, offline_cells, request, evidence):
    """The page of the test's renderer's channel (selected before it loads)."""
    channel, _ = offline_cells[request.node.callspec.params["renderer"]]
    _call(offline_server.base_url, "POST", f"/api/channels/{urllib.parse.quote(channel)}/select", {})
    ctx = browser.new_context(viewport={"width": 1500, "height": 1000})
    ctx.set_default_timeout(45000)
    for host in CDN_HOSTS:
        ctx.route(f"https://{host}/**", lambda route: route.abort())
    offline = OfflinePage(ctx.new_page())
    ctx.on("request", lambda req: offline.upstream.append(req.url)
           if urllib.parse.urlsplit(req.url).hostname in CDN_HOSTS else None)
    ctx.on("response", lambda res: offline.refused.append(f"{res.status} {res.url}")
           if "/api/mirror/" in res.url and res.status not in (200, 304) else None)
    logs: list[str] = []
    offline.page.on("console", lambda m: logs.append(f"{m.type}: {m.text}"))
    offline.page.on("pageerror", lambda e: logs.append(f"pageerror: {e}"))
    offline.page.goto(offline_server.base_url + "/", wait_until="domcontentloaded")
    offline.page.wait_for_function("() => (document.body.innerText || '').trim().length > 0", timeout=30000)
    offline.page.wait_for_timeout(2000)
    yield offline
    if any(getattr(request.node, "rep_" + when, None) is not None and getattr(request.node, "rep_" + when).failed
           for when in ("setup", "call")):
        evidence.shot(offline.page, f"{request.node.name}.final")
        evidence.text(f"{request.node.name}.console.log", "\n".join(logs[-200:]))
        evidence.text(f"{request.node.name}.mirror.log", "\n".join(offline.refused + offline.upstream))
    ctx.close()


def _renderer_frames(page, renderer: str):
    path = RENDERERS[renderer][0]
    for frame in page.frames:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(frame.url).query)
        if query.get("renderer") == [renderer] and query.get("filename") == [path]:
            yield frame


def _mounted(page, cell_id: str, renderer: str) -> bool:
    path, _, embed, selector, text = RENDERERS[renderer]
    if embed == "native":
        return bool(page.evaluate(_NATIVE_PROBE, [cell_id, path, selector, text]))
    for frame in _renderer_frames(page, renderer):
        try:
            if frame.evaluate(_FRAME_PROBE, [selector, text]):
                return True
        except Exception:  # noqa: BLE001 — a frame navigating away
            pass
    return False


def _shown(page, renderer: str, selector: str) -> list[str]:
    """The trimmed texts of ``selector``'s matches in the renderer's frame."""
    for frame in _renderer_frames(page, renderer):
        try:
            return frame.evaluate("(selector) => [...document.querySelectorAll(selector)]"
                                  ".map(node => (node.textContent || '').trim())", selector)
        except Exception:  # noqa: BLE001 — a frame navigating away
            pass
    return []


def _mirror_failures(log_path: str, start: int) -> list[str]:
    """The server's own record, from byte ``start`` of its log, of what its
    mirror could not serve: access lines for /api/mirror/ answered other
    than 200/304, and its warnings."""
    with open(log_path, encoding="utf-8", errors="replace") as log:
        log.seek(start)
        return [line.rstrip() for line in log
                if "module mirror:" in line
                or ("/api/mirror/" in line and not line.rstrip().endswith(("200 OK", "304 Not Modified")))]


@pytest.mark.parametrize("renderer", list(RENDERERS))
def test_a_shipped_renderer_mounts_with_both_cdns_unreachable(offline_page, offline_server, offline_cells, evidence,
                                                              renderer):
    _, cell_id = offline_cells[renderer]
    page = offline_page.page
    log_start = os.path.getsize(offline_server.log_path)
    view = CellView(page, cell_id, evidence, f"offline-{renderer}").open()
    view.open_path(RENDERERS[renderer][0])
    deadline = time.time() + 60
    while not _mounted(page, cell_id, renderer):
        if time.time() > deadline:
            failures = "\n".join(_mirror_failures(offline_server.log_path, log_start) + offline_page.refused
                                 + offline_page.upstream) or "(none)"
            pytest.fail(f"{renderer} did not mount offline; the mirror could not serve, or the browser sent "
                        f"upstream:\n{failures}")
        page.wait_for_timeout(500)  # also delivers the context's request events
    page.wait_for_timeout(2000)  # late loads (fonts, workers, lazy chunks) still count
    if renderer in CONTENT:
        selector, texts = CONTENT[renderer]
        assert _shown(page, renderer, selector) == texts, f"{renderer} did not render its {selector} content"
    if renderer in TYPING:
        field_selector, send_selector = TYPING[renderer]
        field = page.locator(f"[data-cell-id='{cell_id}'] {field_selector}").first
        field.click()
        page.keyboard.type("typed offline", delay=20)
        assert field.input_value() == "typed offline", f"{renderer} did not take typing"
        assert page.locator(f"[data-cell-id='{cell_id}'] {send_selector}").first.is_enabled(), (
            f"{renderer}'s send stayed disabled after typing")
    assert offline_page.upstream == [], "the browser asked a CDN directly"
    assert offline_page.refused == [], "the mirror could not serve these"
    conn = sqlite3.connect(os.path.join(offline_server.data_dir, ".collimate", "app.db"))
    try:
        stored = [url for (url,) in conn.execute("SELECT url FROM esm_modules")]
    finally:
        conn.close()
    assert stored == [], "the server fetched these from upstream"
    assert _mirror_failures(offline_server.log_path, log_start) == [], "the server's mirror failed or tried upstream"
