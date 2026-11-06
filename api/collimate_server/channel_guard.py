"""The one choke point for writes to a channel in a state that refuses them
(plan 1105 S6, S7): a channel being moved, an archived channel.

:func:`channel_guard` is the app's one global dependency
(``FastAPI(dependencies=[Depends(channel_guard)])``). FastAPI runs it once
the route is matched and before it validates the route's parameters and
body, so a refusal comes before any 422. It covers every route whose
method is not GET, HEAD or OPTIONS, and every websocket route:

- the channel is the matched route's ``channel`` or ``channel_id`` path
  parameter, or the active channel for :data:`ACTIVE_CHANNEL_ROUTES`;
- a route with neither (accounts, the user catalog, storage, the system
  routes, a run's chat and terminal sockets, whose runs cannot live on a
  refusing channel) targets no channel and passes.

Each refusing state is one :class:`RefusingState`: its exception, the
predicate that puts a channel in it, and the routes it still allows. The
first state that holds and does not allow the route refuses it: an HTTP
route answers 409 ``{"detail": {"code", "message", "channel_id", "name"}}``
(:func:`refused_response`), a websocket is accepted and closed with 4409
and the code as its reason. The states, in order:

- ``channel_moving`` (S7, :mod:`collimate_server.channel_move` marks it with
  :func:`mark_moving`): the folder is being copied to another location.
  Reads go on from the source until the switch; every write is refused,
  whatever it is.
- ``channel_archived`` (S6, :mod:`collimate_server.archive`): read-only
  until unarchived, but for :data:`ARCHIVE_SAFE`.

:func:`require_writable` is the domain backstop: ``cell_write_scope`` and
run creation call it, so a write that slips past the guard still fails with
the same exception and the same answer.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar

from fastapi import WebSocket, WebSocketException
from fastapi.responses import JSONResponse
from starlette.requests import HTTPConnection

READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Routes that act on the active channel and name none.
ACTIVE_CHANNEL_ROUTES = frozenset({
    "/api/runners",
    "/api/runners/{runner_id}",
    "/api/renderer_preferences",
    "/api/renderer_availability/{renderer_id}",
})

# What an archived channel still allows (D9): actions that read the channel
# and write elsewhere, or act on the channel as a whole.
ARCHIVE_SAFE = frozenset({
    # Archive again answers the row; unarchive is the way back (D11).
    ("POST", "/api/channels/{channel_id}/archive"),
    ("POST", "/api/channels/{channel_id}/unarchive"),
    # Delete removes the folder; move copies it whole; rename rewrites only
    # channel.json.
    ("DELETE", "/api/channels/{channel_id}"),
    ("POST", "/api/channels/{channel}/move"),
    ("PUT", "/api/channels/{channel_id}/rename"),
    # Select opens it, read-only (the Archived tab's Open).
    ("POST", "/api/channels/{channel_id}/select"),
    # Export reads it (the manifest and the zip are GETs); this ends one.
    ("DELETE", "/api/channels/{channel}/export/{token}"),
    # Copy to channel writes the target (refused itself when archived).
    ("POST", "/api/channels/{channel}/capabilities/cells/{cell_id}/copy/review"),
    ("POST", "/api/channels/{channel}/capabilities/cells/{cell_id}/copy"),
    # Publish to catalog writes the user catalog in app.db.
    ("POST", "/api/channels/{channel}/capabilities/cells/{cell_id}/publish/review"),
    ("POST", "/api/channels/{channel}/capabilities/cells/{cell_id}/publish"),
})


class ChannelRefused(Exception):
    """A write to a channel in a refusing state."""

    code: ClassVar[str]
    message: ClassVar[str]

    def __init__(self, channel_id: str, name: str):
        super().__init__(f"{self.message} ({channel_id})")
        self.channel_id = channel_id
        self.name = name

    def detail(self) -> dict:
        return {"code": self.code, "message": self.message, "channel_id": self.channel_id, "name": self.name}


class ChannelMoving(ChannelRefused):
    code = "channel_moving"
    message = "The channel is being moved to another location; it opens read-only until the move is done."


class ChannelArchived(ChannelRefused):
    code = "channel_archived"
    message = "This channel is archived and read-only. Unarchive it to change it."


# --- the moving state (channel_move) --------------------------------------------------------------

_moving: set[str] = set()
_moving_guard = threading.Lock()


def mark_moving(channel_id: str) -> bool:
    """Mark the channel moving; False when it already was."""
    with _moving_guard:
        if channel_id in _moving:
            return False
        _moving.add(channel_id)
        return True


def clear_moving(channel_id: str) -> None:
    with _moving_guard:
        _moving.discard(channel_id)


def is_moving(channel_id: str) -> bool:
    return channel_id in _moving


def _archived(channel_id: str) -> bool:
    from collimate_server.archive import is_archived
    return is_archived(channel_id)


# --- the states -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class RefusingState:
    refusal: type[ChannelRefused]
    holds: Callable[[str], bool]
    allows: frozenset[tuple[str, str]]


STATES: tuple[RefusingState, ...] = (
    RefusingState(ChannelMoving, is_moving, frozenset()),
    RefusingState(ChannelArchived, _archived, ARCHIVE_SAFE),
)


def refusal(channel_id, route: tuple[str, str] | None = None) -> ChannelRefused | None:
    """The refusal a write to the channel meets through ``route`` (a method
    and a path template; None for a domain write, which no state allows),
    or None. None too for an id with no channel folder: the route answers
    for that itself."""
    import collimate_server.channel_paths as channel_paths
    try:
        found = channel_paths.home(channel_id)
    except ValueError:
        return None
    if found is None:
        return None
    for refusing in STATES:
        if refusing.holds(found.channel_id) and (route is None or route not in refusing.allows):
            return refusing.refusal(found.channel_id, found.manifest.get("name", ""))
    return None


def require_writable(channel_id) -> None:
    """The backstop: raise the channel's refusal for any write to it."""
    refused = refusal(channel_id)
    if refused is not None:
        raise refused


def _target(connection: HTTPConnection, path: str):
    params = connection.path_params
    for name in ("channel", "channel_id"):
        if name in params:
            return params[name]
    if path in ACTIVE_CHANNEL_ROUTES:
        from collimate_server.state import state
        return state.active_channel_id
    return None


async def channel_guard(connection: HTTPConnection) -> None:
    """The global dependency: refuse a write to a channel in a refusing state."""
    websocket = isinstance(connection, WebSocket)
    method = "WS" if websocket else connection.scope["method"]
    if method in READ_METHODS:
        return
    path = getattr(connection.scope.get("route"), "path", "")
    channel = _target(connection, path)
    if channel is None:
        return
    refused = refusal(channel, (method, path))
    if refused is None:
        return
    if websocket:
        # Accepted first, so the close code reaches the client (a close
        # before accept is a bare HTTP 403).
        await connection.accept()
        raise WebSocketException(code=4409, reason=refused.code)
    raise refused


def refused_response(_request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ChannelRefused)
    return JSONResponse(status_code=409, content={"detail": exc.detail()})
