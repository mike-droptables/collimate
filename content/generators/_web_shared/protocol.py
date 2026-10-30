"""The renderer-facing contract of a web session — one place, nothing else.

Everything the cell's renderer sees, and nothing about how the session is
produced:

  * :data:`_CONTROL_WS_PATH`, the one reserved endpoint the daemon mounts
    (everything under ``/__collimate/`` is daemon-internal by convention);
  * :class:`_TabState`, the state the daemon keeps for each of the run's tabs
    — its file, the connected client that said ``hello`` for it and that
    client's capabilities, the tab's current URL and its pending RPCs;
  * the control-WebSocket wire shapes: a ``hello`` naming the tab, and the
    symmetric ``rpc`` down / ``rpc_result`` / ``rpc_error`` / ``url`` up pair
    the daemon drives each tab's native guest view with;
  * :func:`_tab_artifact`, the ``tab-<n>.webview`` file in the run folder
    that tells the renderer how to attach one tab — the single source of
    truth for its fields — and :data:`DEVICES`, what a tab can present as.

There is one way to run a session: the client hosts each tab's page in its
own native guest view and the daemon reaches it over the RPC below. No
browser process, no screencast, no reverse proxy — which is why this contract
is small enough to read in one sitting.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlparse

from aiohttp import web


# Daemon-internal endpoints live under a reserved ``/__collimate/`` prefix so
# they can never collide with anything else the session grows later. Today
# there is exactly one: the renderer channel that carries the RPC. Every tab
# of the run shares it; the ``hello`` names the tab a socket serves.
_CONTROL_WS_PATH = "/__collimate/ws"


@dataclass
class _TabState:
    """One browser tab of the run: one ``tab-<n>.webview`` file, one guest
    view in the client, one RPC peer."""
    # The tab's id, ``tab-<n>`` — the artifact's ``tab``, what the client's
    # ``hello`` names, and the file's stem.
    tab: str
    # The tab file's cell-relative path (``_runs/<label>/tab-<n>.webview``) —
    # what a press on the tab's toolbar names as its action's ``source``.
    path: str
    # The URL the tab opened on — the artifact's ``url``, kept as written so a
    # rewrite of the file (a device switch) does not move the renderer's
    # mount.
    url: str = ""
    # What the tab presents as (a :data:`DEVICES` key) — the artifact's
    # ``device``, switched by the tab's toolbar toggle.
    device: str = "desktop"
    # Main-frame URL, kept current by the client's ``url`` messages so a
    # capture's ``ctx.url`` and the run's status line follow the guest's
    # navigations.
    current_url: str = ""
    # The control-WS client that said ``hello`` for this tab and is therefore
    # driveable over the RPC. One at a time on purpose ("last attach wins"):
    # a second view of the same tab file renders its own guest, but only the
    # most recent one answers RPCs.
    client: Optional[web.WebSocketResponse] = None
    # That client's capabilities from its ``hello``, snake_cased
    # (``native_guests`` / ``guest_capture`` / …) by
    # :func:`_normalise_caps` so this reads the same as ``gen.client_caps()``.
    client_caps: dict[str, Any] = field(default_factory=dict)
    # Its client kind ("electron"), verbatim from the hello.
    client_kind: str = ""
    # In-flight RPCs, ``id`` → future. Filled by the caller
    # (:class:`~.capture.CaptureContext`), resolved by the daemon's WS router
    # when the matching ``rpc_result`` / ``rpc_error`` comes back, and failed
    # wholesale when the client disconnects so nothing hangs on a dead socket.
    rpc_pending: dict[str, asyncio.Future] = field(default_factory=dict)
    rpc_seq: int = 0


# ---------------------------------------------------------------------------
# The RPC — daemon → client guest view, over the control WS
# ---------------------------------------------------------------------------
#
# A minimal symmetric JSON-RPC over the control socket:
#
#   down  {"kind":"rpc","id":"<str>","method":<method>,"params":{...}}
#   up    {"kind":"rpc_result","id":"<str>","value":<json>}
#         {"kind":"rpc_error","id":"<str>","message":"<str>"}
#
# plus two unsolicited client→daemon messages:
#
#   up    {"kind":"hello","tab":"tab-<n>","client":"electron","caps":{...}}
#                                                   (on connect: the socket
#                                                    now serves that tab)
#   up    {"kind":"url","url":"<str>"}              (the tab's guest navigated)
#
# A socket is one tab's peer from its ``hello`` on; everything before it is
# ignored, and so is a hello naming a tab this run does not have.
#
# Each method maps 1:1 onto the shell's ``window.collimateDesktop.guest``
# surface, so the renderer's handler is a switch with no logic in it:
#
#   evaluate   {"js": "<complete JS expression>"} → the expression's value
#              (guest.evaluate; a promise value is awaited before it is sent)
#   html       {}                                 → documentElement.outerHTML
#   screenshot {}                                 → base64 PNG, no data: prefix
#   url        {}                                 → the guest's current URL
#   navigate   {"url": "<http(s) URL>"}           → null when accepted
_RPC_METHODS = ("evaluate", "html", "screenshot", "url", "navigate")


# The evaluate calling convention, in one place so the capture contexts and the
# capture scripts cannot drift. A capture callback passes a zero-arg JS
# FUNCTION SOURCE (``() => {...}``, the shape every capture script already
# uses); the daemon sends the wrapped INVOCATION ``(<source>)()``, and the
# shell's ``executeJavaScript`` awaits a promise value by construction. So one
# script string means one value, wherever it is written down.
def _evaluate_expression(js_function: str) -> str:
    """``(<zero-arg function source>)()`` — the JS the page actually runs."""
    return f"({js_function})()"


def _rpc_request(rpc_id: str, method: str, params: Optional[dict] = None) -> dict[str, Any]:
    """One ``kind:"rpc"`` frame. The only place the down shape is built — and
    the only gate on the method list, so a typo fails here rather than as a
    silent ``rpc_error`` from a renderer that has never heard of it."""
    if method not in _RPC_METHODS:
        raise ValueError(f"unknown rpc method {method!r} (expected one of {_RPC_METHODS})")
    return {"kind": "rpc", "id": rpc_id, "method": method, "params": params or {}}


def _next_rpc_id(state: _TabState) -> str:
    state.rpc_seq += 1
    return f"rpc-{state.rpc_seq}"


_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _normalise_caps(caps: Any) -> dict[str, Any]:
    """A hello's ``caps`` map in the snake_case shape ``gen.client_caps()``
    uses. The wire is camelCase — there is exactly one producer, the renderer,
    and it forwards the shell's contextBridge map verbatim
    (``nativeGuests`` / ``guestCapture``), exactly as the app's
    ``/api/system/client`` handshake receives it. Unknown names are kept
    rather than dropped."""
    if not isinstance(caps, dict):
        return {}
    return {_CAMEL_BOUNDARY.sub("_", str(k)).lower(): v for k, v in caps.items()}


async def _send_client_json(state: _TabState, payload: dict[str, Any]) -> bool:
    """Send one payload to the tab's attached client (the socket that said
    ``hello`` for it). False when there is nobody to send to or the socket is
    gone — the caller turns that into a :class:`~.capture.CaptureUnavailable`."""
    ws = state.client
    if ws is None or ws.closed:
        return False
    try:
        await ws.send_str(json.dumps(payload))
    except Exception:
        return False
    return True


# ---------------------------------------------------------------------------
# The tab artifact
# ---------------------------------------------------------------------------


# The session-partition prefix the desktop shell will manage — mirrors
# ``PARTITION_PREFIX`` in the shell's bridge contract (a guest whose
# partition doesn't start with it is refused at attach). ``persist:`` is part
# of it on purpose: guest state is disk-backed, so a session's login survives
# a restart.
_PARTITION_PREFIX = "persist:collimate-"

# A tab's file: ``tab-<n>.webview`` in the run folder, ``n`` from 1. The
# ``.webview`` extension is what the webview renderer claims, and the one UI
# file type the web generator's manifest declares.
_TAB_SUFFIX = ".webview"


def _tab_id(n: int) -> str:
    """``tab-<n>`` — a tab's id and its file's stem."""
    return f"tab-{int(n)}"


def _tab_file(tab: str) -> str:
    """The folder-relative name of tab ``tab``'s file (``gen.update_ui_file`` name)."""
    return tab + _TAB_SUFFIX


