"""Accepting a renderer freezes its import graph into this installation's store.

A renderer's compiled bundle imports from the module CDNs (esm.sh,
jsDelivr), which the module mirror (``module_mirror``) serves from the
bundled snapshot, else from the store (``esm_modules``), else upstream on a
miss. The snapshot covers the shipped renderers' graphs; a renderer admitted
onto Capabilities (Add to, a suggestion, an import, a move) has its whole
graph fetched into the store right after admission, while the app is online
(:func:`freeze_soon`), so it later works offline like a shipped one without
having been opened first.

The graph of a bundle is its upstream URL literals (an import specifier is
one) and everything they import, statically or dynamically, walked through
the mirror. :func:`walk` with ``fetch=False`` says what this installation
holds without touching the network (the renderer card's offline line);
with ``fetch=True`` it fetches each module it does not hold.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
from collections.abc import Iterable
from dataclasses import dataclass

from collimate._host import module_mirror as upstream

logger = logging.getLogger(__name__)

# A graph larger than this is not walked further (and reported incomplete).
MAX_MODULES = 4096
_FETCH_WORKERS = 4

_loop: asyncio.AbstractEventLoop | None = None
_stopping = threading.Event()
_inflight: dict[str, asyncio.Future] = {}
# The graphs this installation holds entirely, by bundle digest (a held
# module stays held, so they never change).
_complete: dict[str, "Graph"] = {}


@dataclass(frozen=True)
class Graph:
    """What one bundle's graph needs: its roots, the modules walked, the
    keys this installation cannot serve offline (``missing``, each with why
    when a fetch failed), and whether the walk stopped at :data:`MAX_MODULES`."""
    roots: tuple[str, ...]
    modules: int
    missing: tuple[tuple[str, str], ...]
    truncated: bool

    @property
    def complete(self) -> bool:
        return not self.missing and not self.truncated

    def public(self) -> dict:
        return {"complete": self.complete, "roots": len(self.roots), "modules": self.modules,
                "missing": [{"url": url, "reason": reason} for url, reason in self.missing],
                "truncated": self.truncated}


def roots(bundle: bytes) -> tuple[str, ...]:
    """The upstream URLs a bundle names (quoted literals, import specifiers
    among them), canonical and sorted."""
    text = bundle.decode("utf-8", "replace")
    found = set()
    for literal in upstream.upstream_literals(text):
        try:
            found.add(upstream.canonical_url(literal))
        except ValueError:
            continue
    return tuple(sorted(found))


def walk(bundle: bytes, *, fetch: bool) -> Graph:
    """Walk ``bundle``'s graph through the mirror: each key the installation
    holds gives its imports; with ``fetch``, each one it does not hold is
    fetched and stored, else it is ``missing``."""
    from collimate_server.module_mirror import MirrorError, fetch_edges, held_edges
    start = roots(bundle)
    seen: set[str] = set(start)
    frontier = list(start)
    missing: list[tuple[str, str]] = []
    walked = 0
    truncated = False

    def visit(key: str) -> tuple[str, tuple[str, ...] | None, str]:
        edges = held_edges(key)
        if edges is not None or not fetch:
            return key, edges, "not stored on this installation"
        try:
            return key, fetch_edges(key), ""
        except MirrorError as error:
            return key, None, error.detail

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(_FETCH_WORKERS if fetch else 1) as pool:
        while frontier and not _stopping.is_set():
            batch, frontier = frontier, []
            for key, edges, reason in pool.map(visit, batch):
                walked += 1
                if edges is None:
                    missing.append((key, reason))
                    continue
                for target in edges:
                    if target in seen:
                        continue
                    if len(seen) >= MAX_MODULES:
                        truncated = True
                        break
                    seen.add(target)
                    frontier.append(target)
    if _stopping.is_set() and frontier:
        truncated = True
    return Graph(start, walked, tuple(sorted(missing)), truncated)


def graph(bundle: bytes) -> Graph:
    """What this installation holds of ``bundle``'s graph, without the
    network (remembered once complete: a held module stays held)."""
    digest = hashlib.sha256(bundle).hexdigest()
    if digest in _complete:
        return _complete[digest]
    result = walk(bundle, fetch=False)
    if result.complete:
        _complete[digest] = result
    return result


def freeze(bundle: bytes) -> Graph:
    """Fetch every module of ``bundle``'s graph the installation does not
    hold (blocking)."""
    result = walk(bundle, fetch=True)
    if result.complete:
        _complete[hashlib.sha256(bundle).hexdigest()] = result
    elif result.missing:
        logger.warning("offline modules: %d module(s) of a renderer's graph could not be stored, first %s: %s",
                       len(result.missing), result.missing[0][0], result.missing[0][1])
    return result


def attach(loop: asyncio.AbstractEventLoop) -> None:
    """Let admissions schedule freezes on the server's loop (lifespan start),
    forgetting the graphs a previous start found held: its store may differ."""
    global _loop
    _stopping.clear()
    _complete.clear()
    _loop = loop


def detach() -> None:
    """Stop scheduling and let running freezes end at their next step
    (lifespan end)."""
    global _loop
    _stopping.set()
    _loop = None
    for future in list(_inflight.values()):
        future.cancel()
    _inflight.clear()


def freeze_soon(bundles: Iterable[bytes]) -> None:
    """After an admission: freeze each bundle's graph in the background on
    the server's loop. Nothing happens without an attached loop or for a
    bundle whose graph is held already or being frozen."""
    loop = _loop
    if loop is None:
        return
    for bundle in bundles:
        digest = hashlib.sha256(bundle).hexdigest()
        if digest in _complete or digest in _inflight:
            continue

        def start(bundle=bundle, digest=digest) -> None:
            if _stopping.is_set() or digest in _inflight:
                return
            future = asyncio.ensure_future(asyncio.to_thread(freeze, bundle))
            _inflight[digest] = future
            future.add_done_callback(lambda done, digest=digest: _settled(digest, done))

        loop.call_soon_threadsafe(start)


def _settled(digest: str, future: asyncio.Future) -> None:
    if _inflight.get(digest) is future:
        del _inflight[digest]
    if not future.cancelled() and future.exception() is not None:
        logger.warning("offline modules: a freeze failed: %s", future.exception())
