"""The demo runner: ``ProcessManager.start`` replaced by a scripted timeline.

The native ``start()`` spawns the generator subprocess and then wires the
run's stdout/stderr drains, the microbatch log writer, ``_wait_and_cleanup``
and the timeout watchdog around ``self.process``. Here the subprocess is a
:class:`FakeProcess` — the same duck (``pid``, ``returncode``, ``stdout``/
``stderr`` StreamReaders, ``wait()``, ``send_signal``/``terminate``/
``kill``) driving an asyncio task that runs ``play(demo)`` from the
generator's ``timeline.py``. Everything downstream of the spawn — phases,
log flushing, stop / force-stop, status finalization, chat-session
teardown, the run log — is the app's own code, untouched.

Signals reach the fake process through ``os.kill`` / ``os.killpg`` /
``os.getpgid`` wrappers that route fake pids to their FakeProcess and pass
every real pid through, so the ``/stop`` route and the watchdog work as
written. SIGTERM and SIGKILL both cancel the timeline task (a scripted
timeline has nothing to clean up that the cancellation does not unwind).

The ``demo`` object handed to ``play`` is the timeline contract
(playground/README.md, "Writing a demo generator"). Files a timeline writes
land in the run workspace and reach the cell through the host's own
``_emit_partial`` on ``commit()``, so the UI updates over SSE exactly as it
does for a real generator.
"""
from __future__ import annotations

import asyncio
import importlib.util
import inspect
import itertools
import json
import os
import signal
import sys
import traceback
import uuid
from typing import Callable, Optional

from . import terminal as demo_terminal

_fake_processes: dict[int, "FakeProcess"] = {}
# Fake pids start far above anything the runtime hands out (pyodide's own pid
# is a small constant), so a real pid can never collide.
_pids = itertools.count(900_000)
_turns = itertools.count(1)


# ---------------------------------------------------------------------------
# FakeProcess + signal routing
# ---------------------------------------------------------------------------

