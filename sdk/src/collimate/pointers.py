# SPDX-License-Identifier: MIT
"""Canonical pointer-file contracts (shared by generators and renderers) —
public.

A *pointer file* is a small file whose **contents point at a live resource**
a renderer opens at mount time, rather than the artifact to display. This
module pins their shape in one place so the generator side (which writes the
file) and the renderer side (which reads it) cannot drift.

- ``.terminal`` — JSON ``{terminal_id, ws_path, token, label}``. The
  ``terminal`` renderer (xterm.js) opens a WebSocket PTY against it. Build it
  from a :func:`gen.start_terminal` result with :func:`terminal_pointer`.
- ``.website`` — JSON ``{url, directUrl, loadingCover, buttons}`` naming a port
  this run serves. The ``website`` renderer opens it in a native guest view on
  the desktop (``directUrl``) or a sandboxed iframe (``url``) in a browser.
  Build it with :func:`website_pointer` alongside :func:`gen.serve_port`.
- ``.chat`` — JSON describing a live chat session for the **assistant-ui**
  renderer (see :func:`chat_session_pointer`). It carries the WebSocket coords
  and the thread list. Each thread's messages
  live in its own open-format transcript file (``<threads_dir>/<id>.json``;
  :mod:`collimate.transcript`) so a closed chat still renders from disk.
  :func:`gen.serve_chat` writes and reveals this automatically.

**Starting placeholders.** When the host accepts a run whose manifest
``opens`` a pointer file (``.chat``, ``.terminal``, ``.webview``,
``.website``), it writes :func:`starting_pointer` there before the run's
process exists, so the surface shows at once. A renderer reads it
(:func:`is_starting_pointer`) as "starting": it shows the surface, accepts
input and queues it until the run writes the real pointer over it.

``.webview`` — another pointer type — is owned by
:mod:`generators._web_shared`, not this module: it is a web-session
contract between that package's generators and their renderer, not a
generator/renderer boundary the SDK pins.

**Run folders.** Every pointer a run serves lives in its run folder,
``_runs/<label>/`` (``gen.run_info().folder``), written with ``gen.update_ui_file``
— the one way a run writes its folder, for the suffixes its manifest declares
under ``ui_files``. Every file inside a live run's folder is that run's
locked file: other runs read it but never write, move or delete it, and the
user cannot delete, move or copy it out of the cell. The host writes the run
card (``_runs/<label>/<label>.run``) before the run starts and clears the
folder when it stops. :func:`is_run_path` and :func:`run_label` read a path
the same way the host does.

Example (the manifest declares ``ui_files: [".terminal"]``)::

    import json, gen
    from collimate.pointers import terminal_pointer

    sess = await gen.start_terminal(container_id, ["bash"], cwd="/work")
    pointer = gen.update_ui_file("shell.terminal",
                          json.dumps(terminal_pointer(sess, label="Shell")))
    await gen.commit(name="Container")
    await gen.reveal_file(pointer)
"""
from __future__ import annotations

import posixpath
import re
from typing import Optional

TERMINAL_EXT = ".terminal"

# The required key set of a .terminal pointer file (renderer reads all
# four); an optional "buttons" key ([{id, label, title?, confirm?}, ...]) adds
# toolbar buttons whose clicks reach the generator via gen.actions() /
# gen.wait_for_action() — ``title`` is the button's hover text, ``confirm``
# the question the renderer asks before sending the click.
TERMINAL_KEYS = ("terminal_id", "ws_path", "token", "label")

# The exact key set of a .website pointer file (always all four).
WEBSITE_KEYS = ("url", "directUrl", "loadingCover", "buttons")

# The exact key set of a .chat session pointer file.
CHAT_KEYS = (
    "chat_id", "ws_path", "token", "label",
    "threads_dir", "active_thread", "threads",
)


def terminal_pointer(
    session: dict,
    *,
    label: str = "Terminal",
    buttons: Optional[list] = None,
) -> dict:
    """Build a ``.terminal`` pointer dict from a :func:`gen.start_terminal` result.

    ``buttons`` — an optional list of ``{"id", "label", "title"?,
    "confirm"?}`` dicts — adds action buttons to the terminal renderer's
    toolbar (``title`` its hover text, ``confirm`` a question asked before the
    click is sent); clicks arrive in the generator via ``gen.actions()`` /
    ``gen.wait_for_action()``. Most generators never build this dict
    directly: ``gen.serve_terminal`` writes and reveals it.
    """
    out = {
        "terminal_id": session.get("terminal_id", ""),
        "ws_path": session.get("ws_path", ""),
        "token": session.get("token", ""),
        "label": label,
    }
    if buttons:
        out["buttons"] = [_terminal_button(b) for b in buttons]
    return out


def _terminal_button(b: dict) -> dict:
    out = {"id": str(b["id"]), "label": str(b["label"])}
    for key in ("title", "confirm"):
        if b.get(key):
            out[key] = str(b[key])
    return out


