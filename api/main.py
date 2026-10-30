import asyncio
import sys
import os
import logging
import re

# Ensure the api/ directory is on the Python path for relative imports
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

logger = logging.getLogger(__name__)

from genesis import bootstrap
# Resolve the per-server container owner only when cleanup runs: cli_main
# changes into the data directory after this module loads.
from startup_cleanup import reap_orphan_containers
from state import state
from routes.system import router as system_router
from routes.channels import router as channels_router
from routes.stacks import router as stacks_router
from routes.generation import router as generation_router
from routes.runs import router as runs_router
# Proxy router forwards traffic to subprocess generators (vscode/codespaces
# tunnels).
from routes.proxy import router as proxy_router
from routes.blobs import router as blobs_router, runtime_config
from routes.cells import router as cells_router
from routes.options import router as options_router
from routes.chat_live import router as chat_live_router
from routes.terminal import router as terminal_router
from routes.pick_sessions import router as pick_sessions_router
from routes.cell_display import router as cell_display_router
from routes.runners import router as runners_router
from routes.suggested import router as suggested_router
from routes.accounts import router as accounts_router
from routes import transfer
from routes.mirror import router as mirror_router


class StartupRefused(RuntimeError):
    """Starting would overwrite or hide the user's data. The server exits
    with this message instead; nothing on disk was changed."""


# The data dir's server lock. One process owns a data dir at a time: the
# server holds an exclusive flock on this file for its whole lifetime and
# the developer reset under build/ try-locks the same file, so a second server
# (any launcher: the desktop shell, `collimate`, `python main.py`, uvicorn) or
# a reset beside a running server is refused instead of persisting its own copy
# of the state over the other's. The kernel drops the lock with the process,
# so a crash never leaves it held. The file carries the holder's pid, for the
# refusal message only.
SERVER_LOCK_PATH = os.path.join(".collimate", "server.lock")


def lock_data_dir() -> int:
    """Take the data dir's server lock (CWD-relative) and return its fd.
    Raises :class:`StartupRefused` when another process holds it or the
    lock file cannot be opened (a symlink in its place, say)."""
    from data_dir_lock import LockHeld, LockOpenError, acquire
    os.makedirs(".collimate", exist_ok=True)
    try:
        return acquire(SERVER_LOCK_PATH)
    except LockHeld as exc:
        holder = exc.holder
        raise StartupRefused(
            f"another Collimate process (pid {holder}) holds "
            f"{os.path.abspath(SERVER_LOCK_PATH)}: a server or a reset is already "
            "running against this data dir. Stop it, or start this one with "
            "another --data-dir."
        ) from None
    except LockOpenError as failure:
        exc = failure.error
        raise StartupRefused(
            f"could not open {os.path.abspath(SERVER_LOCK_PATH)}, the data dir's "
            f"server lock ({exc.strerror}; it must be a plain file). Remove "
            "whatever is there and start again."
        ) from None


def unlock_data_dir(fd: int) -> None:
    from data_dir_lock import release
    release(fd)


# The saved app state lives in one ui_state row of .collimate/app.db. A row
# that does not validate is kept aside (genesis.quarantine_app_state); while
# any such row exists the startup orphan reconcile (step 2.5) stays off, so
# the channel databases it describes are never parked or deleted.
_APP_DB = os.path.join(".collimate", "app.db")
_APP_DB_ATTEMPTS = 3
_APP_DB_RETRY_SECONDS = 1.0


def _app_db_call(what: str, fn):
    """``fn()`` against app.db for the startup restore. An OperationalError
    (a lock another process holds past the 5 s busy timeout, an I/O error)
    is retried; one that persists, or any other failure, raises
    :class:`StartupRefused`, before anything could persist over the row."""
    import sqlite3
    import time
    for attempt in range(1, _APP_DB_ATTEMPTS + 1):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            if attempt < _APP_DB_ATTEMPTS:
                logger.warning("startup: %s failed (%s); retrying", what, exc)
                time.sleep(_APP_DB_RETRY_SECONDS)
                continue
            failure: Exception = exc
        except Exception as exc:
            failure = exc
        raise StartupRefused(
            f"could not {what} in {os.path.abspath(_APP_DB)}: {failure}. Nothing was "
            "changed. Close whatever else holds the file, or repair it, and start again."
        ) from failure