class FakeProcess:
    """A subprocess-shaped handle on the timeline task."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.pid = next(_pids)
        self.returncode: Optional[int] = None
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdin = None
        self.cancelled = False
        self.finish_request: Optional[tuple[bool, str]] = None
        self._signal: Optional[int] = None
        self._exited = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._on_finish: Optional[Callable] = None
        _fake_processes[self.pid] = self

    def run(self, coro, on_finish: Optional[Callable] = None) -> None:
        self._on_finish = on_finish
        self._task = asyncio.ensure_future(self._main(coro))

    async def _main(self, coro) -> None:
        code = 0
        try:
            await coro
        except asyncio.CancelledError:
            code = -(self._signal or signal.SIGTERM)
        except Exception:  # noqa: BLE001 — a timeline bug is a failed run
            self.stderr.feed_data(("[timeline error] " + traceback.format_exc()).encode("utf-8"))
            code = 1
        else:
            if self._on_finish is not None and self.finish_request is not None:
                success, message = self.finish_request
                try:
                    await self._on_finish(success, message)
                except Exception:  # noqa: BLE001
                    self.stderr.feed_data(("[finish error] " + traceback.format_exc()).encode("utf-8"))
                if not success:
                    code = 1
        finally:
            self._exit(code)

    def _exit(self, code: int) -> None:
        if self.returncode is not None:
            return
        self.returncode = code
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self._exited.set()
        _fake_processes.pop(self.pid, None)
        demo_terminal.close_for_run(self.run_id)

    async def wait(self) -> int:
        await self._exited.wait()
        return self.returncode  # type: ignore[return-value]

    def send_signal(self, sig: int) -> None:
        if self.returncode is not None:
            raise ProcessLookupError(self.pid)
        if int(sig) == 0:  # os.kill(pid, 0): a liveness probe, not a kill
            return
        self._signal = int(sig)
        self.cancelled = True
        if self._task is not None and not self._task.done():
            self._task.cancel()

    def terminate(self) -> None:
        self.send_signal(signal.SIGTERM)

    def kill(self) -> None:
        self.send_signal(signal.SIGKILL)


_signals_installed = False


def _install_signal_routing() -> None:
    global _signals_installed
    if _signals_installed:
        return
    _signals_installed = True
    real_kill = os.kill
    real_killpg = getattr(os, "killpg", None)
    real_getpgid = getattr(os, "getpgid", None)

    def kill(pid, sig):
        proc = _fake_processes.get(pid)
        if proc is not None:
            proc.send_signal(sig)
            return
        return real_kill(pid, sig)

    def killpg(pgid, sig):
        proc = _fake_processes.get(pgid)
        if proc is not None:
            proc.send_signal(sig)
            return
        if real_killpg is None:
            raise ProcessLookupError(pgid)
        return real_killpg(pgid, sig)

    def getpgid(pid):
        if pid in _fake_processes:
            return pid
        if real_getpgid is None:
            raise ProcessLookupError(pid)
        return real_getpgid(pid)

    os.kill = kill
    os.killpg = killpg
    os.getpgid = getpgid


# ---------------------------------------------------------------------------
# The timeline runtime object
# ---------------------------------------------------------------------------

class ChatSession:
    """A run's chat surface (its inbox), served by the app's real
    ``/api/chats/{chat_id}`` route. The timeline owns the transcript
    (``transcript``, a UI file of the run); this object only streams the
    live turn and reads user input."""

    def __init__(self, demo: "Demo", session: dict, pointer_path: str,
                 thread_id: str, transcript: str) -> None:
        self._demo = demo
        self.chat_id = session["chat_id"]
        self.ws_path = session["ws_path"]
        self.token = session["token"]
        self.pointer_path = pointer_path
        # The pointer's active thread. The assistant renderer materializes a
        # live bubble only for frames bound to the thread it shows, so every
        # turn-scoped event carries it — as the SDK's serve_chat does.
        self.thread_id = thread_id
        # The active thread's transcript, folder-relative (``demo.update_ui_file``).
        self.transcript = transcript
        self.turn_id = ""

    async def _push(self, event: dict) -> None:
        from sessions.chat import push_event
        push_event(self._demo._m.run_id, self.turn_id, {"thread_id": self.thread_id, **event})
        await asyncio.sleep(0)

    async def start_turn(self) -> None:
        self.turn_id = f"turn_{next(_turns)}"
        await self._push({"type": "turn_start"})

    async def delta(self, text: str) -> None:
        await self._push({"type": "text_delta", "text": text})

    async def thinking(self, text: str) -> None:
        await self._push({"type": "thinking_delta", "text": text})

    async def tool(self, **fields) -> None:
        """A tool card: ``id``, ``name``, ``status`` (running/done/error),
        optional ``input`` / ``result``."""
        await self._push({"type": "tool", **fields})

    async def event(self, event: dict) -> None:
        """Any other core→renderer stream event, passed through by type."""
        await self._push(event)

    async def end_turn(self) -> None:
        await self._push({"type": "turn_end"})
        self.turn_id = ""

    async def wait_user_message(self, poll: float = 0.2) -> Optional[str]:
        """The next message the visitor sends, or None once the session is
        gone. Raises CancelledError when the run is stopped."""
        from sessions.chat import get_inbox, pop_command
        run_id = self._demo._m.run_id
        while True:
            if get_inbox(run_id) is None:
                return None
            cmd = pop_command(run_id)
            if cmd is not None and cmd.get("kind") == "user_msg":
                return str(cmd.get("content") or "")
            await asyncio.sleep(poll)


class Demo:
    """What ``play(demo)`` receives.

    A run owns one folder on its cell, ``_runs/<label>/`` (``folder``),
    minted by the host: its run card and its UI files live there, locked to
    the run and swept by the host when the run ends. Work files go anywhere
    else in the cell through :meth:`write`; UI files go through
    :meth:`ui_file`, only for the suffixes the manifest's ``ui_files``
    declares — the SDK's ``gen.update_file`` / ``gen.update_ui_file`` split."""

    CHAT_POINTER = "chat/conversation.chat"
    TERMINAL_POINTER = "terminal/session.terminal"

    def __init__(self, manager, process: FakeProcess, manifest: dict) -> None:
        self._m = manager
        self._proc = process
        self._cell_id = manager.cell_id or ""
        self._staged: set[str] = set()
        self.label: str = manager.label
        self.folder: str = manager.folder
        self.workspace: str = manager.workspace_dir
        # The SDK host's conditional-write identity: every commit is one
        # operation of this writer, based on the file revisions it last saw
        # (the run's frozen baseline, then each accepted receipt).
        self._writer_id = f"run:{manager.run_id}:{uuid.uuid4().hex}"
        self._edit_seq = 0
        self._revisions: dict[str, int] = self._read_revisions()
        self.params: dict = self._read_params(manifest)
        self.prompt: str = self._read_prompt()
        self.inputs: list[dict] = self._read_inputs()

    # ---- context ----
    @property
    def cancelled(self) -> bool:
        return self._proc.cancelled

    def run_path(self, name: str) -> str:
        """The cell path of ``name`` inside the run folder."""
        return f"{self.folder}/{name}"

    def _read_revisions(self) -> dict[str, int]:
        """The file revisions the run started from (``.collrun/
        file_revisions.json``, written by the host at workspace hydration)."""
        try:
            with open(os.path.join(self.workspace, ".collrun", "file_revisions.json"), encoding="utf-8") as fh:
                return dict(json.load(fh))
        except FileNotFoundError:
            return {}

    def _read_params(self, manifest: dict) -> dict:
        """The runner's saved main-config values: the seeded main config file
        parsed the way ``gen.config`` parses it (the SDK's public config
        resolver: field defaults merged with the ``values:`` block)."""
        from collimate.config_values import parse_set_yaml, resolve_generator_values
        main = manifest.get("main_config")
        if not (isinstance(main, str) and main):
            return {}
        path = os.path.join(self.workspace, ".collrun", "config", "files", main)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return dict(resolve_generator_values(parse_set_yaml(fh.read())))
        except OSError:
            return {}

    def _read_prompt(self) -> str:
        path = os.path.join(self.workspace, ".collrun", "prompt.md")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return ""

    def _read_inputs(self) -> list[dict]:
        """The run's cell as ``[{name, files: {path: text}}]``: the text
        files the workspace holds before the timeline writes anything, run
        folders (``_runs/``, other runs' UI files) left out."""
        from run_folders import RUNS_PREFIX
        if not self._cell_id:
            return []
        files: dict[str, str] = {}
        for root, dirs, names in os.walk(self.workspace):
            if root == self.workspace:
                dirs[:] = [d for d in dirs if d not in (".collrun", RUNS_PREFIX)]
            for name in sorted(names):
                abs_path = os.path.join(root, name)
                rel = os.path.relpath(abs_path, self.workspace).replace(os.sep, "/")
                try:
                    with open(abs_path, "rb") as fh:
                        files[rel] = fh.read().decode("utf-8")
                except (OSError, UnicodeDecodeError):
                    continue  # binary or unreadable — not part of the text contract
        return [{"name": self._cell_name(), "files": files}]

    def _cell_name(self) -> str:
        from state import state
        for ch in state.channels:
            for stack in [*ch.main_stacks, *ch.nav_stacks]:
                for cell in stack.cells:
                    if str(cell.id) == self._cell_id:
                        return cell.name
        return "Input"

    # ---- log / phase / status ----
    def log(self, line: str) -> None:
        text = str(line)
        if not text.endswith("\n"):
            text += "\n"
        self._proc.stdout.feed_data(text.encode("utf-8"))

    def phase(self, name: str, message: str = "") -> None:
        self._m._apply_phase(str(name), str(message or ""))

    def finish(self, success: bool = True, message: str = "") -> None:
        self._proc.finish_request = (bool(success), str(message or ""))

    # ---- files ----
    def _abs(self, path: str) -> tuple[str, str]:
        from run_folders import normalize
        rel = normalize(path)
        parts = rel.split("/")
        if not rel or ".." in parts or parts[0] == ".collrun":
            raise ValueError(f"invalid cell path: {path!r}")
        return os.path.join(self.workspace, *parts), rel

    def _stage(self, path: str, content) -> str:
        abs_path, rel = self._abs(path)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        data = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        with open(abs_path, "wb") as fh:
            fh.write(data)
        self._staged.add(rel)
        return rel

    def write(self, path: str, content) -> None:
        """Stage a work file anywhere in the cell outside ``_runs/``."""
        from run_folders import is_run_path
        if is_run_path(path):
            raise PermissionError(
                f"{path}: files under _runs/ are UI files — write them with demo.update_ui_file")
        self._stage(path, content)

    def _ui_path(self, name: str) -> str:
        """The cell path of the UI file ``name``, or ValueError when the run
        may not write it: outside the folder, the run card, or a suffix the
        manifest's ``ui_files`` does not declare."""
        from run_folders import in_folder, normalize, run_card_path
        path = normalize(self.run_path(str(name)))
        if not str(name) or not in_folder(path, self.label):
            raise ValueError(f"invalid UI file name: {name!r}")
        if path == run_card_path(self.label):
            raise ValueError(f"{path} is the run card; only the host writes it")
        declared = tuple(self._m.ui_files)
        if not any(path.endswith(suffix) for suffix in declared):
            raise ValueError(
                f"{name}: its type is not in the manifest's ui_files {list(declared)}")
        return path

    def update_ui_file(self, name: str, content) -> str:
        """Stage the UI file ``<folder>/<name>`` and return its cell path.
        ``name`` is folder-relative (subfolders allowed); its suffix must be
        one the manifest's ``ui_files`` declares, and the run card is the
        host's."""
        return self._stage(self._ui_path(name), content)

    def read(self, path: str) -> str:
        abs_path, _rel = self._abs(path)
        with open(abs_path, "r", encoding="utf-8") as fh:
            return fh.read()

    async def commit(self, cell_name: Optional[str] = None) -> None:
        """Persist every staged write as a new version of the cell. The UI
        follows over SSE (``dag_commit`` → FILES_CHANGED in mounted
        renderers). ``cell_name`` None leaves the cell's name as it is; a
        given name is accepted only for a cell this run created. The version
        is labelled with the accepted name, else the last accepted name this
        run gave, else the generator id (``CellsMixin._update_cell``).
        Raises PermissionError when the host refused a path, after the rest
        landed — as ``gen.commit`` does."""
        artifacts = [
            {"type": "workspace", "cell_path": rel, "local_path": rel}
            for rel in sorted(self._staged)
        ]
        staged = self._staged.copy()
        self._staged.clear()
        self._m._maybe_auto_running()
        self._edit_seq += 1
        try:
            _version_id, refused = self._m._emit_partial(
                artifacts=artifacts, cell_name=cell_name, operation_id=uuid.uuid4().hex,
                writer_id=self._writer_id, edit_seq=self._edit_seq,
                base_revisions={rel: self._revisions.get(rel, 0) for rel in staged})
            receipt = self._m._last_file_receipt
            if receipt.get("outcome") != "accepted":
                raise RuntimeError(f"the cell changed under the run ({receipt.get('outcome')})")
        except Exception as exc:  # noqa: BLE001 — same handling as the IPC op
            self._m.log_buffer.append({"type": "stderr", "payload": f"[commit error] {exc}"})
            self._staged.update(staged)
            raise
        for item in receipt.get("files", []):
            self._revisions[item["path"]] = item["file_revision"]
        await asyncio.sleep(0)
        if refused:
            raise PermissionError("; ".join(
                f"{r['path']} refused ({r['reason']}, {r['owner']})" for r in refused))

    async def reveal_file(self, path: str) -> None:
        """Show one file of the cell to the visitor: its tab opens, focused
        when the visitor's active tab is empty, not a file, or one of this
        run's own (the surface the manifest's ``opens`` declares is focused
        at the run's start, so a reveal of it keeps the focus); otherwise it
        opens in the background. A path still staged is committed first —
        the host reveals only what the cell holds."""
        from run_folders import normalize
        rel = normalize(path)
        if rel in self._staged:
            await self.commit()
        await self._m._op_reveal_file({"path": rel})

    async def sleep(self, seconds: float) -> None:
        if self.cancelled:
            raise asyncio.CancelledError()
        await asyncio.sleep(max(0.0, float(seconds)))

    # ---- live surfaces ----
    def _require_cell(self) -> str:
        if not self._cell_id:
            raise RuntimeError("this run has no target cell; live surfaces need one")
        return self._cell_id

    def chat_session(self, label: str = "Chat") -> ChatSession:
        """Register the run's chat surface (its inbox) and stage its pointer
        (``chat/conversation.chat``); the transcript of its one thread is
        ``session.transcript`` (``chat/main.json``). Reveal
        ``session.pointer_path`` to open it."""
        from sessions.chat import register_surface_session
        from collimate.pointers import chat_session_pointer
        pointer_path = self._ui_path(self.CHAT_POINTER)
        threads_dir = pointer_path.rsplit("/", 1)[0]
        self._ui_path("chat/main.json")  # the transcript's type, before registering
        session = register_surface_session(
            self._m.run_id, self._require_cell(), pointer_path,
            broadcast=False, answer_route="inbox", inbox=True)
        pointer = chat_session_pointer(session, threads_dir=threads_dir, label=label)
        self.update_ui_file(self.CHAT_POINTER, json.dumps(pointer, indent=2))
        thread = pointer["threads"][0]
        transcript = thread["file"][len(self.folder) + 1:]
        return ChatSession(self, session, pointer_path, pointer["active_thread"], transcript)

    def terminal(self, label: str = "Terminal", name: str = TERMINAL_POINTER) -> demo_terminal.DemoTerminal:
        """A scripted terminal served by the shim's own websocket route;
        stages its ``.terminal`` pointer at the folder-relative ``name``."""
        from collimate.pointers import terminal_pointer
        self._ui_path(name)
        term = demo_terminal.create_session(self._m.run_id)
        pointer = terminal_pointer(term.pointer_session(), label=label)
        term.pointer_path = self.update_ui_file(name, json.dumps(pointer, indent=2))
        return term


