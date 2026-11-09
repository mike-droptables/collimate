"""Execution backends — where a generator's main loop runs.

A backend answers one question: given a run the host has fully prepared
(workspace hydrated, env built, permissions resolved), where does the
generator process execute and how does the host keep its four contracts
with it?

    1. **Spawn + host IPC.** The backend produces a child process whose
       stdio pipes the manager drains, and tells the manager how to bind
       the per-run IPC endpoint (address, connection handler, extra env
       the child needs to dial back — ``COLLIMATE_IPC_HOST`` /
       ``COLLIMATE_IPC_TOKEN``). Every backend's endpoint takes a fresh
       per-run token as the first frame (:func:`make_authed_ipc_handler`):
       the port serves the run's account secrets, and loopback is shared
       with every local process and user. The line-JSON wire protocol
       itself never varies by backend.
    2. **Workspace in/out.** How the run's workspace reaches the
       execution site. Both local backends share the host filesystem
       (native runs in-place; local docker bind-mounts the same absolute
       paths), so this contract is currently implicit; a non-local
       backend must hydrate/commit through the content-addressed store
       instead of assuming a shared kernel.
    3. **Exec / PTY.** Terminal cells attach via
       ``routes/terminal.py``, which today forks ``docker exec -ti`` (or
       a plain subprocess) on the host — valid precisely while the
       execution site is reachable by the host's own docker CLI. A
       remote backend bridges the same WebSocket to its runner's
       exec/PTY API instead.
    4. **Port exposure.** ``routes/proxy.py`` forwards
       ``/proxy/{ch}/{run}/{port}/…`` to :meth:`port_target`. Local
       backends publish on loopback; a remote backend returns whatever
       host:port its forwarded port or reverse tunnel landed on. A port
       reaches the proxy only once :meth:`serves_port` (or one of the
       run's environments) confirms the run serves it.

Backend selection is a function of the run's permission grant and lives
in :func:`select_backend` — the ONE place the ``docker == "limited"``
branch exists. Generators never know or care which backend hosts them:
capabilities are declared in manifests and resolved by the host, and the
same generator cell must behave identically under every backend. That
invariant — not this file's line count — is what any new backend is
measured against.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Awaitable, Callable

_logger = logging.getLogger("collimate.backends")


@dataclass
class IpcEndpoint:
    """How the manager should bind this run's IPC server."""

    bind_host: str
    #: asyncio connection handler, wrapped in the per-run token check.
    handler: Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]
    #: Extra env the child needs to dial back (the per-run token, and the
    #: host alias where loopback is not shared). ``COLLIMATE_IPC_PORT`` is
    #: appended by the manager after the bind.
    env: dict[str, str] = field(default_factory=dict)


#: The longest first frame an IPC connection may send: the auth frame is
#: ~80 bytes, so an unauthenticated peer cannot make the host buffer a
#: whole protocol-sized line (``collimate._host.ipc.IPC_MAX_LINE_BYTES``) before it is
#: refused.
_AUTH_FRAME_MAX_BYTES = 1024


async def _read_auth_frame(reader: asyncio.StreamReader) -> bytes | None:
    """The connection's first line, read at most :data:`_AUTH_FRAME_MAX_BYTES`
    ahead of its newline, or None (EOF or too long). Read byte by byte so
    nothing past the newline leaves the stream: the frames after it belong
    to the IPC loop."""
    frame = bytearray()
    while len(frame) <= _AUTH_FRAME_MAX_BYTES:
        byte = await reader.read(1)
        if not byte:
            return None
        if byte == b"\n":
            return bytes(frame)
        frame += byte
    return None


