"""The module mirror: the renderers' module CDNs, served from this origin.

Renderers import from the allowlisted upstreams of the SDK's contract
(``collimate._host.module_mirror.UPSTREAMS``, read from
``module_mirror_contract.json``). The app's document import maps send each to
``GET /api/mirror/<host>/<path>`` (``routes/mirror.py``); the renderer
runtime's ``mirrorUrl()`` and the hosts' ``api.loadCss`` map the loads an
import map does not cover. A module is keyed by its canonical upstream URL
(``mirror_key``) and looked up in three places, in order:

1. **The bundled snapshot**, ``genesis.bundled_dir("module-snapshot")``:
   the installed wheel's ``collimate_module_snapshot/`` package data, or
   the checkout's ``module-snapshot/``, indexed at startup by
   :func:`load_snapshot`. It is authoritative, floating keys included, and
   every read re-verifies the file's sha256.
2. **The store**: the global ``esm_modules`` row of a URL fetched before,
   whose body is a verified CAS object. A missing or damaged object is a miss.
3. **Upstream**: one fetch however many requests wait on it (at most
   :data:`UPSTREAM_CONCURRENCY` at a time), no credentials, a size cap and
   redirects followed only within the allowlist. The body is rewritten by
   the SDK's ``served_body`` and published first-writer-wins; then the
   module's static imports are prefetched in the background, which hides the
   browser's request waterfall.

An exact key never changes once stored and is served ``immutable``; a
floating one (a range, a moving tag) freezes at its first fetch and is served
``no-cache`` with its ETag. Failures are :class:`MirrorError`, an HTTP status
and an ``{error, detail}`` body.
"""
from __future__ import annotations

import asyncio
import hashlib
import http.cookiejar
import logging
import stat
import threading
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

import httpx
from collimate._host import module_mirror as upstream
from collimate._host.esm_lexer import LexError

logger = logging.getLogger(__name__)

# Upstream fetches in flight at once, requests and prefetches together.
UPSTREAM_CONCURRENCY = 8
# How long a host that refused or timed out a connection is not retried.
UNREACHABLE_SECONDS = 15.0
# Static imports prefetched per fetched module, and prefetches pending at once.
PREFETCH_EDGES = 64
PREFETCH_PENDING = 256
_REDIRECTS = frozenset({301, 302, 303, 307, 308})


class MirrorError(Exception):
    """A module the mirror cannot serve: the HTTP status it answers and its
    ``{error, detail}`` body (plus the upstream ``status`` when upstream
    answered one)."""

    def __init__(self, status: int, error: str, detail: str, *, upstream_status: int | None = None):
        super().__init__(detail)
        self.status = status
        self.error = error
        self.detail = detail
        self.upstream_status = upstream_status

    def body(self) -> dict:
        body: dict = {"error": self.error, "detail": self.detail}
        if self.upstream_status is not None:
            body["status"] = self.upstream_status
        return body


@dataclass(frozen=True)
class Module:
    """A served module: its bytes, the exact upstream Content-Type, the
    bytes' sha256 (its ETag) and whether its key is exact (``immutable``)."""

    body: bytes
    media_type: str
    digest: str
    immutable: bool


@dataclass(frozen=True)
class _Row:
    blob_hash: str
    media_type: str


@dataclass(frozen=True)
class _Loaded:
    modules: Path
    entries: Mapping[str, upstream.SnapshotEntry]


_snapshot: _Loaded | None = None
# The parsed index by (path, mtime, size, inode): a restart in one process
# (tests, embedded servers) re-checks the files but does not re-parse.
_parsed: dict[tuple, upstream.Snapshot] = {}
# Stored rows by URL. Safe to keep: an exact row never changes, and a
# floating one changes only when its object was lost (``_publish``).
_rows: dict[str, _Row] = {}
_rows_lock = threading.Lock()
# host -> (until, detail) after a connection-level failure.
_unreachable: dict[str, tuple[float, str]] = {}
_client: httpx.Client | None = None
_client_lock = threading.Lock()


@dataclass
class _LoopState:
    loop: asyncio.AbstractEventLoop
    semaphore: asyncio.Semaphore
    inflight: dict[str, asyncio.Task]
    background: set[asyncio.Task]


_loop_state: _LoopState | None = None


