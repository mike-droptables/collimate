"""Host-placed environments — the docker side of the ``env_*`` IPC ops.

The generator declares WHAT environment it needs (image, mount, env —
the ``env_place`` spec); this service decides WHERE it lands and owns
every docker call. Generator code never touches a docker daemon: the
``_env_shared.DockerEnvironment`` library in the SDK repo is a thin
client of these ops, its public API unchanged. That inversion is what
lets the host's execution backend place an environment beside the run —
today on the local dockerd, later wherever a remote backend runs.

The logic here is the proven body of the old generator-side library
moved across the IPC boundary: ensure-image (build from a Dockerfile
context on first use, base image first), workspace bind at the declared
mount, per-run labels (the docker reaper's backstop), exec with
argv-verbatim semantics, loopback port forwarders, graceful teardown.
A port a program listens on only inside its container (127.0.0.1 there,
the default of most dev servers) is reached through an exec relay instead:
a loopback listener whose every connection runs a small stdio-to-TCP relay
in the container with ``docker exec`` (``find_listener``,
``expose_listener``).

The placed environments are also the host's record of which containers
and loopback ports belong to the run: a snapshot and a terminal session act
only on its containers (``owned_container``), and a ``port_bound`` claim is
checked against its ports (``serves_port``).

All methods are synchronous (docker-py blocks); the IPC handlers call
them via ``asyncio.to_thread``. The docker client is injected for
tests; ``None`` (default) lazily builds the real one.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import selectors
import socket
import stat
import sys
import tempfile
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Optional

from collimate_server import image_uses
from collimate_server.instance import INSTANCE_LABEL, instance_id
from collimate_server.process_manager.docker_runner import _DEFAULT_PIDS_LIMIT

_logger = logging.getLogger("collimate.environments")

# The most exec output the host carries back to a run, in characters: the
# whole output when it fits, else its LAST bytes (where a failure shows)
# behind a marker naming how many were dropped, the marker included in the
# count. Uncapped, the whole output would sit in this process and be
# json-serialized into the ack on the event loop. The IPC frame's byte limit,
# counted after JSON escaping, can still refuse an escape-heavy one.
# A caller that wants a file whole reads it in chunks
# (the Docker generators' ``DockerEnvironment.read_file``), never through
# one exec.
_EXEC_OUTPUT_CAP = 8_000_000
# Room kept for the marker: longer than any "[... N earlier bytes ...]\n".
_EXEC_MARKER_ROOM = 64


# The relay programs an image may offer, in the order the host prefers them:
# python3 and node relay a half-close both ways; socat ends 0.5 s after
# either side's EOF (its default ``-t``), so a client that half-closes gets
# only that long for the answer. nc is not used: its variants disagree on
# what stdin's EOF does.
_RELAY_PROGRAMS = ("python3", "node", "socat")

# Connect to argv[1]:argv[2], copy stdin to it (its EOF a half-close) and
# it to stdout, unbuffered; exit when the server's side ends. A refused
# connect is one stderr line and exit 1.
_PY_RELAY = """\
import os, socket, sys, threading
try:
    s = socket.create_connection((sys.argv[1], int(sys.argv[2])))
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
except OSError as e:
    sys.stderr.write("relay: %s\\n" % e)
    sys.exit(1)
def up():
    try:
        while True:
            d = os.read(0, 65536)
            if not d:
                break
            s.sendall(d)
        s.shutdown(socket.SHUT_WR)
    except OSError:
        pass
threading.Thread(target=up, daemon=True).start()
try:
    while True:
        d = s.recv(65536)
        if not d:
            break
        while d:
            d = d[os.write(1, d):]
except OSError:
    pass
