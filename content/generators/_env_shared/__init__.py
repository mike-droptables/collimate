# SPDX-License-Identifier: MIT
"""Container helpers for a run working in its own workspace.

``_env_shared`` also holds the run's Git account (``git_account``), the
hardened git process layer (``git_run``) and the repository engine
(``repositories``). Callers import them by name (``from _env_shared import
repositories``); this package never re-exports them."""
from __future__ import annotations

import asyncio
import functools
import hashlib
import os
import shlex
import threading
import time
from typing import Optional

__all__ = ["DockerEnvironment", "context_digest", "derive_image_tag", "BASE_ARG",
           "BASE_NAME", "base_context", "layered_image_spec"]

# The build arg every layered Dockerfile takes for its base: ``ARG BASE=<tag>``
# / ``FROM ${BASE}``. The builder injects the RESOLVED (content-addressed) base
# tag, so a change to the shared base propagates into every image layered on
# it. The Dockerfile's own default keeps a hand-run ``docker build`` working.
BASE_ARG = "BASE"
BASE_NAME = "linux-small"
_base_contexts: dict[str, str] = {}
_base_context_lock = threading.Lock()
# The installed wheel's package data under ``collimate/``: the shared docs and
# renderer runtime, the renderer compilers and the bundler tools. A container
# imports the SDK's Python modules and its native verifier only, so staging
# leaves these out and stages the tree a source checkout's ``src/collimate``
# holds.
_SDK_PACKAGE_DATA = frozenset({"renderer_compilers", "renderer_tools", "shared"})


def base_context() -> str | None:
    """Stage the shared base and current SDK into this run's private workspace.

    The resulting content hash includes the actual SDK source, so a command
    image cannot silently test a draft against an older SDK release. Only the
    SDK's Python packages are staged, never its package data. No account
    material is copied. One staging per workspace avoids re-copying every time
    another harness leg resolves the same base image.
    """
    from pathlib import Path
    from importlib.resources import files
    import gen
    from collimate import fs
    from collimate.shared import sdk_distribution

    base = Path(__file__).resolve().parent / "base"
    if not (base / "Dockerfile").is_file():
        return None
    workspace = gen.workspace_path()
    with _base_context_lock:
        if workspace in _base_contexts:
            return _base_contexts[workspace]
        relative = ".collrun/container-base"
        def stage_tree(source, target, skip=frozenset()):
            for parent, directories, files in os.walk(source):
                top = Path(parent) == source
                directories[:] = sorted(d for d in directories
                                        if d not in ("__pycache__", "llm") and not (top and d in skip))
                for name in sorted(files):
                    if name.endswith((".pyc", ".pyo")):
                        continue
                    path = Path(parent) / name
                    if path.is_symlink():
                        raise ValueError(f"SDK build context contains a symlink: {path.name}")
                    rel = path.relative_to(source).as_posix()
                    fs.write_file(workspace, f"{relative}/{target}{rel}", path.read_bytes())
        stage_tree(base, "")
        sdk, facade = files('collimate'), files('gen')
        if not isinstance(sdk, Path) or not isinstance(facade, Path):
            raise RuntimeError('SDK image staging requires filesystem-backed collimate and gen packages')
        if not sdk.is_dir() or not facade.is_dir():
            raise RuntimeError('SDK image staging package directories are missing')
        stage_tree(sdk.resolve(), "sdk/collimate/", _SDK_PACKAGE_DATA)
        stage_tree(facade.resolve(), "sdk/gen/")
        distribution = sdk_distribution()
        requirements = [value for value in distribution.requires or [] if "extra ==" not in value]
        fs.write_file(workspace, f"{relative}/sdk-requirements.txt", "\n".join(requirements) + "\n")
        # Source imports and package metadata travel together for draft tools
        # that introspect their SDK version/dependencies inside uv's fresh env.
        metadata = distribution.read_text("METADATA")
        if metadata:
            fs.write_file(workspace, f"{relative}/sdk/collimate_sdk-{distribution.version}.dist-info/METADATA", metadata)
        context = os.path.join(workspace, relative)
        _base_contexts[workspace] = context
        return context