def _state() -> _LoopState:
    """The coalescing and concurrency state of the running event loop."""
    global _loop_state
    loop = asyncio.get_running_loop()
    if _loop_state is None or _loop_state.loop is not loop:
        _loop_state = _LoopState(loop, asyncio.Semaphore(UPSTREAM_CONCURRENCY), {}, set())
    return _loop_state


def load_snapshot(directory) -> int:
    """Index the bundled snapshot in ``directory``
    (``genesis.bundled_dir("module-snapshot")``) and reset the store's
    in-process state; answer its entry count.

    An absent folder is an installation without a snapshot (0). A present
    one refuses startup (ValueError) when its index is missing, unreadable
    or invalid, was frozen under another mirror contract, or names a module
    file that is not there.
    """
    global _snapshot
    with _rows_lock:
        _rows.clear()
    _unreachable.clear()
    _snapshot = None
    root = Path(directory)
    if not root.exists():
        return 0
    index = root / "index.json"
    try:
        info = index.lstat()
    except FileNotFoundError:
        raise ValueError("Module snapshot index is missing") from None
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Module snapshot index must be a regular file")
    key = (str(root.resolve()), info.st_mtime_ns, info.st_size, info.st_ino)
    snapshot = _parsed.get(key)
    if snapshot is None:
        snapshot = upstream.read_snapshot(root)
        if snapshot.contract != upstream.contract_hash():
            raise ValueError(f"Module snapshot was frozen under another mirror contract; run {upstream.FREEZE_COMMAND}")
        _parsed.clear()
        _parsed[key] = snapshot
    modules = root / "modules"
    missing = sorted(digest for digest in {entry.hash for entry in snapshot.entries.values()}
                     if not (modules / digest).is_file())
    if missing:
        raise ValueError(f"Module snapshot is missing {len(missing)} module file(s), first modules/{missing[0]} "
                         "(a checkout fetches them: python3 build/snapshot_store.py fetch)")
    _snapshot = _Loaded(modules, snapshot.entries)
    return len(snapshot.entries)


def known_digest(key: str) -> str | None:
    """The digest a conditional request can be answered with before any body
    is read: the snapshot's, or a stored row's already in memory."""
    if _snapshot is not None and (entry := _snapshot.entries.get(key)) is not None:
        return entry.hash
    with _rows_lock:
        row = _rows.get(key)
    return row.blob_hash if row is not None else None


async def get_module(key: str) -> Module:
    """The module stored under the canonical upstream URL ``key``
    (``mirror_key``): snapshot, then store, then upstream. Raises
    :class:`MirrorError`."""
    if _snapshot is not None and (entry := _snapshot.entries.get(key)) is not None:
        return await asyncio.to_thread(_read_snapshot_module, _snapshot.modules, key, entry)
    stored = await asyncio.to_thread(_read_stored, key)
    if stored is not None:
        return stored
    return await _miss(key)


def shutdown() -> None:
    """Cancel the running loop's background prefetches (lifespan end)."""
    global _loop_state
    state = _loop_state
    _loop_state = None
    if state is not None:
        for task in tuple(state.background):
            task.cancel()


def _immutable(key: str) -> bool:
    return not upstream.is_floating(key)


def _read_snapshot_module(modules: Path, key: str, entry: upstream.SnapshotEntry) -> Module:
    try:
        body = (modules / entry.hash).read_bytes()
    except OSError:
        body = None
    if body is None or hashlib.sha256(body).hexdigest() != entry.hash:
        logger.error("module mirror: vendored module %s for %s is missing or changed", entry.hash, key)
        raise MirrorError(502, "snapshot", "vendored module changed; restart")
    return Module(body, entry.media_type, entry.hash, _immutable(key))


def _stored_row(key: str) -> _Row | None:
    with _rows_lock:
        row = _rows.get(key)
    if row is not None:
        return row
    from cas import is_cas_hash
    from database import get_global_db_connection
    conn = get_global_db_connection()
    try:
        found = conn.execute("SELECT blob_hash, media_type FROM esm_modules WHERE url=?", (key,)).fetchone()
    finally:
        conn.close()
    if found is None or not is_cas_hash(found["blob_hash"]):
        return None
    row = _Row(found["blob_hash"], found["media_type"])
    with _rows_lock:
        _rows[key] = row
    return row


def _read_stored(key: str) -> Module | None:
    from cas import CorruptBlobError, read_blob
    row = _stored_row(key)
    if row is None:
        return None
    try:
        body = read_blob(row.blob_hash)
    except (CorruptBlobError, OSError):
        body = None
    if body is None:
        logger.warning("module mirror: the stored object for %s is missing or damaged; fetching it again", key)
        return None
    return Module(body, row.media_type, row.blob_hash, _immutable(key))