os._exit(0)
"""

# The same in node. Its stdout on a pipe is asynchronous: a write the pipe
# has no room for waits in the stream, and process.exit() drops it, so the
# server's side ending exits only once stdout has drained (end's callback)
# and, when stdin has already ended, once the half-close's last bytes
# reached the server (s's finish). pipe() never ends process.stdout itself.
_NODE_RELAY = (
    'const s = require("net").connect({host: process.argv[1], port: +process.argv[2], allowHalfOpen: true, noDelay: true});'
    's.on("error", e => { process.stderr.write("relay: " + e.message + "\\n"); process.exit(1); });'
    'process.stdout.on("error", () => process.exit(0));'
    'process.stdin.pipe(s);'
    's.pipe(process.stdout);'
    'let left = 2; const done = () => --left || process.exit(0);'
    's.on("end", () => { process.stdout.end(done);'
    ' s.writableEnded && !s.writableFinished ? s.on("finish", done) : done(); });'
)

# The relay program's stderr kept for the log line of a relay that failed.
_RELAY_STDERR_TAIL = 2000


@dataclass(frozen=True)
class PortListener:
    """A program listening on ``port`` in an open environment's container.

    ``bridge``: the host reaches it at the container's bridge address.
    Otherwise ``address`` is where it listens inside the container (read
    from its ``/proc/net/tcp*``: ``127.0.0.1`` for most dev servers), and
    ``relay`` is the program in the image the host relays each connection
    through with a ``docker exec`` (``python3``, ``node`` or ``socat``),
    None when the image has none of them."""

    env_id: str
    port: int
    bridge: bool
    address: str = ""
    relay: str | None = None


class _Forwarder:
    """One loopback listener the host runs for a container, and the
    connections it carries; ``close`` ends both."""

    def __init__(self, server: socket.socket) -> None:
        self.server = server
        self.stop = threading.Event()
        self._conns: set[socket.socket] = set()
        self._lock = threading.Lock()

    def track(self, *socks: socket.socket) -> bool:
        """Count ``socks`` as carried; False once the forwarder is closed."""
        with self._lock:
            if self.stop.is_set():
                return False
            self._conns.update(socks)
            return True

    def untrack(self, *socks: socket.socket) -> None:
        with self._lock:
            self._conns.difference_update(socks)

    def close(self) -> None:
        """Stop accepting and wake every carried connection: its sockets
        are shut down here and closed by the thread that carries it."""
        with self._lock:
            self.stop.set()
            conns = list(self._conns)
            self._conns.clear()
        # A listening socket a select() is waiting on stays open until it
        # returns; the shutdown ends it at once, so the port refuses now.
        for sock in (self.server, *conns):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        try:
            self.server.close()
        except OSError:
            pass


class PlacedEnvironment:
    """One placed container, its host-side forwarders, and the loopback
    host ports the host serves for it (published at placement, forwarded
    by ``expose_port``, relayed by ``expose_listener``)."""

    def __init__(self, env_id: str, container, readonly_files=None) -> None:
        self.env_id = env_id
        self.container = container
        self.forwarders: list[_Forwarder] = []
        # Container port -> the loopback host port forwarding to it at the
        # container's bridge address.
        self.forwarded: dict[int, int] = {}
        # Container port -> the loopback host port relaying to it with an
        # exec per connection.
        self.relayed: dict[int, int] = {}
        # The image's relay program (``_RELAY_PROGRAMS``), once looked up.
        self.relay_program: str | None = None
        self.relay_probed = False
        self.host_ports: set[int] = set()
        self.closed = False
        self.readonly_files = readonly_files


class EnvironmentService:
    """Places and drives environment containers for ONE run."""

    def __init__(self, manager, client_factory: Optional[Callable] = None) -> None:
        self._manager = manager
        self._client_factory = client_factory
        self._client = None
        self._envs: dict[str, PlacedEnvironment] = {}
        self._seq = 0
        # Held while a port is forwarded (and registered) or an environment
        # closes: the two run on different worker threads, and neither may
        # see the other half done.
        self._ports_lock = threading.Lock()

    # -- plumbing ------------------------------------------------------------

    def _docker(self):
        if self._client is None:
            if self._client_factory is not None:
                self._client = self._client_factory()
            else:
                import docker

                self._client = docker.from_env()
                self._client.ping()
        return self._client

    def _log(self, line: str) -> None:
        try:
            self._manager.log_buffer.append(
                {"type": "stderr", "payload": line.rstrip("\n") + "\n"}
            )
        except Exception:
            pass

    def _env(self, env_id: str) -> PlacedEnvironment:
        placed = self._envs.get(env_id)
        if placed is None or placed.closed:
            raise RuntimeError(f"no such environment: {env_id!r}")
        return placed

    # -- ops -----------------------------------------------------------------

    def _placement_paths(self, spec: dict) -> tuple[str, str | None, dict, dict]:
        """Resolve host paths before Docker or permission changes can touch them."""
        def container_path(value):
            return (isinstance(value, str) and value.startswith("/")
                    and not value.startswith("//")
                    and not any(char in value for char in ("\0", ":", "\\"))
                    and ".." not in value.split("/"))

        if spec.get("mount") and not container_path(spec["mount"]):
            raise RuntimeError("env_place: mount must be an absolute container path")
        if spec.get("mount_mode", "rw") not in {"ro", "rw"}:
            raise RuntimeError("env_place: mount_mode must be ro or rw")
        # The mounted workspace is the run's own; the app directory (the
        # generator version's stable tree, shared by every run of it) is
        # only read, as a build context.
        workspace_root = Path(self._manager.workspace_dir).resolve()
        build_roots = [workspace_root]
        app_root = getattr(self._manager, "app_root_dir", None)
        if app_root:
            build_roots.append(Path(app_root).resolve())

        def directory(value, field, roots, where):
            if not isinstance(value, str) or not value:
                raise RuntimeError(f"env_place: {field} must name a directory inside this run")
            try:
                path = Path(value).resolve(strict=True)
            except (OSError, ValueError, RuntimeError):
                raise RuntimeError(f"env_place: {field} is not an accessible run directory") from None
            if not path.is_dir() or not any(path.is_relative_to(root) for root in roots):
                raise RuntimeError(f"env_place: {field} is outside the run's {where}")
            return str(path)

        workspace = directory(spec.get("workspace") or self._manager.workspace_dir, "workspace",
                              [workspace_root], "workspace")
        build_context = (directory(spec["build_context"], "build_context", build_roots,
                                   "workspace and app directory")
                         if spec.get("build_context") else None)
        volumes = spec.get("volumes") or {}
        if not isinstance(volumes, dict):
            raise RuntimeError("env_place: volumes must map host-derived bindings or declared sockets to container paths")
        sockets = getattr(self._manager, "env_passthrough_sockets", frozenset())
        host_bindings = list(getattr(self._manager, "host_bindings", {}).values())
        validated = {}
        for source, binding in volumes.items():
            if (not isinstance(binding, dict) or set(binding) - {"bind", "mode"}
                    or not container_path(binding.get("bind"))
                    or binding.get("mode", "rw") not in {"ro", "rw"}):
                raise RuntimeError("env_place: a binding requires a container path and ro/rw mode")
            try:
                path = Path(source).resolve(strict=True)
                allowed = str(path) in sockets and stat.S_ISSOCK(path.stat().st_mode)
                if not allowed:
                    allowed = any(
                        path.is_relative_to(Path(grant["path"]))
                        and (grant["mode"] == "rw" or binding.get("mode", "rw") == "ro")
                        for grant in host_bindings
                    )
            except (OSError, TypeError, ValueError, RuntimeError):
                allowed = False
            if not allowed:
                raise RuntimeError("env_place: volumes may mount only host-derived account bindings or declared env_passthrough sockets")
            validated[str(path)] = {"bind": binding["bind"], "mode": binding.get("mode", "rw")}

        readonly = spec.get("readonly_files", {})
        if not isinstance(readonly, dict):
            raise RuntimeError("env_place: readonly_files must map container paths to text")
        occupied = [PurePosixPath(binding["bind"]) for binding in validated.values()]
        if spec.get("mount"):
            occupied.append(PurePosixPath(spec["mount"]))
        for target, content in readonly.items():
            if not container_path(target) or not isinstance(content, str):
                raise RuntimeError("env_place: readonly_files requires absolute container paths and text content")
            dest = PurePosixPath(target)
            if dest == PurePosixPath("/") or any(
                dest.is_relative_to(other) or other.is_relative_to(dest) for other in occupied
            ):
                raise RuntimeError("env_place: readonly_files must not overlap another mount")
            occupied.append(dest)
        return workspace, build_context, validated, readonly

    def _git_paths(self) -> tuple[dict[str, str], dict[str, str]]:
        """The run's repositories with a ``.git``: those it materialized as
        its cell's lease holder, and those whose read-only ``.git`` it has;
        none for ``docker: limited`` (R10)."""
        if getattr(self._manager.permissions, "docker", None) == "limited":
            return {}, {}
        run = getattr(self._manager, "git", None)
        lease = getattr(run, "lease", None)
        held = lease.materialized if lease is not None and lease.run_id == self._manager.run_id else {}
        return dict(held), dict(getattr(run, "readonly", None) or {})

    def _store_mount(self, spec: dict, volumes: dict, readonly: dict) -> list:
        """The read-only bind of the channel's git store objects at their own
        path (§4.4), so the alternates of the run's ``.git`` resolve in the
        container: only for a run with a ``.git`` (it holds its cell's lease
        and materialized a repository, or has a read-only ``.git``), when
        ``<store>/objects`` is a real directory the server user owns and its
        path overlaps no other mount of the placement. Otherwise none
        (``docker: limited`` never: R10); an overlap leaves the repositories
        out of the container, with a line."""
        held, readonly_gits = self._git_paths()
        paths = {**held, **readonly_gits}
        if not paths:
            return []
        import collimate_server.git_store as git_store
        objects = git_store.objects_path(self._manager.channel)
        try:
            found = os.lstat(objects)
        except OSError:
            return []
        if not stat.S_ISDIR(found.st_mode) or found.st_uid != os.getuid():
            return []
        target = PurePosixPath(objects)
        taken = [PurePosixPath(binding["bind"]) for binding in volumes.values()]
        taken += [PurePosixPath(path) for path in readonly]
        if spec.get("mount"):
            taken.append(PurePosixPath(spec["mount"]))
        clash = next((other for other in taken
                      if other == target or other.is_relative_to(target) or target.is_relative_to(other)), None)
        if clash is not None:
            self._log(f"git: {', '.join(sorted(path or '.' for path in paths))} left out of "
                      f"this container: the channel's git objects at {objects} overlap its mount at {clash}")
            return []
        import docker.types
        return [docker.types.Mount(target=objects, source=objects, type="bind", read_only=True)]

    def _readonly_git_mounts(self, spec: dict, workspace: str) -> list:
        """A read-only bind of each read-only ``.git`` of the run over its
        place in the workspace mount, so a root user in the container cannot
        write it either (the host's write bits stop everyone else). Only
        for a ``.git`` that is a real directory, reached through no link,
        inside the mounted ``workspace``. The bind keeps its directory, which is why a newer
        state replaces its entries in place
        (``readonly_trees.replace_entries``)."""
        mount = str(spec.get("mount") or "")
        _held, readonly_gits = self._git_paths()
        if not mount or not readonly_gits:
            return []
        import docker.types
        from collimate import fs as collimate_fs
        root = Path(self._manager.workspace_dir).resolve()
        mounted = Path(workspace)
        mounts = []
        for repo_path in sorted(readonly_gits):
            source = root.joinpath(*collimate_fs.relative_parts(repo_path), ".git") if repo_path else root / ".git"
            try:
                found = os.lstat(source)
            except OSError:
                continue
            if (not stat.S_ISDIR(found.st_mode) or os.path.realpath(source) != str(source)
                    or not source.is_relative_to(mounted)):
                continue
            target = PurePosixPath(mount).joinpath(*source.relative_to(mounted).parts)
            mounts.append(docker.types.Mount(target=str(target), source=str(source), type="bind", read_only=True))
        return mounts

    def place(self, spec: dict) -> dict:
        """Create the container a generator asked for; return its ids and
        the image it runs.

        ``base_image: collimate-linux-small`` names the host's base image
        (:mod:`process_manager.base_image`), which the host stages and builds
        itself: with no ``build_context`` the container runs the base, and
        ``image`` names it (``collimate-linux-small``); with one, ``image``
        is an untagged ``collimate-<name>`` the host tags from the context,
        the base and the build args, passing the base's tag as ``BASE``. Any
        other ``base_image`` is refused."""
        from collimate_server.process_manager import base_image as host_base
        image = str(spec.get("image") or "")
        if not image:
            raise RuntimeError("env_place: 'image' is required")
        base = spec.get("base_image") or None
        if base is not None and base != host_base.BASE_IMAGE:
            raise RuntimeError(f"env_place: base_image names the host's {host_base.BASE_IMAGE} only")
        if base is not None and not spec.get("build_context") and image != host_base.BASE_IMAGE:
            raise RuntimeError(f"env_place: a placement on the base without a build_context runs "
                               f"image {host_base.BASE_IMAGE}")
        workspace, build_context, extra_volumes, readonly = self._placement_paths(spec)
        client = self._docker()

        # May this placement ask UPSTREAM for newer content (pull the FROM
        # base, pull a registry-only image)? The generator may state it
        # explicitly; otherwise the run's ``update_packages`` grant decides.
        # Note this only bites when a build/pull actually happens: a tag the
        # host already holds short-circuits either way, so on an unchanged
        # (content-addressed) context freshness costs nothing.
        refresh = spec.get("refresh")
        if refresh is None:
            refresh = self._manager.permissions.update_packages
        build_args = dict(spec.get("build_args") or {})
        if base is None:
            self._ensure_image(client, image, build_context, None, None,
                               build_args=build_args, refresh=bool(refresh))
        else:
            with host_base.staged() as staged:
                if build_context is None:
                    image = staged.tag
                else:
                    build_args[host_base.BASE_ARG] = staged.tag
                    try:
                        image = host_base.layered_tag(image, build_context, staged, build_args)
                    except ValueError as exc:
                        raise RuntimeError(f"env_place: {exc}") from None
                self._ensure_image(client, image, build_context, staged.tag, staged.context,
                                   build_args=build_args, refresh=bool(refresh))

        mount = str(spec.get("mount") or "")
        if mount:
            _relax_perms(workspace)

        vols: dict = {}
        if mount:
            vols[workspace] = {"bind": mount, "mode": str(spec.get("mount_mode") or "rw")}
        vols.update(extra_volumes)
        mounts = self._store_mount(spec, vols, readonly) + self._readonly_git_mounts(spec, workspace)

        run: dict = {
            "init": True,
            # A fork-bomb backstop, as docker:limited runs get (docker_runner's
            # _DEFAULT_PIDS_LIMIT); without it the only cap is the host's
            # systemd TasksMax, if any. No cap_drop / no-new-privileges: the
            # launch images rely on sudo inside the container.
            "pids_limit": _DEFAULT_PIDS_LIMIT,
            "labels": {
                "com.collimate.run_id": self._manager.run_id,
                "com.collimate.kind": str(spec.get("kind") or "environment"),
                # Which server placed this. The run-id label alone is not
                # enough for the startup reaper: a second Collimate server
                # (the dev run beside the installed app) stamps run ids of
                # the same shape on the same dockerd, so without the owner
                # stamp its boot sweep would force-remove THIS run's live
                # container. See api/collimate_server/instance.py.
                INSTANCE_LABEL: instance_id(),
            },
        }
        # command=None runs the image's own ENTRYPOINT/CMD. The spec uses
        # an explicit null to say so; absence means the library default
        # ("sleep infinity") was already filled in client-side.
        if spec.get("command") is not None:
            run["command"] = spec["command"]
        if spec.get("ports"):
            run["ports"] = _port_bindings(spec["ports"])
        if spec.get("extra_hosts"):
            run["extra_hosts"] = dict(spec["extra_hosts"])
        working_dir = spec.get("working_dir") or (mount or None)
        if working_dir:
            run["working_dir"] = working_dir

        name = f"{spec.get('name_prefix') or 'collimate-env'}-{uuid.uuid4().hex[:8]}"
        readonly_files = None
        try:
            if readonly:
                readonly_files = tempfile.TemporaryDirectory(prefix="collimate-env-files-")
                for index, (target, content) in enumerate(readonly.items()):
                    source = Path(readonly_files.name) / str(index)
                    source.write_text(content, encoding="utf-8")
                    source.chmod(0o444)
                    vols[str(source)] = {"bind": target, "mode": "ro"}
            if mounts:
                run["mounts"] = mounts
            container = client.containers.run(
                image,
                detach=True,
                name=name,
                volumes=vols,
                environment=dict(spec.get("environment") or {}),
                **run,
            )
        except BaseException:
            if readonly_files is not None:
                readonly_files.cleanup()
            raise
        self._seq += 1
        env_id = f"env-{self._seq}"
        placed = PlacedEnvironment(env_id, container, readonly_files)
        self._envs[env_id] = placed
        if run.get("ports"):
            placed.host_ports.update(_published_ports(container))
        return {"env_id": env_id, "container_id": container.id, "image": image}

    def exec(
        self,
        env_id: str,
        command,
        *,
        cwd: str = "",
        env: Optional[dict] = None,
        user: str = "",
        detach: bool = False,
    ) -> tuple[int, str]:
        """Run ``command`` (argv list, verbatim — the client owns shell
        quoting/timeout wrappers) in the environment's container. The
        output returned is at most :data:`_EXEC_OUTPUT_CAP`
        characters: its tail, marked, when it is longer."""
        placed = self._env(env_id)
        cmd = list(command) if isinstance(command, (list, tuple)) else ["sh", "-c", str(command)]
        if detach:
            placed.container.exec_run(
                cmd,
                workdir=cwd or None,
                environment=env or None,
                demux=False,
                user=user or "",
                detach=True,
            )
            return (0, "")
        # Streamed through a bounded tail. docker-py's exec_run(stream=True)
        # drops the exit code, so the low-level calls run it and read the
        # code once the stream ends.
        api = placed.container.client.api
        exec_id = api.exec_create(
            placed.container.id, cmd,
            workdir=cwd or None, environment=env or None, user=user or "",
        )["Id"]
        # docker-py leaves a streamed response open for the caller to close.
        stream = api.exec_start(exec_id, stream=True)
        try:
            output = _output_tail(stream, _EXEC_OUTPUT_CAP)
        finally:
            try:
                stream.close()
            except Exception as e:  # noqa: BLE001 — a socket already gone, or ssh://
                _logger.debug("closing an exec stream failed: %s", e)
        code = api.exec_inspect(exec_id).get("ExitCode")
        return (code if code is not None else -1, output)

    def expose_port(self, env_id: str, port: int, *, serve: bool = False) -> int:
        """Forward a loopback host port to the container's ``port``;
        return the host port. A port already forwarded keeps its forwarder
        and host port, concurrent callers included. Exposing is not
        serving: the generator registers the host port with serve_port;
        ``serve`` registers it in the run's ``bound_ports`` at once (a site
        the user opens), in the same step, so a close cannot fall between.
        Raises RuntimeError once the environment is closed."""
        port = int(port)
        with self._ports_lock:
            placed = self._env(env_id)
            host_port = placed.forwarded.get(port)
            if host_port is None:
                host_port = _start_forwarder(placed, _container_ip(placed.container), port)
                placed.forwarded[port] = host_port
                placed.host_ports.add(host_port)
            if serve:
                self._manager.bound_ports.add(host_port)
            return host_port

    def expose_listener(self, listener: PortListener, *, serve: bool = False) -> int:
        """The loopback host port that reaches ``listener``: the bridge
        forwarder of ``expose_port`` for a listener the bridge reaches,
        else an exec relay through ``listener.relay`` to its address in
        the container (reused for the same container port, concurrent
        callers included). ``serve`` as for ``expose_port``. Raises
        RuntimeError once the environment is closed, or for a listener
        with no relay program."""
        if listener.bridge:
            return self.expose_port(listener.env_id, listener.port, serve=serve)
        if listener.relay is None:
            raise RuntimeError(f"no relay program for port {listener.port}")
        port = int(listener.port)
        with self._ports_lock:
            placed = self._env(listener.env_id)
            host_port = placed.relayed.get(port)
            if host_port is None:
                argv = _relay_argv(listener.relay, _relay_target(listener.address), port)
                host_port = _start_relay(placed, argv, port)
                placed.relayed[port] = host_port
                placed.host_ports.add(host_port)
            if serve:
                self._manager.bound_ports.add(host_port)
            return host_port

    def find_listener(self, port: int, *, timeout: float = 0.5) -> PortListener | None:
        """A program listening on ``port`` in an open environment's
        container, or None (blocking). Per container, a short connect
        probe at its bridge address; one that refuses is asked with an exec
        which address listens there (``/proc/net/tcp*``: a server bound to
        127.0.0.1 in the container is no bridge's to reach), and which
        relay program its image has. A listener the bridge reaches wins
        over one only a relay reaches. An image with no ``sh`` or no
        ``/proc`` shows no listener."""
        port = int(port)
        found = None
        for env_id, placed in list(self._envs.items()):
            if placed.closed or placed.container is None:
                continue
            try:
                target = (_container_ip(placed.container), port)
                with socket.create_connection(target, timeout=timeout):
                    return PortListener(env_id, port, bridge=True)
            except Exception:  # noqa: BLE001 — a refused connect, or a container docker no longer reports
                pass
            if found is None:
                address = self._listen_address(env_id, port)
                if address is not None:
                    found = PortListener(env_id, port, bridge=False, address=address,
                                         relay=self._relay_program(env_id))
        return found

    def _listen_address(self, env_id: str, port: int) -> str | None:
        """The address a program listens on ``port`` at in the container,
        from its kernel's TCP tables; None when nothing does or they cannot
        be read."""
        try:
            _code, out = self.exec(
                env_id, ["sh", "-c", "cat /proc/net/tcp /proc/net/tcp6 2>/dev/null"], cwd="/")
        except Exception as e:  # noqa: BLE001 — no sh in the image, or the container gone
            _logger.debug("reading %s's TCP tables failed: %s", env_id, e)
            return None
        return _proc_listen_address(out, port)

    def _relay_program(self, env_id: str) -> str | None:
        """The image's relay program (``_RELAY_PROGRAMS``' first it has),
        looked up once per environment; None when it has none (a failed
        lookup is not kept)."""
        placed = self._env(env_id)
        if not placed.relay_probed:
            lookup = "; ".join(
                f'command -v {name} >/dev/null 2>&1 && echo {name}' for name in _RELAY_PROGRAMS)
            try:
                _code, out = self.exec(env_id, ["sh", "-c", lookup + "; true"], cwd="/")
            except Exception as e:  # noqa: BLE001 — the container gone meanwhile
                _logger.debug("looking up %s's relay program failed: %s", env_id, e)
                return None
            names = out.split()
            placed.relay_program = next((name for name in _RELAY_PROGRAMS if name in names), None)
            placed.relay_probed = True
        return placed.relay_program

    def owned_container(self, ref: str):
        """The container of an open environment whose id or name is
        ``ref``, or None."""
        if not ref:
            return None
        for placed in list(self._envs.values()):
            container = placed.container
            if not placed.closed and container is not None and ref in (container.id, container.name):
                return container
        return None

    def serves_port(self, port: int) -> bool:
        """Whether an open environment publishes or forwards host ``port``."""
        return any(
            port in placed.host_ports
            for placed in list(self._envs.values()) if not placed.closed
        )

    def close(self, env_id: str) -> None:
        """Stop forwarders and remove the container. Idempotent. Its host
        ports leave the run's ``bound_ports``: a freed port is the next
        placement's to take, and the proxy must not reach it for this run."""
        placed = self._envs.get(env_id)
        if placed is None or (placed.closed and placed.container is None
                              and placed.readonly_files is None):
            return
        with self._ports_lock:
            placed.closed = True
            self._manager.bound_ports.difference_update(placed.host_ports)
            for forwarder in placed.forwarders:
                forwarder.close()
            placed.forwarders.clear()
            placed.forwarded.clear()
            placed.relayed.clear()
        container = placed.container
        if container is not None:
            # Graceful stop first (SIGTERM + 2s) so a process writing into
            # the mounted workspace isn't SIGKILL'd mid-write.
            try:
                container.stop(timeout=2)
            except Exception:
                pass
            # Removal failures must reach the close acknowledgement. Keep
            # ownership so run teardown can try again, even though the
            # environment is already closed to exec/port consumers.
            container.remove(force=True)
            placed.container = None
        if placed.readonly_files is not None:
            placed.readonly_files.cleanup()
            placed.readonly_files = None

    def close_all(self) -> None:
        """Run teardown: every environment this run placed goes with it
        (the run-id label reaper is the backstop, this is the front door)."""
        for env_id in list(self._envs):
            try:
                self.close(env_id)
            except Exception:
                _logger.warning("close_all: %s failed", env_id, exc_info=True)

    # -- image ensure ---------------------------------------------------------

    def _ensure_image(
        self,
        client,
        image: str,
        build_context: Optional[str],
        base_image: Optional[str],
        base_context: Optional[str],
        *,
        build_args: Optional[dict] = None,
        refresh: bool = True,
    ) -> None:
        """Build-if-missing, base image first (a local-only ``FROM`` tag
        must exist before the main build).

        ``refresh`` only reaches the build whose ``FROM`` is actually UPSTREAM.
        ``docker build --pull`` re-pulls every ``FROM``, so passing it to an
        image layered on our own locally-built base makes the build fail
        outright ("pull access denied for collimate-linux-small") — that base
        exists in no registry. The upstream fetch belongs to the base's OWN
        build (its ``FROM`` is debian), or to a leaf that has no local base
        (the vscode image FROMs code-server, the runner FROMs python).

        Then the use of the image and of its base is recorded
        (:mod:`collimate_server.image_uses`): a cache hit and a build both
        count."""
        import docker.errors
        refs = (image, base_image) if base_image else (image,)
        with image_uses.ensuring(refs):
            try:
                client.images.get(image)
            except docker.errors.ImageNotFound:
                if base_image:
                    self._refuse_offline_base(client, base_image)
                    # The base's own FROM is upstream, so this is where a refresh pulls.
                    self._get_or_build(client, base_image, base_context, refresh=refresh)
                self._get_or_build(
                    client, image, build_context, build_args=build_args,
                    refresh=refresh and not base_image,
                )
            for ref in filter(image_uses.is_collimate_ref, refs):
                try:
                    found = client.images.get(ref)
                except docker.errors.ImageNotFound:
                    continue  # a base untagged since the image was built on it
                image_uses.record_use(ref, found.id)

    def _refuse_offline_base(self, client, tag: str) -> None:
        """A run with ``internet: none`` may not build the base: its build
        installs Debian packages, Node and uv from the network. Once this
        machine holds the tag, any run uses it."""
        import docker.errors
        if getattr(self._manager.permissions, "internet", None) != "none":
            return
        try:
            client.images.get(tag)
        except docker.errors.ImageNotFound:
            raise RuntimeError(
                f"The {tag.partition(':')[0]} base image is not built on this machine yet, and building "
                "it needs the internet: run once with internet: full to build it") from None

    def _get_or_build(
        self, client, tag: str, build_context: Optional[str], *,
        build_args: Optional[dict] = None, refresh: bool = True,
    ) -> None:
        """Ensure ``tag`` is present locally, building or pulling it if not.

        Tag presence is the cache: image tags are content-addressed by their
        build context (``collimate.image_tags.derive_image_tag``), so "present" means
        "this exact content was already built" and a Dockerfile edit arrives
        as a tag the host has never seen.

        ``refresh`` (the run's ``update_packages`` grant) only matters when
        work actually happens: it decides whether the build may re-pull its
        ``FROM`` base, and whether a tag with no build context may be pulled
        from a registry at all."""
        import docker.errors

        try:
            client.images.get(tag)
            return
        except docker.errors.ImageNotFound:
            pass
        if not build_context:
            # No context to build from. A ``collimate-*`` tag is one WE build,
            # so arriving here without a context is a wiring bug — say so
            # plainly rather than attempting a pull that would fail with a
            # confusing registry error. Anything else is a registry reference
            # (a runner's user-supplied ``image``), which we may pull —
            # reaching upstream, so it rides the same grant.
            if tag.startswith("collimate-"):
                raise RuntimeError(
                    f"image {tag!r} is not present and no build_context was "
                    "given — a collimate-owned image must be built, not pulled"
                )
            if not refresh:
                raise RuntimeError(
                    f"image {tag!r} is not present locally and this run may not "
                    "fetch it — the runner would need permissions."
                    "update_packages: true (or the image must be pre-pulled)"
                )
            self._log(f"[env] pulling image {tag}")
            try:
                client.images.pull(*_split_ref(tag))
                return
            except docker.errors.DockerException as e:
                raise RuntimeError(f"could not pull image {tag!r}: {e}")
        self._log(f"[env] building image {tag} (first run — may take a few minutes)")
        try:
            _, logs = client.images.build(
                path=str(Path(build_context).resolve()), tag=tag, rm=True,
                buildargs=dict(build_args or {}), pull=bool(refresh),
                labels={image_uses.BUILT_LABEL: "1"},
            )
            for chunk in logs:
                if chunk.get("error"):
                    detail = chunk.get("errorDetail", {}).get("message") or chunk["error"]
                    raise RuntimeError(f"Docker build failed: {detail}")
                line = (chunk.get("stream") or "").rstrip()
                if line:
                    self._log(f"[env] {line}")
        except docker.errors.BuildError as e:
            raise RuntimeError(f"Docker build failed: {e}")


