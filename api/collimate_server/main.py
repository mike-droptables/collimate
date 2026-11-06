import asyncio
import sys
import os
import logging
import re

current_dir = os.path.dirname(os.path.abspath(__file__))

from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

logger = logging.getLogger(__name__)

from collimate_server.genesis import bootstrap
# Resolve the per-server container owner only when cleanup runs: cli_main
# changes into the data directory after this module loads.
from collimate_server.startup_cleanup import reap_orphan_containers
from collimate_server.state import state
import collimate_server.channel_guard as channel_guard
from collimate_server.routes.system import router as system_router
from collimate_server.routes.channels import router as channels_router
from collimate_server.routes.stacks import router as stacks_router
from collimate_server.routes.generation import router as generation_router
from collimate_server.routes.runs import router as runs_router
# Proxy router forwards traffic to subprocess generators (vscode/codespaces
# tunnels).
from collimate_server.routes.proxy import router as proxy_router
from collimate_server.routes.blobs import router as blobs_router, runtime_config
from collimate_server.routes.cells import router as cells_router
from collimate_server.routes.options import router as options_router
from collimate_server.routes.chat_live import router as chat_live_router
from collimate_server.routes.terminal import router as terminal_router
from collimate_server.routes.pick_sessions import router as pick_sessions_router
from collimate_server.routes.cell_display import router as cell_display_router
from collimate_server.routes.runners import router as runners_router
from collimate_server.routes.suggested import router as suggested_router
from collimate_server.routes.accounts import router as accounts_router
from collimate_server.routes import transfer
from collimate_server.routes.mirror import router as mirror_router


class StartupRefused(RuntimeError):
    """Starting would overwrite or hide the user's data. The server exits
    with this message instead; nothing on disk was changed."""


# The data dir's server lock. One process owns a data dir at a time: the
# server holds an exclusive flock on this file for its whole lifetime, so a
# second server (any launcher: the desktop shell, `collimate`, `python -m
# collimate_server`, uvicorn) is refused instead of persisting its own copy
# of the state over the other's. The kernel drops the lock with the process,
# so a crash never leaves it held. The file carries the holder's pid, for the
# refusal message only.
SERVER_LOCK_PATH = os.path.join(".collimate", "server.lock")


