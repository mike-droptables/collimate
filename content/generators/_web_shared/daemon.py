"""The web session daemon — server, route, and the session run loop.

There is no browser here. The client already has a real browser engine — its
native guest view — so :func:`run_web_session` launches nothing, streams
nothing and proxies nothing. What it does:

  1. write the run's first tab, ``tab-1.webview`` in the run folder, that
     tells the client which URL and which session partition to mount (see
     ``protocol``), and reveal it;
  2. serve one loopback WebSocket (``/__collimate/ws``) that each tab's client
     attaches to, says ``hello`` on (naming its tab), and pushes ``url``
     messages up;
  3. run the action loop over the buttons on each tab's toolbar: New tab
     writes the next ``tab-<n>.webview`` and reveals it; the device toggle
     rewrites that tab's file with the device it picked; Capture (there when
     the run has a capture callback) becomes RPCs that tab's client answers
     out of its guest (:class:`~.capture.CaptureContext`), and whatever the
     capture ``ctx.save``s lands in THIS cell under one
     ``<host>-<YYYYMMDD-HHMMSS>`` folder per press.

Daemon-shaped — it returns only when the user hits Stop (or an exception
escapes the run loop). Stop is also the cleanup: each tab file is a live
handle onto a socket that is closing, so every one is deleted on the way out
and the cell is left holding its captures. The login bucket is the guests'
``persist:`` session partition, named by ``profile_name``, so signing in once
through the cell keeps the login across runs.

Sibling generators import this via a `sys.path` shim — see the
package's `__init__.py` for the canonical snippet.
"""
from __future__ import annotations

import asyncio
import json
import posixpath
import re
import socket
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlsplit

from aiohttp import web

import gen

from .capture import CaptureContext, CaptureError, CaptureUnavailable
from .protocol import (
    DEVICES,
    _CONTROL_WS_PATH,
    _device_of,
    _normalise_caps,
    _normalise_url,
    _partition_for,
    _tab_artifact,
    _tab_file,
    _tab_id,
    _TabState,
)

# The largest message the control WS accepts from a client. Every capture reply
# rides it as one JSON frame (a page's whole outerHTML, a base64 PNG, whatever
# a capture script's evaluate scrapes), and aiohttp's 4 MiB default closed the
# socket (1009) on exactly the large pages worth capturing. The socket is
# loopback-only and the app's proxy leg does not cap; the renderer's hop into
# the app (uvicorn's 16 MiB ws_max_size) is the next limit.
_CONTROL_WS_MAX_MSG = 64 * 1024 * 1024


@dataclass
class _Tabs:
    """The run's tabs, ``tab id`` → :class:`_TabState`, and which control
    socket serves which of them."""
    tabs: dict[str, _TabState] = field(default_factory=dict)
    # Every control-WS socket currently connected (a tab's view, a second view
    # of the same tab, a view that has not said hello yet).
    sockets: set[web.WebSocketResponse] = field(default_factory=set)
    # socket → the tab its hello named.
    served: dict[web.WebSocketResponse, str] = field(default_factory=dict)
    # The last tab number handed out; tab numbers are never reused.
    last_n: int = 0

    def by_path(self, path: str) -> Optional[_TabState]:
        norm = posixpath.normpath(str(path or "")).lstrip("/")
        for tab in self.tabs.values():
            if tab.path == norm:
                return tab
        return None


