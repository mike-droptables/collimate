"""Boot the real app inside pyodide and seed the single demo channel.

Order matters and is fixed:

1. ``threads.install()`` + ``modules.install()`` — before ``import main``,
   because the app binds ``asyncio.to_thread`` and imports the unix-only
   modules at import time.
2. ``import main`` from the staged api tree, then the demo runner replaces
   ``ProcessManager.start`` and the demo terminal route joins the app.
3. The app's own lifespan startup runs (bootstrap, the built-in renderers
   compiled through the shim's compiler and stored as server bundles, state
   restore, WAL checkpoint task).
4. The demo channel is created through the app's own routes, in-process:
   ``POST /api/channels`` creates the channel from the ``demo`` suggestion
   of the app's bundled default source (the staged ``collimate_content/``,
   whose ``suggested/demo.json`` the build wrote) with the runner drafts
   from ``channel.yaml`` (admission compiles and stores every renderer's
   server bundle; retried while the verifier answers busy), then
   ``POST /api/blob/stream`` per welcome file and ``POST .../import`` per
   welcome cell, then ``PATCH .../cells/{id}/meta`` for the pinned file.
5. The guard wraps the app: single-channel 400s and browser-demo 501s.

After ``boot()`` the page calls :func:`http` and :func:`ws_connect`, the
bridge entry points bound to the guarded app.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from urllib.parse import urlencode

from . import bridge, demo_runner, modules, terminal, threads
from .verification import BUSY

DEFAULT_APP_ROOT = "/mnt/app"
DEFAULT_DATA_DIR = "/mnt/data"

SINGLE_CHANNEL_DETAIL = "The demo runs a single channel"
# The suggestion the build writes into collimate_content/suggested (build/vendor.py).
DEMO_SUGGESTION = "demo"
UNAVAILABLE_DETAIL = "Not available in the browser demo"
# Creating the channel admits every package of the suggestion through the
# verifier, each file under its own deadline (verification.FILE_SECONDS).
# Several tabs booting at once on one machine can overrun that deadline; the
# app then refuses the creation with the verifier's busy detail and rolls it
# back. That refusal alone is retried, after these pauses in seconds.
CHANNEL_BUSY_PAUSES = (1.0, 2.0, 4.0, 8.0)

# The generator port proxy (routes/proxy.py) lives at /proxy/*, which the
# service worker never forwards, so it needs no guard here.

_app = None
_lifespan = None


def _json_response(status: int, payload) -> tuple[int, bytes]:
    return status, json.dumps(payload).encode("utf-8")


def guard_verdict(method: str, path: str):
    """``(status, body_bytes)`` when the demo answers this request itself,
    else None. Pure, so it can be unit-tested outside pyodide."""
    method = method.upper()
    segments = [s for s in path.split("/") if s]
    if segments[:2] == ["api", "channels"]:
        if method == "POST" and len(segments) == 2:
            return _json_response(400, {"detail": SINGLE_CHANNEL_DETAIL})
        if method == "DELETE" and len(segments) == 3:
            return _json_response(400, {"detail": SINGLE_CHANNEL_DETAIL})
        if method == "PUT" and len(segments) == 4 and segments[3] == "rename":
            return _json_response(400, {"detail": SINGLE_CHANNEL_DETAIL})
    if segments[:2] == ["api", "accounts"]:
        if method == "GET" and len(segments) == 2:
            return _json_response(200, [])
        return _json_response(501, {"detail": UNAVAILABLE_DETAIL})
    return None


class Guard:
    """ASGI wrapper: answers the guarded routes, forwards everything else."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            verdict = guard_verdict(scope.get("method", "GET"), scope.get("path", ""))
            if verdict is not None:
                status, body = verdict
                await send({
                    "type": "http.response.start",
                    "status": status,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                })
                await send({"type": "http.response.body", "body": body, "more_body": False})
                return
        await self.app(scope, receive, send)


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------

async def _check(response: bridge.Response, what: str) -> bridge.Response:
    if response.status >= 400:
        raise RuntimeError(f"{what} failed: HTTP {response.status} {response.text[:500]}")
    return response


def _read_cell_folder(folder: str) -> list[tuple[str, bytes]]:
    """Every file under ``folder`` as ``(path in the cell, bytes)``."""
    files = []
    for root, dirs, names in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(names):
            abs_path = os.path.join(root, name)
            with open(abs_path, "rb") as fh:
                files.append((os.path.relpath(abs_path, folder).replace(os.sep, "/"), fh.read()))
    if not files:
        raise RuntimeError(f"demo cell folder {folder} has no files")
    return files


async def _upload_cell_folder(app, folder: str) -> list[dict]:
    """Every file under ``folder`` streamed into the CAS, as the import
    route's ``{path, blob_hash}`` references."""
    files = []
    for path, raw in _read_cell_folder(folder):
        stored = (await _check(await bridge.request(
            app, "POST", "/api/blob/stream", body=raw,
        ), f"upload {path}")).json()
        files.append({"path": path, "blob_hash": stored["blob_hash"]})
    return files


def _verifier_busy(response: bridge.Response) -> bool:
    if response.status != 400:
        return False
    try:
        return response.json() == {"detail": BUSY}
    except ValueError:
        return False