def lock_data_dir() -> int:
    """Take the data dir's server lock (CWD-relative) and return its fd.
    Raises :class:`StartupRefused` when another process holds it or the
    lock file cannot be opened (a symlink in its place, say)."""
    from collimate_server.data_dir_lock import LockHeld, LockOpenError, acquire
    os.makedirs(".collimate", exist_ok=True)
    try:
        return acquire(SERVER_LOCK_PATH)
    except LockHeld as exc:
        holder = exc.holder
        raise StartupRefused(
            f"another Collimate process (pid {holder}) holds "
            f"{os.path.abspath(SERVER_LOCK_PATH)}: a server is already "
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
    from collimate_server.data_dir_lock import release
    release(fd)


_APP_DB = os.path.join(".collimate", "app.db")
_APP_DB_ATTEMPTS = 3
_APP_DB_RETRY_SECONDS = 1.0

# The exit code of a start refused because the default channel location
# belongs to another installation (EX_CONFIG), so the desktop shell can
# offer --claim-channels and --channels-dir; cli_main exits with it.
LOCATION_OWNED_EXIT = 78
_refused_exit: int | None = None


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


def establish_channel_locations() -> None:
    """Startup steps 3-6: the previous layout's one log line, then ownership
    of the default channel location, each owned location's reconcile and
    the channel index (``channel_paths.establish``). A default location
    another installation owns refuses the start (exit 78, with a
    machine-readable stderr line) unless ``--claim-channels``."""
    global _refused_exit
    import collimate_server.channel_paths as channel_paths
    note = channel_paths.previous_layout_note()
    if note:
        logger.warning("startup: %s", note)
    try:
        outcomes = channel_paths.establish()
    except channel_paths.LocationOwned as refused:
        print(channel_paths.refusal_line(refused), file=sys.stderr, flush=True)
        _refused_exit = LOCATION_OWNED_EXIT
        raise StartupRefused(str(refused)) from None
    except channel_paths.OwnerRunning as running:
        raise StartupRefused(f"--claim-channels: {running}") from None
    except OSError as exc:
        raise StartupRefused(f"could not set up the channel location: {exc}") from exc
    for outcome in outcomes:
        logger.warning("startup: %s", outcome)


def restore_app_state() -> None:
    """Startup step 7: every indexed channel's layout into ``state``, and
    the active channel.

    Each channel loads from its folder (``genesis.load_channel``: the name
    from channel.json, the layout and files from channel.db). A channel whose
    layout does not validate, or whose accepted files cannot be read, is left
    unavailable with the reason and its data untouched; the others load.
    Every restored channel gets its Capabilities stack with the build's
    built-in cards (``genesis.ensure_capabilities_stack``), persisted with
    the next write, so an app upgrade's cards show without a reset."""
    import sqlite3
    import collimate_server.channel_paths as channel_paths
    from collimate_server.genesis import (
        _saved_active, ensure_capabilities_stack, load_active_channel_id, load_channel,
        reconcile_restored_cell_files,
    )
    active = _app_db_call("read the active channel", load_active_channel_id)
    _saved_active[os.getcwd()] = active
    restored = []
    for channel_id in channel_paths.indexed_channels():
        try:
            channel = load_channel(channel_id)
            reconcile_restored_cell_files(channel)
        except (ValueError, OSError, sqlite3.Error) as exc:
            reason = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            channel_paths.mark_unavailable(channel_id, f"its saved state does not open: {reason}")
            logger.error("startup: the channel %s is unavailable, its folder untouched: %s", channel_id, reason)
            continue
        ensure_capabilities_stack(channel)
        restored.append(channel)
    created = {found.channel_id: str(found.manifest.get("created_at") or "") for found in channel_paths.homes()}
    restored.sort(key=lambda channel: (created.get(str(channel.id), ""), str(channel.id)))
    state.channels = restored
    ids = {str(channel.id) for channel in restored}
    from uuid import UUID
    # With no saved active channel, the first one not archived (an archived
    # channel opens only from the Archived tab).
    from collimate_server.archive import successor
    state.active_channel_id = UUID(active) if active in ids else successor(restored, None)
    from collimate_server.cell_metadata import recover_pending_cell_metadata
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
        from collimate_server.keystore import available
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
    from collimate_server.events import capture_loop
    capture_loop()

    # Re-open the run-creation gate (set during the previous shutdown) so
    # repeated lifespan cycles in one process — tests, embedded servers —
    # accept runs again.
    from collimate_server.run_registry import mark_shutting_down
    mark_shutting_down(False)

    # 1. Bootstrap .collimate/ if this is the first launch in this CWD
    bootstrap()

    # 1.5 Open app.db, which runs its migrations: one this build does not
    # upgrade (older than global schema 8) refuses the start with the reason.
    from collimate_server.database import get_global_db_connection
    try:
        get_global_db_connection().close()
    except RuntimeError as exc:
        logger.error("startup: %s (%s)", exc, os.path.abspath(_APP_DB))
        raise StartupRefused(str(exc)) from exc

    # A stable generator tree a stopped server was still writing
    # (envs/.staging-*) is removed before any run can stage one.
    from collimate_server.run_staging import remove_staging_trees
    remove_staging_trees()

    # The module mirror's bundled snapshot (module-snapshot/), indexed once:
    # absent serves upstream misses only, a broken one refuses the start.
    from collimate_server.genesis import bundled_dir
    import collimate_server.module_mirror as module_mirror
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
    from collimate_server.builtin_packages import publish_bundles
    publish_bundles()

    # 2. The channel locations: ownership (a refusal, or --claim-channels),
    #    each location's reconcile, the channel index; then every indexed
    #    channel's layout from its folder (see restore_app_state).
    establish_channel_locations()
    restore_app_state()

    # Recover stale runs only after restoring their channels, before serving.
    from collimate_server.process_manager.finalization import recover_interrupted_runs
    recovered = recover_interrupted_runs()

    # The channel git stores' hygiene, before serving and the reap thread,
    # so before any quarantine, store ref or lock can appear: stale
    # quarantines and export staging go, and every store's refs and locks.
    try:
        import collimate_server.git_store as git_store
        from collimate_server.state import channel_db_name as _store_of
        for outcome in git_store.startup_hygiene(_store_of(c) for c in state.channels):
            logger.warning("startup: %s", outcome)
    except Exception:
        logger.exception("git store startup hygiene failed")

    # No "ensure at least one channel" fallback: zero channels is a valid state.
    # The frontend renders the channel-creation view when none exist.

    # 4. Periodic passive checkpoint copies available frames without
    # reserving the writer lock while a reader pins the WAL. Transaction
    # durability remains governed by SQLite's synchronous policy.
    import collimate_server.wal_checkpoint as wal_checkpoint
    interval = float(os.environ.get("COLLIMATE_WAL_CHECKPOINT_SECONDS", "60") or 60)
    _wal_task = asyncio.create_task(wal_checkpoint.run_periodic(interval))

    # The reaps and the lease sweep (0.5 / 0.6) start last, once nothing
    # above can refuse the start: a refused start leaves no worker thread
    # reaping or sweeping on after its lifespan releases the server lock.
    _reap_task = asyncio.create_task(asyncio.to_thread(reap_orphan_containers, _boot_ts, recovered))

    from collimate_server.state import start_persistence_writer, stop_persistence_writer
    start_persistence_writer()
    from collimate_server.user_validation import start_user_validation
    start_user_validation()
    _ready_task = asyncio.create_task(asyncio.to_thread(_load_readiness_diagnostics))
    # Last (startup step 11): the collection scheduler, which collects at
    # once when the last collection is more than a day old.
    import collimate_server.cas_gc as cas_gc
    _gc_task = asyncio.create_task(cas_gc.run_periodic())

    # Admissions freeze each renderer's import graph on this loop.
    import collimate_server.offline_modules as offline_modules
    offline_modules.attach(asyncio.get_running_loop())

    yield

    offline_modules.detach()
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
    from collimate_server.routes.runs import stop_all_runs
    from collimate_server.run_registry import mark_shutting_down
    from collimate_server.routes.runs.teardown import wait_for_run_reaps
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

    from collimate_server.routes.proxy import close_proxy_transport
    await close_proxy_transport()
    from collimate_server.user_validation import close_user_validation
    await asyncio.to_thread(close_user_validation)
    from collimate_server.verifier_pool import close_verifier_pool
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
    # A storage survey or size update still walking stops and finishes
    # before the data dir is released.
    import collimate_server.storage_survey as storage_survey
    await storage_survey.shutdown()

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


# Every write to a channel in a refusing state (archived, moving) is refused here,
# before its parameters and body are validated (channel_guard).
app = FastAPI(lifespan=lifespan, dependencies=[Depends(channel_guard.channel_guard)])
app.add_exception_handler(channel_guard.ChannelRefused, channel_guard.refused_response)


def _storage_refusal(request, exc):
    """A channel with no folder at all (``channel_not_found``, 404), one
    whose folder is unavailable (``channel_unavailable``) or no default
    location for a new one (``no_default_location``), both 409: the code and
    the reason."""
    import collimate_server.channel_paths as channel_paths
    status = 404 if isinstance(exc, channel_paths.ChannelNotFound) else 409
    return JSONResponse(status_code=status, content={"detail": {"code": exc.code, "message": str(exc)}})


def _install_storage_refusals() -> None:
    import collimate_server.channel_paths as channel_paths
    app.add_exception_handler(channel_paths.ChannelUnavailable, _storage_refusal)
    app.add_exception_handler(channel_paths.NoDefaultLocation, _storage_refusal)


_install_storage_refusals()

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
    ("POST", re.compile(r"/api/channels/[^/]+/blob/stream")),
    ("POST", re.compile(r"/api/channels/[^/]+/import/zip")),
    ("POST", re.compile(r"/api/import/zip")),
    ("POST", re.compile(r"/api/channels/[^/]+/import/repository")),
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
        if re.fullmatch(r"/api/channels/[^/]+/blob", scope.get("path", "")):
            from collimate_server.cas import MAX_BLOB_BYTES
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
from collimate_server.auth_middleware import allowed_origins as _allowed_origins

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(_allowed_origins()),
    allow_methods=["*"],
    allow_headers=["*"],
)