# -- misses ------------------------------------------------------------------
def _host(key: str) -> str:
    return key[len("https://"):].partition("/")[0]


def _check_reachable(host: str) -> None:
    until, detail = _unreachable.get(host, (0.0, ""))
    if time.monotonic() < until:
        raise MirrorError(502, "upstream", detail)


def _settled(key: str, task: asyncio.Task) -> None:
    state = _loop_state
    if state is not None and state.inflight.get(key) is task:
        del state.inflight[key]
    if not task.cancelled():
        task.exception()  # retrieved: a prefetch nobody awaited is not an unhandled error


async def _miss(key: str) -> Module:
    state = _state()
    task = state.inflight.get(key)
    if task is None:
        task = state.loop.create_task(_resolve(key, state))
        state.inflight[key] = task
        task.add_done_callback(lambda done, key=key: _settled(key, done))
    # A request that goes away leaves the fetch running for the others.
    return await asyncio.shield(task)


async def _resolve(key: str, state: _LoopState) -> Module:
    host = _host(key)
    _check_reachable(host)
    async with state.semaphore:
        _check_reachable(host)
        module, edges = await asyncio.to_thread(_fetch_and_publish, key)
    _prefetch(edges, state)
    return module


def _prefetch(edges: Iterable[str], state: _LoopState) -> None:
    """Fetch a module's static imports ahead of the browser, which asks for
    them only once it has parsed the module (and then six at a time)."""
    for target in list(edges)[:PREFETCH_EDGES]:
        if len(state.background) >= PREFETCH_PENDING:
            return
        if target in state.inflight or known_digest(target) is not None:
            continue
        task = state.loop.create_task(_prefetch_one(target))
        state.background.add(task)
        task.add_done_callback(state.background.discard)


async def _prefetch_one(key: str) -> None:
    try:
        await get_module(key)
    except MirrorError as error:
        logger.debug("module mirror: prefetch of %s failed: %s", key, error.detail)


def _http_client() -> httpx.Client:
    """The one upstream client: the contract's timeouts and User-Agent,
    no cookies kept or sent, redirects handled by :func:`_fetch`."""
    global _client
    with _client_lock:
        if _client is None:
            contract = upstream.CONTRACT
            no_cookies = http.cookiejar.CookieJar(http.cookiejar.DefaultCookiePolicy(allowed_domains=[]))
            _client = httpx.Client(
                timeout=httpx.Timeout(float(contract["timeout_seconds"]),
                                      connect=float(contract["connect_timeout_seconds"])),
                follow_redirects=False, trust_env=True, cookies=no_cookies,
                headers={"User-Agent": str(contract["user_agent"]), "Accept": "*/*"},
            )
        return _client


def _first_line(response: httpx.Response) -> str:
    try:
        for chunk in response.iter_bytes():
            lines = chunk[:300].decode("utf-8", "replace").strip().splitlines()
            return f": {lines[0][:200]}" if lines else ""
    except httpx.HTTPError:
        pass
    return ""


def _fetch(url: str) -> tuple[bytes, str, str]:
    """GET ``url`` upstream: (body, Content-Type, the URL it landed on)."""
    limit = int(upstream.CONTRACT["max_module_bytes"])
    hops = int(upstream.CONTRACT["max_redirects"])
    client = _http_client()
    current = url
    for _ in range(hops + 1):
        with client.stream("GET", current) as response:
            status = response.status_code
            if status in _REDIRECTS:
                location = response.headers.get("location")
                if not location:
                    raise MirrorError(502, "upstream", f"{current}: HTTP {status} without a Location",
                                      upstream_status=status)
                try:
                    current = upstream.check_redirect(location, current)
                except upstream.InvalidKey as error:
                    raise MirrorError(502, "upstream", f"{current}: {error}", upstream_status=status) from None
                continue
            if status != 200:
                raise MirrorError(404 if status in (404, 410) else 502, "upstream",
                                  f"{current}: HTTP {status}{_first_line(response)}", upstream_status=status)
            declared = response.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > limit:
                raise MirrorError(502, "upstream", f"{url}: larger than {limit} bytes")
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > limit:
                    raise MirrorError(502, "upstream", f"{url}: larger than {limit} bytes")
                chunks.append(chunk)
            media_type = response.headers.get("content-type", "").strip() or "application/octet-stream"
            return b"".join(chunks), media_type, current
    raise MirrorError(502, "upstream", f"{url}: more than {hops} redirects")