def restore_app_state() -> None:
    """Startup step 2: the saved app state into ``state``.

    No saved row: nothing to restore (a fresh data dir). A read error:
    retried, then :class:`StartupRefused`. A row that does not validate (a
    model change an old row predates, a corrupt value): kept aside under an
    ``app_state.invalid-*`` key and the server starts with no channels, their
    databases untouched; the first persist then writes the empty state over
    ``app_state``, which the kept copy survives. Every restored channel gets
    its Capabilities stack with the build's built-in cards
    (``genesis.ensure_capabilities_stack``), persisted with the next write,
    so an app upgrade's cards show without a reset."""
    from genesis import load_global_ui_state, parse_saved_app_state, quarantine_app_state
    raw = _app_db_call("read the saved app state", load_global_ui_state)
    if raw is None:
        return
    try:
        restored = parse_saved_app_state(raw)
    except OSError as exc:
        raise StartupRefused(f"Cannot restore saved file content: {exc}. Nothing was changed.") from exc
    except Exception:
        logger.exception("startup: the saved app state in %s does not validate", os.path.abspath(_APP_DB))
        key = _app_db_call("set the saved app state aside", lambda: quarantine_app_state(raw))
        logger.error(
            "startup: the saved app state is kept as ui_state key %r in %s and the "
            "server starts with no channels; every channel database stays in "
            ".collimate/channels/. To recover, stop the server, repair that row and "
            "put it back under the key 'app_state', then delete %r.",
            key, os.path.abspath(_APP_DB), key,
        )
        # Empty whatever a previous lifespan cycle in this process (tests,
        # embedded servers) left in state: its channels are not restored.
        state.channels = []
        state.active_channel_id = None
        return
    from genesis import ensure_capabilities_stack, reconcile_restored_cell_files
    _app_db_call("reconcile saved cells with accepted files",
                 lambda: reconcile_restored_cell_files(restored))
    for channel in restored.channels:
        ensure_capabilities_stack(channel)
    state.channels = restored.channels
    state.active_channel_id = restored.active_channel_id
    from cell_metadata import recover_pending_cell_metadata
    try:
        recover_pending_cell_metadata()
    except Exception as exc:
        raise StartupRefused(f"Could not complete durable cell metadata operations: {exc}") from exc


@asynccontextmanager
async def lifespan(app: FastAPI):
    # The data dir's server lock (see SERVER_LOCK_PATH), held from before the
    # first write until the lifespan ends. A run-end cleanup the shutdown left
    # going can still write after that, during the loop close (see the reap
    # wait in _serve's shutdown).
    lock_fd = lock_data_dir()
    try:
        async with _serve(app):
            yield
    finally:
        unlock_data_dir(lock_fd)