def _output_tail(chunks, cap: int) -> str:
    """A byte-chunk stream, decoded, as at most ``cap`` characters: all of
    it when it fits, else its last bytes behind a marker naming how many
    earlier bytes were dropped (the marker's length comes off the tail).
    Holds at most about twice ``cap`` bytes at once however long the stream
    runs."""
    buf = bytearray()
    dropped = 0
    for chunk in chunks:
        buf += chunk
        if len(buf) > 2 * cap:
            cut = len(buf) - cap
            del buf[:cut]
            dropped += cut
    if not dropped and len(buf) <= cap:
        return buf.decode("utf-8", errors="replace")
    cut = len(buf) - (cap - _EXEC_MARKER_ROOM)
    del buf[:cut]
    dropped += cut
    # Decoding never yields more characters than bytes.
    return (f"[... {dropped} earlier bytes of output dropped]\n"
            + buf.decode("utf-8", errors="replace"))


def _split_ref(ref: str) -> tuple[str, Optional[str]]:
    """``"python:3.12-slim"`` → ``("python", "3.12-slim")`` for ``images.pull``.

    Splits on the LAST colon only when it belongs to the tag — a registry host
    may carry a port (``reg.local:5000/img``), which is not a tag separator.
    A digest reference (``img@sha256:...``) goes whole with no tag: docker-py
    splits the digest itself."""
    if "@" in ref:
        return ref, None
    head, sep, tail = ref.rpartition(":")
    if sep and "/" not in tail:
        return head, tail
    return ref, "latest"