def layered_image_spec(name: str, context: str, *, build_args: dict | None = None) -> dict:
    """Image kwargs for a build layered on the content-addressed shared base.

    Callers resolve their own artifact versions. Both those args and the base
    content participate in the derived image tag; the host builds the base
    first and injects its exact tag through BASE.
    """
    base = base_context()
    if not base:
        raise RuntimeError("the shared base build context (_env_shared/base) is missing")
    base_tag = derive_image_tag(BASE_NAME, base)
    args = {**(build_args or {}), BASE_ARG: base_tag}
    return {
        "image": derive_image_tag(name, context, base_digest=context_digest(base), build_args=args),
        "build_context": context,
        "base_image": base_tag,
        "base_context": base,
        "build_args": args,
    }



def context_digest(path: str) -> str:
    """A stable content hash of a build context (sorted, framed relpath + size + streamed bytes).
    Python bytecode caches (``__pycache__`` / ``*.pyc`` / ``*.pyo``) are
    excluded: a test importing a context's script must not re-tag the image.

    This is an image's IDENTITY. Deriving the tag from it is what makes a
    Dockerfile edit propagate: a changed context is a different tag, and a
    tag the host has never seen is built. With a fixed tag like
    ``collimate-goose:latest`` the opposite is true — the host's
    build-if-missing check sees the tag present and never reads the Dockerfile
    again, so a corrected harness pin can sit unused forever."""
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for fname in sorted(filenames):
            if fname.endswith((".pyc", ".pyo")):
                continue
            abs_path = os.path.join(dirpath, fname)
            rel = os.path.relpath(abs_path, path).replace("\\", "/")
            try:
                with open(abs_path, "rb") as fh:
                    h.update(rel.encode("utf-8") + b"\0")
                    h.update(str(os.fstat(fh.fileno()).st_size).encode("ascii") + b"\0")
                    while chunk := fh.read(1024 * 1024):
                        h.update(chunk)
            except OSError:
                continue
    return h.hexdigest()


def derive_image_tag(
    name: str, context: str, *, base_digest: str = "", build_args: "dict | None" = None,
) -> str:
    """``collimate-<name>:<12 hex>`` — the content-addressed tag for a build.

    The identity is every input that changes what the image CONTAINS:

      * the build context's bytes — so a Dockerfile edit re-tags;
      * ``base_digest`` — so rebuilding the shared base re-tags everything
        layered on it;
      * ``build_args`` — so a runner asking for a different artifact version
        re-tags. This one is load-bearing for the runner-declared version
        design: the Dockerfile's install line reads ``@${TOOL_VERSION}``, so
        the version lives in the args, not in the context bytes. Omit it and
        two different requested versions would collide on one tag, which is
        exactly the stale-image bug in a new place."""
    digest = context_digest(context)
    extra = base_digest
    for key in sorted(build_args or {}):
        extra += f"\0{key}={build_args[key]}"
    if extra:
        digest = hashlib.sha256((digest + extra).encode()).hexdigest()
    return f"collimate-{name}:{digest[:12]}"


@functools.lru_cache(maxsize=1)
def _docker_client():
    """One docker-py client per process (each ``from_env()`` opens its own
    connection pool and probes the API version). A failed connect raises and
    is not cached, so an unreachable daemon is retried on the next access."""
    import docker  # generator-side dependency (PEP 723), never the SDK's

    return docker.from_env()


def _quote(command) -> str:
    if isinstance(command, (list, tuple)):
        return " ".join(shlex.quote(str(c)) for c in command)
    return str(command)


