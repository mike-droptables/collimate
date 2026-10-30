"""Docker runner — hosts a generator's MAIN LOOP inside a container.

Selected by ``ProcessManager.start()`` when (and ONLY when) the run's grant is
``permissions.docker == "limited"``. The pinned docker-ladder semantics:

  * ``none``   — today's native subprocess. Never reaches here.
  * ``limited``   — the main loop runs in the runner image with NO docker
    CLI/socket, so "cannot spawn further containers" is hard-enforced. THIS
    module. The container is otherwise a faithful re-host of the native launch:
    same ``uv run <entrypoint>``, same env, same workspace + app dirs bound at
    IDENTICAL absolute paths, same IPC protocol.
  * ``full`` — container-creation is granted; the main loop stays a NATIVE
    subprocess with host docker-socket access. Never reaches here (the internet
    invariant guarantees a full grant is always ``internet: full``, so nothing
    forces its containerization in this iteration).

Two halves:

  * a PURE command/config builder (:func:`build_run_argv`, :func:`plan_network`)
    that assembles the ``docker run`` argv + the network plan from plain inputs,
    unit-tested without touching docker;
  * an async orchestrator (:func:`start_containerized_run`) that ensures the
    runner image (reusing ``sandbox.ensure_image``), sets up the per-run
    network + egress proxy for a bounded grant (internet proxy/none), spawns the ``docker run`` child
    the manager reaps, and wires a supervisor that tears the network/proxy down
    when the container exits.

IPC exposure: the manager's TCP IPC server binds 0.0.0.0 for a containerized
run (loopback is container-local and unreachable), and the child connects over
the ``host.docker.internal`` gateway alias. Like every run, it carries a
per-run ``COLLIMATE_IPC_TOKEN`` the child presents as its first frame;
``backends.make_authed_ipc_handler`` validates it before handing the
connection to the normal IPC loop.

Network by ``permissions.internet`` (see the live integration test in
``tests/test_pytest_docker_runner.py`` for the demonstrated enforcement):

  * ``full``  — the default bridge + the host-gateway add-host. Full internet.
  * ``proxy`` — a per-run bridge with IP masquerade DISABLED (no working route
    to the public internet) + the host-gateway add-host (so IPC + the proxy stay
    reachable). ``HTTP(S)_PROXY`` points at a host-side :class:`EgressProxy`,
    with its per-run credential in the URL, enforcing the run grant's
    ``internet_allow`` suffix list.
  * ``none``  — identical to ``proxy`` with an EMPTY allowlist (deny-all proxy).
    A bare ``--network none`` is NOT viable: it would kill the TCP IPC the run
    depends on. So ``none`` == bounded network + a proxy that denies every host,
    while IPC still rides the gateway to the host.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from instance import INSTANCE_LABEL, instance_id
from manifest_limits import manifest_rlimit

from .optional_deps import uv_run_argv
from .streams import STREAM_LINE_LIMIT

_logger = logging.getLogger("docker_runner")

# Host vars that must NEVER cross into a docker:limited container: host-specific
# paths (would point at nonexistent in-container locations) and — critically —
# the docker client vars (a main-loop container gets NO docker access, that's
# the whole point of the ``limited`` tier). Everything else in the run env
# (COLLIMATE_*, the resolved keystore secrets, locale) is forwarded verbatim.
_ENV_DENYLIST: frozenset[str] = frozenset({
    "HOME", "PATH", "USER", "LOGNAME", "SHELL", "PWD",
    "TMPDIR", "TMP", "TEMP",
    # Host cache path, not mounted; the SDK falls back to $HOME/.cache
    # (= /tmp/.cache in the container).
    "XDG_CACHE_HOME",
    # Host CA-bundle paths — the image ships its own ca-certificates; a host
    # path would break TLS inside the container.
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
    # Docker access — denied by design at the ``limited`` tier.
    "DOCKER_HOST", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH",
    # The server's own bearer token: it authenticates the whole API, so a
    # sandboxed run holding it could start unsandboxed ones.
    "COLLIMATE_TOKEN", "COLLIMATE_CONNECT_TOKEN",
})

# Variables the host's docker CLIENT reads for itself (which binary, which
# daemon, which config/context, how to reach it). A container env value for one
# of these goes on the argv inline, so it never redirects the host CLI; every
# other value is passed by name only (see env_argv).
_CLIENT_ENV: frozenset[str] = frozenset({
    "PATH", "HOME", "USER", "LD_PRELOAD", "LD_LIBRARY_PATH", "TMPDIR",
    "XDG_CONFIG_HOME", "XDG_RUNTIME_DIR", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
})


def client_reads_env(name: str) -> bool:
    upper = name.upper()
    return upper in _CLIENT_ENV or upper.startswith("DOCKER_")


def env_argv(env: dict[str, str]) -> tuple[list[str], dict[str, str]]:
    """``-e`` flags for a docker client, plus the environment to run it in.

    Run envs carry account secrets and the IPC token, and a process argv is
    world-readable (/proc/<pid>/cmdline, ps) for the client's whole life. So
    each value rides in the client's own environment (/proc/<pid>/environ is
    owner-only) and the argv carries a bare ``-e NAME``, which docker copies
    from there. Names the client reads itself stay inline (``client_reads_env``).
    """
    flags: list[str] = []
    client_env = dict(os.environ)
    for k, v in env.items():
        if client_reads_env(k):
            flags += ["-e", f"{k}={v}"]
        else:
            flags += ["-e", k]
            client_env[k] = str(v)
    return flags, client_env


# The gateway alias the container uses to reach the host (IPC + egress proxy).
HOST_GATEWAY_ALIAS = "host.docker.internal"

# A conservative fork-bomb backstop for the container's pid cgroup. The native
# path bounds this indirectly via RLIMIT_NOFILE; a container has no preexec
# rlimits (skipped — see start_containerized_run) so a cheap explicit cap keeps
# a runaway generator from exhausting host pids. Generous enough for legitimate
# multi-process work (uv + a subprocess tree).
_DEFAULT_PIDS_LIMIT = 2048


# ---------------------------------------------------------------------------
# Image identity
# ---------------------------------------------------------------------------


def runner_image_build_args() -> dict[str, str]:
    """The interpreter selected by the runner Dockerfile's FROM argument."""
    return {"PYTHON_VERSION": f"{sys.version_info.major}.{sys.version_info.minor}"}


