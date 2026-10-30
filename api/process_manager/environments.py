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

The placed environments are also the host's record of which containers
and loopback ports belong to the run: a snapshot and a terminal session act
only on its containers (``owned_container``), and a ``port_bound`` claim is
checked against its ports (``serves_port``).

All methods are synchronous (docker-py blocks); the IPC handlers call
them via ``asyncio.to_thread``. The docker client is injected for
tests; ``None`` (default) lazily builds the real one.
"""
from __future__ import annotations

import logging
import os
import socket
import stat
import tempfile
import threading
import uuid
from pathlib import Path, PurePosixPath
from typing import Callable, Optional

from instance import INSTANCE_LABEL, instance_id
from process_manager.docker_runner import _DEFAULT_PIDS_LIMIT

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


class PlacedEnvironment:
    """One placed container, its host-side forwarders, and the loopback
    host ports the host serves for it (published at placement, forwarded
    by ``expose_port``)."""

    def __init__(self, env_id: str, container, readonly_files=None) -> None:
        self.env_id = env_id
        self.container = container
        self.forwarders: list[tuple[socket.socket, threading.Event]] = []
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

    def _placement_paths(self, spec: dict) -> tuple[str, str | None, str | None, dict, dict]:
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
        roots = [Path(self._manager.workspace_dir).resolve()]
        app_root = getattr(self._manager, "app_root_dir", None)
        if app_root:
            roots.append(Path(app_root).resolve())

        def directory(value, field):
            if not isinstance(value, str) or not value:
                raise RuntimeError(f"env_place: {field} must name a directory inside this run")
            try:
                path = Path(value).resolve(strict=True)
            except (OSError, ValueError, RuntimeError):
                raise RuntimeError(f"env_place: {field} is not an accessible run directory") from None
            if not path.is_dir() or not any(path.is_relative_to(root) for root in roots):
                raise RuntimeError(f"env_place: {field} is outside the run's workspace and app directory")
            return str(path)

        workspace = directory(spec.get("workspace") or self._manager.workspace_dir, "workspace")
        contexts = [directory(spec[key], key) if spec.get(key) else None
                    for key in ("build_context", "base_context")]
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
        return workspace, contexts[0], contexts[1], validated, readonly

    def place(self, spec: dict) -> dict:
        """Create the container a generator asked for; return its ids."""
        image = str(spec.get("image") or "")
        if not image:
            raise RuntimeError("env_place: 'image' is required")
        workspace, build_context, base_context, extra_volumes, readonly = self._placement_paths(spec)
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
        self._ensure_image(
            client,
            image,
            build_context,
            spec.get("base_image") or None,
            base_context,
            build_args=dict(spec.get("build_args") or {}),
            refresh=bool(refresh),
        )

        mount = str(spec.get("mount") or "")
        if mount:
            _relax_perms(workspace)

        vols: dict = {}
        if mount:
            vols[workspace] = {"bind": mount, "mode": str(spec.get("mount_mode") or "rw")}
        vols.update(extra_volumes)

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
                # container. See api/instance.py.
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
        return {"env_id": env_id, "container_id": container.id}

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

    def expose_port(self, env_id: str, port: int) -> int:
        """Forward a loopback host port to the container's ``port``;
        return the host port. The generator registers it with serve_port
        exactly as before — the proxy's port contract is unchanged."""
        placed = self._env(env_id)
        container_ip = _container_ip(placed.container)
        host_port = _start_forwarder(placed, container_ip, int(port))
        placed.host_ports.add(host_port)
        return host_port

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
        placed.closed = True
        self._manager.bound_ports.difference_update(placed.host_ports)
        for srv, stop in placed.forwarders:
            stop.set()
            try:
                srv.close()
            except OSError:
                pass
        placed.forwarders.clear()
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
        (the vscode image FROMs code-server, the runner FROMs python)."""
        import docker.errors
        try:
            client.images.get(image)
            return
        except docker.errors.ImageNotFound:
            pass
        if base_image:
            # The base's own FROM is upstream, so this is where a refresh pulls.
            self._get_or_build(client, base_image, base_context, refresh=refresh)
        self._get_or_build(
            client, image, build_context, build_args=build_args,
            refresh=refresh and not base_image,
        )

    def _get_or_build(
        self, client, tag: str, build_context: Optional[str], *,
        build_args: Optional[dict] = None, refresh: bool = True,
    ) -> None:
        """Ensure ``tag`` is present locally, building or pulling it if not.

        Tag presence is the cache: image tags are content-addressed by their
        build context (``_env_shared.derive_image_tag``), so "present" means
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
_DIR_BITS = 0o755
_FILE_BITS = 0o644

#: How many directory levels below the workspace root the placement walk
#: enters. ``os.fwalk`` holds one directory fd per level it is inside, and
#: code in an earlier environment could plant a chain deep enough to use up
#: the server's descriptors; anything deeper is left as it is.
_RELAX_MAX_DEPTH = 64


def _relax_perms(root: str) -> None:
    """Make the workspace ``root`` readable by a container user that is not
    the host user (``tempfile.mkdtemp`` creates 0700 dirs it can't traverse).

    Only missing bits are added (directories gain 0o755, files 0o644) and
    none is ever removed, so scripts and binaries keep their exec bits. Code
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


def _start_forwarder(
    placed: PlacedEnvironment, target_ip: str, target_port: int
) -> int:
    """Forward a kernel-assigned loopback port to ``target_ip:target_port``;
    return the port. Binding port 0 here (not probing a number first) means
    two concurrent placements can never be handed the same port."""
    import select

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(64)
    srv.setblocking(False)
    stop = threading.Event()
    placed.forwarders.append((srv, stop))

    def _pump(a, b):
        try:
            while not stop.is_set():
                r, _, _ = select.select([a, b], [], [], 0.5)
                for s in r:
                    data = s.recv(65536)
                    if not data:
                        return
                    (b if s is a else a).sendall(data)
        except OSError:
            pass
        finally:
            for s in (a, b):
                try:
                    s.close()
                except OSError:
                    pass

    def _serve(client):
        # A failed upstream connect (service not up yet) must not kill
        # the listener or leak the accepted client. It runs on its own
        # thread, with a timeout, so a target that stops answering cannot
        # stall the accept loop for the kernel's SYN-retry window.
        try:
            up = socket.create_connection((target_ip, target_port), timeout=5)
            up.settimeout(None)
        except OSError:
            try:
                client.close()
            except OSError:
                pass
            return
        _pump(client, up)

    def _accept():
        while not stop.is_set():
            try:
                r, _, _ = select.select([srv], [], [], 0.5)
                if not r:
                    continue
                client, _ = srv.accept()
            except OSError:
                break
            threading.Thread(target=_serve, args=(client,), daemon=True).start()

    threading.Thread(target=_accept, daemon=True).start()
    return srv.getsockname()[1]