#: The bits a container user who is not the host user needs: traverse and
#: list every directory, read every file.
# Read and search bits only: a read-only ``.git`` of the run stays read-only.
_DIR_BITS = 0o555
_FILE_BITS = 0o444

#: How many directory levels below the workspace root the placement walk
#: enters. ``os.fwalk`` holds one directory fd per level it is inside, and
#: code in an earlier environment could plant a chain deep enough to use up
#: the server's descriptors; anything deeper is left as it is.
_RELAX_MAX_DEPTH = 64


def _relax_perms(root: str) -> None:
    """Make the workspace ``root`` readable by a container user that is not
    the host user (``tempfile.mkdtemp`` creates 0700 dirs it can't traverse).

    Only missing read and search bits are added (directories gain 0o555,
    files 0o444) and none is ever removed, so scripts and binaries keep
    their exec bits and a read-only ``.git`` stays read-only. Code
    in an earlier environment could write this tree, so nothing here follows
    a symlink: ``os.fwalk`` descends through directory fds it has checked
    are the directories it listed, each directory is changed through its own
    fd, and each file is opened ``O_NOFOLLOW`` relative to its directory's
    fd and changed through that fd — an entry swapped for a symlink after
    the listing is refused by the open itself. Symlinks, FIFOs and devices
    are left alone, and so is an entry the server user cannot open (a file
    without its owner's read bit, a directory without owner r/x): changing
    one would take a chmod by path, which follows a symlink swapped in. The
    Private run state under ``.collrun`` is never widened: account documents
    and CLI homes there remain accessible only to their owner. The
    walk enters at most :data:`_RELAX_MAX_DEPTH` levels, so the fds it holds
    stay bounded however deep a planted tree goes."""
    if ".collrun" in Path(root).parts:
        return
    try:
        for dirpath, dirnames, filenames, dir_fd in os.fwalk(root, follow_symlinks=False):
            if ".collrun" in dirnames:
                dirnames.remove(".collrun")
            _add_bits(dir_fd, _DIR_BITS)
            for name in filenames:
                _relax_file(dir_fd, name)
            rel = os.path.relpath(dirpath, root)
            depth = 0 if rel == os.curdir else rel.count(os.sep) + 1
            if dirnames and depth >= _RELAX_MAX_DEPTH:
                _logger.warning(
                    "workspace permissions: not relaxing below %s (deeper than %d levels)",
                    dirpath, _RELAX_MAX_DEPTH,
                )
                dirnames.clear()
    except OSError as e:
        _logger.warning("could not relax workspace permissions: %s", e)