async def _create_channel(app, name: str, runners: list, log) -> dict:
    """``POST /api/channels`` from the demo suggestion, retried after each
    of :data:`CHANNEL_BUSY_PAUSES` while the verifier is busy; any other
    refusal fails at once."""
    attempts = len(CHANNEL_BUSY_PAUSES) + 1
    for attempt, pause in enumerate((*CHANNEL_BUSY_PAUSES, None), start=1):
        response = await bridge.request(
            app, "POST", "/api/channels", query=urlencode({"name": name}),
            json_body={"suggested": {"id": DEMO_SUGGESTION, "tutorial": False, "runners": runners}},
        )
        if not _verifier_busy(response):
            return (await _check(response, "channel creation")).json()
        if pause is None:
            raise RuntimeError(
                f"channel creation failed after {attempts} attempts, the verifier stayed busy: "
                f"HTTP {response.status} {response.text[:500]}")
        log(f"channel creation: verifier busy (attempt {attempt} of {attempts}); retrying in {pause:g} s")
        await asyncio.sleep(pause)


async def seed_channel(app, demo_dir: str, log) -> dict:
    """Create the single channel and its cells from ``<demo_dir>/channel.yaml``.

    channel.yaml keys read: ``name`` (str), ``cells`` (list of ``{folder,
    name, pinned?}``), ``runners`` (list of RunnerDraft dicts passed verbatim
    as the suggestion's runners to ``POST /api/channels``)."""
    import yaml
    from state import state

    with open(os.path.join(demo_dir, "channel.yaml"), "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    name = str(cfg.get("name") or "Demo")
    runners = cfg.get("runners") or []
    cells = cfg.get("cells") or []

    channel = await _create_channel(app, name, runners, log)
    ch = next(c for c in state.channels if str(c.id) == channel["id"])
    channel_id = str(ch.id)
    log(f"channel {name!r} ({channel_id}) created with {len(runners)} runner(s)")

    created = []
    for spec in cells:
        folder = os.path.join(demo_dir, "channel", str(spec["folder"]))
        files = await _upload_cell_folder(app, folder)
        imported = (await _check(await bridge.request(
            app, "POST", f"/api/channels/{channel_id}/import",
            json_body={"insert_index": None, "stacks": [{"name": "", "cells": [
                {"name": str(spec.get("name") or spec["folder"]), "files": files, "dirs": []},
            ]}]},
        ), f"cell {spec['folder']}")).json()
        made = {"cell_id": imported["cell_ids"][0], "stack_id": imported["stack_ids"][0]}
        pinned = spec.get("pinned")
        if pinned:
            if pinned not in {f["path"] for f in files}:
                raise RuntimeError(f"cell {spec['folder']}: pinned file {pinned!r} is not in the folder")
            await _check(await bridge.request(
                app, "PATCH", f"/api/channels/{channel_id}/cells/{made['cell_id']}/meta",
                json_body={"pinned": pinned},
            ), f"pin {pinned} on {spec['folder']}")
        created.append({"cell_id": made["cell_id"], "stack_id": made["stack_id"], "name": spec.get("name")})
        log(f"cell {spec.get('name') or spec['folder']!r} added ({len(files)} files)")

    # The first cell listed is the one the visitor lands on.
    if created:
        ch.main_selection_id = next(
            s.id for s in ch.main_stacks if str(s.id) == created[0]["stack_id"]
        )
    from state import persist_state
    persist_state()
    return {"id": channel_id, "name": name, "cells": created}


# ---------------------------------------------------------------------------
# Entry points for the page
# ---------------------------------------------------------------------------

async def boot(app_root: str = DEFAULT_APP_ROOT, data_dir: str = DEFAULT_DATA_DIR,
               demo_dir: str | None = None, log=print, host: str | None = None) -> dict:
    """Boot the app in this runtime and seed the demo channel. ``host`` is
    the page's hostname: the name this in-page server answers to."""
    global _app, _lifespan
    threads.install()
    stubbed = modules.install()
    if stubbed:
        log("stubbed modules: " + ", ".join(stubbed))

    os.makedirs(data_dir, exist_ok=True)
    os.chdir(data_dir)
    if app_root not in sys.path:
        sys.path.insert(0, app_root)

    from . import verification as browser_verification
    browser_verification.install()
    import main  # the staged api/main.py
    threads.install_persistence()
    threads.install_validation()
    # A tokenless server answers only the Host names its origin guard trusts
    # (loopback), which keeps other sites off a local port. This server lives
    # in the page and has no port: the page's own name is its name.
    if host:
        from auth_middleware import origin_guard
        origin_guard.trusted_hosts.add(host.lower())
    demo_runner.install()
    terminal.register_route(main.app)

    _lifespan = main.app.router.lifespan_context(main.app)
    await _lifespan.__aenter__()
    log("lifespan started")

    channel = await seed_channel(main.app, demo_dir or os.path.join(app_root, "_demo"), log)
    _app = Guard(main.app)
    return {"stubbed": stubbed, "channel": channel}


def http(method, path, query, headers_json, body, sink):
    """One HTTP request from the service worker (see bridge.http)."""
    if _app is None:
        raise RuntimeError("boot() has not completed")
    return bridge.http(_app, method, path, query, headers_json, body, sink)


def ws_connect(url, protocols_json, sock):
    """One WebSocket from the page's WebSocket replacement (see bridge.ws_connect)."""
    if _app is None:
        raise RuntimeError("boot() has not completed")
    return bridge.ws_connect(_app, url, protocols_json, sock)