def website_pointer(
    port: int,
    *,
    loading_cover: bool = False,
    buttons: Optional[list] = None,
) -> dict:
    """Build a ``.website`` pointer for a port this run serves.

    ``port`` is the **host** port passed to :func:`gen.serve_port` (for a
    container, the loopback port it was forwarded to — e.g.
    ``Endpoint.port`` from ``env.expose_port``), which the pointer names two
    ways: ``url`` is the app-origin proxy path browser clients iframe
    (:func:`gen.proxy_url`); ``directUrl`` is the daemon-loopback URL a
    native guest view loads without going through the app. Both must stay
    in sync, so never hand-build either one.

    ``loading_cover`` holds a theme-coloured panel over the iframe until it
    fires ``load`` — set it for an app that boots on a white background (e.g.
    code-server's workbench) so it never flashes before its own theme paints.
    ``buttons`` — a list of ``{"id", "label"}`` dicts — adds toolbar buttons
    whose clicks reach the generator via ``gen.actions()`` /
    ``gen.wait_for_action()``.
    """
    from collimate.native import proxy_url

    return {
        "url": proxy_url(port),
        "directUrl": f"http://127.0.0.1:{int(port)}/",
        "loadingCover": bool(loading_cover),
        "buttons": [
            {"id": str(b["id"]), "label": str(b["label"])} for b in (buttons or [])
        ],
    }


def chat_session_pointer(
    session: dict,
    *,
    threads_dir: str,
    label: str = "Chat",
    active_thread: str = "main",
    threads: Optional[list] = None,
) -> dict:
    """Build a ``.chat`` session pointer for the assistant-ui renderer.

    ``session`` is the ``{chat_id, ws_path, token}`` of the run's chat
    session (the native streaming channel). ``threads_dir`` is the folder the
    transcripts live in, inside the run folder. ``threads``
    is the list of ``{id, title, file}`` thread entries (defaults to a single
    ``main``); ``active_thread`` is the open one. Each thread's messages are an
    open-format transcript at ``<threads_dir>/<id>.json``.
    """
    if threads is None:
        threads = [{
            "id": active_thread,
            "title": "Main",
            "file": f"{threads_dir}/{active_thread}.json",
        }]
    return {
        "chat_id": session.get("chat_id", ""),
        "ws_path": session.get("ws_path", ""),
        "token": session.get("token", ""),
        "label": label,
        "threads_dir": threads_dir,
        "active_thread": active_thread,
        "threads": threads,
    }


def is_terminal_pointer(name: str) -> bool:
    """True when ``name`` is a ``.terminal`` pointer file."""
    return name.endswith(TERMINAL_EXT)


# The pointer suffixes the host writes a starting placeholder for.
POINTER_EXTS = (".chat", ".terminal", ".webview", ".website")


def starting_pointer(label: str) -> dict:
    """The placeholder the host writes to a pointer file a run ``opens``
    before the run's process exists: ``{"starting": true, "label": <runner
    name>}``. The run replaces it with its real pointer."""
    return {"starting": True, "label": str(label or "")}


def is_starting_pointer(obj) -> bool:
    """True when ``obj`` (a parsed pointer file) is a host starting
    placeholder rather than a live pointer."""
    return isinstance(obj, dict) and obj.get("starting") is True






# The cell folder that holds every run folder (``_runs/<label>/``).
RUNS_PREFIX = "_runs"
# The run card's extension: ``_runs/<label>/<label>.run``, written by the host.
RUN_CARD_EXT = ".run"
LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9-]*-[0-9a-f]{8}$")


def norm_path(path: str) -> str:
    """A cell-relative path in the one spelling the host compares: forward
    slashes, no leading ``/`` or ``./``."""
    norm = posixpath.normpath((path or "").replace("\\", "/").lstrip("/")).lstrip("/")
    return "" if norm == "." else norm


def is_run_path(path: str) -> bool:
    """True for a cell-relative path under ``_runs/`` — a run folder or a
    file in one (the folder itself included)."""
    p = norm_path(path)
    return p == RUNS_PREFIX or p.startswith(RUNS_PREFIX + "/")


def run_label(path: str) -> str:
    """The label of the run folder ``path`` lies in — ``"agent--x-3f9a2c1d"``
    for ``"_runs/agent--x-3f9a2c1d/chat/conversation.chat"`` — or ``""`` when the
    path is not inside a run folder."""
    parts = norm_path(path).split("/")
    if len(parts) >= 2 and parts[0] == RUNS_PREFIX and parts[1]:
        return parts[1]
    return ""


def run_card_path(label: str) -> str:
    """The run card of the run labelled ``label``:
    ``_runs/<label>/<label>.run``. The host writes and removes it; nobody
    else does."""
    return f"{RUNS_PREFIX}/{label}/{label}{RUN_CARD_EXT}"


__all__ = [
    "CHAT_KEYS",
    "LABEL_RE",
    "POINTER_EXTS",
    "RUNS_PREFIX",
    "RUN_CARD_EXT",
    "TERMINAL_EXT",
    "TERMINAL_KEYS",
    "WEBSITE_KEYS",
    "chat_session_pointer",
    "is_run_path",
    "is_starting_pointer",
    "is_terminal_pointer",
    "norm_path",
    "run_card_path",
    "run_label",
    "starting_pointer",
    "terminal_pointer",
    "website_pointer",
]