def _add_bits(fd: int, bits: int) -> None:
    """OR ``bits`` into the mode of the open ``fd``; no syscall when every
    bit is already set, and an owner mismatch (a file the container wrote as
    another uid) is left as it is."""
    try:
        mode = stat.S_IMODE(os.fstat(fd).st_mode)
        if mode & bits != bits:
            os.fchmod(fd, mode | bits)
    except OSError:
        pass


def _relax_file(dir_fd: int, name: str) -> None:
    """Add the read bits to the regular file ``name`` in ``dir_fd``."""
    try:
        st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        return
    if not stat.S_ISREG(st.st_mode) or stat.S_IMODE(st.st_mode) & _FILE_BITS == _FILE_BITS:
        return
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY,
                     dir_fd=dir_fd)
    except OSError:
        return
    try:
        # The open refused a symlink; this refuses anything else swapped in
        # since the stat (a FIFO or device the O_NONBLOCK open let through).
        if os.path.samestat(st, os.fstat(fd)):
            _add_bits(fd, _FILE_BITS)
    finally:
        os.close(fd)


def _port_bindings(ports: dict) -> dict:
    """The spec's port map in the shape docker-py takes.

    A generator binds a port to one host interface as ``(address, port)``.
    The spec crosses the IPC as JSON, which turns that pair into a list, and
    docker-py reads a list as several host ports. Give the pair back its
    tuple; every other binding passes through as sent.
    """
    return {
        key: tuple(binding)
        if isinstance(binding, list) and len(binding) == 2 and isinstance(binding[0], str)
        else binding
        for key, binding in ports.items()
    }