# Opt-in bearer-token auth. Added after CORS so it sits outside it: with a
# token configured, nothing — routes, static assets, SSE, WebSockets — is
# reachable without it, except GET /health. Configured from the environment
# here and again by cli_main (--listen mints a token when the environment
# has none); unset keeps the open loopback server.
from collimate_server.auth_middleware import (
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
# 7: the host's shortcut chords reach the guests (collimate:guest-keys), and
#    a guest reports a consumed chord and the modifier releases as the
#    GuestEvent `key`.
# 8: the page's per-device Settings: buildInfo() replaces the bootstrap's
#    localBuild, and checkForUpdates / get/setAutomaticUpdates join the bridge.
BRIDGE_PROTOCOL = 8


# Liveness: process is up and the event loop is responsive. Cheap; no I/O
# after the first call (the contract digest is cached). Also the desktop
# shell's handshake — it reads `version`, `bridge` and `contract` here to
# decide whether it can talk to this server at all: `contract` is the API
# contract version (collimate.shared.api_contract, the digest of the
# channel-events schema and the IPC op table), and the shell marks a server
# whose contract differs from its own instead of opening it.
@app.get("/health")
async def health():
    from collimate.shared import api_contract
    from collimate_server.app_version import app_version
    return {"status": "ok", "version": app_version(), "bridge": BRIDGE_PROTOCOL,
            "contract": api_contract()}


# Readiness: the things the app needs to actually serve traffic — DB
# reachable, blob dir writable. Returns 503 with details so an orchestrator
# can keep traffic off until the install is healthy.
# Plain def: the checks below do blocking disk I/O; FastAPI runs this in
# the threadpool so it doesn't stall the event loop.
_readiness_diagnostics: dict = {"bundled": "checking"}


def _load_readiness_diagnostics() -> None:
    """Startup diagnostics, published atomically; an installed wheel's
    bundled trees gate readiness. (Sizes are ``GET /api/storage``'s.)"""
    global _readiness_diagnostics
    checks = {}

    # The bundled trees (content, built-ins, module snapshot) against the
    # installed wheel's RECORD: a readiness gate for an installed
    # distribution. A checkout reads its live trees and reports "checkout".
    try:
        import collimate_server.bundled_integrity as bundled_integrity
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
        from collimate_server.database import get_global_db_connection
        conn = get_global_db_connection()
        conn.execute("SELECT 1").fetchone()
        conn.close()
        checks["db"] = "ok"
    except Exception as exc:
        ok = False
        checks["db"] = f"error: {exc}"

    from collimate_server.cas import APP_OBJECTS_DIR as cas_dir
    try:
        os.makedirs(cas_dir, exist_ok=True)
        if not os.access(cas_dir, os.W_OK):
            raise PermissionError(f"{cas_dir} not writable")
        checks["cas"] = "ok"
    except Exception as exc:
        ok = False
        checks["cas"] = f"error: {exc}"

    checks.update(_readiness_diagnostics)
    import collimate_server.bundled_integrity as bundled_integrity
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
from collimate_server.routes.handlers import router as handlers_router
app.include_router(handlers_router)
from collimate_server.routes.builtins import router as builtins_router
app.include_router(builtins_router)
from collimate_server.routes.capabilities_stack import router as capabilities_stack_router
app.include_router(capabilities_stack_router)
from collimate_server.routes.capability_cells import router as capability_cells_router
app.include_router(capability_cells_router)
from collimate_server.routes.user_catalog import router as user_catalog_router
app.include_router(user_catalog_router)
from collimate_server.routes.file_mutations import router as file_mutations_router
app.include_router(file_mutations_router)
from collimate_server.routes.annotations import router as annotations_router
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
from collimate_server.routes.storage import router as storage_router
app.include_router(storage_router)

# ---------------------------------------------------------------------------
# Static file serving — bundled Vite frontend
# ---------------------------------------------------------------------------
_static_dir = os.path.join(current_dir, "static")
_INDEX_PATH = os.path.join(_static_dir, "index.html")


# index.html's module-mirror import map (frontend/index.html), built for the
# default ``./api`` base.
_MIRROR_MAP = re.compile(r'(<script type="importmap" id="collimate-mirror-map">)[^<]*(</script>)')


def _mirror_import_map(api_base: str) -> str:
    """The import map sending each mirrored CDN to ``<api_base>/mirror/<host>/``."""
    import json as _json
    from collimate._host.module_mirror import HOSTS
    base = api_base.rstrip("/")
    imports = {f"https://{host}/": f"{base}/mirror/{host}/" for host in HOSTS}
    return _json.dumps({"imports": imports}, separators=(",", ":")).replace("<", "\\u003c")


def _render_index_with_runtime_config() -> str | None:
    """Read api/collimate_server/static/index.html and inject window.__COLLIMATE_CONFIG__
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
    if config["apiBase"] != "./api":
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

    `python -m collimate_server` (``__main__.py``) runs it too, from a
    source checkout or an install. Kept deliberately thin — further configuration knobs (log
    level, reload) can grow as flags when someone actually asks for them.

    Bind rationale: loopback by default. Browsers grant http://localhost
    and http://127.0.0.1 a "secure context" for free, which is what code-
    server webviews and the Claude Code extension need (service workers,
    clipboard APIs, etc.), and loopback-only means the API isn't exposed
    to the LAN by accident.

    Beyond this machine: ``--listen ADDR`` binds a LAN address, and then a
    bearer token and TLS are both on, with the token and the self-signed
    certificate minted and kept by :mod:`listen_identity`. Every request
    except GET /health then needs ``Authorization: Bearer <token>`` —
    the UI's static assets, SSE and WebSockets included — and the desktop
    shell presents it for the whole session, so pairing is a shell
    feature, not a browser one. An operator who wants a certificate a
    browser trusts keeps the loopback default, sets ``$COLLIMATE_TOKEN``
    and puts a TLS reverse proxy (Caddy, nginx, Tailscale Serve) in front.

    Data-dir contract: every persistent path in the app (`.collimate/`,
    generator workdirs) is CWD-relative, so the data dir
    IS the process CWD. `--data-dir` beats $COLLIMATE_DATA beats the
    current CWD (the default, i.e. unchanged behaviour); the chosen dir is
    created and chdir'd into before the server starts, so the resolution
    happens exactly once, here, and nothing downstream has to know. The
    token file is one of those paths, so it is read after the chdir.

    Generator runs on this machine start with ``uv run``. ``--server-env
    ROOT`` names the self-contained environment the server runs from (a
    desktop package's server passes the one the package carries): runs then
    start with its ``bin/uv`` and, where their ``requires-python`` admits
    it, its ``bin/python3`` (``process_manager.native_tools``). Without it
    they start with the ``uv`` their PATH finds and uv chooses their Python
    (a checkout, ``uvx collimate``). A containerized run uses its image's
    own uv and Python either way.
    """
    import argparse

    import uvicorn

    import collimate_server.listen_identity as listen_identity
    from collimate_server.process_manager import native_tools

    parser = argparse.ArgumentParser(
        prog="collimate",
        description=(
            "Run the Collimate server, headless. By default it listens on loopback "
            "only; open the printed URL in a browser. The desktop app is the "
            "full-window experience, and --listen lets one pair with this server "
            "from another machine."
        ),
    )
    parser.add_argument(
        "-p", "--port", type=int, default=5001, metavar="N",
        help="port to listen on (default: 5001)",
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
        "--listen", metavar="ADDR", default=None,
        help=(
            "serve beyond this machine on this IPv4 address (0.0.0.0 for every "
            "interface). Turns on TLS with a kept self-signed certificate and "
            "requires a bearer token on every request except GET /health: "
            "$COLLIMATE_TOKEN, else one kept in .collimate/access-token. "
            "Prints what the desktop app needs to pair. Without --listen the "
            "server binds loopback and speaks plain HTTP, and $COLLIMATE_TOKEN "
            "alone requires the token (for a TLS reverse proxy in front)."
        ),
    )
    parser.add_argument(
        "--channels-dir", metavar="PATH", default=None,
        help=(
            "keep this installation's channels in a channel location at PATH, "
            "created if missing (default: $COLLIMATE_CHANNELS, else "
            "~/Collimate/Channels). Consulted only while the installation "
            "owns no location yet; afterwards locations are managed in the "
            "app, and the flag is ignored with a log line."
        ),
    )
    parser.add_argument(
        "--claim-channels", action="store_true",
        help=(
            "when the default channel location belongs to another Collimate "
            "installation (the server otherwise refuses to start, exit code "
            "78), make this installation its owner and adopt its channels. "
            "Refused while that installation's server is running. Ignored "
            "once this installation owns a location."
        ),
    )
    parser.add_argument(
        "--server-env", metavar="ROOT", default=None,
        help=(
            "the self-contained environment this server runs from: generator "
            "runs on this machine start with its bin/uv and, where their "
            "requires-python allows, its bin/python3, so no Python is "
            "downloaded (default: the uv on the run's PATH, and uv's choice "
            "of Python). The desktop app passes the one it carries."
        ),
    )
    args = parser.parse_args()
    port = args.port

    # Resolved against the directory the server was started in, before the
    # chdir into the data dir below.
    if args.server_env is not None:
        try:
            native_tools.configure_server_env(os.path.abspath(os.path.expanduser(args.server_env)))
        except native_tools.Refused as refused:
            parser.error(f"--server-env {args.server_env}: {refused}")

    # Resolved against the launch directory too, before the chdir.
    import collimate_server.channel_paths as channel_paths
    channel_paths.configure_startup(channels_dir=args.channels_dir, claim=args.claim_channels)

    data_dir = args.data_dir or os.environ.get("COLLIMATE_DATA") or os.getcwd()
    data_dir = os.path.abspath(os.path.expanduser(data_dir))
    os.makedirs(data_dir, exist_ok=True)
    os.chdir(data_dir)

    serve: dict = {}
    if args.listen:
        try:
            listen = listen_identity.listen_address(args.listen)
            token = listen_identity.access_token()
            cert = listen_identity.certificate(listen)
        except listen_identity.Refused as refused:
            parser.error(str(refused))
        _auth.configure(token.value)
        serve = {"host": listen, "ssl_certfile": str(cert.cert_file), "ssl_keyfile": str(cert.key_file)}
        # This print IS the front door: the three lines the desktop app's
        # Add server form asks for.
        print(listen_identity.pairing_printout(
            listen_identity.pairing_urls(listen, port), token, cert, data_dir, show_token=sys.stdout.isatty(),
        ), flush=True)
    else:
        token_value = os.environ.get("COLLIMATE_TOKEN")
        _auth.configure(token_value)
        serve = {"host": "127.0.0.1"}
        # This print IS the front door: there is no window to open, so the
        # URL a user pastes into a browser has to be the first thing they see.
        print(f"\n  Collimate is running at:  http://localhost:{port}")
        print(f"  Data directory:           {data_dir}\n")
        if token_value:
            print("  Auth:                     bearer token required (GET /health exempt)\n")
        print(
            "  Other ways to run it:\n"
            "    collimate --port N            run on a different port (default 5001)\n"
            "    collimate --data-dir PATH     keep state somewhere other than the CWD\n"
            "    collimate --listen 0.0.0.0    serve the LAN with TLS and a token, for the\n"
            "                                  desktop app on another machine\n",
            flush=True,
        )
    # Terminal input backpressure detaches explicitly after 20 seconds; keep
    # transport pongs above that bound so it can send recoverable 1013.
    import signal
    # Uvicorn re-raises a captured signal after lifespan teardown. SIGTERM's
    # default action would exit before asyncio.Runner closes pending tasks;
    # unwind like Ctrl-C so their final cleanup still runs.
    previous_term = signal.signal(signal.SIGTERM, signal.default_int_handler)
    try:
        uvicorn.run(app, port=port, ws_ping_timeout=40.0, **serve)
    except SystemExit:
        # Uvicorn exits 3 on any refused start; a location another
        # installation owns has its own code (LOCATION_OWNED_EXIT).
        if _refused_exit is not None:
            sys.exit(_refused_exit)
        raise
    finally:
        signal.signal(signal.SIGTERM, previous_term)


if __name__ == "__main__":
    cli_main()