@asynccontextmanager
async def _serve(app: FastAPI):
    # 0. Install a generously-sized default ThreadPoolExecutor.
    #
    # Python's default executor caps at min(32, cpu_count() + 4) — ~36
    # workers on a modern laptop. The async-hygiene cleanup pushes
    # every sync sqlite + filesystem operation through
    # ``asyncio.to_thread``, so under concurrent load (six live chats
    # each emitting cell updates and SSE polls plus a deep DAG /
    # version-history walk per turn) the executor saturates and
    # newly-dispatched tasks queue behind the existing backlog. 256
    # is conservative — well above the saturation estimate, well
    # below any per-process resource cost; workers are lazy and only
    # created on demand. Hitting saturation on this ceiling is a
    # signal that some sync-on-loop violation snuck back in, not a
    # reason to raise the ceiling further. See docs/async-hygiene.md.
    try:
        from concurrent.futures import ThreadPoolExecutor
        _executor = ThreadPoolExecutor(
            max_workers=256, thread_name_prefix="collimate-async",
        )
        asyncio.get_running_loop().set_default_executor(_executor)
        logger.info("installed 256-worker default ThreadPoolExecutor")
    except Exception:
        logger.exception("failed to install custom thread pool; falling back to default")

    # 1. Keystore probe. Availability only — the host process env stays
    # clean: secrets are resolved per run (subprocess env / IPC fast path)
    # from the account registry, so nothing is mirrored into os.environ here.
    try:
        from keystore import available
        if available():
            logger.info("keystore: OS keychain available")
        else:
            logger.info("keystore: no persistent backend; secrets will not survive restart")
    except Exception:
        logger.exception("keystore probe failed")

    # 0.5 / 0.6. Docker container reaps,
    # then the account lease sweep, run OFF the startup critical path.
    #
    # Each `docker rm -f` can block for ~10s per orphaned container when the
    # daemon is slow to terminate it — and pathologically so when Docker
    # *can't* kill it at all (e.g. an AppArmor signal-mediation denial under
    # snap Docker leaves code-server containers un-killable; every boot then
    # re-pays the full timeout and the orphans never clear). Reaping is
    # best-effort cleanup, so it must never gate "application startup
    # complete": schedule it as a background task that runs the blocking
    # `docker` calls in a worker thread via asyncio.to_thread (scheduled at
    # the end of startup, below).
    import time as _time
    _boot_ts = _time.time()

    # 0.7. Capture the event loop early so broadcast() works before any
    # SSE subscriber connects.
    from events import capture_loop
    capture_loop()

    # Re-open the run-creation gate (set during the previous shutdown) so
    # repeated lifespan cycles in one process — tests, embedded servers —
    # accept runs again.
    from run_registry import mark_shutting_down
    mark_shutting_down(False)

    # 1. Bootstrap .collimate/ if this is the first launch in this CWD
    bootstrap()

    # 1.5 Open app.db, which runs its migrations: one this build does not
    # upgrade (older than global schema 8) refuses the start with the reason.
    from database import get_global_db_connection
    try:
        get_global_db_connection().close()
    except RuntimeError as exc:
        logger.error("startup: %s (%s)", exc, os.path.abspath(_APP_DB))
        raise StartupRefused(str(exc)) from exc

    # The module mirror's bundled snapshot (module-snapshot/), indexed once:
    # absent serves upstream misses only, a broken one refuses the start.
    from genesis import bundled_dir
    import module_mirror
    module_mirror.load_snapshot(bundled_dir("module-snapshot"))
    # The SDK's built renderer runtime (served at /api/sdk/) and verifier
    # prelude, against the manifests that pin them: in a checkout they are
    # git-ignored build outputs, which a pull can leave stale.
    from collimate.renderer_runtime import verify_prelude_resources, verify_runtime_resources
    try:
        verify_runtime_resources()
        verify_prelude_resources()
    except (OSError, ValueError) as exc:
        raise StartupRefused(f"{exc} (a checkout rebuilds them: python3 sdk/scripts/build_outputs.py)") from exc
    # The app's built-in packages, read, checked and compiled once: a broken
    # build refuses to start rather than serve quietly. Each built-in
    # renderer's bundle is stored as its server bundle before any request.
    from builtin_packages import publish_bundles
    publish_bundles()

    # 2. Restore persisted UI state from the global (channel-independent) db,
    #    the source of truth — it's readable even with zero channels (the
    #    channel-creation view). A read error refuses to start; a row that
    #    does not validate is kept aside (see restore_app_state).
    restore_app_state()

    # Recover stale runs only after restoring their channels, before serving.
    from process_manager.finalization import recover_interrupted_runs
    recover_interrupted_runs()

    # 2.5 A channel db file that no restored channel owns (a creation that
    #     died half-way, a wipe that raced a running server) would otherwise
    #     retain unowned durable records invisibly: park them or remove proven empty files.
    #     Not while a saved app state is kept aside unrestored: the channels
    #     it lists are exactly the "unowned" files, so the reconcile waits
    #     until that row is repaired and its key deleted.
    try:
        from database import reconcile_orphan_channel_dbs
        from genesis import quarantined_app_states
        from state import channel_db_name as _id_of
        held = quarantined_app_states()
        if held:
            logger.warning(
                "startup: channel databases left as they are while the saved app "
                "state kept as %s is unrestored", ", ".join(held),
            )
        else:
            for outcome in reconcile_orphan_channel_dbs({_id_of(c) for c in state.channels}):
                logger.warning("startup: %s", outcome)
    except Exception:
        logger.exception("orphan channel database reconcile failed")

    # No "ensure at least one channel" fallback: zero channels is a valid state.
    # The frontend renders the channel-creation view when none exist.

    # 4. Periodic passive checkpoint copies available frames without
    # reserving the writer lock while a reader pins the WAL. Transaction
    # durability remains governed by SQLite's synchronous policy.
    import wal_checkpoint
    interval = float(os.environ.get("COLLIMATE_WAL_CHECKPOINT_SECONDS", "60") or 60)
    _wal_task = asyncio.create_task(wal_checkpoint.run_periodic(interval))

    # The reaps and the lease sweep (0.5 / 0.6) start last, once nothing
    # above can refuse the start: a refused start leaves no worker thread
    # reaping or sweeping on after its lifespan releases the server lock.
    _reap_task = asyncio.create_task(asyncio.to_thread(reap_orphan_containers, _boot_ts))

    from state import start_persistence_writer, stop_persistence_writer
    start_persistence_writer()
    from user_validation import start_user_validation
    start_user_validation()
    import cas_gc
    _gc_task = asyncio.create_task(cas_gc.run_periodic())
    _ready_task = asyncio.create_task(asyncio.to_thread(_load_readiness_diagnostics))

    yield

    module_mirror.shutdown()

    # Drop the background orphan-reap task if it's still running. Cancelling
    # the await detaches us from the worker thread (a blocked `docker rm` may
    # run to its own timeout in the background) so shutdown isn't held up;
    # stop_all_runs below is what actually tears down live containers.
    if not _reap_task.done():
        _reap_task.cancel()
        try:
            await _reap_task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("orphan reap task shutdown failed")

    # Shutdown: stop every running generator + sandbox container, giving
    # each a grace window to exit cleanly (Docker-using generators spend it
    # calling ``docker stop`` on their own container) before SIGKILLing the
    # stragglers. Same ladder the in-app "Stop all" button uses — see
    # routes/runs/teardown.stop_all_runs. uvicorn runs this teardown on
    # SIGINT and on SIGTERM. The desktop shell quits by POSTing
    # /api/system/shutdown, whose sequence runs one Stop all and then SIGINTs
    # the server, so this pass follows that one: it stops only what that one
    # did not reach and never waits again for a run-end cleanup it waited
    # out. The shell SIGTERMs, then SIGKILLs, only a server still up 30 s
    # after its POST (the budget: teardown.DEFAULT_GRACE_SECONDS).
    from routes.runs import stop_all_runs
    from run_registry import mark_shutting_down
    from routes.runs.teardown import wait_for_run_reaps
    # Gate create_run first: a POST /runs racing this teardown would
    # otherwise spawn a generator after the sweep and orphan it.
    mark_shutting_down()
    try:
        summary = await stop_all_runs()
        logger.info("shutdown teardown complete: %s", summary)
    except Exception:
        logger.exception("shutdown teardown failed")

    # Then wait, bounded, for the stopped runs to leave the registry through
    # their own removers (teardown.wait_for_run_reaps), so a later lifespan
    # in this process (tests, an embedded server) normally starts with none
    # registered. Nothing is cancelled or removed here. A run-end cleanup
    # still going after this outlives the lifespan and the data dir's lock:
    # the CLI's SIGINT/SIGTERM handlers both unwind through loop close, which
    # cancels it once and waits for its finally (see wait_for_run_reaps).
    try:
        await wait_for_run_reaps()
    except Exception:
        logger.exception("shutdown: waiting for the stopped runs to leave the registry failed")

    from routes.proxy import close_proxy_transport
    await close_proxy_transport()
    from user_validation import close_user_validation
    await asyncio.to_thread(close_user_validation)
    from verifier_pool import close_verifier_pool
    await asyncio.to_thread(close_verifier_pool)

    # Final layout mutations must be durable before the final checkpoint and
    # before the lifespan releases this data directory's server lock.
    await asyncio.to_thread(stop_persistence_writer)
    await _ready_task
    _gc_task.cancel()
    try:
        await _gc_task
    except asyncio.CancelledError:
        pass

    # Last: stop the WAL checkpoint task. Its cancel path runs one final
    # checkpoint_all, which now covers the teardown's own commits (stopped
    # statuses, run-end sweeps, run logs).
    await cas_gc.wait_for_collection()
    _wal_task.cancel()
    try:
        await _wal_task
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("wal_checkpoint task shutdown failed")