def _published_ports(container) -> set[int]:
    """The host ports docker published for the container's ``ports``."""
    container.reload()
    ports = (container.attrs.get("NetworkSettings") or {}).get("Ports") or {}
    return {
        int(binding["HostPort"])
        for bindings in ports.values()
        for binding in (bindings or [])
        if binding.get("HostPort")
    }


def _container_ip(container) -> str:
    """The container's bridge IP (reachable from the host on Linux)."""
    container.reload()
    ns = container.attrs.get("NetworkSettings", {}) or {}
    if ns.get("IPAddress"):
        return ns["IPAddress"]
    for net in (ns.get("Networks") or {}).values():
        if net.get("IPAddress"):
            return net["IPAddress"]
    raise RuntimeError("container has no reachable IP address")


def _proc_listen_address(tables: str, port: int) -> str | None:
    """The local address of a LISTEN socket on ``port`` in ``tables`` (the
    text of ``/proc/net/tcp`` and ``/proc/net/tcp6``), loopback before a
    wildcard before any other; None when none listens. The kernel prints
    each address as native-endian 32-bit words of its network-order bytes,
    and a container shares the host's kernel, so this host's byte order
    reads them."""
    found: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for line in tables.splitlines():
        fields = line.split()
        if len(fields) < 4 or fields[3] != "0A" or ":" not in fields[1]:
            continue
        hex_address, _, hex_port = fields[1].partition(":")
        try:
            if int(hex_port, 16) != port or len(hex_address) not in (8, 32):
                continue
            packed = b"".join(int(hex_address[i:i + 8], 16).to_bytes(4, sys.byteorder)
                              for i in range(0, len(hex_address), 8))
        except ValueError:
            continue
        address = ipaddress.ip_address(packed)
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        found.append(address)
    if not found:
        return None

    def rank(address) -> tuple[int, int]:
        kind = 0 if address.is_loopback else 1 if address.is_unspecified else 2
        return (kind, address.version)

    return str(min(found, key=rank))