# ---------------------------------------------------------------------------
# ProcessManager.start replacement
# ---------------------------------------------------------------------------

def _load_timeline(app_dir: str, entrypoint: str, run_id: str):
    path = os.path.join(app_dir, entrypoint)
    if not os.path.isfile(path):
        raise RuntimeError(f"timeline not found: {entrypoint} in {app_dir}")
    spec = importlib.util.spec_from_file_location(f"demo_timeline_{run_id.replace('-', '_')}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load timeline {path}")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, app_dir)
    try:
        spec.loader.exec_module(module)
    finally:
        try:
            sys.path.remove(app_dir)
        except ValueError:
            pass
    play = getattr(module, "play", None)
    if not inspect.iscoroutinefunction(play):
        raise RuntimeError(f"{entrypoint} must export `async def play(demo)`")
    return play


# What the native ``ProcessManager.start`` does AFTER the spawn, verbatim.
# ``start()`` below repeats these lines around a FakeProcess, so each one is
# also an anchor: ``install()`` checks the native source still contains them
# and refuses to install otherwise — the same fail-loud rule the build's
# patches follow, so the two can never drift apart silently.
NATIVE_WIRING = (
    "self.ui_files = self._manifest_ui_files(manifest)",
    "self._stdout_task = asyncio.create_task(self._drain_stdout())",
    "self._stderr_task = asyncio.create_task(self._drain_stderr())",
    "self._writer_task = asyncio.create_task(self._microbatch_writer())",
    "self._cleanup_task = asyncio.create_task(self._wait_and_cleanup())",
    "self._apply_phase(SPINNING_UP, ",
    "timeout_seconds = self._HARD_WALLCLOCK_CAP_SECONDS",
    'raw = manifest.get("timeout_seconds")',
    "if isinstance(raw, (int, float)) and raw > 0:",
    "timeout_seconds = min(int(raw), self._HARD_WALLCLOCK_CAP_SECONDS)",
    "self._enforce_timeout(",
    'timeout_seconds, interactive=bool(manifest.get("chat")),',
)