app = FastAPI(lifespan=lifespan)

# Request-body cap. Every route's request body counts against it as it
# arrives, whether it declares a Content-Length or streams chunked, so no
# handler that buffers a body (a blob upload, the generator proxy) can be
# made to hold more than this in memory.
MAX_REQUEST_BODY_BYTES = 64 * 1024 * 1024

# The streaming upload routes write their body to disk as it arrives and
# enforce their own per-file cap, so the buffered-body cap does not apply to
# them. Each entry is an exact method and a fully anchored path pattern, so
# no other route, present or future, matches by sharing a prefix.
STREAMING_UPLOAD_ROUTES = (
    ("POST", re.compile(r"/api/blob/stream")),
    ("POST", re.compile(r"/api/import/zip")),
)


def is_streaming_upload(method: str, path: str) -> bool:
    return any(method == m and pattern.fullmatch(path) for m, pattern in STREAMING_UPLOAD_ROUTES)


class _BodyTooLarge(Exception):
    """Raised into the app's ``receive`` once the body passes the cap."""


async def _send_body_too_large(send, limit: int) -> None:
    import json
    body = json.dumps({"detail": "request body too large", "limit_bytes": limit}).encode()
    await send({
        "type": "http.response.start",
        "status": 413,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": body})


class BodySizeLimitMiddleware:
    """Enforces :data:`MAX_REQUEST_BODY_BYTES` on http requests.

    A declared Content-Length over the cap is refused before the body is
    read. Otherwise the bytes are counted as the app receives them; past the
    cap its ``receive`` raises, whatever the app answers to that is dropped,
    and the client gets the 413 — unless the app had already started its
    response, which then simply ends. A malformed Content-Length passes to
    the framework. The :data:`STREAMING_UPLOAD_ROUTES` pass through
    uncounted. Pure ASGI, so responses (SSE included) pass straight
    through without an extra task per request."""

    def __init__(self, app, limit: int = MAX_REQUEST_BODY_BYTES) -> None:
        self.app = app
        self.limit = limit

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if is_streaming_upload(scope.get("method", ""), scope.get("path", "")):
            await self.app(scope, receive, send)
            return
        limit = self.limit
        if scope.get("path") == "/api/blob":
            from cas import MAX_BLOB_BYTES
            limit = min(limit, MAX_BLOB_BYTES)
        for name, value in scope.get("headers", ()):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    break
                if declared > limit:
                    await _send_body_too_large(send, limit)
                    return
                break
        received = 0
        over = False
        started = False

        async def counted_receive():
            nonlocal received, over
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    over = True
                    raise _BodyTooLarge()
            return message

        async def guarded_send(message):
            nonlocal started
            if over and not started:
                return
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counted_receive, guarded_send)
        except Exception:
            if not over:
                raise
        if over and not started:
            await _send_body_too_large(send, limit)


# Added first, so innermost: the origin and token checks run before it, and
# its 413 passes back out through CORS.
app.add_middleware(BodySizeLimitMiddleware)


# The UI is served from this origin, so CORS is only for the Vite dev server
# (under COLLIMATE_DEV=1) and any COLLIMATE_CORS_ORIGINS. Never "*": on a
# tokenless loopback server that lets any web page the user visits read the
# API, which lists and reads local files. OriginGuardMiddleware below refuses
# the requests themselves.
from auth_middleware import allowed_origins as _allowed_origins

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(_allowed_origins()),
    allow_methods=["*"],
    allow_headers=["*"],
)