class DockerEnvironment:
    """A container that backs a Docker generator.

    Owns the full container lifecycle *by proxy*: the host places (and on
    teardown reaps) the container; this object drives it. The cell workspace
    is mounted (read-write by default) at ``mount`` so the run and its
    container work on the same files."""

    def __init__(
        self,
        *,
        image: str,
        mount: str = "/work",
        mount_mode: str = "rw",
        workspace: Optional[str] = None,
        build_context: Optional[str] = None,
        base_image: Optional[str] = None,
        base_context: Optional[str] = None,
        environment: Optional[dict] = None,
        ports: Optional[dict] = None,
        volumes: Optional[dict] = None,
        readonly_files: Optional[dict[str, str]] = None,
        command="sleep infinity",
        working_dir: Optional[str] = None,
        extra_hosts: Optional[dict] = None,
        name_prefix: str = "collimate-env",
        kind: str = "environment",
        build_args: Optional[dict] = None,
        refresh: Optional[bool] = None,
    ) -> None:
        import gen

        self.image = image
        self.mount = mount or ""
        self.workspace = os.path.abspath(workspace or gen.workspace_path())
        self._procs: dict[str, dict] = {}
        self._proc_seq = 0
        # The process groups a stop has sent SIGTERM, each with the end of
        # its stop grace (monotonic), and the lock a stop holds from taking
        # its processes off _procs to recording the groups it reached.
        self._stopping: dict[str, float] = {}
        self._stop_lock = threading.Lock()
        self._closed = False
        self._close_status: dict | None = None
        self._close_lock = threading.Lock()

        gen.phase("spinning_up", "Placing environment…")
        spec = {
            "image": image,
            "mount": self.mount,
            "mount_mode": mount_mode,
            "workspace": self.workspace,
            "build_context": (
                str(os.path.abspath(build_context)) if build_context else None
            ),
            "base_image": base_image,
            "base_context": (
                str(os.path.abspath(base_context)) if base_context else None
            ),
            "environment": dict(environment or {}),
            "ports": dict(ports or {}),
            "volumes": dict(volumes or {}),
            "readonly_files": dict(readonly_files or {}),
            # None is meaningful: run the image's own ENTRYPOINT/CMD (used by
            # single-image generators whose entrypoint launches the real
            # process). The host honours the explicit null.
            "command": command,
            "working_dir": working_dir,
            "extra_hosts": dict(extra_hosts or {}),
            "name_prefix": name_prefix,
            "kind": kind,
            # Build args for the image build (``ARG BASE`` / ``FROM ${BASE}``
            # is the one every layered Dockerfile takes).
            "build_args": dict(build_args or {}),
            # Whether this build may ask UPSTREAM for newer content: pull the
            # ``FROM`` base and pull a registry-only image. ``None`` defers to
            # the run's ``update_packages`` grant (the normal case); an
            # explicit bool overrides it for a generator that knows better.
            # Only ever consulted when a build actually happens — a tag the
            # host already has is a cache hit either way, so freshness costs
            # nothing on an unchanged context.
            "refresh": refresh,
        }
        self._env_id, self._container_id = gen.place_environment(spec)
        gen.phase("spinning_up", "Environment ready")

    @property
    def container(self):
        """The underlying docker-py ``Container`` for attached stdio streams
        and fresh lifecycle inspection — the route here that bypasses the host.
        Completed commands and file reads use the host-mediated methods.

        Only meaningful where this process can reach the daemon the host
        placed the container on (true for every local placement under a
        ``docker: full`` grant); attaches lazily by container id."""
        if not self._container_id:
            return None
        # The client is shared; the inspect is fresh on every access so a
        # caller reading ``.status`` sees the container's current state.
        return _docker_client().containers.get(self._container_id)

    @property
    def container_id(self) -> str:
        return self._container_id if not self._closed else ""

    # ── execution plane ─────────────────────────────────────────────

    def exec(self, command, *, cwd: Optional[str] = None,
             env: Optional[dict] = None, timeout: Optional[float] = None,
             user: Optional[str] = None, detach: bool = False) -> tuple:
        """Run ``command`` to completion; return ``(rc, output)``.

        When ``timeout`` is set the command is bounded with coreutils
        ``timeout`` (rc 124 on expiry) so a runaway command can't block the
        exec indefinitely. ``user`` (``"uid:gid"``) runs the exec as that
        user instead of the container default (root) — files it writes into
        the mounted workspace then stay owned by that user on the host.

        The host caps the output it carries back and keeps the tail of a
        longer one behind a marker, so a file whose content is wanted whole
        is read with :meth:`read_file`, never through ``cat``. ``detach`` hands
        process lifetime to Docker and returns after launch; the caller owns
        completion and cleanup instead of expecting output from this call."""
        import gen

        # A string command is shell-interpreted (pipes/redirects), like
        # exec_background — a list is argv as given. The shaping stays HERE
        # (not host-side) so quoting semantics are exactly what they were.
        cmd = list(command) if isinstance(command, (list, tuple)) else ["sh", "-c", str(command)]
        if timeout is not None:
            cmd = ["timeout", str(max(1, int(timeout))), *[str(c) for c in cmd]]
        return gen.exec_environment(
            self._env_id, cmd, cwd=cwd or self.mount, env=env, timeout=timeout,
            user=user, detach=detach,
        )

    # The bytes of a file one :meth:`read_file` exec carries. Their base64,
    # line breaks included, and the line before it come to about 5.4
    # million characters: well inside what one exec's output may be before
    # the host keeps only its tail.
    _READ_CHUNK = 4_000_000

    # Reads bytes $2.. of file $1 (1-based), at most $3 of them: a line
    # "@<offset> <size> <read>" (the file's size on the first chunk, "-"
    # after it: busybox's wc reads the whole file to count it; <read> is
    # $4, the token of the read asking), then the bytes as base64; exit 3
    # when there is no such file, 4 when it is not a readable one.
    _READ_SCRIPT = ('[ -e "$1" ] || exit 3; [ -f "$1" ] && [ -r "$1" ] || exit 4; '
                    'if [ "$2" = 1 ]; then s=$(wc -c < "$1") || exit 4; else s=-; fi; '
                    'echo "@$2 $s $4"; tail -c +"$2" -- "$1" | head -c "$3" | base64')

    def read_file(self, path: str, *, user: Optional[str] = None) -> bytes:
        """The whole content of the file at ``path`` in the container.

        The host keeps only the tail of an exec's output past its cap, so a
        whole document read through :meth:`exec` (``cat``) loses its head
        once it grows past it. This reads the file in chunks instead, one
        exec each, and joins them here: the file arrives whole, or the read
        raises — :class:`FileNotFoundError` when there is no such file,
        :class:`OSError` when it is not a regular file this user can read,
        a chunk did not arrive whole or is not this read's answer for its
        offset (a late answer to an earlier exec, this read's or another
        one's, taken for this one), or fewer bytes arrived than the file
        held when the read began; :class:`RuntimeError` when the host could
        not run the read. A file that grows while it is read (a live
        record) arrives with what it held at its last chunk. ``user`` as
        for :meth:`exec`. Blocking."""
        import base64
        import errno
        import secrets

        n = self._READ_CHUNK
        # Every chunk's answer names this read: acks correlate by op name
        # only, so an earlier exec's late answer arrives as a later one's.
        token = secrets.token_hex(8)
        data = bytearray()
        size = 0
        while True:
            offset = len(data) + 1
            rc, out = self.exec(
                ["sh", "-c", self._READ_SCRIPT, "sh", path, str(offset), str(n), token],
                user=user,
            )
            if rc == -1:
                raise RuntimeError(out or f"reading {path} failed")
            if rc == 3:
                raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), path)
            if rc != 0:
                raise OSError(f"cannot read {path} (exit {rc}): {(out or '').strip()[-300:]}")
            head, _, body = (out or "").partition("\n")
            try:
                at, count, read = head.split()
                if (at, read) != (f"@{offset}", token):
                    raise ValueError(f"it answered {head[:80]!r}, not @{offset} of this read")
                if offset == 1:
                    size = int(count)
                # binascii.Error is a ValueError.
                chunk = base64.b64decode("".join(body.split()), validate=True)
            except ValueError as exc:
                raise OSError(f"reading {path}: a chunk did not arrive whole ({exc})") from exc
            if len(chunk) > n:
                raise OSError(f"reading {path}: a chunk came back longer than asked")
            data += chunk
            if len(chunk) < n:
                if len(data) < size:
                    raise OSError(f"reading {path}: {len(data)} of its {size} bytes arrived")
                return bytes(data)

    def exec_background(self, command, *, cwd: Optional[str] = None,
                        env: Optional[dict] = None) -> str:
        """Start a long-running process; return a process id (for
        :meth:`wait_process` / :meth:`stop_process`)."""
        import gen

        self._proc_seq += 1
        token = f"colproc-{self._proc_seq}"
        pidfile, rcfile = f"/tmp/{token}.pid", f"/tmp/{token}.rc"
        # Run the command in a new session (so stop can signal the whole group)
        # and record its session-leader pid + exit code. The inner script is
        # shlex.quote'd as a single arg so the user command's own quoting can't
        # collide with the wrapper's. The command runs in a subshell on its
        # own line, so a trailing comment, ``exit`` or ``exec`` in it can't
        # swallow the rc capture.
        inner = f"( {_quote(command)}\n); echo $? > {rcfile}"
        launcher = (
            f"setsid sh -c {shlex.quote(inner)} </dev/null >/dev/null 2>&1 & "
            f"echo $! > {pidfile}"
        )
        rc, out = gen.exec_environment(
            self._env_id, ["sh", "-c", launcher],
            cwd=cwd or self.mount, env=env, detach=True,
        )
        if rc == -1:
            raise RuntimeError(out or "exec_background failed")
        self._procs[token] = {"pidfile": pidfile, "rcfile": rcfile,
                              "started": time.monotonic()}
        return token

    def wait_process(self, process_id: str, *, timeout: Optional[float] = None) -> int:
        """Block until the background process exits; return its return code.

        Returns -1 when the process was stopped, vanished without an exit
        code, the environment is unavailable, or ``timeout`` expired."""
        info = self._procs.get(process_id)
        if not info:
            return -1
        rcfile, pidfile = info["rcfile"], info["pidfile"]
        # The rc file first; else whether the session leader is still alive.
        # A zombie counts as gone: the orphaned wrapper is reparented to the
        # container's PID 1, which (``sleep infinity`` by default, no init)
        # may never reap it, and ``kill -0`` succeeds on a zombie. The second
        # cat closes the race where it exits between the checks.
        probe = (
            f"cat {rcfile} 2>/dev/null && exit 0; "
            f"pid=$(cat {pidfile} 2>/dev/null); "
            'if [ -z "$pid" ]; then echo NOPID; '
            'elif kill -0 "$pid" 2>/dev/null && '
            "! grep -qs '^State:[[:space:]]*Z' /proc/$pid/status; then echo RUNNING; "
            f"else cat {rcfile} 2>/dev/null || echo GONE; fi"
        )
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if process_id not in self._procs or self._closed:
                return -1  # stop_process / close ran: no rc file is coming
            rc, rc_out = self.exec(["sh", "-c", probe])
            out = rc_out.strip()
            if out.isdigit():
                return int(out)
            if rc == -1 or out == "GONE":
                return -1
            # NOPID early on is the detached launcher not having written the
            # pidfile yet; past the grace it never will.
            if out == "NOPID" and time.monotonic() - info.get("started", 0) > 10:
                return -1
            if deadline is not None and time.monotonic() >= deadline:
                return -1
            time.sleep(0.5)

    # How long a background process has to exit after its stop's SIGTERM
    # before a teardown freezes it (:meth:`freeze`): the grace the host's
    # close gives the container between SIGTERM and SIGKILL.
    _STOP_GRACE_S = 2.0

    # Of the process groups its arguments name, each one that still has a
    # thread that has not exited, once per such thread. Threads, not
    # processes: a process whose main thread has exited reads as a zombie
    # while its other threads run. The state and the group are the 1st and
    # 3rd fields after the ") " that closes the command name, which may
    # itself hold ") " or a line break. A line break in the name leaves the
    # stat file's first line short of the 50 fields that follow the name
    # (fewer than 20 is taken for that), and then the whole file is read.
    _LIVE_GROUPS_SCRIPT = (
        'g=" $* "; for d in /proc/[0-9]*/task/[0-9]*; do read -r s < "$d/stat" || continue; '
        's=${s##*") "}; set -- $s; '
        '[ $# -ge 20 ] || { s=$(tr "\\n" " " < "$d/stat") || continue; s=${s##*") "}; set -- $s; }; '
        'case $1 in Z|X) continue;; esac; '
        'case $g in *" $3 "*) echo "$3";; esac; done 2>/dev/null; exit 0')

    def stop_process(self, process_id: str, *, wait: bool = False) -> None:
        """Terminate the background process: ``SIGTERM`` to its whole group.

        The group then has :attr:`_STOP_GRACE_S` to exit before a teardown
        freezes it. Without ``wait`` (a parent's stop, routed here) this
        returns once the signal is sent; a teardown that comes within that
        grace (:meth:`serve`'s) waits for the group to exit, for at most
        the rest of it. With ``wait``, return once no process of the group
        is left running, or once the grace has passed with one still
        running (logged): what a teardown does before it freezes the
        container (:meth:`freeze`), so a ``SIGTERM`` handler that rewrites
        a workspace file finishes the rewrite rather than being stopped
        halfway through it. Blocking."""
        groups = self._terminate([process_id])
        if wait:
            self._await_exit(groups)

    def _terminate(self, process_ids) -> "list[str]":
        """Send ``SIGTERM`` to the group of each background process in
        ``process_ids``, in one exec, and forget them; return the ids of the
        groups it reached. Each of those is recorded with the end of its
        stop grace, until which a teardown waits for it (:meth:`_await_exit`)."""
        with self._stop_lock:
            infos = [info for info in (self._procs.pop(p, None) for p in process_ids) if info]
            if not infos:
                return []
            signal, files = [], []
            for info in infos:
                pidfile = shlex.quote(info["pidfile"])
                files += [pidfile, shlex.quote(info["rcfile"])]
                # Nothing but a group leader's pid is signalled: a pidfile the
                # launcher has not written yet holds nothing, and `kill -- -1`
                # would reach every process in the container. Each group the
                # SIGTERM reached is reported as "sigterm:<pid>".
                signal.append(f'pid=$(cat {pidfile} 2>/dev/null) && [ "$pid" -gt 1 ] 2>/dev/null '
                              '&& kill -- -"$pid" 2>/dev/null && echo "sigterm:$pid"')
            _, out = self.exec(["sh", "-c", "; ".join(signal) + f"; rm -f {' '.join(files)}"])
            # The groups reported, whatever the exit status (the rm after the
            # signals may fail); the error of an exec the host could not run
            # reports none.
            marked = (word.split(":", 1) for word in (out or "").split() if ":" in word)
            groups = [pid for tag, pid in marked if tag == "sigterm" and pid.isdigit()]
            now = time.monotonic()
            for group in [g for g, end in self._stopping.items() if end <= now]:
                del self._stopping[group]              # its grace is over
            self._stopping.update(dict.fromkeys(groups, now + self._STOP_GRACE_S))
            return groups

    def _await_exit(self, groups: "list[str] | None" = None) -> None:
        """Poll until no process of the process groups ``groups`` (by
        default, every group a stop reached) is left running, each for at
        most the rest of its stop grace; log a group still running then.
        A group whose grace is over is not polled. When the poll cannot be
        run, log that and wait out the rest of the grace instead: a handler
        may still be rewriting a file. Never raises."""
        import gen

        with self._stop_lock:      # after a stop that is still sending its SIGTERM
            now = time.monotonic()
            ends = {g: end for g, end in self._stopping.items()
                    if end > now and (groups is None or g in groups)}
        while ends:
            try:
                rc, out = self.exec(["sh", "-c", self._LIVE_GROUPS_SCRIPT, "sh", *ends])
            except Exception as exc:  # noqa: BLE001 — a poll the host could not run
                rc, out = -1, str(exc)
            if rc != 0:
                gen.log(f"could not check whether background process group "
                        f"{', '.join(ends)} exited (exit {rc}): "
                        f"{(out or '').strip()[-300:]}; waiting out its stop grace")
                time.sleep(max(0.0, max(ends.values()) - time.monotonic()))
                return
            live = set((out or "").split())
            now = time.monotonic()
            late = [g for g, end in ends.items() if g in live and end <= now]
            if late:
                gen.log(f"background process group {', '.join(late)} still running "
                        f"{self._STOP_GRACE_S:g} s after SIGTERM")
            ends = {g: end for g, end in ends.items() if g in live and end > now}
            if ends:
                time.sleep(min(0.1, min(ends.values()) - now))

    def freeze(self) -> None:
        """Stop (``SIGSTOP``) every process in the container but its PID 1,
        so none of them writes the mounted workspace again: the step before
        a teardown's last walk over what the container wrote. The processes
        stay stopped until :meth:`close` removes the container, which ends
        them. PID 1 cannot be stopped from inside its own namespace, so an
        image whose PID 1 writes the workspace itself keeps writing. Never
        raises. Blocking."""
        import gen

        # The shell's own kill builtin, with no `timeout` wrapper: the
        # caller is the one process kill(-1) spares besides PID 1, so the
        # exec returns. It exits 1 when there was nothing else to stop.
        try:
            rc, out = self.exec(["sh", "-c", "kill -STOP -1"], user="0:0")
        except Exception as exc:  # noqa: BLE001 — a teardown goes on
            rc, out = -1, str(exc)
        if rc not in (0, 1):
            gen.log(f"could not stop the container's processes (exit {rc}): "
                    f"{(out or '').strip()[-300:]}")

    async def expose_port(self, port: int):
        """Forward a container port to a host port, register it so the cell's
        host proxies it, and return its :class:`gen.Endpoint` (a proxy URL
        the cell can iframe). The host call blocks on its ack, so it runs off
        the event loop (a serve loop keeps answering other requests)."""
        import gen

        host_port = await asyncio.to_thread(
            gen.expose_environment_port, self._env_id, int(port))
        await gen.serve_port(host_port, name=f"port-{port}")
        return gen.Endpoint(kind="http", name=f"port-{port}",
                            url=gen.proxy_url(host_port), port=host_port)

    # ── terminals (human-watchable PTYs) ────────────────────────────

    async def open_terminal(self, command=None, *, cwd: Optional[str] = None,
                            env: Optional[dict] = None) -> dict:
        """Allocate a PTY in the container; return ``{terminal_id, ws_path, token}``."""
        import gen

        return await gen.start_terminal(
            self._container_id, command or ["bash"], cwd=cwd or self.mount, env=env,
        )


    # ── lifecycle + serve wiring ────────────────────────────────────

    def close(self) -> dict:
        """Return the host's ``{ok, error}`` close status. One request only.

        A refusal or transport failure is logged and returned, without a retry
        or a cleanup exception that masks the caller's error. The host's run
        teardown and run-id-label reaper remain the cleanup backstop.
        """
        with self._close_lock:
            if self._closed:
                return dict(self._close_status)
            self._closed = True
            import gen

            try:
                ack = gen.close_environment(self._env_id)
                self._close_status = {
                    "ok": bool(ack.get("ok")),
                    "error": None if ack.get("ok") else (
                        ack.get("error") or "environment close was not acknowledged"),
                }
            except Exception as exc:  # noqa: BLE001 — teardown must preserve the body's error
                self._close_status = {"ok": False, "error": str(exc)}
            if not self._close_status["ok"]:
                try:
                    gen.log(f"environment close failed ({self._env_id}): {self._close_status['error']}")
                except Exception:  # noqa: BLE001 — diagnostics cannot break cleanup
                    pass
            return dict(self._close_status)