def default_runner_image() -> str:
    """Content-address the build context and its host-matching Python input."""
    python_version = runner_image_build_args()["PYTHON_VERSION"]
    digest = hashlib.sha256(b"collimate-runner-v1\0" + python_version.encode() + b"\0")
    context = Path(runner_image_dir())
    for path in sorted(context.rglob("*")):
        if path.is_symlink():
            data = os.readlink(path).encode()
            kind = b"link\0"
            size = len(data)
        elif path.is_file():
            data = None
            kind = b"file\0"
            size = path.stat().st_size
        else:
            continue
        name = path.relative_to(context).as_posix().encode()
        digest.update(kind + len(name).to_bytes(8, "big") + name + size.to_bytes(8, "big"))
        if data is not None:
            digest.update(data)
        else:
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
    return f"collimate-runner:py{python_version}-{digest.hexdigest()}"


def runner_image_dir() -> str:
    """The lazy-build context (``runner_image/`` beside this module)."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "runner_image")


# ---------------------------------------------------------------------------
# Pure command/config builders (unit-testable without docker)
# ---------------------------------------------------------------------------


def forward_env(env: dict[str, str], extra: Optional[dict[str, str]] = None) -> dict[str, str]:
    """The env the container receives: the run env minus the host-only denylist,
    plus ``extra`` (COLLIMATE_CONTAINERIZED, proxy vars) which always wins."""
    out = {k: v for k, v in env.items() if k not in _ENV_DENYLIST}
    if extra:
        out.update(extra)
    return out


@dataclass
class NetworkPlan:
    """How a run's container attaches to the network, derived purely from the
    grant's ``internet`` axis. ``network`` is the docker ``--network`` value;
    ``per_run_network`` names a network the orchestrator must create+destroy
    (masquerade off); ``needs_proxy`` gates the egress proxy; ``allow`` is the
    proxy's suffix allowlist (empty ⇒ deny-all, the ``none`` mode)."""
    network: str
    per_run_network: str | None
    masquerade_off: bool
    needs_proxy: bool
    allow: tuple[str, ...]


def plan_network(run_id: str, internet: str, internet_allow: tuple[str, ...] | list[str]) -> NetworkPlan:
    """Map ``(internet, internet_allow)`` → a :class:`NetworkPlan`.

    ``full`` → default bridge, no proxy. ``proxy`` / ``none`` → a per-run
    masquerade-off bridge + egress proxy; ``none`` forces an empty allowlist
    (deny all) regardless of any ``internet_allow`` the grant carried."""
    if internet == "full":
        return NetworkPlan(
            network="bridge", per_run_network=None,
            masquerade_off=False, needs_proxy=False, allow=(),
        )
    per_run = f"collimate-int-{run_id}"
    allow = tuple(internet_allow) if internet == "proxy" else ()
    return NetworkPlan(
        network=per_run, per_run_network=per_run,
        masquerade_off=True, needs_proxy=True, allow=allow,
    )