def _fetch_and_publish(key: str) -> tuple[Module, tuple[str, ...]]:
    """Fetch, rewrite and store one module; answer it and its static edges."""
    host = _host(key)
    try:
        raw, media_type, final_url = _fetch(key)
    except (httpx.ConnectError, httpx.ConnectTimeout) as error:
        detail = f"{host} is unreachable: {error or type(error).__name__}"
        _unreachable[host] = (time.monotonic() + UNREACHABLE_SECONDS, detail)
        logger.warning("module mirror: %s (fetching %s)", detail, key)
        raise MirrorError(502, "upstream", detail) from None
    except httpx.TimeoutException as error:
        logger.warning("module mirror: %s timed out", key)
        raise MirrorError(502, "upstream", f"{key}: timed out ({type(error).__name__})") from None
    except httpx.HTTPError as error:
        logger.warning("module mirror: fetching %s failed: %s", key, error)
        raise MirrorError(502, "upstream", f"{key}: {error or type(error).__name__}") from None
    except MirrorError as error:
        logger.warning("module mirror: %s", error.detail)
        raise
    try:
        body, rewrite = upstream.served_body(raw, media_type, url=key, final_url=final_url)
    except (LexError, ValueError) as error:  # InvalidKey is a ValueError
        logger.warning("module mirror: cannot serve %s: %s", key, error)
        raise MirrorError(502, "upstream", f"{key}: cannot be served: {error}") from None
    digest = hashlib.sha256(body).hexdigest()
    edges = rewrite.imports if rewrite is not None else ()
    try:
        row = _publish(key, body, media_type, hashlib.sha256(raw).hexdigest(), final_url)
    except OSError as error:
        logger.error("module mirror: storing %s failed (served unstored): %s", key, error)
        return Module(body, media_type, digest, _immutable(key)), edges
    if row.blob_hash != digest:
        # Another server process stored this key first; its bytes are the key's.
        from cas import require_blob
        return Module(require_blob(row.blob_hash), row.media_type, row.blob_hash, _immutable(key)), edges
    return Module(body, media_type, digest, _immutable(key)), edges


def _store(body: bytes, digest: str) -> None:
    """Publish the CAS object, replacing a damaged one under the same digest."""
    from cas import CorruptBlobError, blob_path, publication_lock, store_bytes, verify_blob, write_blob_atomic
    try:
        if store_bytes(body) != digest:
            raise OSError(f"CAS publication digest mismatch for {digest}")
        return
    except OSError:
        with publication_lock:
            try:
                verify_blob(digest)
            except CorruptBlobError:
                write_blob_atomic(blob_path(digest), lambda path: Path(path).write_bytes(body), expected_hash=digest)
                return
            raise


def _publish(key: str, body: bytes, media_type: str, upstream_hash: str, final_url: str) -> _Row:
    """Store one fetched module, CAS before SQL, first writer wins: answer
    the row the key now has. A winner whose object was lost is replaced."""
    from cas import CorruptBlobError, publication_lock, retain_blob, verify_blob
    from database import get_global_db_connection
    digest = hashlib.sha256(body).hexdigest()
    _store(body, digest)
    values = (digest, media_type, len(body), upstream_hash, final_url, time.time())
    with publication_lock:
        retain_blob(digest)
        conn = get_global_db_connection()
        try:
            with conn:
                conn.execute(
                    "INSERT OR IGNORE INTO esm_modules(url, blob_hash, media_type, bytes, upstream_hash, "
                    "final_url, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (key, *values))
            found = conn.execute("SELECT blob_hash, media_type FROM esm_modules WHERE url=?", (key,)).fetchone()
            row = _Row(found["blob_hash"], found["media_type"])
            if row.blob_hash != digest:
                try:
                    verify_blob(row.blob_hash)
                except (CorruptBlobError, OSError, ValueError):
                    with conn:
                        conn.execute(
                            "UPDATE esm_modules SET blob_hash=?, media_type=?, bytes=?, upstream_hash=?, "
                            "final_url=?, fetched_at=? WHERE url=?", (*values, key))
                    row = _Row(digest, media_type)
        finally:
            conn.close()
    with _rows_lock:
        _rows[key] = row
    return row