def _relay_target(address: str) -> str:
    """The address a relay in the container connects to for a listener on
    ``address``: a wildcard's loopback of the same family, else itself."""
    parsed = ipaddress.ip_address(address)
    if parsed.is_unspecified:
        return "127.0.0.1" if parsed.version == 4 else "::1"
    return address


def _relay_argv(program: str, address: str, port: int) -> list[str]:
    """The argv that relays stdio to ``address:port`` with ``program``."""
    if program == "python3":
        # -I: the exec's directory and the environment's PYTHON* never
        # reach the relay's imports.
        return ["python3", "-I", "-c", _PY_RELAY, address, str(port)]
    if program == "node":
        return ["node", "-e", _NODE_RELAY, address, str(port)]
    if program == "socat":
        family = "TCP4" if ipaddress.ip_address(address).version == 4 else "TCP6"
        host = address if family == "TCP4" else f"[{address}]"
        return ["socat", "STDIO", f"{family}:{host}:{port}"]
    raise RuntimeError(f"unknown relay program {program!r}")


def _listen_loopback(placed: PlacedEnvironment, serve: Callable) -> int:
    """Start a forwarder on a kernel-assigned loopback port; each accepted
    client is handed to ``serve(client, forwarder)`` on a thread of its
    own. Return the port. Binding port 0 here (not probing a number first)
    means two concurrent placements can never be handed the same port."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(64)
    srv.setblocking(False)
    forwarder = _Forwarder(srv)
    placed.forwarders.append(forwarder)

    def _accept():
        # A selector (epoll, kqueue), never select(): select() takes no
        # descriptor numbered 1024 or above, and the desktop shell starts
        # the server with every slot below its own highest held at
        # /dev/null (serverStdio), so these sockets sit above that. There
        # select() raises, and this loop ending with the port still bound
        # would leave a client's connect to complete and wait forever.
        with selectors.DefaultSelector() as ready:
            ready.register(srv, selectors.EVENT_READ)
            while not forwarder.stop.is_set():
                try:
                    if not ready.select(0.5):
                        continue
                    client, _ = srv.accept()
                except BlockingIOError:  # the connection reset before the accept
                    continue
                except (OSError, ValueError):  # closed meanwhile
                    break
                client.setblocking(True)
                # A relay writes what arrives as it arrives: without this a
                # response's second small write waits out the client's
                # delayed ACK (~40 ms).
                client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                threading.Thread(target=serve, args=(client, forwarder), daemon=True).start()

    threading.Thread(target=_accept, daemon=True).start()
    return srv.getsockname()[1]


def _close_all(*socks) -> None:
    for sock in socks:
        try:
            sock.close()
        except OSError:
            pass


def _start_forwarder(
    placed: PlacedEnvironment, target_ip: str, target_port: int
) -> int:
    """Forward a kernel-assigned loopback port to ``target_ip:target_port``;
    return the port."""

    def _serve(client, forwarder):
        # A failed upstream connect (service not up yet) must not kill
        # the listener or leak the accepted client. It runs on its own
        # thread, with a timeout, so a target that stops answering cannot
        # stall the accept loop for the kernel's SYN-retry window.
        try:
            up = socket.create_connection((target_ip, target_port), timeout=5)
            up.settimeout(None)
        except OSError:
            _close_all(client)
            return
        if not forwarder.track(client, up):
            _close_all(client, up)
            return
        try:
            with selectors.DefaultSelector() as ready:  # not select(): see _listen_loopback
                ready.register(client, selectors.EVENT_READ, data=(client, up))
                ready.register(up, selectors.EVENT_READ, data=(up, client))
                while not forwarder.stop.is_set():
                    for key, _ in ready.select(0.5):
                        s, peer = key.data
                        data = s.recv(65536)
                        if not data:
                            return
                        peer.sendall(data)
        except (OSError, ValueError):
            pass
        finally:
            forwarder.untrack(client, up)
            _close_all(client, up)

    return _listen_loopback(placed, _serve)


def _start_relay(placed: PlacedEnvironment, argv: list[str], port: int) -> int:
    """Relay a kernel-assigned loopback port into the container: each
    client gets its own ``docker exec`` of ``argv`` (a stdio-to-TCP relay
    to the container's own listener), as the image's default user. The
    exec's stdin is the client's bytes, raw, its EOF the client's
    half-close (a write shutdown of the attach socket); its stdout comes
    back in docker's 8-byte-header frames (no TTY), which are unpacked to
    the client; the client is closed when the exec's output ends. A failed
    exec (the server gone, the container stopping) closes that client and
    leaves the listener. Return the port."""
    container = placed.container
    api = container.client.api

    def _serve(client, forwarder):
        try:
            exec_id = api.exec_create(container.id, argv, stdin=True, tty=False, workdir="/")["Id"]
            attached = api.exec_start(exec_id, socket=True)
        except Exception as e:  # noqa: BLE001 — the container stopping, or docker refusing
            _logger.info("relay to container port %s: the exec failed: %s", port, e)
            _close_all(client)
            return
        # docker-py hands the hijacked connection over as a SocketIO on a
        # unix socket; the socket itself is what the selector, recv and
        # shutdown take.
        up = getattr(attached, "_sock", attached)
        up.settimeout(None)

        def _release():
            # The HTTP response docker-py keeps on the SocketIO goes first:
            # closed after its stream, it raises when collected.
            response = getattr(attached, "_response", None)
            try:
                if response is not None:
                    response.close()
            except Exception:  # noqa: BLE001 — already closed
                pass
            _close_all(client, attached, up)

        if not forwarder.track(client, up):
            _release()
            return
        frames = bytearray()
        stderr = bytearray()
        try:
            with selectors.DefaultSelector() as ready:  # not select(): see _listen_loopback
                ready.register(client, selectors.EVENT_READ)
                ready.register(up, selectors.EVENT_READ)
                while not forwarder.stop.is_set():
                    for key, _ in ready.select(0.5):
                        if key.fileobj is client:
                            data = client.recv(65536)
                            if data:
                                up.sendall(data)
                            else:  # the client's half-close: the exec's stdin ends
                                ready.unregister(client)
                                up.shutdown(socket.SHUT_WR)
                            continue
                        data = up.recv(65536)
                        if not data:
                            return
                        frames += data
                        while len(frames) >= 8:
                            size = int.from_bytes(frames[4:8], "big")
                            if len(frames) < 8 + size:
                                break
                            stream, payload = frames[0], bytes(frames[8:8 + size])
                            del frames[:8 + size]
                            if stream == 1:
                                client.sendall(payload)
                            elif stream == 2:
                                stderr = (stderr + payload)[-_RELAY_STDERR_TAIL:]
        except (OSError, ValueError):
            pass
        finally:
            forwarder.untrack(client, up)
            _release()
            if stderr.strip():
                _logger.info("relay to container port %s: %s", port,
                             stderr.decode("utf-8", errors="replace").strip())

    return _listen_loopback(placed, _serve)