def _partition_for(profile_name: str) -> str:
    """``persist:collimate-<profile>`` — the guest's session partition. The
    profile setting names the login bucket: two cells on the same profile name
    share cookies, and a different name is an independent login that can run at
    the same time. Every tab of a run shares it, as a browser's tabs share one
    profile."""
    slug = re.sub(r"[^a-z0-9-]+", "-", (profile_name or "").strip().lower()).strip("-")
    return _PARTITION_PREFIX + (slug or "default")


# What a tab can present as — the ``device`` setting's options and the
# toolbar toggle's. The value is the viewport the renderer letterboxes the
# guest to (``None``: the cell's full width); the user agent and the mobile
# emulation are the desktop shell's to compose from its own Chrome version,
# so nothing here names a UA string.
DEVICES: dict[str, Optional[dict[str, int]]] = {
    "desktop": None,
    "phone": {"width": 412},
    "tablet": {"width": 800},
}


def _device_of(device: Any) -> str:
    """``device`` when it is one of :data:`DEVICES`, else ``"desktop"``."""
    name = str(device or "").strip().lower()
    return name if name in DEVICES else "desktop"


def _tab_artifact(
    *,
    tab: str,
    url: str,
    proxy_base: str,
    partition: str,
    capture_label: str | None,
    device: str,
) -> dict[str, Any]:
    """Build one tab's ``tab-<n>.webview`` payload the renderer reads — v4,
    and the one construction site for it::

        {"v": 4,
         "tab": "tab-1",
         "url": "https://example.com/",   # "" when no site was named
         "device": "phone",               # desktop | phone | tablet
         "viewport": {"width": 412},      # null for desktop
         "control": {"wsBase": "/proxy/<ch>/<run>/<port>/"},
         "requires": ["nativeGuests"],
         "native": {"partition": "persist:collimate-default",
                    "device": "phone"},
         "buttons": [{"id": "capture", "label": "Capture"},
                     {"id": "new_tab", "label": "New tab"}]}

    ``tab`` names the tab: the renderer says it in its ``hello``, so the
    daemon knows which tab's guest a control socket serves, and scopes its
    guest's session key by it (``<wsBase>#<tab>``). ``control.wsBase`` is the
    host-relative base the renderer resolves the control WebSocket off
    (``<wsBase>__collimate/ws``) — one socket endpoint for every tab of the
    run. ``requires`` is unconditional: a fully live tab is hosted by the
    client's own native guest view. It gates the renderer's MODE, not whether
    anything renders — a client without a native view (a browser tab) falls
    back to a best-effort iframe, with a working address bar and a banner
    saying what is degraded, and refuses the capture verbs with an
    ``rpc_error`` rather than hanging on them (Capture still shows there, and
    every press fails). ``native`` stays nested because it is the shell's
    ``mountGuest`` options grouping, passed through as one object. ``url`` may
    be empty — the renderer reads that as "open the guest blank and leave the
    address bar empty", which is what a tab opened without a site is — the
    renderer then shows an Enter page form in the canvas.

    ``device`` is what the tab presents as (:data:`DEVICES`; anything else
    is ``desktop``). ``viewport`` is its width, which the renderer letterboxes
    the guest to on the cell, and ``native.device`` is what the shell composes
    the guest's user agent and mobile emulation from. A tab's toolbar toggle
    sends a ``device`` action and the daemon rewrites this file with the new
    one; the renderer switches the same guest in place.

    ``buttons`` is the tab's toolbar. ``capture_label`` is the label of its
    Capture, first, when the run has a capture callback, and ``None`` when it
    has none; New tab follows, always. A press comes back as a ``capture`` or
    ``new_tab`` action whose source is this tab's file: the daemon captures
    this tab's page, or opens the next tab."""
    buttons = [] if capture_label is None else [{"id": "capture", "label": capture_label}]
    buttons.append({"id": "new_tab", "label": "New tab"})
    device = _device_of(device)
    viewport = DEVICES[device]
    return {
        "v": 4,
        "tab": tab,
        "url": url,
        "device": device,
        "viewport": dict(viewport) if viewport is not None else None,
        "control": {"wsBase": proxy_base},
        "requires": ["nativeGuests"],
        "native": {"partition": partition, "device": device},
        "buttons": buttons,
    }


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------


