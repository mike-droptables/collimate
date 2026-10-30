"""Shared internals for web-session daemon generators.

This package is intentionally private (leading underscore on the dir
name) — it has no .generator definition, so the host lists no generator
for it. Sibling
generators import it via a small sys.path shim:

    import os, sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from _web_shared.daemon import run_web_session

Layout: ``daemon`` is the entry point (aiohttp server, the one control-WS
route, the run loop) — it launches no browser, because the client's own
native guest view is the browser; ``protocol`` is the renderer-facing
contract (control-WS wire shapes, the RPC, and the ``tab-<n>.webview`` tab
file); ``capture`` is the handle capture callbacks are given.
"""
from .capture import (  # noqa: F401
    CaptureContext,
    CaptureError,
    CaptureUnavailable,
)
from .daemon import run_web_session  # noqa: F401