async def run_web_session(
    *,
    url: str,
    cell_name: str = "Web",
    on_capture: Optional[Callable[[CaptureContext], Awaitable[None]]] = None,
    capture_button_label: str = "",
    profile_name: str = "default",
    device: str = "desktop",
) -> None:
    """Run one web session as a daemon. Returns only when the user hits Stop
    (or an exception escapes the run loop).

    Arguments
    ---------
    url : the URL the first tab loads, or ``""`` to start it on a blank page
        (the site is optional — the address bar is how the user names one).
        ``http://`` / ``https://`` prefix optional; added automatically when
        missing. A tab opened later with New tab starts blank.
    cell_name : the cell's display name (the host applies it while this is
        the only run on the cell).
    on_capture : optional async callback. When supplied, every tab's
        toolbar carries Capture, and a press on it triggers
        ``await on_capture(ctx)`` against the tab it was pressed on (the
        action's source is that tab's file; a ``capture`` from anywhere else
        is ignored). The callback uses ``ctx.evaluate(...)`` /
        ``ctx.html()`` / ``ctx.screenshot_png()`` to scrape page state and
        ``ctx.save(...)`` to write it into this cell's files, under the
        capture's own folder.
    capture_button_label : the label of the tab toolbar's Capture
        (``protocol._tab_artifact``); blank is ``Capture``. Unused without
        ``on_capture``.
    profile_name : names the login bucket. It becomes the guests' persistent
        session partition (``persist:collimate-<name>``), so cells sharing a
        name share cookies and a different name is an independent login that
        can run at the same time.
    device : what every tab opens as — ``desktop``, ``phone`` or ``tablet``
        (``protocol.DEVICES``; anything else is ``desktop``). A tab's toolbar
        toggle switches that tab alone with a ``device`` action whose payload
        is ``{"device": <name>}``; the daemon rewrites the tab's file and the
        renderer switches its guest in place.
    """
    tabs = _Tabs()
    full_url = _normalise_url(url)

    gen.phase("spinning_up", "attaching the client guest view")

    # Bound here and handed to the server as is: a port probed, closed and
    # bound again later could be taken in between (two web runs starting at
    # the same moment picked the same one and the second died on
    # EADDRINUSE).
    sock = _bind_free_port(9100)
    port = sock.getsockname()[1]
    # The host-relative path the client reaches this server through (the app
    # reverse-proxies it to 127.0.0.1:port); the control WebSocket
    # (/__collimate/ws) resolves off it. It is the ONLY thing this server
    # serves.
    proxy_base = gen.proxy_url(port)
    partition = _partition_for(profile_name)

    # Bookkeeping the teardown reads, so it is in scope however the run loop
    # ends: how many presses there were, how many of them actually wrote a
    # file (the only ones the cell can show, so the only ones it counts), and
    # the folder names already spoken for this session.
    capture_count = 0
    saved_captures = 0
    capture_dirs: set[str] = set()

    capture_label = (capture_button_label or "Capture") if on_capture is not None else None
    default_device = _device_of(device)

    def write_tab(tab: _TabState) -> str:
        """Write ``tab``'s ``tab-<n>.webview`` (committed by the next update)."""
        artifact = _tab_artifact(
            tab=tab.tab, url=tab.url, proxy_base=proxy_base, partition=partition,
            capture_label=capture_label, device=tab.device,
        )
        return gen.update_ui_file(_tab_file(tab.tab), json.dumps(artifact))

    async def open_tab(tab_url: str) -> _TabState:
        """Write the next ``tab-<n>.webview``, flush it and reveal it."""
        tabs.last_n += 1
        tab_id = _tab_id(tabs.last_n)
        tab = _TabState(tab=tab_id, path="", url=tab_url, device=default_device,
                        current_url=tab_url)
        tab.path = write_tab(tab)
        tabs.tabs[tab_id] = tab
        await gen.commit(name=cell_name)
        await gen.reveal_file(tab.path)
        return tab

    runner = await _start_aiohttp(sock, tabs)
    try:
        await gen.serve_port(port, name="guest control")
        gen.log(f"Using profile '{profile_name}': partition {partition}")
        await open_tab(full_url)
        gen.phase("running", "native guest view")
        gen.phase("running", f"Browsing {full_url}" if full_url else "Live browser")

        while not gen.cancelled:
            # No browser process to watch and no socket to reattach — the only
            # thing to do here is service the presses on the tabs' toolbars. A
            # client that comes and goes is normal (a tab can be closed and
            # reopened); the RPC reports it as CaptureUnavailable if a capture
            # lands while nobody is attached to that tab.
            event = await gen.wait_for_action_event(timeout=0.5)
            if not event:
                continue
            action = event.get("action_id")
            # A tab's toolbar button: the host stamps the press with that
            # tab's file. A press from anywhere else does nothing.
            tab = tabs.by_path(str(event.get("source") or ""))
            if tab is None:
                continue
            if action == "new_tab":
                opened = await open_tab("")
                gen.log(f"opened {opened.tab}")
                continue
            if action == "device":
                payload = event.get("payload")
                picked = payload.get("device") if isinstance(payload, dict) else None
                # An unknown device, or the one the tab already shows, changes
                # nothing.
                if picked not in DEVICES or picked == tab.device:
                    continue
                tab.device = picked
                write_tab(tab)
                await gen.commit()
                gen.log(f"{tab.tab} shows as {picked}")
                continue
            if action != "capture" or on_capture is None:
                continue
            capture_count += 1
            gen.phase("running", f"Capturing #{capture_count}…")
            ctx = CaptureContext(
                url=tab.current_url, capture_count=capture_count, state=tab,
            )
            failure = ""
            try:
                stopped = await _capture_until_stop(ctx, tab, capture_dirs, on_capture)
                if stopped:
                    failure = "cancelled by Stop"
            except asyncio.CancelledError:
                failure = "cancelled"
                raise
            except Exception as exc:
                failure = f"failed: {exc}"
            finally:
                # A callback may have saved useful files before failing or
                # Stop. Commit this press before another press or tab cleanup;
                # count it only after the host acknowledges its flush.
                await gen.commit()
                if ctx.saved:
                    saved_captures += 1
                    outcome = f"partial ({failure}) " if failure else ""
                    gen.log(
                        f"capture #{capture_count} ({tab.tab}) {outcome}→ {ctx.capture_dir}/ "
                        f"({len(ctx.saved)} file{'' if len(ctx.saved) == 1 else 's'})"
                    )
                elif failure:
                    gen.log(f"capture #{capture_count} {failure}")
            gen.phase(
                "running",
                f"Browsing {tab.current_url}" if tab.current_url else "Live browser",
            )
    finally:
        # Stop is the usual way out, so it is also the cleanup: each tab file
        # names a control socket that is about to close, and a session that
        # ended has no tabs. Best-effort — an exception escaping the run loop
        # must still leave the cell clean. It must also leave the cell
        # HONEST, so the closing log line is worded off whatever is in flight
        # here: a run dying on its way through this block did not end, it
        # failed.
        failed = sys.exc_info()[0] not in (None, asyncio.CancelledError)
        try:
            await _end_session(tabs, saved_captures, failed=failed)
        except Exception as exc:
            gen.log(f"session cleanup failed: {exc}")
        gen.log("Tearing down the session server")
        for tab in tabs.tabs.values():
            _detach_client(tab)
        for ws in list(tabs.sockets):
            try:
                await ws.close()
            except Exception:
                pass
        await runner.cleanup()