# Opt-in bearer-token auth. Added after CORS so it sits outside it: with a
# token configured, nothing — routes, static assets, SSE, WebSockets — is
# reachable without it, except GET /health. Configured from the environment
# here and (optionally again) by cli_main's --token; unset keeps the open
# loopback server.
from auth_middleware import (
    BearerAuthMiddleware,
    OriginGuardMiddleware,
    _with_open_cors,
    auth as _auth,
    cell_file_caps,
)

_auth.configure(os.environ.get("COLLIMATE_TOKEN"))
app.add_middleware(BearerAuthMiddleware)


_FONTS_PREFIX = "/fonts/"


class PublicFontsMiddleware:
    """The origin guard, with the bundled fonts open to every Origin.

    ``/fonts/`` (frontend/public/fonts, built into static/fonts/) holds the
    bundled font files and the ``fonts.css`` that declares them. Renderer frames,
    the html preview and the status frames run without ``allow-same-origin``,
    and a browser fetches ``@font-face`` files in CORS mode, so their font
    loads arrive with ``Origin: null``, which :class:`OriginGuardMiddleware`
    refuses. A GET/HEAD under ``/fonts/`` skips the guard (still behind the
    token, and on a loopback Host when tokenless) and answers with
    ``Access-Control-Allow-Origin: *``; every other request goes through the
    guard unchanged.
    """

    def __init__(self, app) -> None:
        self.app = app
        self.guarded = OriginGuardMiddleware(app)

    async def __call__(self, scope, receive, send) -> None:
        if self._font_read(scope):
            await self.app(scope, receive, _with_open_cors(send))
            return
        await self.guarded(scope, receive, send)

    @staticmethod
    def _font_read(scope) -> bool:
        path = scope.get("path", "")
        if (
            scope["type"] != "http"
            or scope.get("method") not in ("GET", "HEAD")
            or not path.startswith(_FONTS_PREFIX)
            or ".." in path.split("/")
        ):
            return False
        host = next(
            (v.decode("latin-1") for k, v in scope.get("headers", ()) if k == b"host"), None,
        )
        return _auth.enabled or OriginGuardMiddleware._trusted_host(host)


# Outermost: a foreign Origin or non-loopback Host (tokenless), an opaque
# Origin (either mode) is refused before anything else runs, except a font
# read (PublicFontsMiddleware), and the cell-file capability route is
# resolved here (see OriginGuardMiddleware).
app.add_middleware(PublicFontsMiddleware)


