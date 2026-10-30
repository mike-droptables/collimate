"""ProcessManager — owns a single generator run's lifecycle.

Key responsibilities:
  - Launch the generator via ``uv run <entrypoint>`` with directory isolation
  - Communicate over a localhost TCP socket for IPC
  - Forward stdout/stderr blindly to the run_events log table
  - Handle explicit artifact commits (no more os.walk snapshotting)
  - ACK commits via the TCP socket so generators can block until CAS write completes

The class body is small because the implementation is split across the
topical mixins mixed in below — see each for its own docstring.
"""

import asyncio
import json
import logging
import os

from process_signals import signal_process_group
import threading
from collections import OrderedDict, deque

from runner_permissions import RunnerPermissions

from .artifacts import ArtifactsMixin
from .cells import CellsMixin
from .finalization import FinalizationMixin
from .own_cell_writes import OwnCellWritesMixin
from .ipc import IPCMixin
from .finalization import wait_process_exit
from .streams import StreamsMixin

_logger = logging.getLogger("process_manager.manager")


def read_generator_manifest(directory: str) -> dict:
    """Read the sole visible generator definition in a staged package."""
    import yaml
    from collimate.definitions import discover_definitions
    try:
        names = ["package/" + name for name in os.listdir(directory)
                 if os.path.isfile(os.path.join(directory, name))]
    except OSError:
        return {}
    packages = discover_definitions(names).packages
    if len(packages) != 1 or packages[0].kind != "generator":
        return {}
    manifest_path = os.path.join(directory, packages[0].definition_path.split("/", 1)[1])
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            loaded = yaml.safe_load(f)
    except (yaml.YAMLError, OSError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


async def retain_account_leases(account_context: dict, run_id: str) -> None:
    """Retain a run's account leases (CLI login stores) before its manager
    is built, off the event loop: retaining takes file locks and reads the
    keyring, which blocks for as long as a locked Secret Service does. Run
    end releases them (``ipc._wait_and_cleanup``); a start that fails
    releases them in its unwind.

    A cancelled caller still waits for the worker thread before the
    cancellation propagates. The thread cannot be stopped, so a lease it
    registered after the unwind had released the run's leases would stay
    held under a run that never started. When the wait itself is cancelled
    (the event loop closing cancels every task, the retain included), the
    caller marks the retain abandoned and the thread releases what it took
    once it finishes."""
    from accounts import release_run_account_leases, retain_account_context
    gate = threading.Lock()
    abandoned = False

    def retain_then_release_if_abandoned() -> None:
        try:
            retain_account_context(account_context, run_id)
        finally:
            with gate:
                release = abandoned
            if release:
                try:
                    release_run_account_leases(run_id)
                except Exception:
                    _logger.warning("Account leases of %s, whose start was cancelled, could not be released",
                                    run_id, exc_info=True)

    retain = asyncio.ensure_future(asyncio.to_thread(retain_then_release_if_abandoned))
    try:
        await asyncio.shield(retain)
    except asyncio.CancelledError:
        while not retain.done():
            try:
                await asyncio.wait({retain})
            except asyncio.CancelledError:
                continue
        if retain.cancelled():
            with gate:
                abandoned = True
        else:
            retain.exception()  # the unwind releases whatever it took
        raise


class ProcessManager(
    StreamsMixin,
    IPCMixin,
    ArtifactsMixin,
    CellsMixin,
    FinalizationMixin,
    OwnCellWritesMixin,
):
    def __init__(
        self,
        channel: str,
        run_id: str,
        app_dir: str,
        workspace_dir: str,
        entrypoint: str,
        generator_name: str = "default",
        cell_id: str | None = None,
        *,
        permissions: RunnerPermissions,
        packages: dict | None = None,
        runner_id: str | None = None,
        runner_name: str | None = None,
        account_context: dict | None = None,
        account_requirements: dict | None = None,
        generator_cell_version_id: str | None = None,
        input_tip_id: str | None = None,
        optional_requirements: list[str] | None = None,
        app_root_dir: str | None = None,
        created_cell: bool = False,
    ):
        from database import canonical_channel_id
        self.channel = canonical_channel_id(channel)
        self.run_id = run_id
        self.app_dir = app_dir              # Generator code dir (CWD, read-only)
        self.workspace_dir = workspace_dir  # Hydrated input files (read/write)
        # The host-staged package and shared sibling libraries; never infer a
        # parent from a generator-provided path when granting environment access.
        self.app_root_dir = os.path.realpath(app_root_dir or app_dir)
        self.env_passthrough_sockets: frozenset[str] = frozenset()
        self.entrypoint = entrypoint
        self.generator_name = generator_name
        import copy
        # The run's private account snapshot. Its leases (CLI login stores)
        # are retained by the spawning route before the
        # manager is built (retain_account_leases, off the event loop) and
        # run end releases them (ipc._wait_and_cleanup).
        self.account_context = copy.deepcopy(account_context or {})
        self.account_requirements = copy.deepcopy(account_requirements or {})
        # Provenance resolved at run start: the generator SOURCE VERSION id (a
        # cells-row on the generator cell's version chain, drafts flushed)
        # and the exact input version tip the run hydrated from. Stamped
        # onto every cells row this run writes + emitted as input_0 edge
        # (see cells.py).
        self.generator_cell_version_id = generator_cell_version_id
        self.input_tip_id = input_tip_id
        # The visible canvas cell this run writes.
        self.cell_id = cell_id
        # Host-owned authority: only the clone made for this run may be named.
        self.created_cell = created_cell

        # The saved runner this run executes
        # and the host-minted label naming its run folder ``_runs/<label>/``
        # — every file there is the run's locked file (run_folders.py).
        from run_folders import run_folder, mint_run_label
        self.runner_id = runner_id
        self.runner_name = runner_name
        self.label = mint_run_label(runner_id, run_id)
        self.folder = run_folder(self.label)

        # Set at run end (ipc._wait_and_cleanup) before the folder sweep:
        # from then on every emit is refused, so nothing re-creates the
        # swept folder. ``_inflight_emits`` are the update_cell writes still
        # on worker threads, which the sweep waits for.
        self.sealed: bool = False
        self._inflight_emits: set = set()
        # Only host materialization can authorize an extra directory mount.
        # Keys are active account lease IDs; generators cannot add grants.
        self.host_bindings: dict[str, dict[str, str]] = {}

        # The filename suffixes of the generator's UI files (manifest
        # ``ui_files``), read at start(): the only files the run may write
        # into its own folder (host._emit_partial refuses the rest as
        # ``ui_type``). Sent to the child as COLLIMATE_UI_FILES.
        self.ui_files: tuple[str, ...] = ()

        # Bounded to 10000 entries (each a whole stdout/stderr line, up to
        # 1 MB); if the 500 ms writer falls that far behind, the oldest
        # unflushed lines are dropped.
        self.log_buffer: deque = deque(maxlen=10000)
        self._log_flush_lock = threading.Lock()
        self.process = None
        # Flipped by abort_spawn() when stop_all_runs finds this manager
        # registered with no process yet; start() honours it on both sides
        # of the spawn.
        self._spawn_aborted = False
        # Execution backend (backends.select_backend) — chosen at start();
        # None until then. Owns where the process runs and the host's four
        # contracts with it (spawn+IPC, workspace, exec, ports).
        self.backend = None

        # Ports this run serves, registered via gen.serve_port (port_bound
        # IPC) once the host saw the run serve them (ipc._op_port_bound).
        # The reverse proxy validates (run_id, port) against this set so a
        # stale renderer whose run has stopped — or whose freed port a new
        # run has since reused — can't be forwarded to whatever now listens
        # on that port. Without it the proxy forwards by port alone, and a
        # stopped cell's preview, which keeps reconnecting to its baked-in
        # proxy URL, reaches whatever run has since reused the port.
        self.bound_ports: set[int] = set()

        # The run holds its saved runner's grant.
        self.permissions = permissions

        # Which version of each explicitly-named upstream artifact the
        # runner asked for ({name: "latest"|"<version>"}). Passed to
        # the child as COLLIMATE_PACKAGES; the generator joins it with its
        # own declaration of where those artifacts live. Kept beside the
        # grant because the two are read together — the grant decides
        # whether 'latest' may be resolved against a registry at all.
        self.packages: dict = dict(packages or {})

        # Manifest-declared extras the run's CONFIG selected, resolved by the
        # starting route (routes/runs/create.py) where
        # the manifest and the merged config values already sit together —
        # never re-read from disk in the spawn path. Both backends turn this
        # into the same ``uv run --with`` tail (optional_deps.uv_run_argv);
        # ``update_packages`` on the grant adds the ``-P`` refresh.
        self.optional_requirements: list[str] = list(optional_requirements or [])

        # Queue of renderer-emitted action ids (e.g. top-bar button clicks
        # from the .website renderer). Consumed via poll_renderer_action.
        # Tagged renderer actions: {action_id, source, payload} dicts
        # (agent-v2 §4.3.4) — poll_renderer_action pops the first entry
        # matching its filters, so concurrent waiters coexist.
        self._pending_renderer_actions: list[dict] = []

        # annotations_pull receipts (the batch and the notice it left) by
        # operation_id (newest 64), so a retried pull replays its answer.
        self._annotation_pulls: OrderedDict[str, dict] = OrderedDict()

        # IPC via localhost TCP socket
        self._ipc_server: asyncio.Server | None = None
        self._ipc_writer: asyncio.StreamWriter | None = None

        # Last cell name an update_cell emit carried. Labels the version of
        # a follow-up emit that omits the name (which never renames the
        # cell).
        self._last_emit_cell_name: str | None = None

        # The _wait_and_cleanup task, created in start(). is_running()
        # tolerates it being None.
        self._cleanup_task: asyncio.Task | None = None

        # Watchdog task that kills the subprocess if it exceeds the
        # manifest-declared timeout. Stored so cancellation (on normal
        # exit / explicit stop) can cancel it.
        self._timeout_task: asyncio.Task | None = None

        # Set by the timeout watchdog; surfaced in the final status so
        # the frontend can render "Timed out" rather than "Failed".
        self.timeout_reason: str | None = None

        # Current lifecycle phase (see process_manager.phases). The run row
        # is INSERTed with phase='queued'; the backend-auto machine advances
        # it (spinning_up → running → terminal) and the generator can refine
        # it via gen.phase(). None until the first _apply_phase; the
        # events_since backfill seeds the client from the runs.phase column.
        self.current_phase: str | None = None
        self.current_phase_message: str = ""

        # Set, never cleared, by every stop of this run — the user's Stop,
        # Stop all, a snapshot's update-and-stop, the watchdog
        # (run_registry.mark_stop_requested). From then on the run's finish
        # op writes no status, so a run still running ends with its
        # stopper's: stopped, or timed_out for the watchdog.
        self.stop_requested: bool = False

        # The run row's terminal status as finalization read it back
        # (``completed``/``failed``/``stopped``/``timed_out``); None until the
        # run is finalized, and left None when finalization itself fails.
        self.final_status: str | None = None

    async def start(self):
        """Launch the generator subprocess with TCP-based IPC."""
        # One manifest read serves every launch-time decision below
        # (live_files_mode, runs_on, rlimits, timeout).
        manifest = await asyncio.to_thread(self._load_manifest)
        env = await asyncio.to_thread(self._build_subprocess_env, manifest)
        env["COLLIMATE_WORKSPACE"] = self.workspace_dir
        # The cell the run writes (update_cell), several runs possibly at once.
        if self.cell_id:
            env["COLLIMATE_CELL_ID"] = self.cell_id
        # live_files_mode drives the SDK's auto file-sync: "auto" makes the
        # runtime open gen.live_files() for the whole run (zero generator code);
        # "manual" leaves the generator to call it itself. Passed verbatim.
        _lf_mode = str(manifest.get("live_files_mode") or "").strip()
        if _lf_mode:
            env["COLLIMATE_LIVE_FILES_MODE"] = _lf_mode
        self.ui_files = self._manifest_ui_files(manifest)
        env["COLLIMATE_UI_FILES"] = json.dumps(list(self.ui_files))

        # Add the SDK to PYTHONPATH so the generator subprocess can
        # ``import gen`` and ``import collimate`` (see sdk_src_dir).
        from .backends import sdk_src_dir
        sdk_src = sdk_src_dir()
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = sdk_src + os.pathsep + existing if existing else sdk_src

        # Backend selection lives in backends.select_backend — the one
        # place the docker-axis branch exists. The manager keeps a single
        # code path from here on; the backend owns where the process runs
        # and how the four host contracts (spawn+IPC, workspace, exec,
        # ports) are satisfied there.
        from .backends import enforce_runs_on, select_backend
        self.backend = select_backend(self.permissions)
        # The generator's declared execution-locus assumption (manifest
        # ``runs_on``) is checked against the chosen backend before
        # anything spawns — a local-machine generator under a
        # containerized grant must refuse loudly, not break silently.
        enforce_runs_on(manifest, self.backend)

        # Start a TCP server on an ephemeral port for IPC. Without an
        # explicit limit the StreamReader caps lines at 64 KB and
        # readline() raises before the oversize NAK path in ipc.py can
        # ever run; size the buffer to the protocol's own frame cap.
        #
        # Bind address and handler come from the backend: loopback for a
        # native subprocess, 0.0.0.0 for a containerized run (loopback is
        # container-local, so the child reaches the host over the docker
        # gateway alias). Either way the connection must open with this
        # run's token (backends.make_authed_ipc_handler).
        from collimate._host.ipc import IPC_MAX_LINE_BYTES
        endpoint = self.backend.ipc_endpoint(self)
        self._ipc_server = await asyncio.start_server(
            endpoint.handler, endpoint.bind_host, 0,
            limit=IPC_MAX_LINE_BYTES,
        )
        port = self._ipc_server.sockets[0].getsockname()[1]
        # Every failure from here to a live child (build_preexec, an abort,
        # the backend's spawn raising) closes the server again: nothing
        # else would, since _wait_and_cleanup only starts after the spawn.
        try:
            env["COLLIMATE_IPC_PORT"] = str(port)
            env.update(endpoint.env)

            # rlimits ride preexec only where the child shares our kernel; the
            # docker backend maps the mappable limits to --memory/--pids-limit
            # instead and returns None here.
            preexec = self.backend.build_preexec(self, manifest)

            # Shutdown race guard: stop_all_runs snapshots active_managers and
            # signals live processes; a manager registered but not yet spawned
            # has nothing to signal, so it is told to abort instead. Spawning
            # anyway would create a process (in its own session, detached from
            # the server's group) that nothing ever kills.
            if self._spawn_aborted:
                raise RuntimeError("run cancelled before the generator was spawned")

            # Whatever the backend, the returned child carries the same
            # stdin/stdout/stderr pipes, so everything below (drains, writer,
            # cleanup, timeout, stop signalling) is backend-agnostic.
            from run_workspace_cleanup import stamp_manager
            await asyncio.to_thread(stamp_manager, self, 'launching')
            self.process = await self.backend.spawn(
                self,
                env=env,
                entrypoint=self.entrypoint,
                manifest=manifest,
                preexec=preexec,
            )
            await asyncio.to_thread(stamp_manager, self, 'running')

            # Same race, other side of the spawn: an abort that landed while the
            # backend was spawning saw process=None and signalled nothing — kill
            # the fresh group ourselves.
            if self._spawn_aborted:
                import signal as _sig
                signal_process_group(self.process.pid, _sig.SIGKILL)
                raise RuntimeError("run cancelled while the generator was spawning")
        except BaseException:
            srv, self._ipc_server = self._ipc_server, None
            try:
                srv.close()
            except Exception:  # noqa: BLE001 — the original error matters
                pass
            raise

        # Start async readers
        self._stdout_task = asyncio.create_task(self._drain_stdout())
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        self._writer_task = asyncio.create_task(self._microbatch_writer())
        self._cleanup_task = asyncio.create_task(self._wait_and_cleanup())

        # Subprocess is live but the generator hasn't shown activity yet —
        # mark the cell "spinning up" (imports, env build, container pull, …).
        # The auto-machine advances to "running" on the first update_cell /
        # first stdout-stderr line (see _maybe_auto_running), which for a cold
        # ``--with`` group is uv's own download progress — so the sub-message is
        # what names the install during the silent window before that.
        from .optional_deps import install_phase_message
        from .phases import SPINNING_UP
        self._apply_phase(SPINNING_UP, install_phase_message(
            self.optional_requirements,
            upgrade=self.permissions.update_packages,
        ))

        # Per-generator timeout. The watchdog task reuses the same
        # SIGTERM→15s→SIGKILL ladder /stop uses. Two layers:
        #
        #   * manifest ``timeout_seconds`` — the generator author's
        #     stated upper bound (default 0 = no manifest limit).
        #   * ``HARD_WALLCLOCK_CAP_SECONDS`` — an absolute ceiling that
        #     always fires regardless of the manifest, so a runaway
        #     generator can't stay alive forever.
        #
        # We pick the smaller of the two when the manifest sets a value.
        timeout_seconds = self._HARD_WALLCLOCK_CAP_SECONDS
        raw = manifest.get("timeout_seconds")
        if isinstance(raw, (int, float)) and raw > 0:
            timeout_seconds = min(int(raw), self._HARD_WALLCLOCK_CAP_SECONDS)
        self._timeout_task = asyncio.create_task(
            self._enforce_timeout(
                timeout_seconds, interactive=bool(manifest.get("chat")),
            )
        )

    def abort_spawn(self) -> None:
        """Refuse to spawn the generator, or kill a spawn already in flight.

        Called by ``stop_all_runs`` for a manager it finds registered with
        ``process=None``: the shutdown snapshot has nothing to signal, and a
        spawn that went ahead would outlive the server as a session leader
        nothing kills. ``start()`` checks the flag before the spawn and
        again right after it, so the window is closed on both sides.
        """
        self._spawn_aborted = True

    # Tests advance this await deterministically; the production poll stays real.
    _watchdog_sleep = staticmethod(asyncio.sleep)

    async def _enforce_timeout(self, seconds: int, *, interactive: bool = False):
        """Kill the subprocess if it outlives the manifest-declared budget.

        SIGTERM, wait 15 s (same budget the /stop endpoint grants
        cleanup), then SIGKILL. Records the reason so the frontend
        renders "Timed out" instead of a bare "Failed".

        Time the run spends waiting on the user does NOT count against the
        budget: the loop polls in short steps and charges a step only when
        the run is in neither ``awaiting_input`` (session open, generator idle
        until the user replies) nor ``awaiting_approval`` (an approval card
        waits on the user mid-turn).

        ``interactive`` (a ``chat: true`` generator) makes the budget bound
        ONE stretch of work rather than the whole session: returning to
        awaiting_input means the turn ended, so the clock starts over — a
        session of several multi-minute turns is not a runaway, and summing
        them would SIGTERM a healthy session mid-turn. ``awaiting_approval``
        only pauses the clock: the turn resumes after the answer, so it keeps
        what it had already spent. Nor is an interactive run charged while a
        live terminal is on its cell (a terminal run's TTY, driven by the
        user at the keyboard): the generator sits on "running" for the whole
        TTY while the user decides its length.
        """
        import signal as _sig
        from .phases import AWAITING_APPROVAL, AWAITING_INPUT
        poll = 1.0
        consumed = 0.0
        try:
            while consumed < seconds:
                step = min(poll, seconds - consumed)
                await self._watchdog_sleep(step)
                if self.current_phase == AWAITING_INPUT:
                    if interactive:
                        consumed = 0.0
                elif self.current_phase == AWAITING_APPROVAL:
                    continue
                elif not (interactive and self._terminal_attached()):
                    consumed += step
        except asyncio.CancelledError:
            return
        if self.process is None or self.process.returncode is not None:
            return
        self.timeout_reason = f"Exceeded {seconds}s timeout"
        # A run the watchdog ends is stopping like every other stop
        # (registry.mark_stop_requested): it registers no new surface, its
        # annotation context refuses writes and /runs/active lists it as
        # stopping.
        from run_registry import mark_stop_requested
        mark_stop_requested(self)
        self.log_buffer.append({
            "type": "stderr",
            "payload": f"[timeout] Generator exceeded {seconds}s limit — terminating.\n",
        })
        pid = self.process.pid
        signal_process_group(pid, _sig.SIGTERM)
        try:
            await asyncio.wait_for(wait_process_exit(self.process), timeout=15.0)
        except asyncio.TimeoutError:
            signal_process_group(pid, _sig.SIGKILL)
            # Reap the killed child; without this it lingers as a zombie
            # until the manager itself exits.
            try:
                await asyncio.wait_for(wait_process_exit(self.process), timeout=5.0)
            except asyncio.TimeoutError:
                self.log_buffer.append({
                    "type": "stderr",
                    "payload": "[timeout] Generator did not exit after SIGKILL.\n",
                })

    def _terminal_attached(self) -> bool:
        """This run serves a live PTY (see _enforce_timeout)."""
        try:
            from sessions.terminal import has_live_session
            return has_live_session(self.run_id)
        except Exception:  # noqa: BLE001 — a registry hiccup just charges the step
            return False

    def _load_manifest(self) -> dict:
        """The generator's definition from ``app_dir`` as a dict;
        ``{}`` when it is missing or unreadable (a manifest is optional)."""
        return read_generator_manifest(self.app_dir)

    @staticmethod
    def _manifest_ui_files(manifest: dict) -> tuple[str, ...]:
        """The manifest's ``ui_files`` suffixes (``runners`` validates their
        shape when the generator is indexed); anything that is not a dotted
        suffix is dropped, so a malformed entry can only narrow what the run
        may write."""
        raw = manifest.get("ui_files")
        if not isinstance(raw, list):
            return ()
        return tuple(s for s in raw
                     if isinstance(s, str) and len(s) > 1 and s.startswith(".")
                     and "/" not in s and s != ".run")

    # Hard wall-clock ceiling — a runaway generator that ignores its
    # manifest budget gets killed at this point regardless. 30 minutes
    # is generous for legitimate long-running daemons (chat sessions,
    # docker containers) while still bounding a stuck process.
    # Override per-deployment with COLLIMATE_HARD_WALLCLOCK_SECONDS.
    _HARD_WALLCLOCK_CAP_SECONDS = int(
        os.environ.get("COLLIMATE_HARD_WALLCLOCK_SECONDS") or 30 * 60
    )

    # Default rlimit ceilings for the generator subprocess. Conservative
    # so a fork-bomb / OOM-bomb / fd-leak doesn't take the host down,
    # but generous enough that legitimate work fits. Each is overridable
    # per-generator via the ``rlimits`` block in the generator definition::
    #
    #     rlimits:
    #       memory_bytes: 4294967296   # 4 GB
    #       cpu_seconds:  900          # 15 min
    #       open_files:   1024
    # memory_bytes caps what the process tree commits — RLIMIT_DATA: the
    # heap plus every private writable mapping — the same quantity the
    # docker backend hands ``--memory``. It is deliberately not an
    # address-space cap (RLIMIT_AS). That counted reservations nothing ever
    # touches, and every descendant inherits the rlimit: V8's sandbox
    # reserves about 1 TiB of PROT_NONE address space when an isolate
    # starts, so Codex's code-mode host (spawned by the Agent cell's
    # host-locus adapter) and Chromium died at startup with SIGTRAP and
    # "Fatal process out of memory: Failed to reserve the virtual address
    # space for the V8 sandbox" under an 8 GB address-space cap, while
    # committing almost nothing. The preexec runs before exec, so the cap
    # also binds ``uv`` and its allocator arenas; 8 GB leaves those room.
    _DEFAULT_RLIMIT_MEMORY_BYTES = 8 * 1024 * 1024 * 1024  # 8 GB
    # CPU is real CPU time (not wallclock), cumulative over the process's
    # life. The wallclock watchdog (_enforce_timeout) is the actual bound
    # on runaway processes — RLIMIT_CPU is redundant safety, so it matches
    # the default wallclock cap rather than firing earlier on legitimate
    # work. An interactive (``chat: true``) generator gets no default CPU
    # cap: its watchdog budget bounds one turn and restarts per turn, so a
    # cumulative cap would end a healthy long session. The trade-off: CPU
    # such a generator burns while it awaits input, or while a terminal is
    # attached, is bounded by nothing (the watchdog charges neither); its
    # manifest's ``rlimits.cpu_seconds`` still sets a cap when it wants one.
    _DEFAULT_RLIMIT_CPU_SECONDS  = 30 * 60                 # 30 minutes
    # 1024 is the conventional Linux soft default. 256 was tight for
    # daemons holding concurrent sockets (HTTP pools, docker SDK,
    # IPC, log files) plus mmap'd configs.
    _DEFAULT_RLIMIT_OPEN_FILES   = 1024

    # Static allowlist — host vars every generator legitimately needs. A
    # variable one generator or the tool it spawns needs (a harness CLI's own
    # flag, an agent socket) is that generator's manifest ``env_passthrough``
    # entry, never a row here; anything else must come from the selected
    # account snapshot.
    _ENV_ALLOWLIST: tuple[str, ...] = (
        "HOME", "PATH", "USER", "LOGNAME", "TZ",
        "LANG", "LANGUAGE",
        "TMPDIR", "TMP", "TEMP",
        "SHELL", "PWD",
        "PYTHONUNBUFFERED", "PYTHONIOENCODING",
        "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
        # The SDK stores CLI adapter installations under XDG_CACHE_HOME.
        # Keep the server and generator install cache locations consistent.
        # Credentials use temporary account-manager leases instead.
        "XDG_CACHE_HOME",
    )

    # Forwarded ONLY when the run's grant carries docker (limited or full). A
    # ``docker: none`` native run cannot be OS-confined, but
    # it gets no help either — daemon coordinates stay out of its env.
    _ENV_DOCKER_ALLOWLIST: tuple[str, ...] = (
        "DOCKER_HOST", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH",
    )

    _ENV_PREFIX_ALLOWLIST: tuple[str, ...] = (
        "COLLIMATE_",  # We set these explicitly later; passthrough preserves anything caller set
        "LC_",         # locale family (LC_ALL, LC_CTYPE, …)
    )

    # These describe one saved execution. Neither an inherited server value
    # nor manifest passthrough may populate them when this run leaves one unset.
    _ENV_RUNTIME_OWNED = frozenset({
        "COLLIMATE_PERMISSIONS", "COLLIMATE_CHANNEL", "COLLIMATE_RUN_ID", "COLLIMATE_RUN_DIR",
        "COLLIMATE_WORKSPACE", "COLLIMATE_PACKAGES",
        "COLLIMATE_RUNNER", "COLLIMATE_CELL_ID", "COLLIMATE_CALL_DEPTH",
        "COLLIMATE_SUB_RUN",
        "COLLIMATE_LIVE_FILES_MODE", "COLLIMATE_UI_FILES",
        "COLLIMATE_IPC_PORT", "COLLIMATE_IPC_HOST", "COLLIMATE_IPC_TOKEN", "COLLIMATE_IPC_OPS",
    })

    # The server's own auth credentials. They authenticate the whole API, so
    # neither the COLLIMATE_ prefix passthrough nor a manifest's
    # env_passthrough may hand them to a generator.
    _ENV_SERVER_SECRETS = frozenset({"COLLIMATE_TOKEN", "COLLIMATE_CONNECT_TOKEN"})

    @classmethod
    def _build_preexec_fn(cls, manifest: dict):
        """Return a preexec_fn that applies POSIX rlimits in the child process.

        Reading manifest values here keeps the subprocess launch site
        focused on the asyncio plumbing — manifests can override any of
        the three caps via an ``rlimits`` block; missing keys fall back
        to the class defaults, except that an interactive generator
        (``chat: true``) has no default CPU cap (see
        ``_DEFAULT_RLIMIT_CPU_SECONDS``).

        Each setrlimit is wrapped in try/except OSError because some
        kernels reject lowering past a hard limit; in that case we
        leave the child unconstrained on that axis rather than failing
        the whole launch — degrading gracefully beats the alternative.
        """
        import resource
        from manifest_limits import manifest_rlimit

        interactive = bool(manifest.get("chat"))
        memory_bytes = manifest_rlimit(manifest, "memory_bytes", cls._DEFAULT_RLIMIT_MEMORY_BYTES)
        cpu_seconds  = manifest_rlimit(manifest, "cpu_seconds",
                                   None if interactive else cls._DEFAULT_RLIMIT_CPU_SECONDS)
        open_files   = manifest_rlimit(manifest, "open_files",   cls._DEFAULT_RLIMIT_OPEN_FILES)

        def _apply():
            for rlimit_name, value in (
                ("RLIMIT_DATA",   memory_bytes),
                ("RLIMIT_CPU",    cpu_seconds),
                ("RLIMIT_NOFILE", open_files),
            ):
                rlimit = getattr(resource, rlimit_name, None)
                if rlimit is None or value is None:
                    continue
                try:
                    resource.setrlimit(rlimit, (value, value))
                except (OSError, ValueError):
                    # Kernel refused the cap (e.g. exceeds the hard
                    # limit on this user). Leaving uncapped is safer
                    # than failing exec.
                    if rlimit_name == "RLIMIT_DATA":
                        # stderr is already the child's run-event pipe.
                        # Use a syscall, not Python logging in preexec:
                        # the parent drains/persists this one plain entry.
                        try:
                            os.write(2, b"memory cap not applied on this platform\n")
                        except OSError:
                            # Reporting must not turn best-effort limits
                            # into a failed launch if stderr is unavailable.
                            pass

        return _apply

    def _build_subprocess_env(self, manifest: dict | None = None) -> dict[str, str]:
        """Build the generator's environment: the run's identity and grant,
        then an allowlist of host variables.

        No host secret (AWS keys, GITHUB_TOKEN, etc.) reaches a generator
        unless one of these names it:

        1. The run's own identity and grant (set here, never inherited).
        2. The static allowlist above (HOME/PATH/locale/temp dirs/Docker).
        3. The private account snapshot resolved once for this run. It never
           consults the registry again or falls back to host credentials.
        4. The generator manifest's optional ``env_passthrough: [VAR, …]``
           list, for vars a generator legitimately needs that don't fit
           the keystore model.

        Anything else is dropped. ``start()`` adds the workspace and the IPC
        coordinates.
        """
        env: dict[str, str] = {}

        # Who the run is and what it may do. The SDK requires the run id, the
        # channel, the runner identity (label + folder) and the grant (every
        # axis) of every run connected to a host and stops without them;
        # packages are a JSON object when present. Native and contained runs
        # build their environment here.
        env["COLLIMATE_CHANNEL"] = self.channel
        env["COLLIMATE_RUN_ID"] = self.run_id
        env["COLLIMATE_PERMISSIONS"] = self.permissions.to_env()
        if self.packages:
            env["COLLIMATE_PACKAGES"] = json.dumps(self.packages, sort_keys=True)
        # Who the run is: its runner and the
        # host-minted label + run folder. Always sent.
        env["COLLIMATE_RUNNER"] = json.dumps({
            "runner": self.runner_id, "name": self.runner_name,
            "label": self.label, "folder": self.folder,
        }, sort_keys=True)
        for name in ProcessManager._ENV_ALLOWLIST:
            if name in os.environ:
                env[name] = os.environ[name]

        if self.permissions.docker != "none":
            for name in ProcessManager._ENV_DOCKER_ALLOWLIST:
                if name in os.environ:
                    env[name] = os.environ[name]

        for name, value in os.environ.items():
            if (name not in ProcessManager._ENV_RUNTIME_OWNED
                    and name not in ProcessManager._ENV_SERVER_SECRETS
                    and any(name.startswith(p) for p in ProcessManager._ENV_PREFIX_ALLOWLIST)):
                env.setdefault(name, value)

        from accounts import account_environment
        env.update(account_environment(self.account_context))

        if manifest is None:
            manifest = ProcessManager._load_manifest(self)
        passthrough = manifest.get("env_passthrough")
        if isinstance(passthrough, list):
            for name in passthrough:
                if (isinstance(name, str) and name in os.environ and name not in env
                        and name not in ProcessManager._ENV_RUNTIME_OWNED
                        and name not in ProcessManager._ENV_SERVER_SECRETS):
                    env[name] = os.environ[name]

        import stat
        sockets = set()
        for name in passthrough if isinstance(passthrough, list) else []:
            if (not isinstance(name, str) or name not in env
                    or name in ProcessManager._ENV_RUNTIME_OWNED
                    or name in ProcessManager._ENV_SERVER_SECRETS):
                continue
            try:
                path = os.path.realpath(env[name])
                if stat.S_ISSOCK(os.stat(path).st_mode):
                    sockets.add(path)
            except (OSError, ValueError):
                pass
        self.env_passthrough_sockets = frozenset(sockets)

        return env

    # ------------------------------------------------------------------
    # Run-state predicates
    # ------------------------------------------------------------------

    def is_running(self) -> bool:
        """True while the run is still live: the subprocess hasn't exited,
        or it has and ``_wait_and_cleanup`` (final drain, finalization,
        teardown) is still in flight.
        """
        if self._cleanup_task is not None and not self._cleanup_task.done():
            return True
        if self.process is not None and self.process.returncode is None:
            return True
        return False