def check_native_wiring(native_start) -> None:
    """Raise unless ``native_start``'s source still carries every line of
    :data:`NATIVE_WIRING`. Pure; the demo unit tests run it against the
    repo's ``ProcessManager.start`` so a change there fails CI, not a
    visitor's boot."""
    source = inspect.getsource(native_start)
    missing = [line for line in NATIVE_WIRING if line not in source]
    if missing:
        raise RuntimeError(
            "ProcessManager.start no longer matches the demo runner's copy of its "
            "post-spawn wiring — update demo_runner.start and NATIVE_WIRING:\n  "
            + "\n  ".join(missing)
        )


async def start(self) -> None:
    """Demo replacement for ``ProcessManager.start``: the native method's
    post-spawn wiring (:data:`NATIVE_WIRING`) around a FakeProcess instead
    of a spawned subprocess. The native post-spawn ``_spawn_aborted`` re-check
    has no counterpart: nothing is awaited between the check below and the
    FakeProcess, so an abort cannot land in between."""
    manifest = self._load_manifest()
    entrypoint = self.entrypoint or manifest.get("entrypoint") or "timeline.py"
    play = _load_timeline(self.app_dir, entrypoint, self.run_id)

    self.ui_files = self._manifest_ui_files(manifest)

    from process_manager.backends import select_backend
    self.backend = select_backend(self.permissions)

    if self._spawn_aborted:
        raise RuntimeError("run cancelled before the generator was spawned")

    process = FakeProcess(self.run_id)
    self.process = process
    demo = Demo(self, process, manifest)

    self._stdout_task = asyncio.create_task(self._drain_stdout())
    self._stderr_task = asyncio.create_task(self._drain_stderr())
    self._writer_task = asyncio.create_task(self._microbatch_writer())
    self._cleanup_task = asyncio.create_task(self._wait_and_cleanup())

    from process_manager.phases import SPINNING_UP
    self._apply_phase(SPINNING_UP, "Starting the scripted timeline")

    timeout_seconds = self._HARD_WALLCLOCK_CAP_SECONDS
    raw = manifest.get("timeout_seconds")
    if isinstance(raw, (int, float)) and raw > 0:
        timeout_seconds = min(int(raw), self._HARD_WALLCLOCK_CAP_SECONDS)
    self._timeout_task = asyncio.create_task(
        self._enforce_timeout(
            timeout_seconds, interactive=bool(manifest.get("chat")),
        )
    )

    async def on_finish(success: bool, message: str) -> None:
        # The native finish op carries only a failure reason; a success
        # message is a plain host log line.
        if success and message:
            self.push_log(message)
        await self._op_finish({"success": success, "error_message": "" if success else message})

    process.run(play(demo), on_finish)


def install() -> None:
    from process_manager.manager import ProcessManager
    native = getattr(ProcessManager, "start", None)
    if native is None:
        raise RuntimeError("ProcessManager.start is gone — the demo runner's seam moved")
    check_native_wiring(native)
    ProcessManager.start = start  # type: ignore[assignment]
    _install_signal_routing()