@dataclass
class RunnerSpec:
    """Everything :func:`build_run_argv` needs. Kept as a dataclass so tests can
    assert the assembled argv from plain fields without a live manager."""
    image: str
    container_name: str
    run_id: str
    app_dir: str                 # the container WORKDIR (== native cwd)
    workspace_dir: str
    sdk_src: str
    entrypoint: str
    env: dict[str, str]
    network: str
    # The dir bind-mounted for the generator tree. Defaults to app_dir, but the
    # orchestrator sets it to app_dir's PARENT so the generator's ``_*`` shared
    # siblings (which live beside its own dir and resolve via the native
    # parent-of-parent-of-__file__ sys.path shim) come along too.
    app_mount: str | None = None
    user: str | None = None
    memory_bytes: int | None = None
    pids_limit: int | None = _DEFAULT_PIDS_LIMIT
    # Upstream DNS override. On a restricted-internet network this is set to
    # the container's OWN loopback (a dead resolver) so Docker's embedded DNS
    # cannot recursively resolve external names via the HOST's stack — closing
    # the DNS-tunnel exfiltration channel the masquerade-off black-hole and
    # the egress proxy don't cover. Proxy-mode HTTP(S) is unaffected: the
    # host-side CONNECT proxy resolves target hosts itself.
    dns: str | None = None
    # The manifest-declared extras the run's config selected
    # (optional_deps.resolve_optional_requirements) and whether the grant lets
    # uv re-resolve them. The container's uv cache is per-run (--rm), so a
    # group here is always a cold download — and it needs egress, which a
    # restricted network only has through the forwarded proxy env.
    requirements: list[str] = field(default_factory=list)
    upgrade_packages: bool = False


def build_run_client_env(spec: RunnerSpec, extra_env: Optional[dict[str, str]] = None) -> dict[str, str]:
    """The environment to spawn :func:`build_run_argv`'s argv in: it holds the
    values its bare ``-e NAME`` flags refer to."""
    return env_argv(forward_env(spec.env, extra_env))[1]


def build_run_argv(spec: RunnerSpec, extra_env: Optional[dict[str, str]] = None) -> list[str]:
    """Assemble the full ``docker run`` argv. Pure — no docker calls.

    Mirrors the native launch (``uv run <entrypoint>`` with ``cwd == app_dir``):
    ``app_dir`` + ``workspace_dir`` bind at IDENTICAL absolute paths (they live
    under ``~/.collimate/tmp`` so snap-docker can reach them), the SDK source
    binds read-only at its host path (env carries the matching PYTHONPATH), the
    workdir is ``app_dir``, and the entrypoint runs under the SAME
    ``optional_deps.uv_run_argv`` tail the native backend builds — a grant that
    only moves where the run lands must not change what it installs. ``--rm`` +
    the ``com.collimate.run_id`` label let the existing run-id reaper backstop
    teardown, and the ``com.collimate.instance`` label tells the boot-time
    reaper of a SECOND live server (the dev run beside the installed app, same
    dockerd) that this container is not its orphan to remove."""
    argv: list[str] = [
        # No ``-i``/``-t``: the generator talks to the host over TCP IPC, never
        # stdin, so we don't attach the container's stdin. Attaching it (``-i``)
        # makes the docker client relay stdin and intermittently races the flush
        # of the container's stdout/stderr to our async pipe drain — dropping the
        # run's output (and the IPC-readiness window) on ~half of cold starts.
        "docker", "run", "--rm",
        "--name", spec.container_name,
        "--label", f"com.collimate.run_id={spec.run_id}",
        "--label", f"{INSTANCE_LABEL}={instance_id()}",
        "--network", spec.network,
        "--add-host", f"{HOST_GATEWAY_ALIAS}:host-gateway",
    ]
    if spec.user:
        argv += ["--user", spec.user]
    if spec.dns:
        argv += ["--dns", spec.dns]
    if spec.memory_bytes:
        argv += ["--memory", str(spec.memory_bytes)]
    if spec.pids_limit:
        argv += ["--pids-limit", str(spec.pids_limit)]

    # Bind mounts at identical absolute paths (snap-docker: all under $HOME).
    # The app mount covers the generator dir + its ``_*`` shared siblings; the
    # workdir remains the generator's own dir.
    app_bind = spec.app_mount or spec.app_dir
    argv += ["-v", f"{app_bind}:{app_bind}"]
    argv += ["-v", f"{spec.workspace_dir}:{spec.workspace_dir}"]
    argv += ["-v", f"{spec.sdk_src}:{spec.sdk_src}:ro"]

    argv += ["-w", spec.app_dir]

    argv += env_argv(forward_env(spec.env, extra_env))[0]

    argv += [spec.image, *uv_run_argv(
        spec.entrypoint,
        requirements=spec.requirements,
        upgrade=spec.upgrade_packages,
    )]
    return argv


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _log_sink(manager) -> Callable[[str], None]:
    def _sink(line: str) -> None:
        try:
            manager.log_buffer.append({"type": "stderr", "payload": line.rstrip("\n") + "\n"})
        except Exception:
            pass
    return _sink


