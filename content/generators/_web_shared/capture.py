"""The capture context — the handle a capture callback drives the page with.

:class:`CaptureContext` is the contract a capture callback sees: which page it
is looking at, which capture this is, three async accessors
(``evaluate`` / ``html`` / ``screenshot_png``) and one writer (:meth:`save`).
It is backed by the client's own native guest view: there is no browser
process to talk to, so each accessor is one RPC over the control WebSocket.

A capture writes into the RUNNING cell, never a new one: every
:meth:`~CaptureContext.save` lands under this capture's own
``<host>-<timestamp>`` folder, and the daemon flushes the folder when the
callback finishes, fails, or is cancelled by Stop.

The name is the contract — the built-in captures, the per-host scripts a runner
stages, and the authoring docs all annotate against ``CaptureContext`` — and the
one shared :func:`~.protocol._evaluate_expression` convention is what makes a
script's ``ctx.evaluate(...)`` string mean the same thing wherever it is
written down.
"""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
from typing import Any, Optional

import gen

from .protocol import (
    _evaluate_expression,
    _next_rpc_id,
    _rpc_request,
    _send_client_json,
    _TabState,
)


# How long one page round-trip may take before the capture gives up. Sized for
# the slowest thing the contract does: a full-page screenshot of a heavy page.
_RPC_TIMEOUT = 60.0


class CaptureError(RuntimeError):
    """A capture round-trip failed (the page threw, or the client did)."""


class CaptureUnavailable(CaptureError):
    """There is no client able to serve a capture right now.

    A tab's page lives in the client's guest view, so a capture needs a client
    attached to the control WebSocket for that tab AND a client whose shell can
    read that view back (``guestCapture``). Raised — rather than hanging on a
    future nobody will resolve — when either is missing."""


@dataclass
class CaptureContext:
    """Handle passed to capture callbacks, backed by one tab's native guest
    view in the client. ``evaluate`` is the main attachment point — pass a
    zero-arg JS function source (``() => {...}``) and get its JSON-safe return
    value back; :meth:`save` is the way out, into this capture's folder in the
    running cell.

    Each accessor is one RPC over the tab's control WebSocket (see
    ``protocol``) that the renderer answers by calling
    ``window.collimateDesktop.guest``. The future for each call lives in
    ``state.rpc_pending`` keyed by the request id; the daemon's WS router
    resolves it when the matching ``rpc_result`` / ``rpc_error`` arrives, and
    fails every pending one if the client disconnects.

    ``url`` is a snapshot taken when the context is built: the daemon keeps
    ``state.current_url`` fed by the client's ``url`` messages and pulls the
    guest's authoritative URL over :meth:`current_url` before each capture."""
    url: str
    capture_count: int  # 1-indexed; bumps before each on_capture call.
    state: _TabState
    # This capture's folder in the running cell's files —
    # ``<host>-<YYYYMMDD-HHMMSS>``, named by the daemon once the guest's URL
    # is authoritative (so the folder and the script dispatch agree on the
    # host). Every :meth:`save` goes under it.
    capture_dir: str = ""
    timeout: float = _RPC_TIMEOUT
    # Cell-relative paths this capture has staged, in save order.
    saved: list[str] = field(default_factory=list)

    # -- the RPC ----------------------------------------------------------

    def _require_client(self) -> None:
        state = self.state
        if state is None or state.client is None or state.client.closed:
            raise CaptureUnavailable(
                "no client is attached to this tab — open it in the desktop "
                "app (its guest view is the page) and try the capture again"
            )
        if not state.client_caps.get("guest_capture"):
            raise CaptureUnavailable(
                "the attached client cannot read its guest view back "
                "(guestCapture is off), so this tab can't be captured"
            )

    async def _rpc(self, method: str, params: Optional[dict] = None) -> Any:
        """One request/response round-trip to the client's guest."""
        self._require_client()
        state = self.state
        rpc_id = _next_rpc_id(state)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        state.rpc_pending[rpc_id] = fut
        try:
            if not await _send_client_json(state, _rpc_request(rpc_id, method, params)):
                raise CaptureUnavailable(
                    f"the client guest view went away before {method!r} was sent"
                )
            try:
                return await asyncio.wait_for(fut, self.timeout)
            except asyncio.TimeoutError:
                raise CaptureError(
                    f"the client guest view did not answer {method!r} within "
                    f"{self.timeout:.0f}s"
                ) from None
        finally:
            state.rpc_pending.pop(rpc_id, None)

    # -- the CaptureContext contract --------------------------------------

    def save(self, relpath: str, content: "str | bytes") -> None:
        """Stage one of this capture's files into the running cell.

        ``relpath`` is relative to this capture's folder, so a capture never
        names — or collides with — another one's files; nested paths are fine
        (``images/hero.png``). ``content`` is text or bytes. The write is
        staged, not flushed: the daemon flushes when the callback settles,
        keeping and counting partial output on failure or Stop.

        That containment is the contract, so the path is enforced, not
        trusted: a leading ``/`` and ``.`` segments are dropped, and anything
        that would climb out of the folder (a ``..`` segment) or name nothing
        at all is a :class:`ValueError`."""
        parts = [
            part
            for part in str(relpath).strip().replace("\\", "/").split("/")
            if part and part != "."
        ]
        if ".." in parts or not parts:
            raise ValueError(
                f"capture path {relpath!r} must name a file inside this "
                f"capture's own folder (no '..' segments)"
            )
        rel = "/".join(parts)
        path = f"{self.capture_dir}/{rel}" if self.capture_dir else rel
        gen.update_file(path, content)
        self.saved.append(path)

    async def evaluate(self, js_function: str) -> Any:
        # The caller passes a zero-arg function source; the shared wrapper is
        # what turns it into the expression the guest runs.
        return await self._rpc("evaluate", {"js": _evaluate_expression(js_function)})

    async def html(self) -> str:
        return str(await self._rpc("html") or "")

    async def screenshot_png(self) -> bytes:
        # The guest's PNG bytes ride back base64-encoded (the wire is JSON).
        data = await self._rpc("screenshot")
        if not isinstance(data, str) or not data:
            return b""
        try:
            return base64.b64decode(data)
        except Exception as exc:
            raise CaptureError(f"the client returned an unreadable PNG: {exc}") from exc

    # -- navigation --------------------------------------------------------

    async def current_url(self) -> str:
        """The guest's live URL, straight from the client. The client also
        pushes ``url`` messages on every navigation; this is the pull side, so
        a capture never scrapes under a URL it only *thinks* is current."""
        return str(await self._rpc("url") or "")

    async def navigate(self, url: str) -> None:
        """Point the guest at ``url``."""
        await self._rpc("navigate", {"url": url})