async def _capture_until_stop(ctx, tab, capture_dirs, on_capture) -> bool:
    """Race the complete URL/capture operation against Stop, joining both tasks."""
    async def capture():
        try:
            ctx.url = await ctx.current_url() or ctx.url
            tab.current_url = ctx.url
        except CaptureError as exc:
            gen.log(f"current URL unavailable ({exc}) — using {ctx.url}")
        ctx.capture_dir = _claim_capture_dir(ctx.url, capture_dirs)
        await on_capture(ctx)

    work = asyncio.create_task(capture())
    stop = asyncio.create_task(gen.wait_until_cancelled(poll=0.1))
    try:
        await asyncio.wait((work, stop), return_when=asyncio.FIRST_COMPLETED)
        if work.done():
            await work
            return False
        return True
    finally:
        # Joining cancellation runs CaptureContext._rpc's finally, removing
        # pending futures, before any saved bytes are flushed or sockets close.
        for task in (work, stop):
            if not task.done():
                task.cancel()
        await asyncio.gather(work, stop, return_exceptions=True)


# ---------------------------------------------------------------------------
# The control WebSocket — one handler, one router
# ---------------------------------------------------------------------------


def _ws_handler_factory(tabs: _Tabs):
    """The ``/__collimate/ws`` handler: route every up-message through
    :func:`_handle_client_message` (which also needs the socket itself, to
    register the client that says hello for a tab)."""
    async def _handler(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=_CONTROL_WS_MAX_MSG)
        await ws.prepare(request)
        tabs.sockets.add(ws)
        try:
            async for msg in ws:
                if msg.type != web.WSMsgType.TEXT:
                    continue
                try:
                    payload = json.loads(msg.data)
                except Exception:
                    continue
                if isinstance(payload, dict):
                    await _handle_client_message(tabs, ws, payload)
        finally:
            tabs.sockets.discard(ws)
            # A tab's RPC peer just went away. Drop it and fail everything
            # waiting on it rather than let a capture hang on a future no
            # socket will ever resolve.
            tab = tabs.tabs.get(tabs.served.pop(ws, ""))
            if tab is not None and tab.client is ws:
                _detach_client(tab)
        return ws
    return _handler


async def _handle_client_message(
    tabs: _Tabs, ws: web.WebSocketResponse, payload: dict[str, Any]
) -> None:
    """The client→daemon half of the RPC (see ``protocol``).

    ``hello`` registers the socket as THE client of the tab it names (one at a
    time per tab — a later hello for the same tab takes over); a hello naming
    a tab this run does not have is ignored. ``rpc_result`` / ``rpc_error``
    resolve a pending call of the socket's tab; ``url`` keeps that tab's
    ``current_url`` following its guest's navigations, which is what a
    capture's ``ctx.url`` and the run's status line read. Anything from a
    socket that has not said hello is ignored."""
    kind = payload.get("kind")
    if kind == "hello":
        tab = tabs.tabs.get(str(payload.get("tab") or ""))
        if tab is None:
            gen.log(f"ignored a hello for unknown tab {payload.get('tab')!r}")
            return
        previous = tabs.tabs.get(tabs.served.get(ws, ""))
        if previous is not None and previous is not tab and previous.client is ws:
            _detach_client(previous)
        tabs.served[ws] = tab.tab
        tab.client = ws
        tab.client_kind = str(payload.get("client") or "")
        tab.client_caps = _normalise_caps(payload.get("caps"))
        gen.log(
            f"client attached to {tab.tab}: {tab.client_kind or 'unknown'} "
            f"(guest_capture="
            f"{bool(tab.client_caps.get('guest_capture'))})"
        )
        return
    tab = tabs.tabs.get(tabs.served.get(ws, ""))
    if tab is None:
        return
    if kind in ("rpc_result", "rpc_error"):
        rpc_id = str(payload.get("id") or "")
        fut = tab.rpc_pending.pop(rpc_id, None)
        if fut is None or fut.done():
            return  # already timed out, or an id we never sent
        if kind == "rpc_error":
            fut.set_exception(
                CaptureError(str(payload.get("message") or "the client refused the call"))
            )
        else:
            fut.set_result(payload.get("value"))
    elif kind == "url":
        new_url = str(payload.get("url") or "").strip()
        if new_url:
            tab.current_url = new_url