def _make_deny_hook(manager) -> Callable[[str, int, str], None]:
    """Emit an ``egress_denied`` run_event (structured, for the UI) plus a
    ``[WARNING]`` stderr line (visible in today's canonical drawer/log without
    a one-sided log_format allowlist edit) whenever the proxy blocks a host."""
    def _on_deny(host: str, port: int, method: str) -> None:
        try:
            manager.log_buffer.append({
                "type": "egress_denied",
                "payload": json.dumps({"host": host, "port": port, "method": method}),
            })
            manager.log_buffer.append({
                "type": "stderr",
                "payload": f"[WARNING] egress blocked: {method} {host}:{port} (not in allowlist)\n",
            })
        except Exception:
            _logger.debug("egress deny hook failed to buffer", exc_info=True)
    return _on_deny


async def _create_network(name: str, masquerade_off: bool) -> None:
    opts = ["docker", "network", "create", "--driver", "bridge"]
    if masquerade_off:
        # No SNAT for this network → the container has NO working route to the
        # public internet (packets leave with a private source and never route
        # back), while the on-link host gateway stays reachable for IPC + proxy.
        opts += ["--opt", "com.docker.network.bridge.enable_ip_masquerade=false"]
    opts.append(name)
    proc = await asyncio.create_subprocess_exec(
        *opts,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out = (await proc.stdout.read()).decode("utf-8", errors="replace") if proc.stdout else ""
    rc = await proc.wait()
    if rc != 0:
        raise RuntimeError(f"docker network create failed for {name!r}: {out.strip()}")


async def _remove_network(name: str) -> None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "network", "rm", name,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=15)
    except Exception:
        _logger.debug("docker network rm %s failed", name, exc_info=True)