@app.post("/auth/session", include_in_schema=False)
async def _auth_session_probe():
    """The desktop shell's auth probe for a remote server (Servers → Add /
    Test). With a token configured the middleware has already checked it, so
    reaching here means the caller presented it: 204 (a missing or wrong
    token got the middleware's 401). 404 when auth is off: an open server."""
    if not _auth.enabled:
        return JSONResponse(status_code=404, content={"detail": "auth not enabled"})
    return Response(status_code=204)


@app.get("/api/channels/{channel}/cells/{cell_id}/file_cap")
async def cell_file_cap(channel: str, cell_id: str):
    """The read capability for one cell's files, which the renderer hosts
    build every ``api.getFileUrl`` URL on:
    ``/api/cellcap/<cap>/channels/<channel>/cells/<cell_id>/files/<path>``
    is the only cell-file URL an opaque-origin frame (a rendered page's
    sandboxed preview) may read. Same-origin only, like every route; see
    auth_middleware.CellFileCaps."""
    return {"cap": cell_file_caps.cap(channel, cell_id)}

# The shell<->server protocol number. The desktop client refuses to drive a
# server whose `bridge` it doesn't implement, so bump it only on a breaking
# change to that contract.
# 2: guests moved from <webview> tags to main-process WebContentsViews —
#    string guest ids, bridge-call creation, nativeGuests/guestCapture
#    capability names.
# 3: live page zoom is required by the desktop geometry bridge.
# 4: the interface scale (getInterfaceScale/setInterfaceScale over the
#    collimate:interface-scale channel) joins the bridge.
# 5: guests present as a device: createGuest's `device` replaces
#    `userAgent`, and collimate:guest-set-device switches a live guest.
# 6: one window = one server origin: ServerInfo carries {id, kind} in place
#    of `mode`, and the servers registry ops (collimate:servers-*,
#    collimate:shell-prefs-*) join the bridge.
BRIDGE_PROTOCOL = 6


# Liveness: process is up and the event loop is responsive. Cheap; no I/O.
# Also the desktop shell's handshake — it reads `version` + `bridge` here to
# decide whether it can talk to this server at all.
@app.get("/health")
async def health():
    from app_version import app_version
    return {"status": "ok", "version": app_version(), "bridge": BRIDGE_PROTOCOL}


# Readiness: the things the app needs to actually serve traffic — DB
# reachable, blob dir writable. Returns 503 with details so an orchestrator
# can keep traffic off until the install is healthy.
# Plain def: the checks below do blocking disk I/O; FastAPI runs this in
# the threadpool so it doesn't stall the event loop.
_readiness_diagnostics: dict = {"bundled": "checking", "cas_usage": "checking"}


def _load_readiness_diagnostics() -> None:
    """Startup diagnostics, published atomically; an installed wheel's
    bundled trees gate readiness."""
    global _readiness_diagnostics
    checks = {}
    # CAS disk usage — informational only, never 503's. Surfaces the
    # trend so an operator can spot runaway growth before disks fill.
    try:
        import cas_gc
        usage = cas_gc.disk_usage()
        checks["cas_blobs"] = usage["blob_count"]
        checks["cas_bytes"] = usage["bytes"]
        if usage.get("trash_bytes"):
            checks["cas_trash_bytes"] = usage["trash_bytes"]
    except Exception as exc:
        checks["cas_usage"] = f"error: {exc}"

    # The bundled trees (content, built-ins, module snapshot) against the
    # installed wheel's RECORD: a readiness gate for an installed
    # distribution. A checkout reads its live trees and reports "checkout".
    try:
        import bundled_integrity
        result = bundled_integrity.verify_bundled()
        if result["status"] == "drift":
            n = (
                len(result["mismatched"])
                + len(result["missing"])
                + len(result["extra"])
            )
            checks["bundled"] = f"drift: {n} files differ from the collimate RECORD"
        else:
            checks["bundled"] = result["status"]
    except Exception as exc:
        # A packaged server also fails readiness on verification errors.
        checks["bundled"] = f"verify_error: {exc}"

    _readiness_diagnostics = checks