def _detach_client(tab: _TabState) -> None:
    """Forget the tab's attached client and fail every in-flight RPC."""
    tab.client = None
    tab.client_kind = ""
    tab.client_caps = {}
    pending = list(tab.rpc_pending.items())
    tab.rpc_pending.clear()
    for _rpc_id, fut in pending:
        if not fut.done():
            fut.set_exception(
                CaptureUnavailable("the client guest view disconnected mid-call")
            )


async def _start_aiohttp(sock: socket.socket, tabs: _Tabs) -> web.AppRunner:
    """The session's loopback server: the control WS and nothing else. There is
    no page to reverse-proxy and no frames to stream, so every other path 404s
    on purpose. ``sock`` is already bound (:func:`_bind_free_port`); the site
    listens on it and ``runner.cleanup()`` closes it."""
    port = sock.getsockname()[1]
    try:
        app = web.Application()
        app.router.add_get(_CONTROL_WS_PATH, _ws_handler_factory(tabs))
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.SockSite(runner, sock)
        await site.start()
    except BaseException:
        sock.close()
        raise
    gen.log(f"session control server listening on 127.0.0.1:{port}")
    return runner


# ---------------------------------------------------------------------------
# Capture bookkeeping
# ---------------------------------------------------------------------------


# Everything a host can't put in a folder name. Dots and hyphens survive, so
# ``docs.example.com`` stays readable at a glance.
_CAPTURE_DIR_UNSAFE = re.compile(r"[^a-z0-9.-]+")


def _capture_dir_name(url: str) -> str:
    """``<host>-<YYYYMMDD-HHMMSS>`` — the folder one capture writes into.

    The host is the current page's, port stripped and lowercased (the same
    host the capture-script dispatch reads, so a folder always names the site
    whose script produced it); a session with no page yet is ``page``. Local
    time, because the name is read against the clock the button was pressed
    under."""
    try:
        host = (urlsplit(url or "").hostname or "").lower()
    except ValueError:
        host = ""
    slug = _CAPTURE_DIR_UNSAFE.sub("-", host).strip("-.") or "page"
    return f"{slug}-{time.strftime('%Y%m%d-%H%M%S')}"


def _claim_capture_dir(url: str, used: set[str]) -> str:
    """The folder THIS press writes into: :func:`_capture_dir_name`, made
    unique against the ones this session already took.

    The timestamp is one second wide, so two presses on the same host inside
    the same second name the same folder — and the second would silently
    overwrite the first. Collisions keep the readable base and take a ``-2``,
    ``-3``, … suffix. ``used`` is the session's own set (one run, one set) and
    is updated in place with whatever this call claims."""
    base = _capture_dir_name(url)
    name = base
    nth = 2
    while name in used:
        name = f"{base}-{nth}"
        nth += 1
    used.add(name)
    return name


async def _end_session(tabs: _Tabs, saved_captures: int, *, failed: bool = False) -> None:
    """Close the cell out: delete every tab file and flush.

    A tab file is a live handle (a control-WS base that dies with this run),
    so a session that is over leaves none however it ended — each goes with
    :func:`gen.delete_ui_file` and the flush carries the deletions. The
    captures stay.

    ``saved_captures`` counts the presses that actually wrote a file, not the
    presses: the closing line may only claim captures the cell can show.
    ``failed`` words the closing line for a run an exception escaped, which is
    not the same event as Stop and must not read like it."""
    for tab in tabs.tabs.values():
        gen.delete_ui_file(_tab_file(tab.tab))
    captures = (
        "no captures" if not saved_captures
        else f"{saved_captures} capture{'' if saved_captures == 1 else 's'}"
    )
    gen.log(f"{'Session failed' if failed else 'Session ended'} — {captures}")
    await gen.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bind_free_port(start: int = 9100) -> socket.socket:
    """A socket already BOUND to the first free loopback port from ``start``,
    kept open so nothing can take the port before the server listens on it."""
    port = start
    while port < start + 1000:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", port))
            return s
        except OSError:
            s.close()
            port += 1
    raise RuntimeError(f"No free port found in range {start}-{start + 1000}")