async def start_containerized_run(
    manager,
    *,
    env: dict[str, str],
    entrypoint: str,
    manifest: dict,
) -> "asyncio.subprocess.Process":
    """Spawn the ``docker run`` child that hosts the generator main loop.

    Preconditions (asserted by the caller — ``ProcessManager.start()``):
      * ``manager.permissions.docker == "limited"``;
      * the IPC server is already bound on 0.0.0.0 with a token, and ``env``
        carries ``COLLIMATE_IPC_PORT`` / ``COLLIMATE_IPC_HOST`` /
        ``COLLIMATE_IPC_TOKEN`` accordingly.

    Returns the child process (docker run, with stdin/stdout/stderr pipes) for
    the manager to wire exactly like the native subprocess. On any failure
    before the spawn, the per-run network + proxy are torn down and the error
    propagates (the caller's cleanup marks the run failed)."""
    log = _log_sink(manager)
    perms = manager.permissions
    run_id = manager.run_id

    image = default_runner_image()
    from sandbox import ensure_image
    await ensure_image(
        image, runner_image_dir(), log, refresh=perms.update_packages,
        build_args=runner_image_build_args(),
    )

    plan = plan_network(run_id, perms.internet, perms.internet_allow)

    extra_env: dict[str, str] = {"COLLIMATE_CONTAINERIZED": "1"}
    proxy = None
    created_network = False
    try:
        if plan.per_run_network:
            await _create_network(plan.per_run_network, plan.masquerade_off)
            created_network = True

        if plan.needs_proxy:
            from egress_proxy import EgressProxy
            proxy = EgressProxy(run_id, plan.allow, on_deny=_make_deny_hook(manager))
            await proxy.start()
            # Carries the proxy's per-run credential as userinfo.
            proxy_url = proxy.url(HOST_GATEWAY_ALIAS)
            extra_env.update({
                "HTTP_PROXY": proxy_url, "HTTPS_PROXY": proxy_url,
                "http_proxy": proxy_url, "https_proxy": proxy_url,
                # IPC (raw TCP) and loopback must never be proxied.
                "NO_PROXY": f"{HOST_GATEWAY_ALIAS},localhost,127.0.0.1",
                "no_proxy": f"{HOST_GATEWAY_ALIAS},localhost,127.0.0.1",
            })

        from .backends import sdk_src_dir
        sdk_src = sdk_src_dir()

        try:
            uid_gid = f"{os.getuid()}:{os.getgid()}"  # match host owner of the bind mounts
        except AttributeError:
            uid_gid = None  # non-POSIX host — no user remap

        # Bind the PARENT of the generator's own dir so its ``_*`` shared
        # siblings (materialized beside it by find_generator_dir) come along —
        # the generator's sys.path shim resolves them at parent-of-parent-of-
        # __file__, which must exist inside the box. Workdir stays the gen dir.
        app_mount = os.path.dirname(manager.app_dir) or manager.app_dir

        spec = RunnerSpec(
            image=image,
            container_name=f"collimate-run-{run_id}",
            run_id=run_id,
            app_dir=manager.app_dir,
            app_mount=app_mount,
            workspace_dir=manager.workspace_dir,
            sdk_src=sdk_src,
            entrypoint=entrypoint,
            env=env,
            network=plan.network,
            user=uid_gid,
            # Only explicit memory maps to a cgroup hard limit. Native's
            # virtual-address default has different semantics.
            memory_bytes=manifest_rlimit(manifest, "memory_bytes"),
            dns="127.0.0.1" if plan.masquerade_off else None,
            requirements=manager.optional_requirements,
            upgrade_packages=perms.update_packages,
        )
        argv = build_run_argv(spec, extra_env)

        _logger.info(
            "run %s: launching containerized main loop (image=%s network=%s internet=%s)",
            run_id, image, plan.network, perms.internet,
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                env=build_run_client_env(spec, extra_env),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.PIPE,
                start_new_session=True,
                limit=STREAM_LINE_LIMIT,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                "`docker` not found on PATH while starting a docker:limited run"
            ) from exc
    except BaseException:
        # Unwind the network + proxy we set up before the spawn succeeded.
        if proxy is not None:
            await proxy.stop()
        if created_network and plan.per_run_network:
            await _remove_network(plan.per_run_network)
        raise

    # Supervisor: when the run finishes, retire the per-run proxy + network.
    # ONLY created for a bounded grant (proxy/none) — full mode has nothing of
    # ours to reclaim (the container is --rm, with the run-id-label reaper as
    # the force-stop backstop). Critically it does NOT add a second
    # ``proc.wait()`` waiter on the process: asyncio's subprocess transport
    # bearing the stdio pipes is shared, and a second independent waiter races
    # the manager's output drain (``_drain_stdout``/``_drain_stderr`` in
    # _wait_and_cleanup) — losing the container's stdout/stderr and the IPC
    # window. Instead it awaits the manager's OWN cleanup task (the single
    # proc.wait() owner, which drains first), then tears down.
    if proxy is not None or created_network:
        # Stored only to hold the sole strong reference to the supervisor task
        # (asyncio keeps tasks weakly); nothing reads the attribute.
        manager._egress_cleanup_task = asyncio.create_task(
            _supervise_teardown(
                manager, proxy, plan.per_run_network if created_network else None,
            )
        )
    return proc


async def _supervise_teardown(manager, proxy, network_name) -> None:
    # Wait for the run to fully wind down via the manager's cleanup task — the
    # ONE owner of proc.wait() + the stdio drain — rather than adding a second
    # waiter on the process. The cleanup task is wired right after this task is
    # scheduled, so poll briefly for it.
    for _ in range(200):
        ct = getattr(manager, "_cleanup_task", None)
        if ct is not None:
            try:
                await asyncio.shield(ct)
            except Exception:
                _logger.debug("supervise: cleanup task raised", exc_info=True)
            break
        await asyncio.sleep(0.05)
    if proxy is not None:
        await proxy.stop()
    if network_name:
        # The container holds a reference to the network until docker reaps the
        # --rm container; a brief retry covers that window.
        for _ in range(10):
            await _remove_network(network_name)
            chk = await asyncio.create_subprocess_exec(
                "docker", "network", "inspect", network_name,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            if await chk.wait() != 0:
                return
            await asyncio.sleep(0.5)


__all__ = [
    "default_runner_image",
    "runner_image_dir",
    "forward_env",
    "build_run_argv",
    "plan_network",
    "start_containerized_run",
    "RunnerSpec",
    "NetworkPlan",
    "HOST_GATEWAY_ALIAS",
]