def _normalise_url(raw: str) -> str:
    """The session's starting URL, or ``""`` when none was named.

    The site is optional: blank in, blank out — the session starts on a blank
    page and the address bar is how the user gets somewhere. Anything
    non-blank is repaired and validated here, so a typo fails before the
    guest ever sees it."""
    s = (raw or "").strip()
    if not s:
        return ""
    bad = RuntimeError(
        f"{raw!r} doesn't look like a URL — try e.g. https://example.com"
    )
    # Repair hand-typed scheme mangling (https:/site, HTTP:site) instead
    # of just prefix-checking — urlparse would otherwise read "https" as
    # the hostname.
    m = re.match(r"^(https?):/{0,2}(.+)$", s, re.IGNORECASE)
    if m:
        s = f"{m.group(1).lower()}://{m.group(2)}"
    elif re.match(r"^[a-z][a-z0-9+.-]*://", s, re.IGNORECASE):
        # An explicit non-http(s) scheme (ftp://, file://) would otherwise
        # become https://ftp://... with "ftp" as the hostname.
        raise bad
    else:
        s = "https://" + s
    parsed = urlparse(s)
    try:
        # about:blank, javascript:..., host:notaport all land here.
        parsed.port
    except ValueError:
        raise bad from None
    host = parsed.hostname
    if not host or host in ("http", "https"):
        raise bad
    return s