@app.get("/ready")
def ready():
    checks: dict = {}
    ok = True

    try:
        # Readiness probes the global database independently of channel creation.
        from database import get_global_db_connection
        conn = get_global_db_connection()
        conn.execute("SELECT 1").fetchone()
        conn.close()
        checks["db"] = "ok"
    except Exception as exc:
        ok = False
        checks["db"] = f"error: {exc}"

    from cas import OBJECTS_DIR as cas_dir
    try:
        os.makedirs(cas_dir, exist_ok=True)
        if not os.access(cas_dir, os.W_OK):
            raise PermissionError(f"{cas_dir} not writable")
        checks["cas"] = "ok"
    except Exception as exc:
        ok = False
        checks["cas"] = f"error: {exc}"

    checks.update(_readiness_diagnostics)
    import bundled_integrity
    if bundled_integrity.is_packaged(__file__) and checks.get("bundled") != "ok":
        ok = False

    if not ok:
        return JSONResponse(status_code=503, content={"status": "not_ready", "checks": checks})
    return {"status": "ok", "checks": checks}


# --- Existing UI routes ---
app.include_router(system_router)
app.include_router(channels_router)
app.include_router(stacks_router)
app.include_router(generation_router)

# --- New execution-pipeline routes ---
app.include_router(runs_router)
app.include_router(proxy_router)
app.include_router(blobs_router)
from routes.handlers import router as handlers_router
app.include_router(handlers_router)
from routes.builtins import router as builtins_router
app.include_router(builtins_router)
from routes.capabilities_stack import router as capabilities_stack_router
app.include_router(capabilities_stack_router)
from routes.file_mutations import router as file_mutations_router
app.include_router(file_mutations_router)
from routes.annotations import router as annotations_router
app.include_router(annotations_router)
app.include_router(cells_router)
app.include_router(options_router)
app.include_router(terminal_router)
app.include_router(chat_live_router)
# Per-cell panel sessions (runners, files, handlers; channel ui_state KV).
# Pure UI scratch state — never exported, never part of a cell's history.
app.include_router(pick_sessions_router)
app.include_router(cell_display_router)
app.include_router(runners_router)
app.include_router(suggested_router)
app.include_router(accounts_router)
app.include_router(transfer.router)
app.include_router(mirror_router)

# ---------------------------------------------------------------------------
# Static file serving — bundled Vite frontend
# ---------------------------------------------------------------------------
_static_dir = os.path.join(current_dir, "static")
_INDEX_PATH = os.path.join(_static_dir, "index.html")


# index.html's module-mirror import map (frontend/index.html), built for the
# default ``../api`` base.
_MIRROR_MAP = re.compile(r'(<script type="importmap" id="collimate-mirror-map">)[^<]*(</script>)')


def _mirror_import_map(api_base: str) -> str:
    """The import map sending each mirrored CDN to ``<api_base>/mirror/<host>/``."""
    import json as _json
    from collimate._host.module_mirror import HOSTS
    base = api_base.rstrip("/")
    imports = {f"https://{host}/": f"{base}/mirror/{host}/" for host in HOSTS}
    return _json.dumps({"imports": imports}, separators=(",", ":")).replace("<", "\\u003c")


def _render_index_with_runtime_config() -> str | None:
    """Read api/static/index.html and inject window.__COLLIMATE_CONFIG__
    inline before </head>.

    Why inject at serve-time instead of bundling a runtime fetch:
    Vite's HTML processor refuses non-module <script src> tags, and
    declaring it as a module makes Vite try to resolve the path as a
    local entry at build time (the config endpoint isn't on disk).
    Doing the injection here keeps the static bundle untouched and
    sidesteps the build-tooling fight.

    Returns None if the index.html doesn't exist (dev mode without a
    bundled frontend); the StaticFiles mount handles that branch.
    """
    if not os.path.isfile(_INDEX_PATH):
        return None
    try:
        with open(_INDEX_PATH, "r", encoding="utf-8") as f:
            html = f.read()
    except OSError:
        return None
    import json as _json
    config = runtime_config()
    if config["apiBase"] != "../api":
        html = _MIRROR_MAP.sub(lambda match: match.group(1) + _mirror_import_map(config["apiBase"]) + match.group(2),
                               html, count=1)
    payload = _json.dumps(config)
    snippet = (
        "<script>"
        f"window.__COLLIMATE_CONFIG__ = {payload};"
        "</script>"
    )
    # Inject just before </head>; falls through to <body> for HTML
    # documents that lack a head close tag (shouldn't happen but
    # don't hard-fail). Worst case the snippet ends up nowhere and
    # the frontend uses build-time defaults.
    if "</head>" in html:
        return html.replace("</head>", snippet + "</head>", 1)
    if "<body" in html:
        return html.replace("<body", snippet + "<body", 1)
    return html