def make_authed_ipc_handler(manager, token: str) -> Callable:
    """Wrap the manager's IPC connection handler with a one-frame token check.

    The first line a connection sends MUST be
    ``{"op":"__auth__","token":<token>}`` with this run's token; only then is
    the connection handed to the normal ``_handle_ipc_connection`` loop (the
    reader is a stream — consuming the auth line just advances it). A
    missing, wrong or oversized first frame drops the connection before it
    can read an ack or take the run's writer slot."""

    async def _authed(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            first = await asyncio.wait_for(_read_auth_frame(reader), timeout=15)
        except (asyncio.TimeoutError, Exception):
            first = None
        ok = False
        try:
            msg = json.loads((first or b"").decode("utf-8", errors="replace").strip())
            ok = (
                isinstance(msg, dict)
                and msg.get("op") == "__auth__"
                and secrets.compare_digest(str(msg.get("token") or ""), token)
            )
        except Exception:
            ok = False
        if not ok:
            _logger.warning(
                "run %s: rejected IPC connection (bad/missing auth token)",
                getattr(manager, "run_id", "?"),
            )
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            return
        await manager._handle_ipc_connection(reader, writer)

    return _authed


def authed_ipc_endpoint(manager, bind_host: str, env: dict[str, str] | None = None) -> IpcEndpoint:
    """An endpoint on ``bind_host`` behind a fresh per-run token, which the
    child receives as ``COLLIMATE_IPC_TOKEN`` and presents as its first
    frame."""
    token = secrets.token_urlsafe(32)
    return IpcEndpoint(
        bind_host=bind_host,
        handler=make_authed_ipc_handler(manager, token),
        env={**(env or {}), "COLLIMATE_IPC_TOKEN": token},
    )


class ExecutionBackend:
    """Base class; see the module docstring for the four contracts."""

    name: str = "abstract"

    def ipc_endpoint(self, manager) -> IpcEndpoint:
        raise NotImplementedError

    def build_preexec(self, manager, manifest: dict):
        """A ``preexec_fn`` for the spawned child, or None. Only a spawn
        that shares the host kernel can use one; containerized backends
        map the mappable rlimits onto their own isolation primitives."""
        return None

    async def spawn(self, manager, *, env: dict, entrypoint: str, manifest: dict, preexec):
        """Start the generator's main loop; return an asyncio subprocess
        whose stdio the manager drains and whose exit ends the run."""
        raise NotImplementedError

    def port_target(self, port: int) -> tuple[str, int]:
        """Where the reverse proxy dials to reach a port this run bound."""
        return ("127.0.0.1", port)

    def serves_port(self, manager, port: int) -> bool:
        """Whether the run's own execution site listens on loopback
        ``port``. With the ports its environments publish, these are the
        only ports a ``port_bound`` claim, or a site the user opens
        (``routes/runs/sites.py``), may register. A backend that cannot
        tell says no."""
        return False


class NativeSubprocessBackend(ExecutionBackend):
    """Today's default: ``uv run`` on the host, IPC on loopback behind the
    per-run token (any local process or user can reach a loopback port). The
    uv and the Python are :mod:`native_tools`'s: a server started with
    ``--server-env`` gives the run its environment's uv and, where the
    entrypoint's ``requires-python`` admits it, its interpreter, and says
    which in the run log."""

    name = "native"

    def ipc_endpoint(self, manager) -> IpcEndpoint:
        return authed_ipc_endpoint(manager, "127.0.0.1")

    def build_preexec(self, manager, manifest: dict):
        return manager._build_preexec_fn(manifest)

    async def spawn(self, manager, *, env, entrypoint, manifest, preexec):
        from collimate_server.process_manager.native_tools import native_uv, run_python
        from collimate_server.process_manager.optional_deps import uv_run_argv
        from collimate_server.process_manager.streams import STREAM_LINE_LIMIT

        entry = os.path.join(manager.app_dir, entrypoint)
        python, said = run_python(entry)
        if said:
            manager.push_log(said)
        uv = native_uv()
        manager.native_launch = (uv, entry, env)
        argv = uv_run_argv(
            entrypoint,
            uv=uv,
            python=python,
            requirements=manager.optional_requirements,
            upgrade=manager.permissions.update_packages,
        )
        return await asyncio.create_subprocess_exec(
            *argv,
            cwd=manager.app_dir,
            env=native_bytecode_env(env),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE,
            start_new_session=True,
            limit=STREAM_LINE_LIMIT,
            preexec_fn=preexec,
        )

    def serves_port(self, manager, port: int) -> bool:
        """A native run shares the host's loopback, so the ports it serves
        are the ones a process of the run listens on: one in the generator's
        group (spawned as its own group leader, the unit the host signals to
        stop the run), or one in a group of a host terminal session the run
        served that is still live — the groups the run-end fence signals."""
        from collimate_server.process_manager.finalization import _session_groups, _still_names

        process = manager.process
        if process is not None and process.returncode is None and group_listens(process.pid, port):
            return True
        for session_id, started in dict(manager._host_terminal_sessions).items():
            if not _still_names(session_id, started):
                continue
            if any(group_listens(group, port) for group in _session_groups(session_id)):
                return True
        return False


class LocalDockerBackend(ExecutionBackend):
    """``docker: limited`` — the main loop inside a container on the host's
    own dockerd, with NO docker socket ("no further containers"). The
    ``docker run`` child carries the same stdio pipes as a native spawn,
    so drains/cleanup/timeout/stop signalling are identical. Loopback is
    container-local, so IPC binds 0.0.0.0 and the child dials the docker
    gateway alias, behind the same per-run token as every backend.
    The container publishes no ports, so it serves none on host loopback."""

    name = "local-docker"

    def ipc_endpoint(self, manager) -> IpcEndpoint:
        from collimate_server.process_manager.docker_runner import HOST_GATEWAY_ALIAS

        return authed_ipc_endpoint(
            manager, "0.0.0.0", {"COLLIMATE_IPC_HOST": HOST_GATEWAY_ALIAS},
        )

    async def spawn(self, manager, *, env, entrypoint, manifest, preexec):
        from collimate_server.process_manager.docker_runner import start_containerized_run

        return await start_containerized_run(
            manager, env=env, entrypoint=entrypoint, manifest=manifest,
        )


#: Where every native run keeps its bytecode, relative to the data dir (the
#: server's CWD).
NATIVE_PYCACHE = os.path.join(".collimate", "pycache")


def native_bytecode_env(env: dict[str, str]) -> dict[str, str]:
    """``env`` for a native run, its bytecode kept under the data dir.

    The run's cwd is its generator version's read-only tree
    (``run_staging.stage_stable_tree``), so no ``__pycache__`` can be written
    beside its modules; and a ``-B`` server's environment (the desktop
    package's, whose hash names it and which on macOS is inside the signed
    app) must stay as built though the run imports the SDK from it
    (:func:`sdk_src_dir`, its PYTHONPATH), possibly on another Python. So
    every native run gets ``PYTHONPYCACHEPREFIX`` under the data dir: its
    paths are stable (one tree per generator version), so it stays bounded,
    and every module the run imports is still cached."""
    return {**env, "PYTHONPYCACHEPREFIX": os.path.abspath(NATIVE_PYCACHE)}


#: The packages a run's PYTHONPATH directory holds, and nothing else.
SDK_PACKAGES = frozenset({"collimate", "gen"})


def sdk_src_dir() -> str:
    """The directory a generator run gets as its PYTHONPATH, so it can
    ``import gen`` and ``import collimate`` (the ``uv run`` environment is
    fresh and holds only the run's own dependencies): the parent of the
    imported ``collimate`` package. The native spawn puts it on PYTHONPATH
    and docker_runner bind-mounts this same directory read-only at the same
    path, so the two agree.

    It holds the SDK's two packages and nothing else in every install
    layout, so nothing the server imports can shadow a run's own
    dependencies:

      * a checkout (the editable install): ``<repo>/sdk/src``;
      * the wheel (``pip install collimate``, ``uvx collimate``, the desktop
        package's server environment): ``<site-packages>/collimate-run-sdk``
        (``collimate.shared.WHEEL_SDK_HOME``), a directory of their own that
        the wheel's ``collimate-run-sdk.pth`` puts on the server's
        ``sys.path``; ``site-packages`` itself holds the server and its
        dependencies.

    Raises when the directory holds anything else (a name a run could
    import), rather than hand a run the server's packages."""
    import collimate
    home = os.path.dirname(os.path.dirname(os.path.abspath(collimate.__file__)))
    stray = sorted(name for name in os.listdir(home) if not name.startswith(".") and name not in SDK_PACKAGES)
    if stray:
        raise RuntimeError(f"the SDK directory {home} holds {', '.join(stray)} beside collimate and gen; "
                           "a run would import them ahead of its own dependencies")
    return home


def group_listens(pgid: int, port: int) -> bool:
    """Whether a process in group ``pgid`` holds a TCP socket listening on
    ``port``: read from the kernel's socket tables on Linux, from ``lsof``
    elsewhere."""
    if sys.platform.startswith("linux"):
        return _proc_group_listens(pgid, port)
    return _lsof_group_listens(pgid, port)


def _proc_group_listens(pgid: int, port: int) -> bool:
    sockets: set[str] = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(table, encoding="ascii") as fh:
                rows = fh.read().splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            # local_address is HEXIP:HEXPORT, state 0A is LISTEN, and field 9
            # is the inode a holder's fd links to as socket:[inode].
            fields = row.split()
            if fields[3] == "0A" and int(fields[1].rsplit(":", 1)[1], 16) == port:
                sockets.add(f"socket:[{fields[9]}]")
    if not sockets:
        return False
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            if os.getpgid(int(entry)) != pgid:
                continue
            fds = os.listdir(f"/proc/{entry}/fd")
        except OSError:
            continue
        for fd in fds:
            try:
                if os.readlink(f"/proc/{entry}/fd/{fd}") in sockets:
                    return True
            except OSError:
                continue
    return False


def _lsof_group_listens(pgid: int, port: int) -> bool:
    try:
        found = subprocess.run(
            ["lsof", "-nP", "-a", f"-g{pgid}", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return found.returncode == 0 and bool(found.stdout.strip())


class RunsOnViolation(RuntimeError):
    """The run's placement contradicts the generator's declared
    execution-locus assumption."""


def enforce_runs_on(manifest: dict | None, backend: ExecutionBackend) -> None:
    """Host-side enforcement of the manifest's ``runs_on`` declaration.

    ``local-machine`` means the generator legitimately assumes the user's
    own machine — reads ``~/.ssh``, talks to a localhost service, touches
    paths outside the workspace. No sandboxed or remote backend can
    preserve that, and it must not break silently: a containerized grant
    paired with such a generator is a configuration contradiction,
    refused loudly here before anything spawns. The default
    (``anywhere``, or the field absent) runs under any backend.
    """
    runs_on = str((manifest or {}).get("runs_on") or "anywhere").strip()
    if runs_on == "local-machine" and backend.name != "native":
        raise RunsOnViolation(
            f"this generator declares 'runs_on: local-machine' — it assumes the "
            f"user's own machine — but the run's permission grant places it on "
            f"the {backend.name!r} backend. Use a runner without docker "
            f"containment, or remove the runs_on constraint if the generator "
            f"no longer needs the local machine."
        )


def select_backend(permissions) -> ExecutionBackend:
    """The one place backend choice is decided.

    A grant of ``docker: limited`` hosts the generator main loop in a local
    container; every other grant (``none`` or ``full``) is a native
    subprocess. ``full`` stays native by the pinned ladder semantics — the
    internet invariant guarantees a full grant is ``internet: full``, so
    nothing forces its containerization.
    """
    if permissions.docker == "limited":
        return LocalDockerBackend()
    return NativeSubprocessBackend()