if os.path.isdir(_static_dir):
    @app.get("/", include_in_schema=False)
    @app.get("/index.html", include_in_schema=False)
    async def _serve_index_with_config():
        rendered = _render_index_with_runtime_config()
        if rendered is None:
            return JSONResponse(
                status_code=404, content={"detail": "index.html not built"},
            )
        return Response(content=rendered, media_type="text/html")

    app.mount("/", StaticFiles(directory=_static_dir, html=True), name="static")


def cli_main() -> None:
    """Console entry point — `collimate` in PATH after `pip install collimate`.

    Runs the FastAPI app on loopback so pip-installed end users get the
    same behaviour as `python main.py` from a source checkout. Kept
    deliberately thin — further configuration knobs (log level, reload)
    can grow as flags when someone actually asks for them.

    Bind rationale: loopback only. Browsers grant http://localhost and
    http://127.0.0.1 a "secure context" for free, which is what code-
    server webviews and the Claude Code extension need (service
    workers, clipboard APIs, etc.). Loopback-only also means the API
    isn't exposed to the LAN by accident.

    Remote access story: the server still binds loopback and speaks
    plain HTTP — it never grows TLS code. An operator who wants remote
    access sets a token (``--token`` / ``COLLIMATE_TOKEN``) and puts a
    TLS reverse proxy (Caddy, nginx, Tailscale Serve) in front; with a
    token set, everything except GET /health requires
    ``Authorization: Bearer <token>`` — including the UI's static
    assets, SSE, and WebSockets. The desktop shell injects that header
    for the whole session, so remote access is a shell feature, not a
    browser one.

    Data-dir contract: every persistent path in the app (`.collimate/`,
    generator workdirs) is CWD-relative, so the data dir
    IS the process CWD. `--data-dir` beats $COLLIMATE_DATA beats the
    current CWD (the default, i.e. unchanged behaviour); the chosen dir is
    created and chdir'd into before the server starts, so the resolution
    happens exactly once, here, and nothing downstream has to know.
    """
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(
        prog="collimate",
        description=(
            "Run the Collimate server, headless, on loopback. "
            "Open the printed URL in a browser; the desktop app is the "
            "full-window experience."
        ),
    )
    parser.add_argument(
        "-p", "--port", type=int, default=5001, metavar="N",
        help=(
            "port to listen on (default: 5001). Always binds loopback only "
            "(127.0.0.1), so changing the port never exposes the server to the LAN."
        ),
    )
    parser.add_argument(
        "--data-dir", metavar="PATH", default=None,
        help=(
            "directory to keep state in — .collimate/ is "
            "created inside it (default: $COLLIMATE_DATA, else the current "
            "directory). Created if missing."
        ),
    )
    parser.add_argument(
        "--token", metavar="TOKEN", default=None,
        help=(
            "require this bearer token on every request except GET /health "
            "(default: $COLLIMATE_TOKEN, else no auth). For serving through a "
            "TLS reverse proxy to the desktop app; the server itself still "
            "binds loopback only."
        ),
    )
    args = parser.parse_args()
    port = args.port

    token = args.token or os.environ.get("COLLIMATE_TOKEN")
    _auth.configure(token)

    data_dir = args.data_dir or os.environ.get("COLLIMATE_DATA") or os.getcwd()
    data_dir = os.path.abspath(os.path.expanduser(data_dir))
    os.makedirs(data_dir, exist_ok=True)
    os.chdir(data_dir)

    # This print IS the front door: there is no window to open, so the URL
    # a user pastes into a browser has to be the first thing they see.
    print(f"\n  Collimate is running at:  http://localhost:{port}")
    print(f"  Data directory:           {data_dir}\n")
    if token:
        print("  Auth:                     bearer token required (GET /health exempt)\n")
    print(
        "  Other ways to run it:\n"
        "    collimate --port N            run on a different port (default 5001)\n"
        "    collimate --data-dir PATH     keep state somewhere other than the CWD\n"
        "    collimate --token TOKEN       require a bearer token (for remote access\n"
        "                                  behind a TLS reverse proxy)\n"
    )
    # Terminal input backpressure detaches explicitly after 20 seconds; keep
    # transport pongs above that bound so it can send recoverable 1013.
    import signal
    # Uvicorn re-raises a captured signal after lifespan teardown. SIGTERM's
    # default action would exit before asyncio.Runner closes pending tasks;
    # unwind like Ctrl-C so their final cleanup still runs.
    previous_term = signal.signal(signal.SIGTERM, signal.default_int_handler)
    try:
        uvicorn.run(app, host="127.0.0.1", port=port, ws_ping_timeout=40.0)
    finally:
        signal.signal(signal.SIGTERM, previous_term)


if __name__ == "__main__":
    cli_main()
